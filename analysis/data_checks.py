"""data_checks.py - data hygiene checks BEFORE any count is trusted.
Run: python -I analysis/data_checks.py   (from repo root)
"""
import json, sys
from pathlib import Path
import pandas as pd

D = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")

ev = pd.read_json(D / "inbound_events.jsonl", lines=True, dtype=False)
at = pd.read_csv(D / "delivery_attempts.csv", dtype=str, keep_default_na=False)
dl = pd.read_csv(D / "dead_letter_queue.csv", dtype=str, keep_default_na=False)
tc = pd.read_csv(D / "tenant_config.csv", dtype=str)
pc = pd.read_csv(D / "partner_catalog.csv", dtype=str)

def hdr(t): print(f"\n=== {t} ===")

hdr("shapes")
for n, d in [("events", ev), ("attempts", at), ("dlq", dl), ("tenants", tc), ("partners", pc)]:
    print(n, d.shape)

hdr("events: duplicate rows / ids")
print("fully duplicate rows (excl payload):", ev.drop(columns="payload").duplicated().sum())
print("duplicate event_id:", ev.event_id.duplicated().sum())
print("null event_id/tenant/partner/doc_type:", ev[["event_id","tenant_id","partner_id","doc_type"]].isna().sum().to_dict())
print("doc_type counts:", ev.doc_type.value_counts().to_dict())
print("payload_version counts:", ev.payload_version.value_counts().to_dict())
print("tenants not in config:", set(ev.tenant_id) - set(tc.tenant_id))
print("partners not in catalog:", set(ev.partner_id) - set(pc.partner_id))

hdr("events: timestamp formats")
def fmt(s):
    s = str(s)
    import re
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", s): return "iso_Z"
    if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", s): return "iso_offset"
    return "other:" + re.sub(r"\d", "9", s)
print("received_at formats:", ev.received_at.map(fmt).value_counts().to_dict())
print("attempted_at formats:", at.attempted_at.map(fmt).value_counts().to_dict())
print("dlq first_seen formats:", dl.first_seen_at.map(fmt).value_counts().to_dict())
sent = ev.payload.map(lambda p: p.get("sent_at"))
print("sent_at formats:", sent.map(fmt).value_counts().to_dict())

hdr("events: idempotency_key vs partner catalog")
ev["has_key"] = ev.idempotency_key.notna() & (ev.idempotency_key.astype(str) != "")
x = ev.merge(pc[["partner_id", "sends_idempotency_key", "payload_version"]], on="partner_id", suffixes=("", "_catalog"))
print(pd.crosstab(x.partner_id, x.has_key))
print("catalog says sends key but row has none / vice versa:")
print(pd.crosstab(x.sends_idempotency_key, x.has_key))
print("declared payload_version vs catalog:")
print(pd.crosstab([x.partner_id, x.payload_version_catalog], x.payload_version))

hdr("attempts: duplicates / orphans")
print("exact duplicate rows:", at.duplicated().sum())
print("duplicate (event_id, attempt_no):", at.duplicated(["event_id", "attempt_no"]).sum())
print("attempts with event_id not in events:", (~at.event_id.isin(ev.event_id)).sum(),
      "distinct:", at.loc[~at.event_id.isin(ev.event_id), "event_id"].nunique())
print("events with no attempts:", (~ev.event_id.isin(at.event_id)).sum())
print("outcome counts:", at.outcome.value_counts().to_dict())
print("http_status x outcome:")
print(pd.crosstab(at.http_status, at.outcome))
print("target_endpoint counts:"); print(at.target_endpoint.value_counts())

hdr("dlq: duplicates / orphans")
print("duplicate event_id in dlq:", dl.event_id.duplicated().sum())
print("dlq ids not in events:", (~dl.event_id.isin(ev.event_id)).sum())
print("dlq ids with no attempts:", (~dl.event_id.isin(at.event_id)).sum())
print("attempts_made distribution:"); print(dl.attempts_made.astype(int).describe())
print("requeued:", dl.requeued.value_counts().to_dict())
print("last_error_code:", dl.last_error_code.value_counts().to_dict())
