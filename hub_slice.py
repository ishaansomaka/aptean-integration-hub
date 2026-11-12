#!/usr/bin/env python3
"""Integration Hub - Part C thin slice. Standard library only.

    python hub_slice.py                      demo: same document twice lands once, plus one trace per outcome
    python hub_slice.py --replay             all 24,000 real deliveries through the new rules + reconciliation
    python hub_slice.py --where "<customer>" <number>    "where is my document?" (e.g. --where Harrowgate SO-123456)
    python hub_slice.py EV-608137            trace for the document that delivery belongs to

The ERP is a STUB that replays what the real ERP answered (data/delivery_attempts.csv). Where the new hub removes the
cause of a failure (text totals, version mapping, waiting for a customer) the stub ASSUMES acceptance and the trace says so.
"""
import csv, hashlib, json, sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

DATA = Path(__file__).parent / "data"
BN = {"order": "order_number", "invoice": "invoice_number", "customer": "customer_ref", "shipment": "shipment_id"}
MAX_TRIES, WAIT_CUSTOMER_H = 5, 12
T = lambda s: datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")

# The ONE place a failure is classified. ERP error code -> (state, who must act, next step). Unknown codes hold, never drop.
POLICY = {
    "upstream_unavailable": ("RETRY",            "hub",    "retry with backoff, up to 5 tries"),
    "rate_limited":         ("BACKING_OFF",      "hub",    "slow this tenant's lane, retry later"),
    "customer_not_found":   ("WAITING_CUSTOMER", "hub",    f"hold until the customer record lands (max {WAIT_CUSTOMER_H} h)"),
    "invalid_tax_code":     ("NEEDS_TENANT_FIX", "tenant", "tried once; ask the tenant to fix the tax code on their side"),
    "unsupported_version":  ("HELD_NO_MAPPING",  "hub",    "hold; replay automatically once a mapping exists for this tenant"),
    "validation_error":     ("NEEDS_REVIEW",     "hub",    "hold for review: ERP rejected the shape and no known cause applies"),
    "parse_error":          ("NEEDS_REVIEW",     "hub",    "hold for review: ERP could not read a value"),
    "partial_content":      ("DELIVERED_PARTIAL","hub",    "landed, but some lines were rejected: review the rejected lines"),
}
UNKNOWN = ("NEEDS_REVIEW", "hub", "unrecognised ERP answer: hold for review")
# Partner contracts: required and optional fields per (document type, DECLARED version). A shape that fits none is held, never guessed.
_REQ = {"order": {"order_number", "customer_ref", "order_date", "currency", "total", "lines"},
        "customer": {"customer_ref", "legal_name", "billing_address", "payment_terms"},
        "invoice": {"invoice_number", "customer_ref", "amount_due", "currency", "due_date"},
        "shipment": {"shipment_id", "order_number", "carrier", "tracking", "shipped_at"}}
CONTRACT = {(t, v): (_REQ[t] | ({"ship_to" if v == "v1" else "shipping_address"} if t == "order" else set()),
                     {"primary_contact"} if t == "customer" else set()) for t in _REQ for v in ("v1", "v2")}
FIXED_BY = {"parse_error": "total", "validation_error": "version"}   # which canonicalisation fix removes which ERP complaint


def load():
    rd = lambda f: list(csv.DictReader(open(DATA / f)))
    att = {}
    for r in rd("delivery_attempts.csv"):
        att.setdefault((r["event_id"], int(r["attempt_no"])), r)        # drops the log-shipper's duplicate rows
    by = defaultdict(list)
    for (e, n), r in sorted(att.items()):
        by[e].append(r)
    events = sorted((json.loads(l) for l in open(DATA / "inbound_events.jsonl")), key=lambda e: e["received_at"])
    return events, by, {r["tenant_id"]: r for r in rd("tenant_config.csv")}, {r["partner_id"]: r for r in rd("partner_catalog.csv")}


def shape_problem(ev):
    """None if the payload fits the contract for its declared version, else a plain-words reason."""
    need, maybe = CONTRACT.get((ev["doc_type"], ev["payload_version"]), (None, None))
    if need is None:
        return f"declares payload version {ev['payload_version']}: no contract for it"
    keys = set(ev["payload"]) - {"sent_at"}
    miss, extra = need - keys, keys - need - maybe
    return (f"does not fit the {ev['payload_version']} {ev['doc_type']} contract: missing {sorted(miss) or 'nothing'}, "
            f"unexpected {sorted(extra) or 'nothing'}") if miss or extra else None


def canonicalise(ev, registered):
    """Partner payload -> canonical document. Returns (canonical, fixes). Version comes from the REQUEST, not the catalogue."""
    p = {k: v for k, v in ev["payload"].items() if k not in ("sent_at", "primary_contact")}   # personal data stays out
    fixes = set()
    if ev["payload_version"] != registered:
        fixes.add("version")
    if "total" in p:
        if isinstance(p["total"], str): fixes.add("total")
        p["total"] = f'{Decimal(str(p["total"]).replace(",", "")):.2f}'
    if "ship_to" in p: p["shipping_address"] = p.pop("ship_to")
    return p, fixes


class Hub:
    def __init__(self, attempts, tenants, partners):
        self.seen = set()
        self.att, self.tenants, self.partners = attempts, tenants, partners
        self.docs, self.waiting, self.erp_calls, self.deliveries = {}, defaultdict(list), 0, 0

    def log(self, d, ts, step, text): d["trace"].append((ts, step, text))

    def receive(self, ev):
        """Intake + identity + idempotency. Every delivery ends up attached to exactly one ledger document."""
        self.deliveries += 1
        ts, tid, typ = ev["received_at"], ev["tenant_id"], ev["doc_type"]
        problem = shape_problem(ev)
        if problem:                                                                      # unknown shape: hold, never guess
            d = {"id": "D-" + hashlib.sha1(ev["event_id"].encode()).hexdigest()[:8] + "-S", "ident": ev["event_id"], "tenant": tid, "type": typ,
                 "number": str(ev["payload"].get(BN[typ], "?")), "fp": "-", "dups": 0, "customer_ref": None, "trace": [], "basis": "recorded",
                 "tries": 0, "state": "RECEIVED", "owner": "hub", "next": ""}
            self.docs[ev["event_id"]] = d
            self.log(d, ts, "RECEIVED", f"{ev['event_id']} from {ev['partner_id']}, declares payload {ev['payload_version']}")
            self.log(d, ts, "SHAPE", problem)
            return self.set(d, ts, "HELD_SHAPE", "partner", "hold; ask the partner what changed; replay once the contract is updated")
        canon, fixes = canonicalise(ev, self.partners[ev["partner_id"]]["payload_version"])
        number = ev["payload"][BN[typ]]
        ident = f"{tid}|{typ}|{number}"
        fp = hashlib.sha1(json.dumps([typ, canon], sort_keys=True).encode()).hexdigest()[:10]
        doc_id = "D-" + hashlib.sha1(ident.encode()).hexdigest()[:8]
        d = self.docs.get(ident)
        if d is not None and d["fp"] == fp:                                              # same document, again
            d["dups"] += 1
            again = ev["event_id"] in self.seen
            self.seen.add(ev["event_id"])
            self.log(d, ts, "DUPLICATE", f"{ev['event_id']} from {ev['partner_id']} " + ("arrived AGAIN (same delivery id: a replay) and " if again else "") +
                                         f"is the same document (same fingerprint {fp}); "
                                         f"attached, NOT sent to the ERP again. Current state: {d['state']}")
            return d
        if d is not None:                                                                # same identity, different content
            ident, doc_id = ident + "#conflict", doc_id + "-X"
        d = {"id": doc_id, "ident": ident, "tenant": tid, "type": typ, "number": number, "fp": fp, "dups": 0,
             "customer_ref": canon.get("customer_ref"), "trace": [], "basis": "recorded", "tries": 0, "state": "RECEIVED", "owner": "hub", "next": ""}
        self.docs[ident] = d
        self.log(d, ts, "RECEIVED", f"{ev['event_id']} from {ev['partner_id']}, declares payload {ev['payload_version']}")
        for f in sorted(fixes):
            self.log(d, ts, "NORMALISED", {"total": "order total arrived as text with a thousands comma: converted to a decimal",
                                          "version": f"partner declares {ev['payload_version']} but the catalogue says "
                                                     f"{self.partners[ev['partner_id']]['payload_version']}: mapped by the declared version"}[f])
        if ident.endswith("#conflict"):
            return self.set(d, ts, "HELD_CONFLICT", "hub", "same business number, DIFFERENT content: held for a person to decide (never merged)")
        self.log(d, ts, "IDENTITY", f"{doc_id}: new document ({typ} {number}, tenant {tid}, fingerprint {fp})")
        self.deliver(d, ev, fixes)
        if typ == "customer" and d["state"].startswith("DELIVERED"):
            self.release_orders(d)
        return d

    def set(self, d, ts, state, owner, nxt, note=""):
        d["state"], d["owner"], d["next"] = state, owner, nxt
        self.log(d, ts, "STATE", f"{state}" + (f" - {note}" if note else ""))
        return d

    def deliver(self, d, ev, fixes):
        rows = self.att.get(ev["event_id"], [])
        for k, r in enumerate(rows[:MAX_TRIES]):
            code, ts = r["error_code"], r["attempted_at"]
            self.erp_calls += 1; d["tries"] += 1
            if code and FIXED_BY.get(code) in fixes:
                d["basis"] = "assumed-" + FIXED_BY[code]
                self.log(d, ts, "ATTEMPT", f"#{k+1} ERP (recorded answer to the OLD request) {r['http_status']} {code}; the fix above removes this cause")
                return self.set(d, ts, "DELIVERED", "-", "none", "ASSUMED accepted: the stub cannot see inside the ERP")
            state, owner, nxt = ("DELIVERED", "-", "none") if not code else POLICY.get(code, UNKNOWN)
            self.log(d, ts, "ATTEMPT", f"#{k+1} ERP answered {r['http_status']} {code or 'ok'} -> {state}")
            d["state"], d["owner"], d["next"] = state, owner, nxt
            if state not in ("RETRY", "WAITING_CUSTOMER"):                               # terminal for this pass
                return self.set(d, ts, state, owner, nxt)
        ts = rows[-1]["attempted_at"] if rows else ev["received_at"]
        if d["state"] == "RETRY" and len(rows) >= MAX_TRIES:
            return self.set(d, ts, "NEEDS_REVIEW", "hub", "retry budget spent: hold for review")
        if d["state"] == "WAITING_CUSTOMER":                                              # probes done; now wait for the customer record
            self.waiting[(d["tenant"], d["customer_ref"])].append((d, T(ev["received_at"]) + timedelta(hours=WAIT_CUSTOMER_H)))
        return self.set(d, ts, d["state"], d["owner"], d["next"])

    def release_orders(self, cust):
        ts = cust["trace"][-1][0]
        for d, deadline in self.waiting.pop((cust["tenant"], cust["number"]), []):
            if T(ts) <= deadline:
                d["basis"] = "assumed-customer"
                self.log(d, ts, "RELEASED", f"customer {cust['number']} landed ({cust['id']}); order re-sent")
                self.set(d, ts, "DELIVERED", "-", "none", "ASSUMED accepted now that the customer exists")
            else:
                self.set(d, ts, "NEEDS_REVIEW", "hub", "customer landed after the wait limit: review")


def show(d, tenants):
    t = tenants[d["tenant"]]
    print(f"\n{d['id']}  {d['type']} {d['number']}  |  {t['tenant_name']} ({d['tenant']}, SLA {t['sla_minutes']} min)")
    for ts, step, text in d["trace"]:
        print(f"  {ts}  {step:<10} {text}")
    print(f"  => STATE {d['state']}   ACTED ON BY {d['owner']}   NEXT {d['next']}   (ERP calls: {d['tries']}, re-sends attached: {d['dups']})")


def replay(events, att, tenants, partners):
    hub = Hub(att, tenants, partners)
    for ev in events:
        hub.receive(ev)
    return hub


def demo(events, att, tenants, partners):
    seen, pair = {}, None
    for ev in events:                                                          # first real invoice the old hub posted twice
        if ev["doc_type"] == "invoice":
            k = (ev["tenant_id"], ev["payload"]["invoice_number"])
            if k in seen and att.get(seen[k]["event_id"]) and att.get(ev["event_id"]):
                pair = (seen[k], ev); break
            seen.setdefault(k, ev)
    h = Hub(att, tenants, partners)
    print("=== 1. The same business document delivered twice (a real pair from the log) lands once ===")
    print(f"deliveries fed in: {pair[0]['event_id']} (first send), {pair[1]['event_id']} (partner's resend), {pair[1]['event_id']} (that same delivery processed again)")
    for ev in (pair[0], pair[1], pair[1]):
        h.receive(ev)
    show(next(iter(h.docs.values())), tenants)
    print(f"\ndeliveries received: {h.deliveries}   ERP calls made: {h.erp_calls}   documents in ledger: {len(h.docs)}   "
          f"(old hub posted this invoice twice)")
    print("\n=== 3. A partner changes its layout without changing the declared version (a real invoice with one field renamed) ===")
    inv = next(e for e in events if e["doc_type"] == "invoice" and e["partner_id"] == "P-02")
    bad = json.loads(json.dumps(inv)); bad["event_id"] = "EV-SIMULATED"; bad["payload"]["amount"] = bad["payload"].pop("amount_due")
    h3 = Hub(att, tenants, partners); h3.receive(bad); show(next(iter(h3.docs.values())), tenants)
    print(f"ERP calls made: {h3.erp_calls}  (the old hub would have sent it on and let the ERP refuse it)")
    print("\n=== 4. One real document per outcome, replaying the whole log (full trace: python hub_slice.py EV-xxxx) ===")
    hub, shown = replay(events, att, tenants, partners), set()
    for d in hub.docs.values():
        kind = (d["state"], d["basis"], d["tries"] > 1 and d["state"] == "DELIVERED")
        if kind not in shown:
            shown.add(kind)
            label = d["state"] + ("" if d["basis"] == "recorded" else f" ({d['basis']})") + (" after retry" if kind[2] else "")
            print(f"  {label:<36} {d['trace'][0][2].split()[0]}  {d['type']:<8} {d['number']:<11} {d['tenant']}  owner={d['owner']:<6} next: {d['next']}")


def reconcile(hub, events):
    docs = hub.docs.values()
    states, dups = Counter(d["state"] for d in docs), sum(d["dups"] for d in docs)
    ok = len(hub.docs) + dups == len(events)
    print(f"deliveries in: {len(events)}   ledger documents: {len(hub.docs)}   duplicates attached: {dups}   "
          f"{len(hub.docs)} + {dups} = {len(hub.docs)+dups}, must equal {len(events)}: {'OK' if ok else 'MISMATCH'}")
    known = {p[0] for p in POLICY.values()} | {"DELIVERED", "HELD_CONFLICT", "HELD_SHAPE"}
    print(f"documents in a state not on the list: {sum(n for s, n in states.items() if s not in known)} (must be 0)")
    for s, n in states.most_common():
        print(f"  {s:<18} {n:>6}")
    print(f"documents held for an unknown shape: {states['HELD_SHAPE']} (the real log fits the contracts, so 0 is expected)")
    basis = Counter(d["basis"] for d in docs if d["state"] == "DELIVERED")
    print(f"of DELIVERED: {basis['recorded']} the real ERP accepted (recorded); the rest are ASSUMED accepted after our fix: "
          + ", ".join(f"{n} {b.split('-')[1]}" for b, n in basis.items() if b != "recorded"))
    print(f"ERP calls by the new rules: {hub.erp_calls}   (old hub: 56,832 attempts after removing duplicate log rows)")


if __name__ == "__main__":
    events, att, tenants, partners = load()
    a = sys.argv[1:]
    if not a:
        demo(events, att, tenants, partners)
    elif a[0] == "--replay":
        reconcile(replay(events, att, tenants, partners), events)
    elif a[0] == "--where" and len(a) == 3:
        hub = replay(events, att, tenants, partners)
        hits = [d for d in hub.docs.values() if a[1].lower() in tenants[d["tenant"]]["tenant_name"].lower() and d["number"] == a[2]]
        [show(d, tenants) for d in hits] or print("no such document for that customer: nothing received (check the number, or ask the partner)")
    else:
        hub = replay(events, att, tenants, partners)
        ev = next((e for e in events if e["event_id"] == a[0]), None)
        d = hub.docs.get(f"{ev['tenant_id']}|{ev['doc_type']}|{ev['payload'][BN[ev['doc_type']]]}") if ev else None
        show(d, tenants) if d else print("unknown delivery id")
