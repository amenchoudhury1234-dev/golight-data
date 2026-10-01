"""
Weekly tune-up analysis for the Memecoin Radar (run by weekly_tune.ps1, or by hand).

Reads pings_export.csv (python radar.py --export) and prints, for the last N days: how each ping type, filter
and feature bucket actually did on paper (2x rate and average GBP result per GBP50 for each exit style), plus
which filters blocked coins that later did 2x. Read-only: it changes nothing.

    python tune_analysis.py          # last 7 days
    python tune_analysis.py 14       # last 14 days
"""
import csv
import os
import statistics as st
import sys
import time
from datetime import datetime

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 7
HERE = os.path.dirname(os.path.abspath(__file__))


def num(r, k, d=None):
    try:
        return float(r[k])
    except (ValueError, KeyError, TypeError):
        return d


def load():
    path = os.path.join(HERE, "pings_export.csv")
    rows = list(csv.DictReader(open(path, encoding="utf-8")))
    cutoff = time.time() - DAYS * 86400
    out = []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["time"]).timestamp()
        except Exception:
            continue
        if t >= cutoff and num(r, "pnl_rules") is not None:
            out.append(r)
    return out


def line(name, g):
    if len(g) < 5:
        return None
    hit2 = sum(num(r, "peak_x", 0) >= 2 for r in g) / len(g)
    parts = [f"{name:38s} n={len(g):4d} | 2x {hit2:4.0%}"]
    for k in ("rules", "lotto", "moonbag"):
        vals = [num(r, f"pnl_{k}") for r in g if num(r, f"pnl_{k}") is not None]
        if vals:
            parts.append(f"{k} {st.mean(vals):+6.1f}")
    return " | ".join(parts)


def report(title, groups):
    lines = [l for l in (line(n, g) for n, g in groups) if l]
    if lines:
        print(f"\n== {title} ==")
        print("\n".join(lines))


def bucket(rows, key, edges, label):
    return [(f"{label} {lo:g}..{hi:g}", [r for r in rows if num(r, key) is not None and lo <= num(r, key) < hi])
            for lo, hi in zip(edges, edges[1:])]


def main():
    rows = load()
    pinged = [r for r in rows if r.get("pinged") == "True"]
    shadow = [r for r in rows if r.get("pinged") != "True"]
    print(f"Radar tune-up data: last {DAYS} days | {len(rows)} coins tracked | {len(pinged)} phone pings | "
          f"{len(shadow)} silent/filtered")
    if not rows:
        print("No data - nothing to tune.")
        return
    report("Phone pings by type (avg GBP per GBP50)", [(k, [r for r in pinged if r["kind"] == k])
                                                       for k in sorted({r["kind"] for r in pinged})])
    report("Silent / filtered by type", [(k, [r for r in shadow if r["kind"] == k])
                                         for k in sorted({r["kind"] for r in shadow})])
    reasons = {}
    for r in shadow:
        for reason in (r.get("reasons") or "?").split("|"):
            reasons.setdefault(reason.split(":")[0].strip() or "?", []).append(r)
    report("Filters: what the blocked coins did afterwards", sorted(reasons.items(), key=lambda kv: -len(kv[1]))[:15])
    report("AI verdicts", [("AI PING", [r for r in rows if r.get("f_ai") == "PING"]),
                           ("AI SKIP", [r for r in rows if r.get("f_ai") == "SKIP"])])
    report("1-hour change at alert %", bucket(rows, "f_h1", [-100, 0, 50, 100, 250, 1000, 1e6], "h1"))
    report("5-minute change at alert %", bucket(rows, "f_m5", [-100, 0, 10, 25, 50, 100, 1e6], "m5"))
    report("Market cap at alert $", bucket(rows, "mcap", [0, 30e3, 60e3, 150e3, 500e3, 2e6, 1e12], "mcap"))
    report("Liquidity / market cap", bucket(rows, "f_liq_mc", [0, 0.05, 0.1, 0.2, 0.4, 100], "liq/mc"))
    report("Pool age hours", bucket(rows, "f_age_h", [0, 0.1, 0.33, 1, 3, 12, 1e6], "age"))
    report("Buys/sells 1h", bucket(rows, "f_bs", [0, 1, 1.3, 1.7, 2.5, 1e6], "b/s"))
    report("Bonding curve", [("on curve", [r for r in rows if r.get("f_curve") == "True"]),
                             ("migrated", [r for r in rows if r.get("f_curve") == "False"])])
    big = sorted(rows, key=lambda r: -num(r, "peak_x", 0))[:10]
    print("\n== Biggest movers tracked (peak x) ==")
    for r in big:
        print(f"{r['time'][:16]} {r['kind'][:16]:16s} ${r['symbol'][:12]:12s} peak {num(r, 'peak_x', 0):6.1f}x | "
              f"pinged {r.get('pinged')} | {(r.get('reasons') or '')[:70]}")


if __name__ == "__main__":
    main()
