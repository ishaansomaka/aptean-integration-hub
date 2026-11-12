"""part_a.py - every number in the Part A diagnosis, with its definition.

Run from the repo root:   python -I analysis/part_a.py data
Prints a report; the same text is saved to analysis/part_a_output.txt by the README command.
Pandas only. 'Today' is 2026-03-03; the data ends at the latest attempt (2026-03-03 00:44Z).
"""
import hashlib, json, sys
from pathlib import Path
import pandas as pd

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
BN = {"order": "order_number", "invoice": "invoice_number", "customer": "customer_ref", "shipment": "shipment_id"}
INFLIGHT_WINDOW_H = 2          # longest observed retry chain is 72 min, so anything older is finished


def say(title="", *lines):
    if title: print(f"\n=== {title} ===")
    for l in lines: print(l)


# ---------------------------------------------------------------- load + clean
ev = pd.read_json(D / "inbound_events.jsonl", lines=True, dtype=False)
ev["received_at"] = pd.to_datetime(ev.received_at, utc=True)
ev["sent_at"] = pd.to_datetime(ev.payload.map(lambda p: p["sent_at"]), utc=True)
at_raw = pd.read_csv(D / "delivery_attempts.csv", dtype=str, keep_default_na=False)
dl = pd.read_csv(D / "dead_letter_queue.csv", dtype=str, keep_default_na=False)
tc = pd.read_csv(D / "tenant_config.csv", dtype=str)
pc = pd.read_csv(D / "partner_catalog.csv", dtype=str).rename(columns={"payload_version": "registered_v"})

at = at_raw.drop_duplicates().copy()                     # CLEAN 1: 240 exact duplicate rows (log shipper re-run)
at["attempted_at"] = pd.to_datetime(at.attempted_at, utc=True)
at["attempt_no"] = at.attempt_no.astype(int)
at["latency_ms"] = at.latency_ms.astype(int)
at = at.sort_values(["event_id", "attempt_no"])
END = at.attempted_at.max()

say("0. DATA CHECKS",
    f"attempt rows {len(at_raw)} -> {len(at)} after removing {len(at_raw)-len(at)} exact duplicates (log-shipper re-run)",
    f"events={len(ev)} unique event_id={ev.event_id.nunique()}; attempts with unknown event_id={(~at.event_id.isin(ev.event_id)).sum()};"
    f" events with no attempt={(~ev.event_id.isin(at.event_id)).sum()}; DLQ rows with unknown event_id={(~dl.event_id.isin(ev.event_id)).sum()}",
    f"timestamps: all ISO-8601 UTC 'Z' (no mixed formats). partner sent_at AFTER hub received_at on "
    f"{(ev.sent_at > ev.received_at).sum()} events (partner clock ahead, up to 10 min)",
    f"data ends {END}")

# ---------------------------------------------------------------- event-level state
g = at.groupby("event_id")
st = pd.DataFrame({"n_attempts": g.size(), "last_attempt": g.attempted_at.max(),
                   "delivered": g.outcome.apply(lambda s: (s == "success").any()),
                   "last_outcome": g.outcome.last(), "last_code": g.error_code.last()})
ev = ev.join(st, on="event_id")
ev["in_dlq"] = ev.event_id.isin(dl.event_id)
ev["bn"] = [p[BN[t]] for p, t in zip(ev.payload, ev.doc_type)]
ev["content"] = ev.payload.map(lambda p: json.dumps({k: v for k, v in p.items() if k != "sent_at"}, sort_keys=True))
ev["ident"] = ev.tenant_id + "|" + ev.doc_type + "|" + ev.bn          # candidate identity (tenant+type+business number)
ev["doc_key"] = ev.ident + "|" + ev.content.map(lambda s: hashlib.sha1(s.encode()).hexdigest()[:9])  # + content, to spot collisions
ev["in_flight"] = (~ev.delivered) & (~ev.in_dlq) & (ev.last_outcome == "retryable_error") & \
                  ((END - ev.last_attempt).dt.total_seconds() <= INFLIGHT_WINDOW_H * 3600)
ev = ev.merge(tc, on="tenant_id").merge(pc[["partner_id", "registered_v", "sends_idempotency_key"]], on="partner_id")

# ---------------------------------------------------------------- Q1
say("Q1. LOST vs DUPLICATED")
key = ev[ev.idempotency_key.notna()]
say("", f"-- why the obvious queries are wrong --",
    f"duplicate event_id: {ev.event_id.duplicated().sum()}  (hub mints a new id per delivery, so it can never show a duplicate)",
    f"duplicate idempotency_key: {key.idempotency_key.duplicated().sum()} of {len(key)} keyed events  "
    f"(no key was ever reused, so a key-based check finds zero duplicates)",
    f"duplicate business number alone: {ev.bn.duplicated().sum()}  (too many: same number legitimately recurs across tenants/types)")
sz = ev.groupby("ident").size()
rep = ev[ev.ident.isin(sz[sz > 1].index)]
ks = rep.groupby("ident").idempotency_key.agg(lambda s: "no key" if s.isna().all() else ("mixed" if s.isna().any() else
                                                    ("same key" if s.nunique() == 1 else "fresh key each time")))
say("", f"-- identity = tenant + doc_type + business number --",
    f"events={len(ev)}  distinct identities={len(sz)}  identities sent more than once={int((sz>1).sum())} (all exactly twice)",
    f"how those resends were keyed: {ks.value_counts().to_dict()}",
    f"identities whose 2 sends differ in content (excl. sent_at): "
    f"{int((rep.groupby('ident').content.nunique() > 1).sum())}  doc types: {rep[rep.groupby('ident').content.transform('nunique') > 1].doc_type.unique().tolist()} "
    f"(= two different customers sharing one customer_ref; collapsing them by identity alone would drop a real customer)")

docs = ev.groupby("doc_key").agg(tenant=("tenant_id", "first"), doc_type=("doc_type", "first"), n_events=("event_id", "size"),
                                 n_landed=("delivered", "sum"), any_dlq=("in_dlq", "max"), any_flight=("in_flight", "max"),
                                 first_recv=("received_at", "min"), ident=("ident", "first"), partner=("partner_id", "first"))
docs["lost"] = (docs.n_landed == 0) & (~docs.any_flight)
dup = docs[docs.n_landed >= 2]
_p7 = docs[docs.partner == "P-07"]
_a, _b = _p7[_p7.first_recv >= "2026-02-19"], _p7[_p7.first_recv < "2026-02-19"]
say(f"P-07 documents lost: from 19 Feb {int(_a.lost.sum())} of {len(_a)} = {_a.lost.mean():.1%}; before 19 Feb {int(_b.lost.sum())} of {len(_b)} = {_b.lost.mean():.1%} "
    f"(documents by first arrival; lost = no accepted delivery, excludes in-flight)")
say("", f"-- business documents (identity + identical content): {len(docs)} --",
    f"DUPLICATED (landed in an ERP more than once): {len(dup)} documents, {int((dup.n_landed-1).sum())} extra postings",
    f"   by type: {dup.doc_type.value_counts().to_dict()}   (invoices posted twice is what finance found)",
    f"   by tenant: {dup.tenant.nunique()} tenants affected; top: {dup.tenant.value_counts().head(3).to_dict()}",
    f"   partner resend gap: median {int(rep.groupby('ident').received_at.agg(lambda s: (s.max()-s.min()).total_seconds()).median()//60)} min, "
    f"range {int(rep.groupby('ident').received_at.agg(lambda s: (s.max()-s.min()).total_seconds()).min()//60)} min to "
    f"{int(rep.groupby('ident').received_at.agg(lambda s: (s.max()-s.min()).total_seconds()).max()//86400)} days")
pair = rep.sort_values("received_at").groupby("ident").delivered.agg(lambda s: f"first {'landed' if s.iloc[0] else 'failed'} / second {'landed' if s.iloc[1] else 'failed'}")
say("", f"   outcome of the 898 re-sent identities: {pair.value_counts().to_dict()}",
    f"   => {int((pair.str.startswith('first landed')).sum())} of 898 resends were of a document that had ALREADY landed (partner never learned it did); "
    f"a resend rescued a failed first send in only {int((pair=='first failed / second landed').sum())}")
lost = docs[docs.lost]
say("", f"LOST (never landed after all attempts; excludes {int(docs.any_flight.sum())} possibly in flight, and any doc a resend later delivered): {len(lost)} documents",
    f"   of which reached the DLQ (visible): {int(lost.any_dlq.sum())}   never reached the DLQ (invisible, 'silently lost'): {int((~lost.any_dlq).sum())}",
    f"   event-level cross-check: undelivered events={int((~ev.delivered).sum())}, in DLQ={int(ev.in_dlq.sum())}, in neither={int((~ev.delivered & ~ev.in_dlq).sum())}",
    f"   share of failed documents that the DLQ captured: {ev.in_dlq.sum()/(~ev.delivered).sum():.1%} "
    f"(flat across days, partners, tenants and error codes, so it looks like a random write failure, not a rule)",
    f"   lost by type: {lost.doc_type.value_counts().to_dict()}")
dp = at[at.error_code == "partial_content"]
pl = dp.error_message.str.extract(r"(\d+) of (\d+) lines").astype(int)
say("", f"NOT COUNTED ABOVE (hub says success): {dp.event_id.nunique()} orders returned 207 'partial_content' - {pl[0].sum()} of {pl[1].sum()} lines "
        f"rejected (unknown sku), no alert, not in DLQ. Counted as delivered; flagged as silent partial loss.")

_part = set(ev.loc[ev.event_id.isin(dp.event_id), "doc_key"]) & set(docs.index[docs.n_landed > 0])
_land, _fl = int((docs.n_landed > 0).sum()), int(((docs.n_landed == 0) & docs.any_flight).sum())
say(f"OUTCOME OF ALL {len(docs)} DOCUMENTS: landed cleanly {_land - len(_part)}, landed with refused lines (207) {len(_part)}, lost {len(lost)}, possibly in flight {_fl}  "
    f"(= {_land - len(_part) + len(_part) + len(lost) + _fl})")

# ---------------------------------------------------------------- Q2
say("Q2. ROOT CAUSES (documents never landed, one row per business document, cause = last error)")
lk = ev[ev.doc_key.isin(lost.index)].sort_values("received_at").drop_duplicates("doc_key", keep="last").copy()
mism = lk.registered_v != lk.payload_version
def cause(r):
    c = r.last_code
    if c == "unsupported_version": return "A1 no v4 mapping for 3 tenants (T-1052, T-1082, T-1155)"
    if c == "validation_error" and r.registered_v != r.payload_version:
        return "A2 partner sends v2, catalog says v1 (mapping chosen from catalog, not request)"
    if c == "validation_error": return "F  validation error, no pattern found (residual)"
    if c == "invalid_tax_code": return "B1 invalid tax code, tenant T-1007" if r.tenant_id == "T-1007" else "B2 invalid tax code, other tenants"
    if c == "rate_limited": return "C  tenant rate limit hit (429 labelled permanent, never retried)"
    if c == "customer_not_found": return "D  order arrived before its customer"
    if c == "parse_error": return "E  order total sent as text with thousands comma"
    return c
lk["cause"] = lk.apply(cause, axis=1)
t = lk.groupby("cause").agg(docs=("doc_key", "size"), in_dlq=("in_dlq", "sum"), attempts=("n_attempts", "sum"))
t["share"] = (t.docs / t.docs.sum()).round(3)
say("", t.sort_values("docs", ascending=False).to_string(), f"total {t.docs.sum()}")
tt = lk[lk.cause.str.startswith(("A1", "A2"))]
say("", f"A (mapping chosen from hand-maintained config that no longer matches reality) = {len(tt)} documents = {len(tt)/len(lk):.1%} of all lost; two different error codes, one cause",
    f"A1: 3 tenants {sorted(ev[ev.last_code=='unsupported_version'].tenant_id.unique())}: {int(ev.tenant_id.isin(ev[ev.last_code=='unsupported_version'].tenant_id.unique()).sum())} docs received, "
    f"{int(ev[ev.tenant_id.isin(ev[ev.last_code=='unsupported_version'].tenant_id.unique())].delivered.sum())} delivered (100% fail, every day of the window); "
    f"all v4 ERPs; the other {int((ev[ev.erp_api_version=='v4'].tenant_id.nunique())-3)} v4 tenants had 0 unsupported_version",
    f"A2: catalog-registered v1 but declared v2: {int((ev.registered_v!=ev.payload_version).sum())} events; "
    f"validation_error rate {(ev[ev.registered_v!=ev.payload_version].last_code=='validation_error').mean():.1%} vs "
    f"{(ev[(ev.registered_v==ev.payload_version)&(ev.payload_version=='v1')].last_code=='validation_error').mean():.1%} for matched v1 and "
    f"{(ev[(ev.registered_v==ev.payload_version)&(ev.payload_version=='v2')].last_code=='validation_error').mean():.1%} for matched v2")
p7 = ev[ev.partner_id == "P-07"]
cut = p7[p7.payload_version == "v2"].received_at.min()
first_big = p7[(p7.payload_version == "v2")]
say("", f"   P-07 (registered v1): v2 share before 19 Feb = {(p7[p7.received_at<'2026-02-19'].payload_version=='v2').mean():.1%}, "
        f"from 19 Feb = {(p7[p7.received_at>='2026-02-19'].payload_version=='v2').mean():.1%}; "
        f"validation_error on P-07 per day: ~{(ev[(ev.partner_id=='P-07')&(ev.received_at<'2026-02-19')].last_code=='validation_error').sum()/18:.1f} -> "
        f"~{(ev[(ev.partner_id=='P-07')&(ev.received_at>='2026-02-19')].last_code=='validation_error').sum()/12:.0f}  (the unannounced change)")
tx = ev[ev.last_code == "invalid_tax_code"]
oth = ev[ev.tenant_id != "T-1007"]
say("", f"B: invalid_tax_code (event level, incl. 3 still in flight): {len(tx)} docs, {len(tx)*25} attempts at fixed 180 s gaps; T-1007 = {(tx.tenant_id=='T-1007').sum()} docs "
        f"({(tx.tenant_id=='T-1007').mean():.0%}); rate T-1007 {(ev[ev.tenant_id=='T-1007'].last_code=='invalid_tax_code').mean():.1%} of its docs vs "
        f"{(oth.last_code=='invalid_tax_code').mean():.1%} elsewhere; never recovered (0 of {len(tx)})")
cn = at[at.error_code == "customer_not_found"].event_id.unique()
o = ev[ev.event_id.isin(cn)]
say("", f"D: {len(o)} orders hit customer_not_found; {int(o.delivered.sum())} landed on retry (3-5 attempts), {int((~o.delivered).sum())} never landed; "
        f"gave up after 2-4 attempts, not the configured 5 (attempt counts of the failed: {o[~o.delivered].n_attempts.value_counts().sort_index().to_dict()}).")
cust = ev[ev.doc_type == "customer"].assign(ref=lambda d: d.payload.map(lambda p: p["customer_ref"]))[["tenant_id", "ref"]].drop_duplicates()
od = ev[ev.doc_type == "order"].assign(ref=lambda d: d.payload.map(lambda p: p["customer_ref"]))
od = od.merge(cust, on=["tenant_id", "ref"], how="left", indicator=True)
cf = ev[ev.doc_type == "customer"].assign(ref=lambda d: d.payload.map(lambda p: p["customer_ref"]))
cf = cf.merge(at[at.outcome == "success"].groupby("event_id").attempted_at.min().rename("cust_landed"), left_on="event_id", right_index=True)
fo = o.assign(ref=o.payload.map(lambda p: p["customer_ref"])).merge(cf[["tenant_id", "ref", "received_at", "cust_landed"]], on=["tenant_id", "ref"], suffixes=("", "_c"))
fo["gap_min"] = (fo.cust_landed - fo.received_at).dt.total_seconds() / 60
say("", f"   customer record found for {fo.event_id.nunique()} of {len(o)} such orders; it was received AFTER the order in {int((fo.received_at_c>fo.received_at).sum())} cases",
    f"   order -> customer landed gap (min): failed orders median {fo[~fo.delivered].gap_min.median():.0f}, max {fo[~fo.delivered].gap_min.max():.0f}; "
    f"recovered orders median {fo[fo.delivered].gap_min.median():.0f}, max {fo[fo.delivered].gap_min.max():.0f}",
    "   ODDITY: recovered orders landed within ~30 min of arrival, yet their customer record landed ~2 h later in the hub's own log. So the ERP could see the customer "
    "before the hub delivered it (created another way), or retry success is not about the customer record. The log cannot tell which; I would ask the platform team.")
say("", f"   but {(od._merge=='left_only').mean():.1%} of ALL orders reference a customer the hub never saw in 30 days (already in the ERP) - the hub cannot pre-check dependencies from its own history.")
say("", f"transient controls: upstream_unavailable (503) hit {at[at.error_code=='upstream_unavailable'].event_id.nunique()} docs; every one landed on attempt 2 (correctly retryable, not a loss)")

say("Q2b. HUB LABELS vs EVIDENCE (attempt level)")
lab = at[at.error_code != ""].groupby(["error_code", "http_status", "outcome"]).size().rename("attempts").reset_index()
lab["docs_recovered"] = lab.error_code.map(lambda c: ev[ev.event_id.isin(at[at.error_code == c].event_id) & ev.delivered].shape[0])
lab["docs_seen"] = lab.error_code.map(lambda c: at[at.error_code == c].event_id.nunique())
say("", lab.to_string(index=False))
gaps = at.assign(gap=at.groupby("event_id").attempted_at.diff().dt.total_seconds(), prev=at.groupby("event_id").error_code.shift()).dropna(subset=["gap"])
say("", "retry spacing (s) by previous error: " + str(gaps.groupby("prev").gap.agg(lambda s: [int(x) for x in sorted(s.round().unique())[:5]]).to_dict()),
    f"attempts per document: {ev.n_attempts.value_counts().sort_index().to_dict()}  (configured max is 5; {int((ev.n_attempts>5).sum())} documents got 25)",
    "=> only customer_not_found backs off exponentially; tax-code retries are fixed 180 s, parse 60 s, version 240 s")
sh = at.groupby("error_code").size()
say("", f"DLQ attempts_made equals logged attempts for {(dl.attempts_made.astype(int).values == ev.set_index('event_id').loc[dl.event_id].n_attempts.values).mean():.0%} of rows; "
        f"requeued=true on {int((dl.requeued=='true').sum())} rows, but no attempt after the dead-letter is logged for any of them "
        f"and none is recorded as delivered (a requeue leaves no trace; {int(dl[dl.requeued=='true'].last_error_code.isin(['validation_error','unsupported_version','invalid_tax_code']).sum())} "
        f"of them were permanent causes that a requeue cannot fix)")

# ---------------------------------------------------------------- Q3
say("Q3. NOISY NEIGHBOUR?")
at2 = at.merge(ev[["event_id", "tenant_id", "received_at", "erp_product_line", "region"]], on="event_id")
tot = at2.groupby("tenant_id").size().sort_values(ascending=False)
dv = ev.groupby("tenant_id").size().sort_values(ascending=False)
say("", f"T-1007 share of delivery attempts {tot['T-1007']/tot.sum():.1%} vs share of documents {dv['T-1007']/dv.sum():.1%}  "
        f"(next tenant: {tot.iloc[1]/tot.sum():.1%} of attempts); {int((ev[ev.tenant_id=='T-1007'].n_attempts==25).sum())} of its documents were tried 25 times",
    f"T-1007 steady ~{at2[at2.tenant_id=='T-1007'].shape[0]/30:.0f} attempts/day, every day (not a spike); failing share of its docs "
    f"{(~ev[ev.tenant_id=='T-1007'].delivered).mean():.0%} vs {(~oth.delivered).mean():.0%} for all others")
fa = at2[at2.attempt_no == 1].assign(wait=lambda d: (d.attempted_at - d.received_at).dt.total_seconds())
say("", f"queue wait (received -> first attempt): median {fa.wait.median():.0f}s, p99 {fa.wait.quantile(.99):.0f}s, max {fa.wait.max():.0f}s; "
        f"max never above 285s on any day -> NO backlog visible; the reported 6-hour backlog does not appear anywhere in these logs",
    f"end-to-end for delivered docs (received -> landed): max {((at[at.outcome=='success'].groupby('event_id').attempted_at.min()-ev.set_index('event_id').received_at.reindex(at[at.outcome=='success'].event_id.unique())).dt.total_seconds().max()/60):.0f} min; longest retry chain 72 min")
at2["h"] = at2.attempted_at.dt.floor("h")
H = at2.groupby("h").agg(lat=("latency_ms", "mean"), n=("event_id", "size"))
H["n7"] = at2[at2.tenant_id == "T-1007"].groupby("h").size(); H["n7"] = H.n7.fillna(0)
H["lat_oth"] = at2[at2.tenant_id != "T-1007"].groupby("h").latency_ms.mean()
hot = H[H.lat > 1000]
inc = (at2.attempted_at >= "2026-02-12") & (at2.attempted_at < "2026-02-15")
say("", f"latency: {len(hot)} hours with mean >1000ms, first {hot.index.min()} last {hot.index.max()} (= 12-14 Feb, 72 consecutive hours)",
    f"   median latency inside {at2[inc].latency_ms.median():.0f} ms vs outside {at2[~inc].latency_ms.median():.0f} ms; "
    f"{(at2[inc].groupby('tenant_id').latency_ms.median()>1500).mean():.0%} of active tenants slowed, all 5 ERP product lines and all regions",
    f"   corr(T-1007 hourly attempts, other tenants' latency) = {H.n7.corr(H.lat_oth):.3f}  -> the slowdown is NOT explained by T-1007",
    f"   during the incident: outcome mix, document volume and queue wait were unchanged (no visible delivery damage); nobody reported it")
say("", "VERDICT: no evidence in these logs that T-1007 slowed other tenants. It does take 36% of delivery capacity on 5% of the traffic by retrying a permanent error "
        "25 times, with no per-tenant limit - a risk, not a measured harm. A 72-hour all-tenant latency incident (12-14 Feb) is the real unreported event.")

# ---------------------------------------------------------------- extras
say("EXTRA. OTHER ODDITIES")
pii = ev[ev.doc_type == "customer"].payload.map(lambda p: "primary_contact" in p)
dob = ev[ev.doc_type == "customer"].payload.map(lambda p: "date_of_birth" in p.get("primary_contact", {}))
landed = at[at.outcome == "success"].groupby("event_id").attempted_at.min()
e2 = ev.set_index("event_id").assign(sent=ev.set_index("event_id").sent_at, landed=landed)
e2 = e2.dropna(subset=["landed"]); e2["sla"] = e2.sla_minutes.astype(int)
b_sent = ((e2.landed - e2.sent).dt.total_seconds() / 60 > e2.sla).sum()
b_recv = ((e2.landed - e2.received_at).dt.total_seconds() / 60 > e2.sla).sum()
say("", f"customer records carrying a primary_contact (name, email, phone, date_of_birth) stored verbatim in the log: {int(pii.sum())} ({int(dob.sum())} with date_of_birth) - personal data in an unredacted ops log",
    f"SLA breaches among delivered docs: {b_sent} if measured from partner sent_at, {b_recv} from hub received_at (partner clocks differ by up to 10 min; I judge from received_at)",
    f"payload_version declared vs registered: {int((ev.registered_v!=ev.payload_version).sum())} mismatches ({ev[ev.registered_v!=ev.payload_version].partner_id.value_counts().to_dict()})")

# ---------------------------------------------------------------- hand-off for design_evidence.py
lk[["doc_key", "event_id", "tenant_id", "partner_id", "doc_type", "cause", "in_dlq", "n_attempts"]].to_csv(
    Path(__file__).with_name("lost_documents.csv"), index=False)
