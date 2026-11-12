# Integration Hub: diagnosis, design and thin slice

Start with **[WRITEUP.md](WRITEUP.md)** (also as `WRITEUP.pdf`). It has the diagnosis, the design and the decision log.

## Run the Part C slice (about 1 second, no installs)

Needs Python 3.9 or newer. Standard library only.

```
python hub_slice.py
```

This feeds a **real pair from the log** (the same invoice, sent twice) into the new hub and shows that it lands once: 3 deliveries in, 1 ERP call, 1 document. It then shows a partner changing its layout (held, 0 ERP calls) and one real document for each outcome the hub can reach.

Other commands (all use the real data in `data/`):

| Command | What it shows |
|---|---|
| `python hub_slice.py --replay` | All 24,000 real deliveries through the new rules. Checks that every delivery is accounted for: documents + duplicates = deliveries |
| `python hub_slice.py --where Harrowgate CUST-9277` | "Where is my document?": customer name fragment + business number |
| `python hub_slice.py EV-605927` | The full trace of the document a given delivery belongs to |

The ERP is a **stub that replays what the real ERP answered**. Where the new hub removes the cause of a failure (text totals, version mapping, waiting for a customer), the stub assumes acceptance. Every trace line says so.

## Re-run the Part A analysis

Needs `pip install pandas matplotlib` (matplotlib only for the charts). Run from this folder:

```
python -I analysis/data_checks.py data                         # data hygiene checks
python -I analysis/part_a.py data 2>/dev/null > analysis/part_a_output.txt
python -I analysis/design_evidence.py data 2>/dev/null > analysis/design_evidence_output.txt
python -I analysis/charts.py data                             # redraws the three figures in docs/ (needs matplotlib)
```

`design_evidence.py` reads `analysis/lost_documents.csv`, which `part_a.py` writes, so run them in that order. The saved outputs are already in the repo.

## Layout

```
WRITEUP.md, WRITEUP.pdf      the write-up
hub_slice.py                 Part C: one script
analysis/part_a.py           every Part A figure, with its definition (output beside it)
analysis/design_evidence.py  tests each design rule against the logs (output beside it)
analysis/data_checks.py      duplicate, orphan, timestamp and label checks
analysis/charts.py           draws the evidence figures used in the write-up
docs/                        diagrams (.mmd source + .png) and saved slice outputs
data/                        the 5 provided files, unchanged (synthetic)
```
