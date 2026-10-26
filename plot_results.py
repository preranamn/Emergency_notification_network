#!/usr/bin/env python3
"""
plot_results.py - graphs and a results table from results/results.csv (Member D)

    python3 plot_results.py                  # no sudo needed
    sudo apt install -y python3-matplotlib   # if matplotlib is missing

Writes into results/:
    latency_by_load.png      median and p95 latency, per load, per controller mode (loss 0)
    delivery_by_load.png     delivery rate and retransmitted alerts per load (loss 0)
    loss_scenarios.png       delivery and retransmissions vs client-link loss (no background load)
    results_table.md         every run as a markdown table (paste into the report)

If a configuration was run more than once, the most recent row is used.
"""

import csv
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = "results"
MODE_LABELS = {"baseline": "Static baseline", "emergency": "Priority flows only",
               "queue": "Priority flows + queue"}
MODE_ORDER = ["baseline", "emergency", "queue"]
LOAD_ORDER = ["none", "moderate", "heavy"]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def load_rows():
    path = os.path.join(OUT, "results.csv")
    if not os.path.exists(path):
        sys.exit("results/results.csv not found. Run run_experiments.py first.")
    latest = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            latest[(row["mode"], row["load"], num(row["loss"]))] = row
    return latest


def grouped_bars(ax, categories, series, ylabel, title, log=False):
    """series: {label: [value per category]}"""
    n = len(series)
    width = 0.8 / max(n, 1)
    for i, (label, values) in enumerate(series.items()):
        xs = [c + (i - (n - 1) / 2) * width for c in range(len(categories))]
        bars = ax.bar(xs, values, width, label=label)
        for b, v in zip(bars, values):
            if v == v:  # not NaN
                ax.annotate(f"{v:.0f}" if v >= 10 else f"{v:.1f}", (b.get_x() + b.get_width() / 2, v),
                            ha="center", va="bottom", fontsize=7)
    ax.set_xticks(range(len(categories)))
    ax.set_xticklabels(categories)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if log:
        ax.set_yscale("log")
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.3)


def series_for(latest, modes, cats, key_of, field):
    return {MODE_LABELS[m]: [num(latest[key_of(m, c)][field]) if key_of(m, c) in latest else float("nan")
                             for c in cats] for m in modes}


def main():
    latest = load_rows()
    modes = [m for m in MODE_ORDER if any(k[0] == m for k in latest)]

    # ---- by load, no link loss
    loads = [l for l in LOAD_ORDER if any(k[1] == l and k[2] == 0 for k in latest)]
    if loads:
        key = lambda m, l: (m, l, 0.0)
        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        grouped_bars(axes[0], loads, series_for(latest, modes, loads, key, "median_ms"),
                     "median latency (ms)", "Median alert latency vs background load", log=True)
        grouped_bars(axes[1], loads, series_for(latest, modes, loads, key, "p95_ms"),
                     "95th percentile latency (ms)", "p95 alert latency vs background load", log=True)
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, "latency_by_load.png"), dpi=150)

        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        grouped_bars(axes[0], loads, series_for(latest, modes, loads, key, "delivery_pct"),
                     "delivered (%)", "Delivery rate after retransmission")
        axes[0].set_ylim(0, 110)
        grouped_bars(axes[1], loads, series_for(latest, modes, loads, key, "alerts_retransmitted"),
                     "alerts needing a retransmission", "Retransmitted alerts")
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, "delivery_by_load.png"), dpi=150)

    # ---- by loss, no background load
    losses = sorted({k[2] for k in latest if k[1] == "none"})
    if len(losses) > 1:
        cats = [f"{l:g}%" for l in losses]
        key = lambda m, c: (m, "none", float(c.rstrip("%")))
        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        grouped_bars(axes[0], cats, series_for(latest, modes, cats, key, "delivery_pct"),
                     "delivered (%)", "Delivery rate vs client-link loss")
        axes[0].set_ylim(0, 110)
        grouped_bars(axes[1], cats, series_for(latest, modes, cats, key, "alerts_retransmitted"),
                     "alerts needing a retransmission", "Retransmitted alerts vs client-link loss")
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, "loss_scenarios.png"), dpi=150)

    # ---- markdown table
    cols = [("mode", "Mode"), ("load", "Load"), ("loss", "Loss %"), ("delivery_pct", "Delivered %"),
            ("median_ms", "Median ms"), ("p95_ms", "p95 ms"), ("max_ms", "Max ms"),
            ("alerts_retransmitted", "Retx alerts"), ("first_attempt_fail_pct", "1st-attempt fail %"),
            ("unconfirmed", "Unconfirmed")]
    lines = ["| " + " | ".join(c[1] for c in cols) + " |", "|" + "---|" * len(cols)]
    for key in sorted(latest, key=lambda k: (k[2], LOAD_ORDER.index(k[1]), MODE_ORDER.index(k[0]))):
        row = latest[key]
        vals = [MODE_LABELS[row["mode"]] if c == "mode" else row[c] for c, _ in cols]
        lines.append("| " + " | ".join(str(v) for v in vals) + " |")
    with open(os.path.join(OUT, "results_table.md"), "w") as f:
        f.write("\n".join(lines) + "\n")

    print("Wrote graphs and results/results_table.md")


if __name__ == "__main__":
    main()
