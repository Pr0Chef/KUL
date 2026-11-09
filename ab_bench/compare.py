"""Summarize an A/B bench folder: one row per run, plus a verdict per side from the clean runs only.

Usage: python3 compare.py <bench dir>      (e.g. ab_bench/bench_flat)

A run is "tainted" when the guest kernel logged VM pauses or host stalls adding up to more than
LOST_LIMIT seconds while it ran (RCU stalls, soft lockups, clocksource watchdog gaps, clock jumps);
its timing is then left out.
"""
import datetime as dt
import glob
import json
import os
import re
import statistics
import subprocess
import sys

BENCH = sys.argv[1]
STALL = re.compile(r"rcu: +\(detected by \d+, t=|clocksource: Long readout|Clock change detected|soft lockup")
LOST_LIMIT = 60
HZ = 1000  # CONFIG_HZ of this kernel, for RCU stall jiffies


def lost_seconds(line):
    """Rough length of the stall a journal line reports; inf when it cannot tell (a clock jump)."""
    for pat, scale in ((r"cs_nsec: (\d+)", 1e-9), (r"stuck for (\d+)s", 1), (r"t=(\d+) jiffies", 1 / HZ)):
        m = re.search(pat, line)
        if m:
            return int(m.group(1)) * scale
    return float("inf")


def stalls(start, end):
    fmt = "%Y-%m-%d %H:%M:%S"
    out = subprocess.run(["journalctl", "-q", "--no-pager", "-o", "short-iso", "--since", start.strftime(fmt),
                          "--until", end.strftime(fmt)], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if STALL.search(line)]


def grab(pattern, text, cast=float):
    m = re.search(pattern, text)
    return cast(m.group(1)) if m else None


rows = []
for js in sorted(glob.glob(f"{BENCH}/run*_run.json")):
    d = json.load(open(js))
    name = d["name"]
    run = os.path.join(BENCH, name)
    ylog = open(f"{run}/1_2_yosys.log", errors="replace").read()
    stat = open(f"{run}/synth_stat.txt", errors="replace").read()
    start = dt.datetime.strptime(re.search(r"_(\d{8}_\d{6})\.log$", d["log"]).group(1), "%Y%m%d_%H%M%S")
    end = start + dt.timedelta(seconds=d["wall_s"])
    hit = stalls(start, end)
    lost = sum(lost_seconds(line) for line in hit)
    rows.append({
        "run": name, "side": name.split("_", 1)[1], "start": start.strftime("%m-%d %H:%M"), "rc": d["rc"],
        "wall_s": d["wall_s"], "yosys_s": grab(r"End of script\..*?time: ([\d.]+)s", ylog),
        "abc_new_s": grab(r"Took (\d+) seconds: abc_new", ylog),
        "peak_mb": grab(r"MEM: ([\d.]+) MB peak", ylog),
        "area": grab(r"Chip area for top module '\\cva6': ([\d.]+)", stat),
        "cells": int(re.findall(r"^\s+(\d+)\s+\S+\s+\d+\s+\S+\s+cells$", stat, re.M)[-1]),
        "netlist": open(f"{run}/netlist_body.sha1").read()[:12],
        "lost_s": round(lost) if lost < float("inf") else "clock jump", "tainted": lost > LOST_LIMIT,
        "why": max(hit, key=lost_seconds).split(" ", 2)[-1][:90] if hit else "",
    })

cols = ["run", "start", "rc", "wall_s", "yosys_s", "abc_new_s", "peak_mb", "area", "cells", "netlist", "lost_s", "tainted"]
print(" | ".join(cols))
for r in rows:
    print(" | ".join(str(r[c]) for c in cols) + (f"   ({r['why']})" if r["tainted"] else ""))

print()
clean = [r for r in rows if not r["tainted"] and r["rc"] == 0]
for side in ("stock", "parallel"):
    s = [r for r in clean if r["side"] == side]
    if s:
        print(f"{side:9s} clean runs {len(s)}: wall mean {statistics.mean(r['wall_s'] for r in s):7.0f}s"
              f"  abc_new mean {statistics.mean(r['abc_new_s'] for r in s):6.0f}s"
              f"  (wall {', '.join(str(round(r['wall_s'])) for r in s)})")
print("netlists identical across all runs:", len({r["netlist"] for r in rows}) == 1)
json.dump(rows, open(f"{BENCH}/summary.json", "w"), indent=1)
