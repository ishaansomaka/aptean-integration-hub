"""design_evidence.py - tests each Part B design rule against the 30 days of logs.
Run from repo root (after part_a.py has written lost_documents.csv):  python -I analysis/design_evidence.py data
"""
import json, re, sys
from pathlib import Path
import pandas as pd

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
HERE = Path(__file__).parent
BN = {"order": "order_number", "invoice": "invoice_number", "customer": "customer_ref", "shipment": "shipment_id"}
def say(t="", *l):
    if t: print(f"\n=== {t} ===")
    for x in l: print(x)

ev = pd.read_json(D / "inbound_events.jsonl", lines=True, dtype=False)
ev["received_at"] = pd.to_datetime(ev.received_at, utc=True)
at = pd.read_csv(D / "delivery_attempts.csv", dtype=str, keep_default_na=False).drop_duplicates()
at["attempted_at"] = pd.to_datetime(at.attempted_at, utc=True); at["attempt_no"] = at.attempt_no.astype(int)
tc = pd.read_csv(D / "tenant_config.csv", dtype=str); tc["sla_minutes"] = tc.sla_minutes.astype(int)
pc = pd.read_csv(D / "partner_catalog.csv", dtype=str).rename(columns={"payload_version": "registered_v"})
lost = pd.read_csv(HERE / "lost_documents.csv")
landed = set(at.loc[at.outcome == "success", "event_id"])
ev["landed"] = ev.event_id.isin(landed)
ev["bn"] = [p[BN[t]] for p, t in zip(ev.payload, ev.doc_type)]
ev["ident"] = ev.tenant_id + "|" + ev.doc_type + "|" + ev.bn
ev["content"] = ev.payload.map(lambda p: json.dumps({k: v for k, v in p.items() if k != "sent_at"}, sort_keys=True))

# ------------------------------------------------------------------ 1. identity + idempotency replay
say("1. IDENTITY RULE REPLAYED ON THE REAL TRAFFIC")
rows = ev.sort_values("received_at")
seen, out = {}, {"new": 0, "duplicate": 0, "conflict": 0}
verdict = []
for r in rows.itertuples():
    k = r.ident
    if k not in seen: seen[k] = r.content; v = "new"
    elif seen[k] == r.content: v = "duplicate"
    else: v = "conflict"
    out[v] += 1; verdict.append(v)
rows = rows.assign(verdict=verdict)
dup = rows[rows.verdict == "duplicate"]; con = rows[rows.verdict == "conflict"]
say("", f"rule: same tenant+type+business number+same content = duplicate; same identity but different content = conflict (held for a human); else new",
    f"result over {len(rows)} deliveries: {out}",
    f"duplicates that had ALSO landed in the old hub (= postings the rule prevents): {int(dup.landed.sum())} of {len(dup)}"
    f"  (invoices {int(dup[dup.doc_type=='invoice'].landed.sum())})",
    f"keyless deliveries among those duplicates: {int(dup.idempotency_key.isna().sum())}")
first = rows[rows.verdict == "new"].set_index("ident")
dk = dup.merge(first[["idempotency_key"]], left_on="ident", right_index=True, suffixes=("", "_first"))
say("", f"duplicates a partner-key rule would have stopped (resend carries the SAME key as the first send): "
        f"{int(((dk.idempotency_key == dk.idempotency_key_first) & dk.idempotency_key.notna()).sum())} of {len(dup)}")
cn = con.merge(first[["partner_id", "payload", "event_id"]], left_on="ident", right_index=True, suffixes=("", "_first"))
name = lambda p: p.get("legal_name")
same_name = sum(name(a) == name(b) for a, b in zip(cn.payload, cn.payload_first))
say("", f"the {len(con)} conflicts: all customer records={set(cn.doc_type)=={'customer'}}; legal_name differs in {len(cn)-same_name} of {len(cn)}; "
        f"same partner both times: {int((cn.partner_id==cn.partner_id_first).sum())}; both deliveries returned success in old hub: "
        f"{int((cn.landed & cn.event_id_first.isin(landed)).sum())}")
say("", f"partner-key trust check: keyed deliveries={int(ev.idempotency_key.notna().sum())}, distinct keys={ev.idempotency_key.nunique()} (never reused), "
        f"deliveries with no key={int(ev.idempotency_key.isna().sum())} ({ev.idempotency_key.isna().mean():.1%})")

# ------------------------------------------------------------------ 2. normalisation of text totals
say("2. TEXT TOTALS: IS 'strip the comma' SAFE?")
o = ev[ev.doc_type == "order"].copy()
o["is_str"] = o.payload.map(lambda p: isinstance(p["total"], str))
o["lines_sum"] = o.payload.map(lambda p: round(sum(l["qty"] * l["unit_price"] for l in p["lines"]), 2))
o["total_n"] = o.payload.map(lambda p: float(str(p["total"]).replace(",", "")))
o["fmt_ok"] = o.payload.map(lambda p: bool(re.fullmatch(r"\d{1,3}(,\d{3})*\.\d{2}", p["total"])) if isinstance(p["total"], str) else True)
for lab, g in [("text totals", o[o.is_str]), ("numeric totals", o[~o.is_str])]:
    print(f"{lab}: {len(g)}; pattern 1,234.56 holds for {int(g.fmt_ok.sum())}; normalised total equals sum of lines for {int(((g.total_n-g.lines_sum).abs()<0.01).sum())}")
print(f"NOTE: order total equals sum(qty*unit_price) to the cent for {int(((o.total_n-o.lines_sum).abs()<0.01).sum())} of {len(o)} orders, so totals cannot be checked against lines "
      f"(they may include tax or shipping). The comma rule is justified by the uniform 1,234.56 pattern, not by reconciliation.")
say("", f"orders with text totals that landed in the old hub: {int(o[o.is_str].landed.sum())} of {int(o.is_str.sum())}")

# ------------------------------------------------------------------ 3. wasted retries
say("3. RETRY WASTE (attempts that could not have helped)")
g = at.groupby("event_id").agg(n=("attempt_no", "size"), ok=("outcome", lambda s: (s == "success").any()))
tot = len(at)
w_never = int((g[~g.ok].n - 1).sum())
tax = at[at.error_code == "invalid_tax_code"]; uv = at[at.error_code == "unsupported_version"]
say("", f"total attempts {tot}; attempts beyond the first on documents that NEVER landed: {w_never} = {w_never/tot:.1%}",
    f"   invalid_tax_code: {len(tax)} attempts on {tax.event_id.nunique()} docs, 0 recovered -> {len(tax)-tax.event_id.nunique()} repeat attempts ({(len(tax)-tax.event_id.nunique())/tot:.1%} of all)",
    f"   unsupported_version: {len(uv)} attempts on {uv.event_id.nunique()} docs, 0 recovered -> {len(uv)-uv.event_id.nunique()} repeat attempts")
t7 = at.merge(ev[["event_id", "tenant_id"]], on="event_id"); t7 = t7[t7.tenant_id == "T-1007"]
t7_tax = t7[t7.error_code == "invalid_tax_code"]
after_total = tot - (len(tax) - tax.event_id.nunique())
after_t7 = len(t7) - (len(t7_tax) - t7_tax.event_id.nunique())
say("", f"T-1007 share of attempts: {len(t7)/tot:.1%} now -> {after_t7/after_total:.1%} if tax-code failures are tried once (no document is lost by this: 0 of {tax.event_id.nunique()} ever recovered)")

# ------------------------------------------------------------------ 4. what could be fixed by us alone
say("4. WHO CAN FIX EACH LOST DOCUMENT")
own = {"A1": "us", "A2": "us / partner registration", "E": "us", "C": "us (back off, retry later)", "D": "us (wait for customer)",
       "B1": "tenant (their tax set-up)", "B2": "tenant (their tax set-up)", "F": "unknown"}
lost["owner"] = lost.cause.str.split().str[0].map(own)
t = lost.groupby("owner").size().sort_values(ascending=False)
say("", t.to_string(), f"total {len(lost)}; fixable on our side (our config, our rules or our retry policy): "
        f"{int(lost.owner.str.startswith('us').sum())} = {lost.owner.str.startswith('us').mean():.1%}")

# ------------------------------------------------------------------ 5. dependency window
say("5. ORDER BEFORE CUSTOMER: HOW LONG TO WAIT?")
cn_ids = set(at.loc[at.error_code == "customer_not_found", "event_id"])
od = ev[ev.event_id.isin(cn_ids)].assign(ref=lambda d: d.payload.map(lambda p: p["customer_ref"]))
cu = ev[ev.doc_type == "customer"].assign(ref=lambda d: d.payload.map(lambda p: p["customer_ref"]))
cl = at[at.outcome == "success"].groupby("event_id").attempted_at.min().rename("cust_landed")
cu = cu.join(cl, on="event_id").dropna(subset=["cust_landed"])
m = od.merge(cu[["tenant_id", "ref", "cust_landed"]], on=["tenant_id", "ref"])
m["gap_min"] = (m.cust_landed - m.received_at).dt.total_seconds() / 60
say("", f"{len(od)} orders hit customer_not_found; customer record landed later for {len(m)}; gap minutes: "
        f"median {m.gap_min.median():.0f}, p95 {m.gap_min.quantile(.95):.0f}, max {m.gap_min.max():.0f}  ({m.gap_min.max()/60:.1f} h)",
    f"old retry chain for this code spans at most 30 min (120+240+480+960 s)")

# ------------------------------------------------------------------ 6. partner drift detection time
say("6. HOW FAST WOULD A DRIFT ALARM HAVE CAUGHT P-07?")
p = ev.merge(pc[["partner_id", "registered_v"]], on="partner_id")
p = p[p.partner_id == "P-07"].sort_values("received_at").reset_index(drop=True)
p["mis"] = (p.payload_version != p.registered_v).astype(int)
p["roll"] = p.mis.rolling(10).sum()
hit = p[p.roll >= 6].iloc[0]
last_v1 = p[p.mis == 0].received_at.max()
say("", f"alarm rule: 6 of a partner's last 10 documents declare a version other than the registered one",
    f"P-07 alarm would fire at {hit.received_at} (document #{p[p.roll>=6].index[0]+1}); last old-layout (v1) document {last_v1}",
    f"false alarms of the same rule on the other partners in 30 days: " +
    str({pid: int((g.sort_values('received_at').assign(m=lambda d: (d.payload_version!=d.registered_v).astype(int)).m.rolling(10).sum()>=6).any())
         for pid, g in ev.merge(pc[['partner_id','registered_v']], on='partner_id').query("partner_id!='P-07'").groupby('partner_id')}),
    f"P-07 documents sent after the alarm time, to end of window (any failure cause): "
    f"{int(ev[(ev.partner_id=='P-07')&(ev.received_at>=hit.received_at)].shape[0])} sent, "
    f"{int((~ev[(ev.partner_id=='P-07')&(ev.received_at>=hit.received_at)].landed).sum())} did not land (old hub)")

# ------------------------------------------------------------------ 7. silent-tenant detection
say("7. IF ONE TENANT WENT SILENT, HOW LONG UNTIL SOMEONE KNEW?")
ev2 = ev.merge(tc[["tenant_id", "sla_minutes"]], on="tenant_id")
perday = ev.groupby("tenant_id").size() / 30
say("", f"documents per tenant per day: median {perday.median():.1f}, p10 {perday.quantile(.1):.1f}, p90 {perday.quantile(.9):.1f}; "
        f"tenants under 1 document/day: {int((perday<1).sum())} of {len(perday)}",
    f"SLA values (min): {tc.sla_minutes.value_counts().sort_index().to_dict()}")
ev2 = ev2.sort_values(["tenant_id", "received_at"])
ev2["gap_h"] = ev2.groupby("tenant_id").received_at.diff().dt.total_seconds() / 3600
gp = ev2.groupby("tenant_id").gap_h.quantile(.99)
vol = perday.reindex(gp.index)
say("", f"p99 gap (h) for the busiest tenants (top 10% by volume): median {gp[vol>=vol.quantile(.9)].median():.1f}; for the quietest half: median {gp[vol<=vol.median()].median():.1f}")
say("", f"inbound silence: p99 gap between a tenant's documents has median {gp.median():.1f} h, p90 across tenants {gp.quantile(.9):.1f} h, max {gp.max():.1f} h",
    "=> 'no new documents' cannot be a fast alarm for quiet tenants; 'a document we accepted has not landed within its SLA' can")
# consecutive-failure alarm
ev2["bad"] = ~ev2.landed
def first_run(g, k=6):
    c = 0
    for r in g.itertuples():
        c = c + 1 if r.bad else 0
        if c >= k: return r.received_at
    return pd.NaT
runs = ev2.groupby("tenant_id").apply(first_run)
fired = runs.dropna()
say("", f"alarm rule: {6} failed documents in a row for one tenant -> fires for {len(fired)} of 180 tenants in 30 days")
for tid in ["T-1052", "T-1082", "T-1155", "T-1007"]:
    f1 = ev2[ev2.tenant_id == tid].received_at.min()
    print(f"   {tid}: first document {f1:%d %b %H:%M}Z, alarm at {runs[tid]:%d %b %H:%M}Z -> {(runs[tid]-f1).total_seconds()/3600:.1f} h after first document ({int((ev2.tenant_id==tid).sum())} docs in window)")
others = fired.drop(["T-1052", "T-1082", "T-1155", "T-1007"], errors="ignore")
print(f"   other tenants that would also alarm: {len(others)}; per-tenant failed share for them (median): "
      f"{ev2[ev2.tenant_id.isin(others.index)].groupby('tenant_id').bad.mean().median():.0%}")

# ------------------------------------------------------------------ 8. figures quoted in the write-up that need their own derivation
say("8. SUMMARY FIGURES QUOTED IN THE WRITE-UP")
n_docs = ev.assign(c=ev.content).groupby(["ident", "c"]).ngroups
lost["grp"] = lost.cause.str.split().str[0].replace({"A1": "A", "A2": "A", "B1": "B", "B2": "B"})
sh = lost.groupby("grp").size().sort_values(ascending=False)
say("", f"distinct business documents (identity + content): {n_docs}; lost {len(lost)} = {len(lost)/n_docs:.1%}; per day {len(lost)/30:.1f}",
    f"lost, in no list: {int((~lost.in_dlq).sum())} = {(~lost.in_dlq).mean():.1%} of lost; per day {(~lost.in_dlq).sum()/30:.1f}",
    f"tenants with at least one lost document: {lost.tenant_id.nunique()} of {len(tc)}",
    f"tenants with a lost rate_limited document: {lost[lost.cause.str.startswith('C')].tenant_id.nunique()}",
    "causes merged as in the write-up (A = A1+A2, B = B1+B2): " + ", ".join(f"{k} {v} ({v/len(lost):.1%})" for k, v in sh.items()),
    f"repeat attempts on documents that never landed: {w_never} of {tot} attempts = {w_never/tot:.1%}",
    f"documents whose success the new rules only ASSUME (version 1063 + text total 167 + customer 280) = {1063+167+280}",
    f"wait limit 12 h / longest observed gap {m.gap_min.max()/60:.2f} h = {12/(m.gap_min.max()/60):.1f}x")
at["h"] = at.attempted_at.dt.floor("h"); at["lat"] = at.latency_ms.astype(int)
H = at.groupby("h").lat.mean(); inc = (H.index >= "2026-02-12") & (H.index < "2026-02-15")
say("", f"hourly mean attempt latency outside 12-14 Feb: median {H[~inc].median():.0f} ms, p99 {H[~inc].quantile(.99):.0f}, max {H[~inc].max():.0f}; "
        f"inside: min {H[inc].min():.0f}, median {H[inc].median():.0f}  -> a 1,300 ms hourly threshold separates them with 0 false alarms and fires in the first hour ({H[inc].index.min()})",
    f"alarm (d) hours above 1,300 ms outside the incident: {int((H[~inc] > 1300).sum())}; inside: {int((H[inc] > 1300).sum())} of {int(inc.sum())}")

# ------------------------------------------------------------------ 9. how sensitive are the alarm thresholds? (answers "fitted to 30 days")
say("9. ALARM SENSITIVITY: do the thresholds sit on a plateau or a knife edge?")
print("(b) N failed documents in a row, per tenant -> tenants that would alarm (the 4 genuinely broken ones are T-1007, T-1052, T-1082, T-1155):")
for k in range(2, 9):
    f = ev2.groupby("tenant_id").apply(lambda g: first_run(g, k)).dropna()
    extra = sorted(set(f.index) - {"T-1007", "T-1052", "T-1082", "T-1155"})
    print(f"   N={k}: fires for {len(f)} tenants; false alarms: {len(extra)}" + (f" (e.g. {extra[:3]})" if extra else ""))
pv = ev.merge(pc[["partner_id", "registered_v"]], on="partner_id").sort_values("received_at")
pv["mis"] = (pv.payload_version != pv.registered_v).astype(int)
print("(c) drift rule 'k of last n documents declare an unregistered version':")
for k, n in [(3, 10), (4, 10), (5, 10), (6, 10), (7, 10), (4, 6), (3, 5)]:
    first_alarm, false_p = None, []
    for pid, g in pv.groupby("partner_id"):
        r = g.mis.rolling(n).sum()
        if (r >= k).any():
            t = g[(r >= k).values].received_at.iloc[0]
            if pid == "P-07": first_alarm = t
            else: false_p.append(pid)
    print(f"   {k} of {n}: P-07 alarm {first_alarm:%d %b %H:%M}Z ({(first_alarm-pd.Timestamp('2026-02-19 00:18:04', tz='UTC')).total_seconds()/60:.0f} min after its last old-layout doc); false alarms on other partners: {len(false_p)} {false_p}")
Hh = at.groupby(at.attempted_at.dt.floor("h")).latency_ms.apply(lambda s: s.astype(int).mean()); inc = (Hh.index >= "2026-02-12") & (Hh.index < "2026-02-15")
print(f"(d) hourly latency threshold: any value between {Hh[~inc].max():.0f} ms (highest normal hour) and {Hh[inc].min():.0f} ms (lowest incident hour) gives 0 false alarms and fires in the first incident hour")

# ------------------------------------------------------------------ 10. wait limit for order-before-customer
say("10. HOW LONG SHOULD AN ORDER WAIT FOR ITS CUSTOMER? (limit vs orders released)")
print(f"orders with a customer record that landed later: {len(m)}; gap (h): median {m.gap_min.median()/60:.1f}, p95 {m.gap_min.quantile(.95)/60:.1f}, max {m.gap_min.max()/60:.2f}")
for lim in [0.5, 1, 2, 3, 4, 4.5, 6, 8, 12, 24]:
    rel = int((m.gap_min <= lim * 60).sum())
    print(f"   limit {lim:>4} h: released {rel:>3} of {len(m)}; would expire to NEEDS_REVIEW {len(m)-rel:>3}")
print("   (0.5 h is the old hub's whole retry chain)")

# ------------------------------------------------------------------ 11. scale and the noisy-neighbour measurements the logs cannot give
say("11. SCALE, AND WHAT THE LOGS CANNOT TELL US ABOUT CAPACITY")
ev["h"] = ev.received_at.dt.floor("h")
print(f"documents per day: mean {len(ev)/30:.0f}; attempts per day: mean {len(at)/30:.0f}; busiest hour: {ev.groupby('h').size().max()} arrivals, {at.groupby(at.attempted_at.dt.floor('h')).size().max()} attempts")
a2 = at.merge(ev[["event_id", "tenant_id", "received_at"]], on="event_id")
a2["h"] = a2.attempted_at.dt.floor("h")
hr = a2.groupby("h").apply(lambda g: pd.Series({"n": len(g), "n7": (g.tenant_id == "T-1007").sum()}))
hr["share7"] = hr.n7 / hr.n
fa = a2[a2.attempt_no == 1].assign(wait=lambda d: (d.attempted_at - d.received_at).dt.total_seconds(), rh=lambda d: d.received_at.dt.floor("h"))
w_oth = fa[fa.tenant_id != "T-1007"].groupby("rh").wait.mean()
j = hr.join(w_oth.rename("wait_others"), how="inner")
print(f"T-1007 share of an hour's attempts: median {hr.share7.median():.0%}, busiest hour {hr.share7.max():.0%}, hours above 50%: {int((hr.share7>.5).sum())} of {len(hr)}")
print(f"correlation: T-1007 hourly attempts vs other tenants' mean wait to first attempt = {j.n7.corr(j.wait_others):.3f}; wait for others when T-1007 share is in top decile: "
      f"{j[j.share7>=j.share7.quantile(.9)].wait_others.mean():.0f}s vs {j[j.share7<=j.share7.quantile(.1)].wait_others.mean():.0f}s in bottom decile")
print("NOT in the logs: queue depth, worker count, concurrency, capacity. So 'T-1007 harmed nobody' means 'no harm is visible in wait or latency', not 'capacity was ample'.")
