#!/usr/bin/env python3
"""
FXStreet probe - benchmark analyzer.

Reads the probe's existing output files and evaluates the Phase-0 acceptance
gates. No browser, no network. Run this ANY TIME after the probe has been up:

    python analyze.py

What it reports:
  A. Release records (logs/releases.jsonl + output/benchmark.jsonl)
     - true releases only (actual is not null; pre-fix rows with actual=null
       from older runs are excluded automatically)
     - latency stats: median / p90 / p95 / p99 / max
     - correlation strength, impact mix, duplicates
  B. Acceptance gates (PASS / FAIL per gate)
  C. Probe health from the last portion of logs/probe.jsonl
     - delivery lag (is the pump fix live?), recovery cycles,
       challenge events, last health state

Paste the whole printed report back.
"""
import json
import math
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

RELEASES = Path("logs/releases.jsonl")
BENCH = Path("output/benchmark.jsonl")
PROBE = Path("logs/probe.jsonl")

GATES = [
    ("detector median (network->python)", "network_to_python_ms", 500, "median"),
    ("detector p95", "network_to_python_ms", 1500, "p95"),
    ("detector p99", "network_to_python_ms", 3000, "p99"),
    ("bridge+process median (dom->python)", "dom_to_python_ms", 100, "median"),
    ("delivery lag median", "delivery_lag_ms", 100, "median"),
]


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = max(0, math.ceil(p / 100 * len(sorted_vals)) - 1)
    return sorted_vals[k]


def stats(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": vals[0],
        "median": percentile(vals, 50),
        "p90": percentile(vals, 90),
        "p95": percentile(vals, 95),
        "p99": percentile(vals, 99),
        "max": vals[-1],
    }


def load_jsonl(path):
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def main():
    print("=" * 64)
    print("FXSTREET PROBE - BENCHMARK ANALYSIS")
    print("=" * 64)

    # ---------- collect release records ----------
    recs = load_jsonl(RELEASES) + load_jsonl(BENCH)
    seen = set()
    unique = []
    for r in recs:
        k = (r.get("event_key"), r.get("actual"), r.get("t_python_mono_ms"))
        if k in seen:
            continue
        seen.add(k)
        unique.append(r)
    releases = [r for r in unique if r.get("actual") not in (None, "", "-")]

    print(f"\n[A] RELEASE RECORDS")
    print(f"    raw records: {len(recs)}   unique: {len(unique)}   "
          f"TRUE releases (actual non-null): {len(releases)}")
    if not releases:
        print("    -> No true releases captured yet. The pipeline has not seen a")
        print("       real economic release. Leave probe.py running through live")
        print("       market hours and re-run this analyzer.")
        print("       (If rows in releases.jsonl all show \"actual\": null, those are")
        print("        pre-fix hydration rows from an older build - ignored here.)")
    else:
        names = Counter(r.get("event_name", "?") for r in releases)
        impacts = Counter(str(r.get("impact")) for r in releases)
        corrs = Counter(str(r.get("correlation")) for r in releases)
        currs = Counter(str(r.get("currency")) for r in releases)
        print(f"    events: {dict(names)}")
        print(f"    impact mix: {dict(impacts)}")
        print(f"    correlation: {dict(corrs)}")
        print(f"    currencies: {dict(currs)}")
        t0 = min(r.get("t_python_mono_ms") for r in releases if r.get("t_python_mono_ms"))
        t1 = max(r.get("t_python_mono_ms") for r in releases if r.get("t_python_mono_ms"))
        if t0 and t1:
            print(f"    span: {(t1 - t0) / 60000:.1f} minutes of log time")

        print(f"\n[B] LATENCY STATS (true releases)")
        fields = ["network_to_python_ms", "dom_to_python_ms", "network_to_dom_ms",
                  "delivery_lag_ms"]
        col = {}
        for f in fields:
            s = stats([r.get(f) for r in releases])
            col[f] = s
            if s:
                print(f"    {f:26s} n={s['n']:3d}  median={s['median']:8.0f}  "
                      f"p90={s['p90']:8.0f}  p95={s['p95']:8.0f}  p99={s['p99']:8.0f}  "
                      f"max={s['max']:8.0f} ms")
            else:
                print(f"    {f:26s} (no data)")

        print(f"\n[C] ACCEPTANCE GATES")
        all_pass = True
        for label, f, limit, mode in GATES:
            s = col.get(f)
            if not s:
                print(f"    {label:40s} SKIP (no data)")
                continue
            v = s.get(mode)
            ok = v is not None and v <= limit
            all_pass = all_pass and ok
            print(f"    {label:40s} {v:8.0f} ms  (<= {limit})  {'PASS' if ok else 'FAIL'}")
        dupes = len(recs) - len(unique)
        print(f"    duplicate records suppressed: {dupes} "
              f"{'PASS' if dupes == 0 else 'CHECK'}")
        print(f"\n    OVERALL: {'ALL GATES PASS - Phase 0 complete' if all_pass else 'NOT YET - keep collecting'}")

    # ---------- probe health ----------
    print(f"\n[D] PROBE HEALTH (tail of {PROBE})")
    if not PROBE.exists():
        print("    no probe.jsonl found - probe.py has not been run in this directory")
        return
    tail = []
    with open(PROBE, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            tail.append(line)
            if len(tail) > 20000:
                tail.pop(0)
    events = []
    for line in tail:
        try:
            events.append(json.loads(line))
        except Exception:
            pass
    health = None
    last_hb = None
    lags = []
    recoveries = 0
    challenges = 0
    for e in events:
        t = e.get("type")
        if t == "health":
            health = e
        elif t == "heartbeat":
            last_hb = e
        elif t == "delivery_lag":
            lags.append(e.get("lag_ms", 0))
        elif t == "recovery_start":
            recoveries += 1
        elif t == "challenge_detected":
            challenges += 1
    print(f"    last health state: {health.get('to') if health else '?'} "
          f"(reason: {health.get('reason', '') if health else ''})")
    if last_hb:
        print(f"    last heartbeat: rows_attached={last_hb.get('rows_attached')} "
              f"vis={last_hb.get('visibilityState')} drift={last_hb.get('interval_drift_pct')}%")
    if lags:
        print(f"    delivery lag events in tail: {len(lags)}  max={max(lags)} ms")
        print(f"      -> max > 2000 ms means the OLD probe.py (pre pump-fix) wrote this")
        print(f"         log, or the page was busy. Latest build should show none.")
    else:
        print(f"    delivery lag events in tail: none (good - pump fix is live)")
    print(f"    recovery cycles in tail: {recoveries}   challenge detections: {challenges}")
    print("\nPaste this whole report back for the verdict.")


if __name__ == "__main__":
    main()
