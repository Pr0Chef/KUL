# RTL to GDS with ORFS Flow Runner: every stage, step and input

This guide follows a design from RTL files to a GDS layout. For each stage of the flow it says what the stage
does, which inputs it reads, **which page of the app sets each input**, what it writes, and what to look at
when it goes wrong. Everything below is done in the app: no file needs to be edited by hand.

The flow is [OpenROAD-flow-scripts](https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts) (ORFS),
vendored in `orfs/flow`. Synthesis runs in Yosys; every later stage runs in OpenROAD.

Contents: [The stages at a glance](#the-stages-at-a-glance) · [Start a design](#0-start-a-design) ·
[Synthesis](#1-synthesis) · [Floorplan](#2-floorplan) · [Placement](#3-placement) · [Clock tree](#4-clock-tree-cts) ·
[Routing](#5-routing) · [Finishing](#6-finishing) · [Running and re-running](#running-and-re-running) ·
[Where inputs are saved](#where-the-inputs-are-saved) · [Walkthrough: gcd](#walkthrough-gcd-from-rtl-to-gds) ·
[Testing the app](#testing-the-app)

## The stages at a glance

| Stage | Steps (log and result names) | What it does | Pages that set its inputs |
| --- | --- | --- | --- |
| synth | `1_1_yosys_canonicalize`, `1_2_yosys` → `1_synth` | RTL to a netlist of the platform's cells | Synthesis, Clocks, Constraints, Timing |
| floorplan | `2_1_floorplan`, `2_2_floorplan_macro`, `2_3_floorplan_tapcell`, `2_4_floorplan_pdn` | Die and core, macros, tap cells, power grid | Floorplan, Power grid, Routing (layers) |
| place | `3_1_place_gp_skip_io`, `3_2_place_iop`, `3_3_place_gp`, `3_4_place_resized`, `3_5_place_dp` | Pins, then cells, then timing repair, then legal sites | IO pins, Placement |
| cts | `4_1_cts` | Clock trees, then setup and hold repair | Clock tree, Timing |
| route | `5_1_grt`, `5_2_route`, `5_3_fillcell` | Global route, detailed route, filler cells | Routing |
| finish | `6_1_fill`, `6_1_merge`, `6_report` | Metal fill, GDS merge, final reports | (none) |

Each step writes `logs/<platform>/<design>/<variant>/<step>.log` and a result in
`results/<platform>/<design>/<variant>/`. The **Logs** tab lists the steps of the selected config with their run
time and peak memory; the **Reports** tab shows the reports and the QoR checks.

## 0. Start a design

**New design…** (header bar) creates a design from RTL files:

1. Give it a name and pick the platform (nangate45 or asap7 here).
2. **Add files…**: the Verilog or SystemVerilog sources. The top module is found as the module no other module
   instantiates; pick another one from the list if needed.
3. The clock port is found from the RTL (`always @(posedge clk)`, a port wired to a clock pin, or a clock-like
   name). Choose *no clock* for a purely combinational design; a virtual clock then times it.
4. Clock period (ns on nangate45, ps on asap7) and core utilization.

The app writes `designs/<platform>/<name>/config.mk`, `constraint.sdc` (the clock plus input and output delays of
20% of the period) and, unless you untick it, copies the RTL into `designs/src/<name>/`. The design is then
selected; every other input is set on the Inputs pages below.

**New config…** (Inputs tab) makes a variant of an existing config that includes it and keeps its results in a
separate folder, for trying settings side by side.

## 1. Synthesis

**What it does.** Yosys reads the RTL (`1_1_yosys_canonicalize` turns it into one canonical RTLIL file), then
`1_2_yosys` optimizes it, maps it onto the platform's standard cells with ABC, and writes the netlist
`1_2_yosys.v`. `1_synth` loads the netlist and the SDC into OpenROAD (`1_synth.odb`, `1_synth.sdc`).

**Inputs and pages.**

| Input | Page | Notes |
| --- | --- | --- |
| `VERILOG_FILES`, `DESIGN_NAME` | New design… (All settings to change later) | The sources and the top module |
| `SYNTH_HIERARCHICAL`, `SYNTH_MINIMUM_KEEP_SIZE`, `SYNTH_KEEP_MODULES` | Synthesis | Keep modules as blocks, or flatten |
| `ABC_AREA` | Synthesis | Map for area instead of speed |
| `VERILOG_DEFINES`, `VERILOG_INCLUDE_DIRS`, `SYNTH_HDL_FRONTEND` | Synthesis | `slang` for SystemVerilog the built-in reader can't parse |
| Clocks (`create_clock`, `create_generated_clock`, `set_clock_groups`) | Clocks | Clock ports are read from the RTL; set the period and how clocks relate |
| IO delays, any SDC command | Constraints | Quick fields for the SDC's `set` values; a full editor; **Check with OpenROAD** |
| Uncertainty, latency, design rules, exceptions | Timing | See below |

**Timing page.** Per clock: setup and hold uncertainty (margin for jitter and the skew the clock tree will add),
transition, and source and network latency. Design rules: the cell driving the inputs, the load on the outputs,
max transition, fanout and capacitance. Exceptions: false paths (not timed), multicycle paths (N cycles), and
max or min delays, each from, through or to clocks, ports, pins, cells or nets. The page writes these into one
marked block of the SDC; lines outside the block are listed and never changed. An exception still being filled in
is kept as a comment, and the SDC can't be saved until it is complete, so the flow never sees a broken SDC.

**Results.** `1_2_yosys.v` (netlist), `1_synth.odb`; reports `synth_stat.txt` (cell count and area) and
`synth_check.txt`.

**If it fails.** Read `1_2_yosys.log`: an unknown module means a source file is missing from `VERILOG_FILES`; a
syntax error in SystemVerilog may need `SYNTH_HDL_FRONTEND = slang`. The Constraints page's **Check with
OpenROAD** reads the SDC against the netlist and lists every port, clock or cell it can't find.

## 2. Floorplan

**What it does.**

- `2_1_floorplan`: makes the die, the core and the standard cell rows, removes the buffers synthesis added and
  repairs setup timing by sizing and swapping gates. It also sets up the global routing layers (`FASTROUTE_TCL`).
- `2_2_floorplan_macro`: places macros. Positions from `MACRO_PLACEMENT_TCL` are used first; the macro placer
  (RTL-MP) places any macro left out.
- `2_3_floorplan_tapcell`: inserts tap and end cap cells along the rows.
- `2_4_floorplan_pdn`: builds the power grid from `PDN_TCL`.

**Floorplan page.** Choose one way to size the chip; ORFS stops when both are set, so switching empties the other:

- *Size from utilization*: `CORE_UTILIZATION` (% of the core the cells fill), `CORE_ASPECT_RATIO` (height /
  width) and `CORE_MARGIN` (core to die, microns). After synthesis the page shows the die size this makes,
  estimated from `synth_stat.txt`.
- *Fixed die and core*: `DIE_AREA` and `CORE_AREA`, each `x1 y1 x2 y2` in microns. OpenROAD rounds the core
  inward to whole sites and rows (on nangate45, `10 10 60 60` becomes `10.07 11.2 59.85 58.8`); the page shows
  the rounded core and draws it.

Macros (cells from `ADDITIONAL_LEFS`) are listed from the netlist with their size from the LEF. Select some and
**Place here** to fix them; then drag them in the preview or type X, Y and the orientation. The preview draws the
die, core, macros with the minimum channel (`RTLMP_MIN_CHANNEL_SIZE`) around them, and the IO pin regions. Once
the floorplan stage has run, the real die, core and macro positions of that run are drawn as grey dashed
outlines. Macros that overlap, or stick out of the core, are listed before you save. OpenROAD may move a macro
by a fraction of a micron so its pins sit on the routing tracks. Positions are saved as
`place_macro` lines in a marked block of `MACRO_PLACEMENT_TCL` (the app creates `macro_placement.tcl` in the
design folder if there is none). Placing macros needs the exact core, so use a fixed die and core with them.

**Power grid page.** Shows the grids of `PDN_TCL`: the standard cell grid and grids around macros. For each:
straps (layer, width, pitch, offset; *Follow pins* for the rails on the cell rows), rings (two layers, widths,
spacings, offset from the core) and via connections between layers. Layers are checked against the platform's
routing layers, and a pitch must be larger than the width. **Add ring** picks two free layers below the top straps
and connects the ring: the straps get `-extend_to_core_ring` so they run out to it, and each strap layer gets vias
to the ring layer that crosses it. A ring no strap reaches is dropped or left floating (the final IR drop check
then stops with `PSM-0069`), so the page lists such a ring as a problem. The platform's script is shared by every design, so
the first save writes an edited copy into the design folder and points `PDN_TCL` at it. Leave room for a ring:
it sits outside the core, so the core margin must be larger than offset + 2 × width + spacing.

**Results.** `2_floorplan.odb`; report `2_floorplan_final.rpt`.

**If it fails.** `2_1_floorplan.log`: *No floorplan initialization method* or *more than one method* means
utilization and a fixed die are both set or both missing (fix it on the Floorplan page). `2_2_floorplan_macro.log`:
a macro name the flow can't find. `2_4_floorplan_pdn.log`: `PDN-` errors name the grid and layer.

## 3. Placement

**What it does.**

- `3_1_place_gp_skip_io`: a quick global placement without pins, to know where the logic sits.
- `3_2_place_iop`: places the pins, inside the regions from `IO_CONSTRAINTS`.
- `3_3_place_gp`: global placement: spreads the cells to the target density, timing- and routability-driven.
- `3_4_place_resized`: timing repair: resizes gates and buffers long nets.
- `3_5_place_dp`: detailed placement: moves each cell onto a legal site.

**IO pins page.** Every top-level port, bus or bit range: the edge (left, right, top, bottom), a range on it in
microns, and *together* (keep a bus in one group) or *in order*. Buses can be split into bit ranges. **Check with
OpenROAD** resolves every name against the synthesized netlist. Saved in a marked block of `IO_CONSTRAINTS`.

**Placement page.** `PLACE_DENSITY` (target cell density, 0 to 1), `PLACE_DENSITY_LB_ADDON`, timing- and
routability-driven placement, cell padding, the layers of the pins (`IO_PLACER_H`, `IO_PLACER_V`) and
`TNS_END_PERCENT`.

**Results.** `3_place.odb`; reports `3_global_place.rpt`, `3_resizer.rpt`, `3_detailed_place.rpt`.

**If it fails.** `3_3_place_gp.log`: *overflow* that does not converge means the density is too high for the core;
lower `PLACE_DENSITY` or raise the core size. `3_5_place_dp.log`: cells that can't be legalized point to the same.

## 4. Clock tree (CTS)

**What it does.** `4_1_cts` builds a buffer tree from every clock source to its flip-flops so the clock arrives at
about the same time everywhere, then repairs setup and hold timing with the real clock latency.

**Clock tree page.** The buffers the tree may use (`CTS_BUF_LIST`), the buffer distance, cluster size and diameter,
and the repair after CTS (`SKIP_CTS_REPAIR_TIMING`, `SETUP_SLACK_MARGIN`, `HOLD_SLACK_MARGIN`). The clock
uncertainty and latency on the Timing page also matter here.

**Results.** `4_cts.odb`; report `4_cts_final.rpt` (skew, timing), image `cts_default_*.webp`.

## 5. Routing

**What it does.**

- `5_1_grt`: global routing plans every net over a grid of routing cells (GCells) and repairs antennas and timing.
- `5_2_route`: detailed routing draws the wires and vias, then fixes design rule violations over several
  iterations.
- `5_3_fillcell`: fills the gaps in the rows with filler cells.

**Routing page.** The lowest and highest routing layers, the lowest clock layer, and the capacity held back on
each layer. With `FASTROUTE_TCL` set (nangate45 sets it), per-layer capacities come from that script and
`ROUTING_LAYER_ADJUSTMENT` is not used; the page says so and edits the script's
`set_global_routing_layer_adjustment` lines (the first save makes a copy in the design folder). Also: skipping
timing repair in global routing, extra `global_route` arguments, the detailed routing iteration limit, antenna
repair and the filler cells.

**Results.** `5_route.odb`; reports `5_global_route.rpt`, `5_route_drc.rpt`.

**If it fails.** `5_1_grt.log`: *congestion* means not enough tracks: lower the density, enlarge the core, or hold
back less capacity. `5_route_drc.rpt`: violations left after detailed routing.

## 6. Finishing

**What it does.** `6_1_fill` adds metal fill where the platform asks for it; `6_1_merge` merges the cell and
macro layouts into one GDS with KLayout; `6_report` writes the final timing, power and area reports and images.

**Results.** `6_final.gds` (the layout), `6_final.odb`, `6_final.v`, `6_final.sdc`, `6_final.spef`; report
`6_finish.rpt`; images `final_*.webp` (placement, routing, congestion, IR drop, worst path), shown on the
Reports tab.

## Running and re-running

The side panel picks the config, the CPU cores and the stage range. **From stage**: *Continue* reuses stages that
are done; *Re-run from X* starts again from X. **To stage** stops after that stage. The plan box shows the exact
`make` command before you press **Run**. After you save an input, the app sets *From stage* to the first stage
that reads it (a floorplan change: *Re-run from floorplan*), because results built with the old inputs are out of
date. While any Inputs page has unsaved changes, Run is blocked, so a run never mixes saved and unsaved inputs.

## Where the inputs are saved

| Page | File | How |
| --- | --- | --- |
| Synthesis, Placement, Clock tree, Routing fields, Floorplan sizes, All settings | the config (`config*.mk`) | Variables, in place; new ones at the end under `# Set in ORFS Flow Runner` |
| Clocks | the SDC (`SDC_FILE`) | Periods in place; clocks added here in a marked block |
| Constraints | the SDC | The whole file (editor) |
| Timing | the SDC | One marked block |
| IO pins | `IO_CONSTRAINTS` | One marked block |
| Floorplan (macros) | `MACRO_PLACEMENT_TCL` | One marked block of `place_macro` lines |
| Power grid | `PDN_TCL` | Edited commands rewritten, others kept; a copy for the design if it was the platform's |
| Routing (per layer) | `FASTROUTE_TCL` | The adjustment lines; a copy for the design if it was the platform's |

Every save keeps a backup in `~/.cache/orfs-flow-runner/backups/`, and **Undo save** puts the files back as
they were, including removing a file the save created. A file shared with other designs is never changed for
you: the app writes a copy into the design folder instead.

## Walkthrough: gcd from RTL to GDS

The `nangate45/gcd` design (a greatest common divisor block, about 500 cells) runs the full flow in about seven
minutes.

1. Pick platform *nangate45*, design *gcd*, config *config.mk*.
2. **Clocks**: `core_clock` on port `clk` at 0.25 ns (4 GHz), and the virtual clock `vclk_core_clock` for the IO.
3. **Timing**: give `core_clock` a setup uncertainty of 0.02; set the output load to 0.005.
4. **Floorplan**: *Size from utilization* at 55%; after synthesis the page shows the die it makes (about 36 µm).
5. **Power grid**: the design has its own grid (`grid_strategy-M1-M4-M7.tcl`) with a smaller metal4 pitch for
   this small block.
6. Save each page, choose *To stage: finish* and press **Run**. Watch **Run output**; the Stages list fills in.
7. **Reports**: timing and area, and the final layout images. The GDS is
   `results/nangate45/gcd/base/6_final.gds`.

## Testing the app

Three test suites, run from the repository root:

```bash
python3 -m unittest discover -s gui/tests -p 'test_units.py' -v
```
Unit tests of every reader and writer (LEF, timing block, floorplan and macros, power grid, routing layers,
config files, new designs, IO pins). No display needed; under a second.

```bash
gui/tests/run_gui_tests.sh gui/tests/test_gui_inputs.py
```
Drives every Inputs page on throw-away copies of designs and checks the files each save writes, including that
OpenROAD accepts the SDC. The app runs on a private Broadway display, so nothing opens on the desktop. About two
minutes.

```bash
gui/tests/run_gui_tests.sh gui/tests/test_flow_e2e.py
```
Sets inputs only through the pages, runs the real flow from the app's Run button, and checks the results: the
die and core (rounded as the page shows), the power ring layers, the routing file and placement density in the logs, the timing constraints
in the final SDC, and two RAM macros where the Floorplan page put them (within half a micron). About ten minutes.

`gui/tests/run_gui_tests.sh` with no arguments runs every GUI test. Test designs are named `test_*` and removed
afterwards.
