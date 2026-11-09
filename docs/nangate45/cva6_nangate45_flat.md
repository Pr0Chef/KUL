# CVA6 flat synthesis on nangate45: stock vs parallel `abc_new`

Work log for running CVA6 flat on the nangate45 platform and comparing stock Yosys `abc_new` with the
parallel `abc_new` patch, 6 Nov 2026.

## Summary

CVA6 runs flat on nangate45 with no changes to the RTL or the ORFS scripts. Two new config files are
enough. Across four clean runs (stock, parallel, parallel, stock), the parallel `abc_new` produced a
byte-identical netlist and cut synthesis time by about 10 %. The whole-synthesis wall time fell from
3154 s to 2835 s on average, and `abc_new` itself from 1914 s to 1631 s (15 %, 1.17×). Peak memory stayed
at 1.35–1.38 GB.

No run in this set was disturbed: the guest kernel logged no VM pause or stall in any of the four. That
makes this the cleanest A/B so far. On asap7 flat, two of six runs were spoiled by a laptop sleep and a host
stall.

## Design and platform

| Item | Value |
| --- | --- |
| Design | CVA6, `cv32a65x` configuration (32-bit, no FPU, no MMU, 2 KiB I-cache, hpdcache D-cache), top `cva6`, clock `clk_i` |
| RTL | `orfs/flow/designs/src/cva6` in this repo (same sources as the asap7 runs) |
| Platform | nangate45 (`NangateOpenCellLibrary_typical.lib`, NAND2_X1 = 0.798 µm²) |
| SRAMs | 8 macros: 2 each of `fakeram45_64x256` (33,990 µm²), `_128x64` (5,480 µm²), `_64x28` (1,241 µm²), `_64x25` (1,241 µm²) |
| Base config | `designs/nangate45/cva6/config.mk` as shipped with ORFS |
| Constraints | `designs/nangate45/cva6/constraint.sdc`: 1.0 ns (1 GHz) clock on `clk_i`, input delay 0.46 ns, output delay 0.11 ns, max fanout 10. Yosys and ABC don't read the SDC in this flow (ABC gets only `abc.constr`: driver cell and load), so the clock doesn't change the synthesized netlist; it applies from `1_synth` in OpenROAD onward |
| Frontend | slang (`SYNTH_HDL_FRONTEND = slang`), canonicalize script removes `rvfi_probes_o` |
| Synthesis | flat (`SYNTH_HIERARCHICAL = 0`), `SWAP_ARITH_OPERATORS = 1` (so ORFS maps with `abc_new`) |
| ABC setup | driver `BUF_X1`, load 3.898 fF, dont-use `TAPCELL_X1 FILLCELL_X1 AOI211_X1 OAI211_X1` |

The CVA6 RTL instantiates the asap7 macro names (`fakeram7_*`) directly. The nangate45 design supplies
`designs/nangate45/cva6/macros.v`, which defines each `fakeram7_*` module as a wrapper around the matching
`fakeram45_*` macro, with the write mask tied high.

## Tools

| Side | Yosys | Path |
| --- | --- | --- |
| Stock | `0.69+274` (git `7513f8df8`) | `/usr/local/bin/yosys` |
| Parallel | `0.69+280` (git `7b7e811cb`, branch `abc-new-parallel-split`) | `yosys/build/yosys` |

OpenROAD `26Q4-271-g0907a8656e`. The parallel build is the stock base plus six commits: a shared ABC exit
status check in `abc9_exe`, `abc9_exe -prepare` / `-collect` modes, parallel mapping of independent modules,
stable box ids between prepare and collect, removal of leftover `$input_port` cells, and
largest-module-first scheduling. Its job count (`-j`) defaults to the number of cores (8).

Machine: VirtualBox VM, 8 vCPUs, 10.5 GB RAM, Ubuntu 24.04, kernel 7.0.0-42, on a laptop.

## Setup steps

1. **Checked what ORFS already had.** `designs/nangate45/cva6/config.mk` exists in the full ORFS clone and
   defaults to flat synthesis (`SYNTH_HIERARCHICAL` is unset; ORFS default 0). The repo's vendored
   `orfs/flow` slice shipped only `platforms/asap7` at the time, so the GUI and `ab_bench` used the full
   clone at `OpenROAD-flow-scripts/flow`, which has `platforms/nangate45`. The slice now also carries
   `platforms/nangate45` and `designs/nangate45/cva6/`, so the runs below also work from a fresh clone.
2. **Added two configs** in `designs/nangate45/cva6/`:
   `config_flat_stock.mk` and `config_flat_parallel.mk`. Each includes the stock `config.mk` and sets
   `FLOW_VARIANT` (`flat_stock` / `flat_parallel`) and `SYNTH_HIERARCHICAL = 0` explicitly. Nothing else
   changes, so the two sides differ only in which Yosys binary runs.
3. **Checked the configs resolve.** `make print-…` for both: `PLATFORM nangate45`, `SYNTH_HIERARCHICAL 0`,
   `SWAP_ARITH_OPERATORS 1`, results in `results/nangate45/cva6/flat_{stock,parallel}`, the four `fakeram45`
   libraries in `ADDITIONAL_LIBS`.
4. **Checked the GUI lists them.** The Flow Runner's design scan (`discover()` in `orfs_app.py`) reports
   nangate45 as available with cva6 configs `config.mk`, `config_flat_parallel.mk`, `config_flat_stock.mk`.
5. **Smoke test.** Ran only the canonicalize step (`make … do-yosys-canonicalize`) with stock Yosys:
   exit 0 in 90 s, peak 281 MB. All 8 macros resolved to `fakeram45` cells. One RTL warning
   (`asynchronous load value missing for variable csr_throttle_q`, `hwpf_stride.sv:158`), which the asap7
   runs also show.
6. **Made the A/B bench platform-aware.** `ab_bench/bench.sh` gained `PLATFORM` (default `asap7`): it now
   uses `designs/$PLATFORM`, `platforms/$PLATFORM` and `{results,logs,reports,objects}/$PLATFORM/cva6`, passes
   `DRV_PLATFORM` to the GUI driver, and writes to `ab_bench/bench_flat_nangate45/`.
7. **Started the bench** at 09:47 on 6 Nov: `PLATFORM=nangate45 ./bench.sh flat`.

## How each run is done

`bench.sh` runs stock, parallel, parallel, stock (A/B/B/A), so slow drift in machine speed hits both sides
equally. Before each run it deletes that side's results, logs, reports and objects, reads all design,
platform and script files plus both Yosys binaries once to warm the file cache, syncs, and waits 30 s.

Each run goes through the ORFS Flow Runner GUI, driven by `ab_bench/drive_ab.py`. It selects platform,
design, config, 8 cores, "Continue" and "synth"; sets Yosys in the Tools dialog; presses Run; samples
whole-machine CPU and the run's memory every 5 s; and saves screenshots of the Tools dialog, the Debug
tab, the ready state, the run in progress (every 10 min), the Logs tab, the finished run, the Reports tab
and the final Debug tab.

After each run the bench copies the logs, `synth_stat.txt` and the netlist sha1 (whole file and body
without the header line) into `ab_bench/bench_flat_nangate45/runN_side/`.

`ab_bench/compare.py` then builds the table and checks the VM's kernel journal for pauses or stalls during
each run (RCU stalls, soft lockups, clocksource watchdog gaps, clock jumps). A run that lost more than 60 s
is left out of the timing comparison. This came from the asap7 runs, where a laptop sleep and a host
stall each spoiled a run.

## Run log

All on 6 Nov 2026. "Stalls" is what the VM's kernel journal logged during the run.

| Run | Side | Started | Finished | Exit | Stalls |
| ---- | -------- | ----------- | -------- | ---- | ---------------------- |
| 1 | stock | 09:48 | 10:41 | 0 | none |
| 2 | parallel | 10:42 | 11:28 | 0 | none |
| 3 | parallel | 11:29 | 12:18 | 0 | none |
| 4 | stock | 12:19 | 13:10 | 0 | none |

## Results

| Run | Wall (s) | Yosys (s) | `abc_new` (s) | Rest of Yosys (s) | Peak mem (MB) |
| ---------------- | ------------------: | ---------------: | ------------------: | --------------: | ---------: |
| 1, stock | 3211 | 3160 | 1903 | 1257 | 1377 |
| 2, parallel | 2769 | 2736 | 1570 | 1166 | 1382 |
| 3, parallel | 2901 | 2865 | 1692 | 1173 | 1372 |
| 4, stock | 3097 | 3061 | 1925 | 1136 | 1350 |
| **Stock mean** | **3154** | **3110** | **1914** | **1196** | 1364 |
| **Parallel mean** | **2835** | **2801** | **1631** | **1170** | 1377 |
| **Change (s)** | **−319** | −309 | **−283** | −26 | +13 |
| **Change (%)** | **−10.1 %** | −9.9 % | **−14.8 %** | −2.2 % | +1.0 % |

"Wall" is from pressing Run in the GUI to the end of `make synth`. "Yosys" and `abc_new` come from the
`1_2_yosys.log` timing lines. "Rest of Yosys" is Yosys minus `abc_new`; it runs the same code in both builds,
so its ±60 s spread is the machine's run-to-run noise.

By pair: runs 1 and 2 differ by −443 s (−13.8 %), runs 3 and 4 by −197 s (−6.3 %). Both pairs favour the
parallel build, and the spread between them is about the size of the noise in the rest of Yosys.

**Quality of results: identical in all four runs.**

| Metric | All four runs |
| --- | --- |
| Netlist (`1_2_yosys.v` without its header line) | sha1 `6754270bcb6f…` |
| Cells | 86,905 (8 of them SRAM macros) |
| Area (`synth_stat.txt`) | 238,682.56 µm², of which 83,902.21 µm² SRAM macros and 43,757 µm² sequential (18.3 %) |
| Logic area without macros | 154,780 µm², about 194 kGE (NAND2_X1 = 0.798 µm²) |
| Flip-flops | 8,498 (6,635 `DFFR_X1`, 1,820 `DFF_X1`, 43 `DFFS_X1`) |
| Largest cell type | `MUX2_X1`, 21,799 cells |
| Modules mapped by `abc_new` | 65: `cva6` (the whole CPU) and 64 arithmetic operator wrappers |
| OpenROAD `1_synth` | 86,905 instances, 238,683 µm², 0 flow errors |

## Why the parallel build is faster here

Flat synthesis leaves one large module, `cva6`, plus 64 small `ALU_*` operator wrappers. `cva6` uses 16 of
them (the Han-Carlson adders); the other 48 are alternative adder architectures (Brent-Kung, Sklansky,
Kogge-Stone) that ORFS keeps for later swapping. `cva6` can't be mapped until the 16 it uses are done.

The stock `abc_new` handles one module at a time: prepare it on the main thread, run ABC, wait, read the
result back, then move to the next. The parallel build prepares every module whose submodules are done,
hands the ABC runs to 8 worker threads, and reads results back in the original module order, so the output
doesn't depend on which ABC finishes first. In the run 2 log, all 64 ALU wrappers are prepared and sent to ABC
first; `cva6` is prepared and launched as soon as its 16 adders are collected, and the results of the 48
alternatives are collected after it.

So the saving comes from overlapping ABC runs with each other and with main-thread work. The single ABC
run on `cva6` (the ABC phase spans 1,084 s in run 1 and 1,064 s in run 2, from the 5 s CPU samples) is not split: one module is one ABC
process. Yosys doesn't log per-module times, so the 283 s can't be split exactly between those overlaps.

## Comparison with asap7 flat

| | nangate45 flat | asap7 flat (4–5 Nov) |
| --- | --- | --- |
| Constraint | 1.0 ns (ORFS default) | 5.0 ns (relaxed `constraint_hier.sdc`) |
| Cells | 86,905 | 100,089 |
| Logic size | about 194 kGE | about 140–210 kGE |
| Stock wall (s) | 3211, 3097 | 2936, 3770 (2 of 3 runs usable) |
| Parallel wall (s) | 2769, 2901 | 2789, 3078 (2 of 3 runs usable) |
| Runs spoiled by VM stalls | 0 of 4 | 2 of 6 |
| `abc_new` gain | −15 % (mean of 2 vs 2) | −7 % (cleanest pair) |
| Netlists identical stock vs parallel | yes | yes |

The asap7 number rests on fewer clean runs and a noisier machine, so the two percentages shouldn't be read
as a difference between the platforms.

## Screenshots

Taken by the bench from the Flow Runner window during runs 1 (stock) and 2 (parallel). Files are in
`docs/nangate45/cva6_nangate45_flat_snaps/`; every screenshot of all four runs is in `ab_bench/bench_flat_nangate45/`.

| File | Shows |
| --- | --- |
| `01_stock_tools.png` | Tools dialog with stock Yosys selected |
| `02_stock_debug_before.png` | Debug tab before the run: nangate45, `config_flat_stock.mk`, `SYNTH_HIERARCHICAL 0`, Yosys 0.69+274 |
| `03_stock_running.png` | Synthesis running |
| `04_stock_done.png` | Finished after 53 m 27 s, design area 238,683 µm² |
| `05_stock_reports.png` | QoR checks: 86,905 instances, 0 flow errors |
| `06_stock_debug_after.png` | Debug tab after the run |
| `07_parallel_tools.png` | Tools dialog with `yosys/build/yosys` selected |
| `08_parallel_debug_before.png` | Debug tab: `config_flat_parallel.mk`, Yosys 0.69+280 |
| `09_parallel_running.png` | Running at 15 min, in `extract_fa` |
| `10_parallel_logs.png` | Logs tab with stage and run logs |
| `11_parallel_done.png` | Finished after 46 m 7 s |
| `12_parallel_reports.png` | QoR checks: same 86,905 instances and 238,683 µm² |
| `13_parallel_debug_after.png` | Debug tab after the run |

## Reproduce

```bash
cd ab_bench
PLATFORM=nangate45 ./bench.sh flat           # A/B/B/A, about 3.5 h; GUI screenshots saved
python3 compare.py bench_flat_nangate45      # table, stall check, bench_flat_nangate45/summary.json
```

To run one side by hand, in the Flow Runner pick nangate45 / cva6 / `config_flat_stock.mk` (or
`config_flat_parallel.mk`), set Yosys in Tools…, and run to `synth`. From a terminal:

```bash
cd orfs/flow                                 # or OpenROAD-flow-scripts/flow in a full ORFS clone
OPENROAD_EXE=$(command -v openroad) YOSYS_EXE=/usr/local/bin/yosys \
  make DESIGN_CONFIG=designs/nangate45/cva6/config_flat_stock.mk synth
```

## Files

| Path | What |
| --- | --- |
| `orfs/flow/designs/nangate45/cva6/config_flat_stock.mk` | new: stock side config |
| `orfs/flow/designs/nangate45/cva6/config_flat_parallel.mk` | new: parallel side config |
| `orfs/flow/designs/nangate45/cva6/` (rest) | ORFS's nangate45 CVA6 design, copied unchanged |
| `orfs/flow/platforms/nangate45/` | ORFS's nangate45 platform, copied unchanged |
| `ab_bench/bench.sh` | changed: `PLATFORM`, `SPECS`, `N0` options |
| `ab_bench/compare.py` | new: summary table and VM-stall check |
| `ab_bench/bench_flat_nangate45/` | per-run logs, stats, netlist hashes, CPU samples, 62 screenshots |
| `docs/nangate45/CVA6_nangate45_flat_synthesis.docx` | this report as a Word file, with the screenshots |
| `docs/nangate45/cva6_nangate45_flat_snaps/` | the 13 screenshots listed above |

## Next steps

- Floorplan through route on nangate45 haven't been run. The 1.0 ns clock is far beyond what nangate45
  reaches, so `repair_timing` would struggle in the same way it did on asap7 at 910 ps; a relaxed SDC is the
  first thing to add before running past synthesis.
- A hierarchical nangate45 A/B (`SYNTH_HIERARCHICAL = 1`) would show how the gain compares when `abc_new` has several large
  independent modules to map instead of one.
