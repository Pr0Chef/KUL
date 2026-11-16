"""Logic equivalence checks (LEC) for the ASIC flow, with Yosys's equivalence commands.

Three checks, each comparing a "gold" design with a "gate" design of the same top module:
  RTL vs synthesis      the design's Verilog against Yosys's netlist (1_2_yosys.v);
  synthesis vs final    that netlist against the routed netlist (6_final.v);
  synthesis vs a stage  that netlist against a stage's database, written out as Verilog by OpenROAD.
Netlists are read with the platform's liberty files (LIB_FILES), which give each cell its function. Both sides are
flattened; registers and ports are matched by name and the logic between them is proven equal, first by
simulation-free SAT (equiv_simple), then by induction (equiv_induct). Wire names that tools generate (they start
with _) are hidden first: placement and timing repair swap the inputs of gates and rename such wires, which
says nothing about the function. ORFS's own LEC_CHECK needs the Kepler Formal tool, which is not open to install
here; these checks need only Yosys.
"""

import re
from pathlib import Path

CHECKS = [("rtl_synth", "RTL vs synthesis"), ("synth_final", "Synthesis vs final"),
          ("synth_stage", "Synthesis vs a stage")]
STAGE_DBS = [("floorplan", "2_floorplan.odb"), ("place", "3_place.odb"), ("cts", "4_cts.odb"),
             ("route", "5_route.odb")]


def read_rtl(files, defines=(), incs=()):
    flags = " ".join(["-sv"] + [f"-D{d}" for d in defines] + [f"-I{i}" for i in incs])
    return [f"read_verilog {flags} {f}" for f in files]


def read_netlist(netlist, libs):
    return [f"read_liberty -ignore_miss_func -ignore_miss_dir {lib}" for lib in libs] + [f"read_verilog {netlist}"]


def script(top, gold, gate, seq=5):
    """A Yosys script that proves gate equals gold. gold, gate: the commands that read each side."""
    lines = []
    for side, reads in (("gold", gold), ("gate", gate)):
        lines += reads + [f"hierarchy -check -top {top}", "proc", "flatten", "opt_clean", "async2sync",
                          "rename -hide w:_*", "opt_clean", f"rename {top} {side}", f"design -stash {side}"]
    lines += ["design -copy-from gold -as gold gold", "design -copy-from gate -as gate gate",
              "equiv_make gold gate equiv", "hierarchy -top equiv", f"equiv_simple -seq {seq}",
              f"equiv_induct -seq {seq}", "equiv_status"]
    return "\n".join(lines) + "\n"


def verdict(log):
    """("pass" | "fail" | "error", summary, [unproven points]) from the Yosys log of a check."""
    if "Equivalence successfully proven" in log:
        m = re.findall(r"Of those cells (\d+) are proven", log)
        return "pass", f"equivalent: {m[-1] if m else 'all'} points proven", []
    m = re.findall(r"Of those cells (\d+) are proven and (\d+) are unproven", log)
    if m:
        proven, unproven = m[-1]
        names = sorted(set(re.findall(r"Trying to prove \$equiv for (\S+): failed", log)))
        return "fail", f"not proven: {unproven} of {int(proven) + int(unproven)} points differ", names
    err = re.search(r"^ERROR: (.*)$", log, re.M)
    return "error", (err.group(1)[:240] if err else "the check stopped without a result: see the output"), []


def stage_netlist_script(odb, out, libs=()):
    """OpenROAD commands that write a stage's database out as a Verilog netlist."""
    reads = "".join(f"read_liberty {{{lib}}}\n" for lib in libs)
    return f"{reads}read_db {{{odb}}}\nwrite_verilog {{{out}}}\n"


def inputs(eff, resolve):
    """What the checks read, from the config's variables: {"top", "rtl", "defines", "incs", "libs",
    "synth", "final", "results"}, paths as Path. resolve turns a value into a path."""
    res = resolve(eff["RESULTS_DIR"]) if eff.get("RESULTS_DIR") else None
    return {"top": eff.get("DESIGN_NAME", ""),
            "rtl": [Path(f) for f in eff.get("VERILOG_FILES", "").split()],
            "defines": [d[2:] if d.startswith("-D") else d for d in eff.get("VERILOG_DEFINES", "").split()],
            "incs": eff.get("VERILOG_INCLUDE_DIRS", "").split(),
            "libs": [Path(f) for f in eff.get("LIB_FILES", "").split()],
            "results": res,
            "synth": res / "1_2_yosys.v" if res else None,
            "final": res / "6_final.v" if res else None}


def missing(check, info):
    """Why a check can't run yet, or None."""
    if not info["top"]:
        return "The config has no DESIGN_NAME."
    if check in ("rtl_synth", "synth_final", "synth_stage") and not (info["synth"] and info["synth"].exists()):
        return "Run synthesis first: the check needs 1_2_yosys.v."
    if check == "rtl_synth" and not info["rtl"]:
        return "The config has no VERILOG_FILES."
    if check == "synth_final" and not (info["final"] and info["final"].exists()):
        return "Run the flow to the end first: the check needs 6_final.v."
    if not info["libs"]:
        return "The config has no LIB_FILES: the netlist cells have no function to compare."
    return None
