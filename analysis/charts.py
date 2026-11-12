"""Draws the three evidence figures used in WRITEUP.md from the data and from the saved analysis outputs.
Run from repo root, after part_a.py and design_evidence.py:  python -I analysis/charts.py data"""
import re, sys
from pathlib import Path
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

D = Path(sys.argv[1] if len(sys.argv) > 1 else "data"); HERE = Path(__file__).parent; OUT = HERE.parent / "docs"
INK, YEL, MID, LIGHT = "#3a3a3a", "#f0e91a", "#8c8c8c", "#d9d9d9"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8.5, "axes.edgecolor": INK, "axes.labelcolor": INK, "text.color": INK,
                     "xtick.color": INK, "ytick.color": INK, "axes.spines.top": False, "axes.spines.right": False})

ev = pd.read_json(D / "inbound_events.jsonl", lines=True)
ev["t"] = pd.to_datetime(ev.received_at, utc=True)
at = pd.read_csv(D / "delivery_attempts.csv").drop_duplicates()
at["t"] = pd.to_datetime(at.attempted_at, utc=True)
at = at.merge(ev[["event_id", "tenant_id"]], on="event_id")
lost = pd.read_csv(HERE / "lost_documents.csv")
lost["grp"] = lost.cause.str.split().str[0].replace({"A1": "A", "A2": "A", "B1": "B", "B2": "B"})
lost = lost.merge(ev[["event_id", "t"]], on="event_id")
de = (HERE / "design_evidence_output.txt").read_text()

# ---------------- Figure 1: causes ranked + lost per day by cause
names = {"A": "Mapping out of date", "B": "Tax code, retried 25x", "C": "Speed limit = 'permanent'", "F": "Unexplained refusals", "D": "Order before customer", "E": "Total sent as text"}
col = {"A": INK, "B": YEL, "C": MID, "F": "#6f6f6f", "D": LIGHT, "E": "#b8b24a"}
cnt = lost.grp.value_counts()
fig, (a, b) = plt.subplots(1, 2, figsize=(7.4, 2.45), gridspec_kw={"width_ratios": [1, 1.25]})
order = list(cnt.index)[::-1]
a.barh([names[g] for g in order], [cnt[g] for g in order], color=[col[g] for g in order], edgecolor=INK, linewidth=.6)
for i, g in enumerate(order): a.text(cnt[g] + 30, i, f"{cnt[g]:,} ({cnt[g]/len(lost):.0%})", va="center", fontsize=8)
a.set_xlim(0, cnt.max() * 1.45); a.set_xlabel("lost documents"); a.set_title(f"{len(lost):,} lost documents by cause", loc="left", fontweight="bold", fontsize=9)
a.tick_params(axis="y", length=0)
daily = lost.assign(d=lost.t.dt.floor("D")).groupby(["d", "grp"]).size().unstack(fill_value=0)[list(cnt.index)[::1]]
bottom = None
for g in ["A", "B", "C", "F", "D", "E"]:
    b.bar(daily.index, daily[g], bottom=bottom, color=col[g], edgecolor=INK, linewidth=.3, width=.85, label=names[g])
    bottom = daily[g] if bottom is None else bottom + daily[g]
b.axvline(pd.Timestamp("2026-02-19", tz="UTC"), color=INK, ls="--", lw=1)
b.set_ylim(0, bottom.max() * 1.28); b.text(pd.Timestamp("2026-02-18 12:00", tz="UTC"), bottom.max() * 1.17, "19 Feb: P-07 changes layout,\nunannounced", ha="right", va="center", fontsize=7.5)
b.set_title("Lost documents per day (by arrival)", loc="left", fontweight="bold", fontsize=9); b.set_ylabel("documents")
b.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b")); b.xaxis.set_major_locator(matplotlib.dates.DayLocator(interval=7))
plt.tight_layout(); plt.savefig(OUT / "fig_causes.png", dpi=220); plt.close()

# ---------------- Figure 2: latency vs T-1007 share, by hour
h = at.groupby(at.t.dt.floor("h")).agg(lat=("latency_ms", "mean"), n=("event_id", "size"))
h["share"] = at[at.tenant_id == "T-1007"].groupby(at.t.dt.floor("h")).size().reindex(h.index).fillna(0) / h.n * 100
fig, (a, b) = plt.subplots(2, 1, figsize=(7.4, 2.9), sharex=True, gridspec_kw={"height_ratios": [1, 1], "hspace": .22})
a.plot(h.index, h.lat, color=INK, lw=.8); a.axhline(1300, color=INK, ls="--", lw=1)
a.text(h.index[5], 1380, "alarm line 1,300 ms", fontsize=7.5)
a.axvspan(pd.Timestamp("2026-02-12", tz="UTC"), pd.Timestamp("2026-02-15", tz="UTC"), color=YEL, alpha=.55, lw=0)
a.set_ylim(0, h.lat.max() * 1.25); a.text(pd.Timestamp("2026-02-13 12:00", tz="UTC"), h.lat.max() * 1.12, "12-14 Feb: 98% of tenants slow", ha="center", fontsize=7.5)
a.set_ylabel("mean latency (ms)"); a.set_title("Hourly mean ERP latency (top) against T-1007's share of attempts (bottom)", loc="left", fontweight="bold", fontsize=9)
b.fill_between(h.index, h.share, color=MID, lw=0); b.axhline(50, color=INK, ls=":", lw=.8)
b.set_ylabel("T-1007 share (%)"); b.set_ylim(0, 100)
b.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d %b")); b.xaxis.set_major_locator(matplotlib.dates.DayLocator(interval=3))
plt.savefig(OUT / "fig_noisy.png", dpi=220, bbox_inches="tight"); plt.close()

# ---------------- Figure 3: how firm are the chosen numbers (read from design_evidence_output.txt)
lims = [(float(a_), int(b_)) for a_, b_ in re.findall(r"limit\s+([\d.]+) h: released\s+(\d+) of", de)]
tot = int(re.search(r"released\s+\d+ of (\d+)", de).group(1))
nrow = [(int(n), int(f)) for n, f in re.findall(r"N=(\d+): fires for \d+ tenants; false alarms: (\d+)", de)]
fig, (a, b) = plt.subplots(1, 2, figsize=(7.4, 2.5))
x, y = zip(*lims); a.step(x, y, where="post", color=INK, lw=1.5); a.scatter(x, y, color=YEL, edgecolor=INK, zorder=3, s=22)
a.axvline(12, color=INK, ls="--", lw=1); a.text(11.6, tot * .45, "chosen: 12 h", ha="right", fontsize=8)
a.axhline(tot, color=MID, lw=.8); a.text(.5, tot * 1.03, f"all {tot} released from 4.5 h", fontsize=7.5)
a.set_xscale("log"); a.set_xticks([.5, 1, 2, 4, 12, 24]); a.set_xticklabels(["0.5\n(old hub)", "1", "2", "4", "12", "24"])
a.set_xlabel("wait limit (hours)"); a.set_ylabel("orders released"); a.set_ylim(0, tot * 1.15)
a.set_title("D6: how long an order waits for its customer", loc="left", fontweight="bold", fontsize=9)
xs, fs = zip(*[r for r in nrow if r[0] >= 3]); b.bar(xs, fs, color=[YEL if f == 0 else LIGHT for f in fs], edgecolor=INK, linewidth=.6)
for xx, ff in zip(xs, fs): b.text(xx, ff + .6, str(ff), ha="center", fontsize=8)
b.axvline(5.5, color=INK, ls="--", lw=1); b.text(5.55, max(fs) * .9, "chosen: 6", fontsize=8)
b.set_xlabel("N failed documents in a row"); b.set_ylabel("false alarms (tenants)")
b.set_title("D9: false alarms by threshold", loc="left", fontweight="bold", fontsize=9)
plt.tight_layout(); plt.savefig(OUT / "fig_thresholds.png", dpi=220); plt.close()
# ---------------- Figure 4: the journey of the 24,000 deliveries (numbers read from part_a_output.txt)
pa = (HERE / "part_a_output.txt").read_text()
o = re.search(r"landed cleanly (\d+), landed with refused lines \(207\) (\d+), lost (\d+), possibly in flight (\d+)", pa)
clean, part, lostn, fl = map(int, o.groups()); ndocs = clean + part + lostn + fl; reps = len(ev) - ndocs
fig, ax = plt.subplots(figsize=(7.4, 1.55)); ax.axis("off"); ax.set_xlim(0, len(ev)); ax.set_ylim(0, 2.3)
def seg(y, parts):
    x = 0
    for n, c, lab, tc in parts:
        ax.barh(y, n, left=x, height=.62, color=c, edgecolor=INK, linewidth=.7)
        if n > 900: ax.text(x + n / 2, y, lab, ha="center", va="center", fontsize=8, color=tc)
        x += n
seg(1.6, [(ndocs, "#ececec", f"{ndocs:,} different documents", INK), (reps, YEL, "", INK)])
ax.text(len(ev) + 150, 1.6, f"{reps} re-sends", va="center", fontsize=7.5, clip_on=False)
seg(.55, [(clean, INK, f"{clean:,} landed cleanly ({clean/ndocs:.0%})", "white"), (part + fl, MID, "", INK), (lostn, YEL, f"{lostn:,} lost ({lostn/ndocs:.0%})", INK)])
ax.text(clean + 60, 1.05, f"{part} landed with refused lines, {fl} in flight", fontsize=7, va="center")
ax.text(0, 2.12, f"{len(ev):,} deliveries in 30 days", fontsize=8.5, fontweight="bold", va="center")
ax.text(0, -.02, "Documents counted once: re-sends of the same document removed (identity in section 4). Lost = never accepted by the customer's system.", fontsize=7, va="top", color=INK)
plt.savefig(OUT / "fig_journey.png", dpi=220, bbox_inches="tight"); plt.close()

# ---------------- Figure 5: how long until we know (numbers read from design_evidence_output.txt)
ev_out = de
rows = []
for t, hrs in re.findall(r"(T-\d{4}): first document .*?-> ([\d.]+) h after first document", ev_out): rows.append((f"Tenant {t} failing (6 failed in a row)", float(hrs)))
m = re.search(r"3 of 10|6 of 10: P-07 alarm .*?\((\d+) min after", ev_out); drift = float(re.search(r"6 of 10: P-07 alarm .*?\((\d+) min after", ev_out).group(1)) / 60
rows.append(("Partner changes layout (P-07, 6 of last 10)", drift)); rows.append(("Everyone slows down (12 Feb, hourly latency alarm)", 1.0))
rows.sort(key=lambda r: r[1])
fig, ax = plt.subplots(figsize=(7.4, 1.9))
ax.barh([r[0] for r in rows][::-1], [r[1] for r in rows][::-1], color=[YEL if r[1] < 2 else INK for r in rows][::-1], edgecolor=INK, linewidth=.6)
for i, r in enumerate(rows[::-1]): ax.text(r[1] + .6, i, (f"{r[1]:.1f} h" if r[1] >= 2 else (f"{r[1] * 60:.0f} min" if r[0].startswith("Partner") else "within 1 h")), va="center", fontsize=8)
ax.set_xlim(0, max(r[1] for r in rows) * 1.18); ax.set_xlabel("hours until the alarm fires (replayed on the real 30 days)"); ax.tick_params(axis="y", length=0)
plt.tight_layout(); plt.savefig(OUT / "fig_detect.png", dpi=220); plt.close()
print(rows, drift)
print("figures written:", sorted(p.name for p in OUT.glob("fig_*.png")))
print("lims", lims, "nrow", nrow, "latency max normal/min incident", h.lat[(h.index < "2026-02-12") | (h.index >= "2026-02-15")].max().round(), h.lat[(h.index >= "2026-02-12") & (h.index < "2026-02-15")].min().round())
