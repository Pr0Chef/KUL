# ORFS Flow Runner

A desktop app that runs any OpenROAD-flow-scripts (ORFS) design from RTL to GDS. You set the design's inputs
(SDC, floorplan and other flow settings, constraint files) in the **Inputs** tab, pick the design, CPU cores and
stage range from drop-down menus, press Run, and read the results in separate **Reports**, **Logs** and **Debug**
tabs. No terminal is needed. It drives the stock ORFS Makefile and saves inputs into the design's own files, so a
run here gives the same result as the same `make` command in a terminal.

![ORFS Flow Runner synthesising CVA6](../docs/images/gui/running_parallel.png)

The illustrated user guide, [docs/ORFS_Flow_Runner_user_guide.pdf](../docs/ORFS_Flow_Runner_user_guide.pdf), walks
through every part of the app with screenshots from real runs: setting the inputs (section 6) and a stock against
parallel `abc_new` comparison ([A/B synthesis run](#ab-synthesis-run-stock-and-parallel-abc_new) below).
[docs/ORFS_flow_guide.md](../docs/ORFS_flow_guide.md) explains every stage and step of the flow from RTL to GDS,
which inputs each one reads and which page sets them, with a walkthrough on `gcd`.

Contents: [Install](#install) · [Requirements](#requirements) · [Step by step](#step-by-step) · [The menus](#the-menus) ·
[The Inputs tab](#the-inputs-tab) · [The tabs](#the-tabs) · [Common tasks](#common-tasks) · [A/B synthesis run](#ab-synthesis-run-stock-and-parallel-abc_new) ·
[Troubleshooting](#troubleshooting) · [Files](#files)

## Install

The app ships as a Debian package for Ubuntu (and other Debian-based systems). It installs under
`/opt/orfs-flow-runner`, adds **ORFS Flow Runner** to the app menu and adds an `orfs-flow-runner` command.

1. **Build the package** from a checkout of this repo (or take the `.deb` someone built for you):

   ```bash
   gui/packaging/build_deb.sh
   ```

   This writes `dist/orfs-flow-runner_<version>_all.deb`. The version is `VERSION` in `gui/orfs_app.py`.

2. **Install it.** apt also installs GTK, PyGObject, make and KLayout if they are missing:

   ```bash
   sudo apt install ./dist/orfs-flow-runner_1.2.0_all.deb
   ```

3. **Install OpenROAD, Yosys and an ORFS checkout** if you don't have them; the package doesn't include them
   (see [Requirements](#requirements)).

4. **Start ORFS Flow Runner** from the app menu. On the first start it asks for the ORFS `flow/` folder unless it
   finds one in `$ORFS_FLOW` or `~/OpenROAD-flow-scripts/flow`.

5. **Check Debug → Environment.** If openroad, yosys or klayout shows *NOT FOUND*, click **Tools…** in the title
   bar and choose the program. *Automatic* uses the one on `PATH`, else one in `/usr/local/bin`, `/usr/bin`,
   `/opt/openroad/bin` or `~/.local/bin`.

   ![Tools dialog](../docs/images/gui/tools.png)

To update, build and install the newer `.deb` the same way. To remove it: `sudo apt remove orfs-flow-runner`
(your settings and run logs in your home folder are kept).

If you used `gui/install_launcher.sh` before, run `gui/install_launcher.sh --uninstall` first: its menu entry
has the same name and would hide the installed one.

## Requirements

Tested on Ubuntu 24.04 with these versions:

| Needed | Tested version | How to get it |
| --- | --- | --- |
| Python 3 | 3.12.3 | ships with Ubuntu |
| PyGObject + GTK 3 | `python3-gi` 3.48.2, `gir1.2-gtk-3.0` 3.24.41 | `sudo apt install python3-gi gir1.2-gtk-3.0` (Ubuntu desktops already have them) |
| OpenROAD | `26Q4-271-g0907a8656e` | Step 1 of the [repo README](../README.md#step-1-install-the-tools) |
| Yosys | 0.69+274, with `read_slang` | Step 1 of the repo README; `read_slang` is needed for CVA6 |
| KLayout | 0.28.16 | `sudo apt install klayout`; only the finish stage uses it, to write the GDS |
| ORFS flow folder | `OpenROAD-flow-scripts/flow` | `git clone --recursive https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts` |

The repo's vendored `orfs/flow` also works, but it only has the asap7 platform and the CVA6 design. The full ORFS
clone has 85 designs on 9 platforms, 71 of them runnable (gf12 and gf55 need PDKs that are not public).

## Step by step

This runs the small `gcd` design on nangate45 (7 min 51 s on 4 cores in testing). CVA6 works the same way; only
the choices and the run time change.

1. **Start the app** from the app menu if you [installed](#install) it, or from the repo root without
   installing:

   ```bash
   python3 gui/orfs_app.py
   ```

   It opens the flow folder you used last, else `$ORFS_FLOW`, else `./OpenROAD-flow-scripts/flow`, else
   `./orfs/flow`, else `~/OpenROAD-flow-scripts/flow`. The title bar shows which one. To pick another, click
   **Flow folder…** or start with `--flow /path/to/OpenROAD-flow-scripts/flow`.

2. **Optional, when running from the checkout: add it to the app menu and the Desktop** without installing the
   package:

   ```bash
   gui/install_launcher.sh
   ```

   `gui/install_launcher.sh --uninstall` removes it. Programs are found as described under **Tools…** in
   [Install](#install).

3. **Platform:** choose `nangate45`. The number after each name is its design count. Platforms marked
   *PDK not installed* can't be selected.

4. **Design:** choose `gcd`.

5. **Config:** choose the `config*.mk` file. `gcd` only has `config.mk`; for CVA6 choose `config_hier.mk`. The
   status bar then shows where results will go, for example `./results/nangate45/gcd/base`.

6. **CPU cores:** choose how many OpenROAD may use, for example `4 of 8`. Fewer cores use less memory; use 4 for
   CVA6 routing on a 16 GB machine.

7. **From stage:** choose *Continue (reuse finished stages)*. To redo work, choose *Re-run from <stage>*
   instead (see [The menus](#the-menus)).

8. **To stage:** choose `finish` to go all the way to GDS.

9. **Read the plan box.** It shows the exact command, here
   `make DESIGN_CONFIG=designs/nangate45/gcd/config.mk NUM_CORES=4 finish`, which stages will run, and anything
   that will be deleted.

10. **Press Run.** The **Run output** tab streams the output (errors red, warnings amber). The **Stages** list
    moves each stage from *not built* to *running* to *done*, with its runtime and peak memory. The status bar
    shows the current step (for example `1_2_yosys`), the tool and memory use.

11. **Wait for `>> exit code 0`.** The status bar then says *Run finished*. A non-zero exit code means a step
    failed: see [Troubleshooting](#troubleshooting).

12. **Check the results** in **Reports → QoR checks**: timing slack, slew/cap/fanout violations, DRC, antenna and
    whether `6_final.gds` was written. **Debug → Open results folder** opens the folder with the GDS.

To stop a run, press **Stop** and confirm. The running step is killed, its partial log stays as a `.tmp.log`,
and finished stages are kept.

## The menus

| Menu | Lists | Sets |
| --- | --- | --- |
| Platform | each folder in `flow/designs/` with designs; greyed out without `flow/platforms/<name>/config.mk` | the PDK |
| Design | that platform's designs | the design folder |
| Config | the design's `config*.mk` files | `DESIGN_CONFIG`, and with it the results folder |
| CPU cores | 1 to the number of cores | `NUM_CORES` |
| From stage | *Continue*, or *Re-run from* synth, floorplan, place, cts, route, finish | what is cleaned first |
| To stage | synth, floorplan, place, cts, route, finish | the last `make` target |

- **Continue** runs `make <to-stage>`; make reuses every stage that is already built.
- **Re-run from X** runs `make clean_X` for X and every later stage, then `make <to-stage>`. Later stages are
  cleaned too because they were built from the old X. You are asked to confirm, with the list of stages that will
  be deleted (partial ones included).
- If **From** comes after **To**, Run stays disabled. If a stage before **From** was never built, make builds it
  first, and the plan says so.

The app remembers your choices for the next start.

## The Inputs tab

Everything a run reads, edited in the app and saved into the selected config's own files. The pages are listed
on the left in the order the flow uses them: **Synthesis**, **Clocks**, **Constraints**, **Timing**, **IO pins**,
**Floorplan**, **Power grid**, **Placement**, **Clock tree**, **Routing**, **All settings** and **Files**. The
selected config is named at the top. Every page has the same *Saved / Unsaved changes* label with **Revert** and
**Save**.

**New design…** (header bar) starts a design from RTL files: pick the files, and the app finds the top module
(the module nothing else instantiates) and its clock port, asks for the period and utilization, and writes
`designs/<platform>/<name>/config.mk` and `constraint.sdc`, copying the RTL into `designs/src/<name>/`.

![New design: gcd.v added, top module and clock found from the RTL](../docs/images/gui/new_design.png)

- **Synthesis, Placement, Clock tree, Routing:** the settings of each stage as proper fields: check boxes, numbers
  with their allowed range, and lists of the platform's routing layers (read from its tech LEF). Each field shows
  where its value comes from (*set in this config*, *platform default*, *ORFS default*), ORFS's description, and
  **Use default** to remove it from this config. A value that can't be used (a density of 2, a layer the
  platform doesn't have) is shown in red and not taken. Settings changed on any of these pages are pending
  together and saved into the config by any page's **Save**; **All settings** shows the same pending values.
  - **Routing** also edits the capacity held back per layer, the `set_global_routing_layer_adjustment` lines of
    `FASTROUTE_TCL`. When that file is set (nangate45 sets it), `ROUTING_LAYER_ADJUSTMENT` is not used, and the
    page says so at the field. The platform's file is shared, so the first save writes a copy into the design
    folder and points `FASTROUTE_TCL` at it.

  ![Inputs: routing layers and per-layer capacity](../docs/images/gui/inputs_routing.png)

![Inputs: clocks of chi_async_bridge taken from its RTL, with periods and their relationship set in the app](../docs/images/gui/inputs_clocks.png)

- **Clocks:** the design's clocks, from its RTL and its SDC.
  - The app finds the top module (`DESIGN_NAME`) among `VERILOG_FILES` and reads its ports, in ANSI or
    non-ANSI Verilog/SystemVerilog. An input counts as a clock if it is used in `posedge`/`negedge`, drives a
    clock pin of an instance (`.clock(clock)`), or is named like one; the line at the top says which and why
    (for CVA6: `clk_i`, from `posedge clk_i`).
  - The SDC is run in `tclsh` with the SDC commands replaced by stubs that record what they define, so
    variables, `expr` and `foreach` work as in OpenROAD. Every clock it defines is listed with its port (or
    *virtual*), period, frequency and the line it is on; generated clocks are listed too.
  - A clock port from the RTL with no clock yet is a row with an empty period: type a period to create the
    clock (and rename it if you like). **Add clock…** adds a clock on any input, or a virtual clock for input and
    output delays.
  - Changing the period of a clock defined in the file edits it where it is set: the `set clk_period …` line
    it uses, or the number in its `create_clock` line. Clocks that share a period variable change together
    (ethmac's virtual clocks follow their real ones).
  - *How the clocks relate* has a row for each pair of clocks on ports: *Timed together*, *Asynchronous*,
    *Logically exclusive* or *Physically exclusive*. Relations the file already sets with `set_clock_groups` are
    shown and left alone.
  - What you set here is written to one block in the SDC, between `# >>> ORFS Flow Runner: clocks` and
    `# <<< ORFS Flow Runner: clocks` (`create_clock` and `set_clock_groups` lines), placed after the file's own
    clock definitions. The block is rewritten on each change; the rest of the file is not touched. Clocks are
    saved with the SDC, from this page or **Constraints**.

![Inputs: the six clocks of ethmac and their logically exclusive groups, read from its SDC](../docs/images/gui/inputs_clocks_ethmac.png)

![Inputs: the SDC of gcd checked against the synthesized design](../docs/images/gui/inputs_constraints.png)

- **Constraints:** the SDC file the config uses (`SDC_FILE`). If other configs use the same SDC, the tab says so;
  **Use a separate copy for this config** copies it to `constraint_<config>.sdc` and points this config at the
  copy, so the others are left as they are. Below the settings is the whole file, which you can edit freely.
  - *Quick settings* has a field for every value the file sets with `set` (`clk_name`, `clk_port`, `clk_period`,
    `input_delay`, …) and for a number in `create_clock -period`. The clock period shows its frequency, in ps on
    asap7 and ns elsewhere.
  - Port names come from the synthesized netlist (`1_synth.v` or `1_2_yosys.v`) once synthesis has run, and
    from the top module's RTL before that. A `…port…` setting such as `clk_port` is a list of the design's input
    ports, and a name that isn't a port is shown in red. A clock period is labelled with its clock's name.
  - A variable the file sets but no command uses is marked *set but not used*. CVA6's SDCs, for example, set
    `input_delay` and `output_delay` but never apply them.
  - *Input and output delays* says whether the file has `set_input_delay` and `set_output_delay`.
    **Add input and output delays** asks which clock to use when there is more than one, then appends the
    lines ORFS's own SDCs use:
    `set_input_delay $input_delay -clock $clk_name [all_inputs -no_clocks]` and
    `set_output_delay $output_delay -clock $clk_name [all_outputs]`, plus `set input_delay`/`set output_delay`
    at 20 % of the clock period if the file doesn't set them. **Delay for specific ports…** lists the inputs or
    outputs to tick, with the clock they belong to and, for a bus, optionally which bits (`31:16` or `3`;
    empty means all), and adds a `set_…_delay … [get_ports {…}]` line after the
    general one, so it overrides it for those ports (for example, ports of a second clock domain). Both only change the editor; save to keep them.
  - **Check with OpenROAD** reads the SDC as it is in the editor, unsaved changes included, into OpenROAD with
    the synthesized design (`1_synth.odb`), the same way the flow does. It lists every clock with its period and
    the number of ports it is on, any OpenROAD warning or error (for example `port 'clk_x' not found`), and
    unused variables.
- **Timing:** what makes timing realistic, written into one marked block of the SDC
  (`# >>> ORFS Flow Runner: timing`):
  - per clock: setup and hold uncertainty, transition, source and network latency;
  - inputs, outputs and design rules: the cell (and pin) driving the inputs, the load on the outputs, max
    transition, max fanout and max capacitance;
  - timing exceptions: false paths, multicycle paths (with *setup only* or *hold only*), and max or min delays,
    from, through or to clocks, ports, pins, cells or nets, with `*` and `?` in names.
  Lines elsewhere in the SDC that set the same things are listed below, read-only. An exception still being
  filled in is kept in the block as a comment (`# incomplete:`), and the SDC can't be saved from any page until
  every exception is complete, so the flow never reads a broken SDC.

  ![Inputs: clock uncertainty, driving cell, load and two exceptions on gcd](../docs/images/gui/inputs_timing.png)

- **IO pins:** where the design's pins go on the die edge, written as `set_io_pin_constraint` lines to the
  config's `IO_CONSTRAINTS` file, which the floorplan stage reads.

  ![Inputs: IO pins of gcd, with clock and reset on the left and the response on the right](../docs/images/gui/inputs_io.png)

  - One row per port. A bus is one row with its pin count: widths come from the synthesized netlist, or from the
    RTL before synthesis when they are plain numbers (`[31:0]`); widths set by parameters or types show as `?`
    until synthesis. A bus name stands for all its bits, as it does in OpenROAD.
  - Select rows (Ctrl or Shift for several), choose an edge (left, right, top, bottom) and a range in microns
    (`*` for the whole edge, or `0-200`), and press **Place**. *together* keeps a bus's pins next to each other
    (`-group`); *in order* also keeps them in bit order (`-order`). The edge and range can also be changed in
    the table. **Unplace** leaves a row to the flow.
  - **Split bus…** turns a bus into two rows at a bit you choose, such as `boot_addr_i[31:16]` and
    `boot_addr_i[15:0]`, so the parts can go on different edges; **Join** puts them back together.
  - Below the table: the pins on each edge and the pins left to the flow. Saving checks that every bit range
    fits its bus and that no pin is placed twice. **Check with OpenROAD** looks up every pin name in the
    synthesized design, as the floorplan stage will; an unknown name there stops the floorplan
    (`PPL-0061`).
  - The rows are written to one marked block of the file (`# >>> ORFS Flow Runner: IO pins` …). Constraints
    already in the file outside the block are shown in the table, read-only, and kept; a line that places
    pins by direction or with a Tcl expression is noted above the table. If the config has no
    `IO_CONSTRAINTS` yet, saving creates `io_constraints.tcl` in the design folder and sets it.
  - Checked in the flow on nangate45 `gcd`: with `clk`/`reset` on the left, `req_msg[31:16]` on the bottom
    (together, in order), `req_msg[15:0]` on top and `resp_msg` on the right, every pin was placed on its
    edge and the bottom bits came out in bit order.
- **Floorplan:** the die and core, and the macros.
  - *Size from utilization* (`CORE_UTILIZATION`, `CORE_ASPECT_RATIO`, `CORE_MARGIN`) or *Fixed die and core*
    (`DIE_AREA`, `CORE_AREA`, each `x1 y1 x2 y2` in microns). ORFS stops when both are set, so switching removes
    the other one. OpenROAD rounds a fixed core inward to whole sites and rows; the page shows the rounded core. In utilization mode the page estimates the die from the synthesis area.
  - Macros are the netlist's instances of cells from `ADDITIONAL_LEFS`, with their size from the LEF. Select
    some and **Place here**, then drag them in the preview or type X, Y and the orientation; **Let the flow
    place** hands them back to the macro placer. Overlaps (counting `RTLMP_MIN_CHANNEL_SIZE`) and macros outside
    the core are listed. Positions are saved as `place_macro` lines in a marked block of `MACRO_PLACEMENT_TCL`
    (created as `macro_placement.tcl` if the config has none).
  - The preview draws the die, core, macros and the IO pin regions; once the floorplan stage has run, the real
    die, core and macro positions of that run are read with OpenROAD and drawn as grey dashed outlines.

  ![Inputs: two RAM macros placed on the Floorplan page](../docs/images/gui/inputs_floorplan_macros.png)

- **Power grid:** the grids of `PDN_TCL` (the standard cell grid and the grids around macros) as tables of
  straps (layer, width, pitch, offset, *Follow pins* for the cell rails), rings (two layers, widths, spacings,
  core offset) and vias between two layers. **Add strap**, **Add ring**, **Add vias**, **Remove**. A new ring
  goes on two free layers below the top straps and is connected: the straps get `-extend_to_core_ring` and each
  strap layer gets vias to the ring layer that crosses it. A ring no strap reaches would be left unpowered (the
  final IR drop check stops on it), so the page lists one as a problem. Layers are
  checked against the platform's routing layers and a pitch must be larger than its width. Edited commands are
  rewritten as one line; everything else in the script is kept. The platform's script is shared, so the first
  save writes a copy into the design folder and points `PDN_TCL` at it.

  ![Inputs: the power grid of gcd with a ring added](../docs/images/gui/inputs_power_grid.png)

- **All settings:** every ORFS variable (from `scripts/variables.json`) with its value in this config, the value
  the flow actually uses (`make` resolves it, including included configs and platform defaults), where that value
  is set, and the first stage that reads it. Hover a row for ORFS's description and default. The list opens on
  *Common settings* (die and core area, utilization, aspect ratio, margins, IO and macro placement files, PDN,
  placement density, timing repair, routing layers); pick a stage, *Set in this config* or *All variables*, or
  search names and descriptions. Click a value to change it; an empty value removes it from this config, so the
  included file's or the default value applies again. New values go at the end of the config under
  `# Set in ORFS Flow Runner`; values the config already sets are changed where they are.
- **Files:** every text file in the design folder (configs, SDC, Tcl, Verilog), plus shared files the config uses,
  such as the platform's PDN and tapcell scripts (marked *shared with other designs*). Edit and save any of them.
  **New file…** adds a file from a template:
  - *IO pin constraints* (`io_constraints.tcl`, sets `IO_CONSTRAINTS`): `set_io_pin_constraint` and `place_pin`
    examples, and the design's input and output ports, read from the synthesized netlist. The **IO pins** page
    writes the same kind of file from a table.
  - *Macro placement* (`macro_placement.tcl`, sets `MACRO_PLACEMENT_TCL`): one `place_macro` line per macro in the
    netlist (the 8 SRAMs of CVA6), with names written the way OpenROAD stores them.
  - an empty Tcl or SDC file.

![Inputs: All settings, showing what gcd's config sets, with the placement density changed](../docs/images/gui/inputs_settings.png)

**New config…** (top right) makes `config_<name>.mk` that includes the selected config and sets its own
`FLOW_VARIANT`, then selects it: a variant to try settings on, with its results in a separate folder.

What happens when you save:

1. The file is written; the old version is kept in `~/.cache/orfs-flow-runner/backups/<date>_<time>/`.
2. `make` reads the config again. If it no longer parses, the error is shown and **Undo save** puts the old files
   back.
3. The app works out the first stage that reads what changed (for example floorplan for `CORE_UTILIZATION`, synth
   for the SDC). If that stage or a later one is already built, **From stage** is set to re-run from it, and the
   bar says which stages were built with the old inputs.

While a tab page has unsaved changes, **Run** and the Platform, Design and Config menus are disabled: save or
revert first. Saving is disabled while a run is going, because the flow reads these files as it runs. If a file
changed on disk since the app opened it (an editor, `git pull`), saving asks before replacing it.

## The tabs

![QoR checks for CVA6 up to global routing](../docs/images/gui/qor.png)

- **Inputs:** the design's constraints, flow settings and files ([above](#the-inputs-tab)).
- **Run output:** live output of the current run.
- **Reports:**
  - *QoR checks*: pass/fail tiles at the latest stage with metrics (setup/hold WNS and TNS, setup/hold
    violations, max slew/cap/fanout, antenna, flow errors, DRC report, final GDS), then a table of every metric
    by stage (synth, floorplan, place, cts, grt, route, final). Red fails: negative slack or a non-zero count. The
    numbers come from the ORFS metrics JSON in `logs/<platform>/<design>/<variant>/`.
  - *Report files*: the design's `reports/` folder, with a line filter and an errors-and-warnings-only view.
- **Logs:** stage logs (`logs/…`, one per step; `.tmp.log` = running or killed) and run logs (`~/.local/share/orfs-flow-runner/run_logs/`,
  one per run, newest first). Filter lines, show only errors and warnings, or tick **Follow** to watch a log being written.
- **Debug:**
  - *Actions*: **Dry run (make -n)**, **Open in OpenROAD GUI** for a built stage (needs a display), **Open
    results folder**, **Copy command** (to run the plan in a terminal).
  - *Environment*: flow folder, openroad/yosys/klayout versions and paths, whether Yosys has `read_slang`, CPU
    count, free memory, run-log folder, flow git commit.
  - *Resolved make variables*: `PLATFORM`, `DESIGN_NICKNAME`, `FLOW_VARIANT`, `RESULTS_DIR`, `SDC_FILE`,
    `CORE_UTILIZATION`, `PLACE_DENSITY`, `VERILOG_FILES` and others, as `make print-<VAR>` reports them.
  - *Errors and warnings*: a count of every message ID (for example `RSZ-0104`) in the design's logs, errors
    first. Double-click one to open its log in **Logs**, filtered to that ID.
  - *Debug output*: dry-run output and messages, at the bottom of the tab.

## Common tasks

**Finish CVA6 from where routing stopped:** Platform `asap7`, Design `cva6`, Config `config_hier.mk`, CPU cores
`4 of 8`, From *Continue*, To `finish`. The plan reads *Runs route, finish.* Detailed routing needs about 7 GB, so
close heavy applications first and check **Debug → Environment → Memory**.

**Try a different floorplan:** **Inputs → New config…** (for example `util50`), then in **Floorplan** set
`CORE_UTILIZATION` to `50` (or `DIE_AREA` and `CORE_AREA`) and save. The new config has its own results folder, so
the original stays as it was. Run to `floorplan` and compare the two in **Reports**.

**Tighten the clock:** **Inputs → Constraints**, change `clk_period`, save. From stage is set to *Re-run from
synth*, since ORFS reads the SDC during synthesis. Use a separate copy first if other configs share the SDC.

**Set up the clocks of a new design:** **Inputs → Clocks** lists the clock ports found in the top module's RTL.
Type a period for each, set how each pair relates (for example *Asynchronous* for clocks from separate sources),
add a virtual clock for the I/O if you want one, and save.

**Constrain inputs and outputs:** **Inputs → Constraints**: if it says *no set_input_delay uses it*, press
**Add input and output delays**, set `input_delay` and `output_delay`, add **Delay for specific
ports…** for ports that need their own value, then **Check with OpenROAD** and save.

**Place the IO pins:** **Inputs → IO pins**: select ports, choose an edge and press **Place**; split a bus to put
its parts on different edges; **Check with OpenROAD**, save, and re-run from floorplan.

**Fix the pins or macros by hand:** **Inputs → Files → New file…**, *IO pin constraints* or *Macro placement*, fill in the
commented lines, save, and re-run from floorplan.

**Runs that must survive logging out:** closing the app asks whether to stop its run. Use **Debug → Copy
command** and start it with `systemd-run --user` as in the repo README's
[Running unattended](../README.md#running-unattended). The app still shows that run's results and logs, but not
its live status.

## A/B synthesis run: stock and parallel abc_new

`orfs/flow/designs/asap7/cva6/` has two configs for comparing Yosys builds on CVA6. Both include `config_hier.mk`
and only change `FLOW_VARIANT`, so their results stay apart:

| Config | Results in | Choose this Yosys in **Tools…** |
| --- | --- | --- |
| `config_hier_stock.mk` | `results/asap7/cva6/hier_stock` | a stock build, for example `/usr/local/bin/yosys` |
| `config_hier_parallel.mk` | `results/asap7/cva6/hier_parallel` | the `yosys/` submodule build, `yosys/build/yosys` |

1. Build Yosys from the submodule ([README, Step 2](../README.md#step-2-clone-this-repo-and-build-yosys)).
2. Platform `asap7`, Design `cva6`, Config `config_hier_stock.mk`, To stage `synth`; set Yosys to the stock build
   in **Tools…** and press **Run**.
3. When it finishes, choose `config_hier_parallel.mk`, set Yosys to `yosys/build/yosys` and press **Run**.
4. Repeat in the order stock, parallel, parallel, stock (choose *Re-run from synth* so each run starts from
   scratch) and compare the **Stages** times and the end of each variant's `1_2_yosys.log` (`Took … seconds: abc_new`,
   the time per pass and the CPU time). The CPU time of the Yosys step is the same work every run, so a run with
   much more of it ran on a slower machine.

Measured from the app, four runs from scratch in the order stock, parallel, parallel, stock (`NUM_CORES=8`, an
i5-1235U host in a VirtualBox VM with 8 vCPUs and 10 GB). Before every run the variant's results were deleted and the
sources and tools were read into memory:

| Run | Build | Whole synthesis | `1_2_yosys` step | `abc_new` | Yosys CPU time |
| --- | --- | --- | --- | --- | --- |
| 1 | stock | 2,562 s | 2,510 s | 1,427 s | 3,054 s |
| 2 | parallel | 2,255 s | 2,198 s | 1,105 s | 3,076 s |
| 3 | parallel | 2,873 s | 2,800 s | 1,711 s | 3,890 s |
| 4 | stock | 3,159 s | 3,084 s | 1,453 s | 3,664 s |

| Comparison (stock time / parallel time) | Whole synthesis | `abc_new` |
| --- | --- | --- |
| Runs 1 and 2, machine at normal speed (CPU time about 3,060 s) | 1.14× | 1.29× |
| Runs 4 and 3, machine slower (CPU time 3,660 to 3,890 s) | 1.10× | 0.85× |
| Mean of the two runs of each build | 1.12× | 1.02× |

The four netlists are identical apart from the Yosys version comment (115,493 instances, 18,297.4 µm²). The speedup
is real in the cleanest pair but small on average, and the run-to-run noise on this machine is as large as the
effect: runs 3 and 4 used 20 to 27% more CPU time for the same work, so the machine had slowed down, and in that pair
the parallel `abc_new` was slower. Treat it as `abc_new` between 0.85× and 1.29× and the whole synthesis about 1.1×,
until it is repeated on a quiet machine with more runs. The guide's section 9 has the method and the discussion.

![Wall time of each run](../docs/images/gui/chart_wall.png)

At most 4 `yosys-abc` processes ran at once: Yosys's own serial work around each ABC call (`write_xaiger2` alone
takes 352 to 464 s) caps the saving.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| `No module named gi` at start | `sudo apt install python3-gi gir1.2-gtk-3.0`, and use the system `python3`, not a venv |
| A run fails at once with error 127 | OpenROAD or Yosys wasn't found: check **Debug → Environment** and choose the program with **Tools…** |
| The Platform menu is empty | the folder isn't an ORFS `flow/` folder: use **Flow folder…** |
| A stage shows *interrupted* | the step was killed, usually for lack of memory. Look for `oomd` or `oom-kill` in `journalctl --since "1 hour ago"`, use fewer cores, then run again with *Continue* |
| `read_slang` not found (CVA6) | **Debug** shows *read_slang missing*: build Yosys with the slang frontend (repo README, Step 1) |
| Make variables show *make error* | the config doesn't parse; the message is in **Debug output**. If a save in **Inputs** caused it, use **Undo save** |
| Run and the config menus are greyed out | **Inputs** has unsaved changes: save or revert them |
| **Clocks** says *tclsh was not found* | `sudo apt install tcl`; the `.deb` installs it |
| **Clocks** says reading the SDC *stopped early* | the SDC uses something only the flow has, such as `get_cells` on the netlist; clocks defined before that line are still shown |
| The floorplan stops with `PPL-0061 Pins … were not found` | a pin name in `IO_CONSTRAINTS` doesn't exist: open **Inputs → IO pins** and press **Check with OpenROAD** |
| **IO pins** shows `?` pins | the width comes from a parameter or type; it is known after synthesis |
| A clock port is missing from **Clocks** | it isn't used with `posedge`, connected to a clock pin, or named like a clock; add it with **Add clock…** |
| A value set in **All settings** isn't used | the platform sets it with `=` after the design config is read; the *Value the flow uses* column shows what applies |
| Open in OpenROAD GUI does nothing | it needs a display; see `~/.cache/orfs-flow-runner/openroad_gui.log` |

Limits: one run at a time per window; only runs started from the app show as *running*; viewers show the last
4 MB of a log (the full output is always in the run log).

## Files

| Path | What it is |
| --- | --- |
| `gui/orfs_app.py` | the app |
| `gui/inputs_tab.py` | the Inputs tab: loading a config, the Clocks, Constraints, IO pins, All settings and Files pages, saving with backups |
| `gui/input_page.py` | what every newer Inputs page shares: load, unsaved state, Revert and Save |
| `gui/stage_forms.py` | the Synthesis, Placement, Clock tree and Routing pages, built from field lists |
| `gui/timing_page.py`, `gui/sdc_timing.py` | the Timing page, and the reader and writer of its SDC block |
| `gui/floorplan_page.py`, `gui/floorplan.py` | the Floorplan page with its preview, and the die estimate and macro block |
| `gui/pdn_page.py`, `gui/pdn.py` | the Power grid page, and the reader and editor of pdngen scripts |
| `gui/fastroute.py` | reads and writes the per-layer capacity lines of `FASTROUTE_TCL` |
| `gui/lef.py` | reads routing layers, macro sizes and site sizes from LEF files |
| `gui/new_design.py` | the New design dialog and the files it writes |
| `gui/io_pins.py` | reads and writes the `set_io_pin_constraint` block of the `IO_CONSTRAINTS` file |
| `gui/sdc_clocks.py` | reads clock ports from the RTL and clocks from an SDC (with `tclsh`), and writes the clocks block |
| `gui/tests/` | unit tests (`test_units.py`), GUI tests and the end-to-end flow test, run off screen by `run_gui_tests.sh`; see [Testing](../docs/ORFS_flow_guide.md#testing-the-app) |
| `docs/ORFS_flow_guide.md` | every stage and step from RTL to GDS, the inputs each reads and the page that sets them |
| `gui/flow_gui.py` | the CVA6 web console; the app reuses its metric definitions, so keep both together |
| `gui/install_launcher.sh` | adds or removes a menu and Desktop launcher for running from the checkout |
| `gui/packaging/build_deb.sh` | builds the `.deb`; `orfs-flow-runner.desktop` and `.svg` are its menu entry and icon |
| `docs/ORFS_Flow_Runner_user_guide.pdf` | the illustrated user guide; `docs/images/gui/` has the screenshots used in this README |
| `orfs/flow/designs/asap7/cva6/config_hier_{stock,parallel}.mk` | the A/B configs above |
| `~/.config/orfs-flow-runner/settings.json` | last flow folder, menu choices and the programs chosen with **Tools…** |
| `~/.local/share/orfs-flow-runner/run_logs/` | one full log per run: `<platform>_<design>_<variant>_<from>-<to>_<date>_<time>.log` |
| `~/.cache/orfs-flow-runner/` | OpenROAD GUI log, and `backups/` with every file the Inputs tab replaced; safe to delete |
| `<flow>/results/<platform>/<design>/<variant>/` | ORFS results: one `.odb` per stage, `6_final.gds` |
| `<flow>/logs/…`, `<flow>/reports/…` | ORFS step logs (with metrics JSON) and reports |
| `gui/.cache/` | web-console renders; safe to delete, ignored by git |
