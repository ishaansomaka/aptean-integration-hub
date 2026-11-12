# Data dictionary

Five extracts from the integration hub's own operational logs, covering
**2026-02-01 through 2026-03-02 inclusive** (30 days). Treat **2026-03-03** as "today". A
handful of delivery attempts for late-arriving documents trail over into 2026-03-03.

These are raw operational logs, not a curated dataset. They were pulled by the platform team
straight from the hub's storage with no cleaning, no reconciliation between files, and no
redaction.

| File | Format | What it records | Rows |
| --- | --- | --- | ---: |
| `inbound_events.jsonl` | JSON Lines | every document a partner sent to the hub | 24,000 |
| `delivery_attempts.csv` | CSV | every attempt the hub made to deliver a document into a tenant ERP | 57,072 |
| `dead_letter_queue.csv` | CSV | documents the hub moved to the dead-letter queue after giving up | 2,633 |
| `tenant_config.csv` | CSV | the 180 tenants and how their ERP is configured | 180 |
| `partner_catalog.csv` | CSV | the 9 partner systems that feed the hub | 9 |

---

## `inbound_events.jsonl`

One JSON object per line. The hub writes a row here the moment a document arrives, before it
attempts anything with it.

| Field | Type | Meaning |
| --- | --- | --- |
| `event_id` | string | The hub's own identifier for this delivery of this document. Minted on arrival. |
| `tenant_id` | string | Which tenant's ERP the document is destined for. Joins to `tenant_config.csv`. |
| `partner_id` | string | Which partner system sent it. Joins to `partner_catalog.csv`. |
| `doc_type` | string | `order`, `customer`, `invoice`, or `shipment`. |
| `payload_version` | string | The payload schema version the partner declared for this request. |
| `received_at` | timestamp | When the hub accepted the request. UTC, ISO 8601. |
| `idempotency_key` | string / null | The value the partner supplied in the `Idempotency-Key` request header, where the partner supplied one. |
| `payload` | object | The partner's document body, stored verbatim as received. Shape varies by `doc_type` and by `payload_version`. |

### Payload shapes

The hub stores whatever the partner sent. Fields present depend on `doc_type` and on the
partner's declared `payload_version`.

**`order`** — `order_number`, `customer_ref`, `order_date`, `currency`, `total`, `lines[]`
(each with `sku`, `qty`, `unit_price`), a shipping address, and `sent_at`.

**`customer`** — `customer_ref`, `legal_name`, `billing_address`, `payment_terms`, `sent_at`,
and on some records a `primary_contact` object.

**`invoice`** — `invoice_number`, `customer_ref`, `amount_due`, `currency`, `due_date`, `sent_at`.

**`shipment`** — `shipment_id`, `order_number`, `carrier`, `tracking`, `shipped_at`, `sent_at`.

`sent_at` is the timestamp the *partner* claims it dispatched the document. `received_at` is the
timestamp the *hub* recorded on arrival. They come from different clocks.

## `delivery_attempts.csv`

One row per attempt to push a document into a tenant's ERP. A document may have many attempts.
The hub is configured to retry up to **5 times** with exponential backoff.

| Column | Type | Meaning |
| --- | --- | --- |
| `event_id` | string | The document being delivered. Joins to `inbound_events.jsonl`. |
| `attempt_no` | int | 1 for the first attempt, incrementing per retry. |
| `attempted_at` | timestamp | When the attempt was made. UTC. |
| `outcome` | string | The hub's own classification of the attempt: `success`, `retryable_error`, or `permanent_error`. This is the hub's judgement, written at the time, from its error-handling rules. |
| `http_status` | int | The status code the tenant's ERP API returned. |
| `error_code` | string | Machine-readable error identifier from the ERP. Normally blank when an attempt succeeds. |
| `error_message` | string | Human-readable error text from the ERP. Normally blank when an attempt succeeds. |
| `latency_ms` | int | Round-trip time for the attempt. |
| `target_endpoint` | string | The ERP API path the attempt was made against. |

The log-shipping job that produces this file has been re-run by hand on occasion.

## `dead_letter_queue.csv`

Documents the hub moved to the dead-letter queue after it stopped trying to deliver them.

| Column | Type | Meaning |
| --- | --- | --- |
| `event_id` | string | The document. |
| `tenant_id` | string | Destination tenant. |
| `partner_id` | string | Originating partner. |
| `doc_type` | string | Document type. |
| `first_seen_at` | timestamp | When the document originally arrived at the hub. |
| `attempts_made` | int | How many delivery attempts had been made when it was dead-lettered. |
| `last_error_code` | string | The error on the final attempt. |
| `requeued` | bool | Whether an engineer has since manually requeued it. |

## `tenant_config.csv`

| Column | Type | Meaning |
| --- | --- | --- |
| `tenant_id` | string | Tenant identifier. |
| `tenant_name` | string | Customer name. |
| `erp_product_line` | string | Which ERP product the tenant runs: `Keelrow ERP`, `Foodline`, `Metalcore`, `Axiom Distribution`, or `Pinnacle WMS`. |
| `erp_api_version` | string | The API version of that tenant's ERP instance: `v2`, `v3`, or `v4`. Set when the tenant was onboarded and updated on upgrade. |
| `region` | string | Hosting region. |
| `timezone` | string | The tenant's business timezone. |
| `sla_minutes` | int | Contracted maximum delay between a partner sending a document and it landing in the ERP. |
| `go_live_date` | date | When the tenant was onboarded onto the hub. |

## `partner_catalog.csv`

| Column | Type | Meaning |
| --- | --- | --- |
| `partner_id` | string | Partner identifier. |
| `partner_name` | string | Partner system name. |
| `protocol` | string | How the partner delivers to the hub: `REST`, `SFTP`, or `AS2`. |
| `payload_version` | string | The payload version this partner is registered as sending. Maintained by hand when a partner is onboarded. |
| `sends_idempotency_key` | bool | Whether this partner is known to supply an `Idempotency-Key` header. |

---

All tenant names, partner names, identifiers, contact details and amounts in these files are
synthetic. Any resemblance to a real company or person is coincidental.
