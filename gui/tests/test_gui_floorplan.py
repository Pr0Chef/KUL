"""The Floorplan planner tab, off screen, on two designs in a throw-away work area.

A tiny counter: at the default 50% utilization the planner reports that the power straps do not fit (the cause of
OpenROAD's PDN-0185) and offers the utilization that fixes it; the fix is applied, written to the design, and the
real floorplan stage then passes. A design with two RAM macros and a fixed die: the macros are placed legally,
dragging one onto the other is reported, and Apply writes the macro placement file.

Set SHOTS=<folder> to save screenshots. About two minutes (two synthesis runs and one floorplan run).
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

import new_design  # noqa: E402
import orfs_app  # noqa: E402
import workspace as ws  # noqa: E402

COUNTER = """module counter (input clk, input rst, input en, output reg [7:0] q, output wrap);
  assign wrap = &q;
  always @(posedge clk)
    if (rst) q <= 8'd0;
    else if (en) q <= q + 8'd1;
endmodule
"""
tmp = Path(tempfile.mkdtemp(prefix="fplan-gui-"))
area = tmp / "area"
ws.create_work_area(area, hn.KUL_FLOW)
proj = ws.new_project(area, "counter", "counter")
(proj.dir / "counter.v").write_text(COUNTER)
new_design.create(area, "counter", "nangate45", [proj.dir / "counter.v"], "counter", "clk", "5", 50, copy=False)
hn.macro_design(area, "test_rams")

h = hn.Harness(area, shots=os.environ.get("SHOTS"))
app = h.app
app.resize(1440, 900)
env = orfs_app.flow_env(app.tools())
plan = app.planner


def level(title):
    return next((c["level"] for c in plan.checks if c["title"].startswith(title)), None)


# ---------------- the counter: power straps do not fit at 50% ----------------
h.check("counter design selected", h.select("nangate45", "counter", "config.mk"))
rc, out = hn.run_make(area, "designs/nangate45/counter/config.mk", "synth", env)
h.check("synthesis ran", rc == 0, out[-800:])
h.select("nangate45", "counter", "config.mk")
app.on_config()
h.wait(lambda: app.inputs.effective.get("DESIGN_NAME") == "counter" and not app.inputs.is_dirty(), 60)
h.pump(1.0)
app.show_page("floorplan")
h.pump(0.5)
plan.refresh(force=True)
h.check("cell area comes from synthesis", plan.area_from_synth and 40 < plan.s["area"] < 400, plan.s)
h.check("plan loaded from the design", plan.s["mode"] == "util" and plan.s["util"] == 50, plan.s)
h.check("pins counted from the ports", plan.pins == 12, plan.pins)
h.check("strap layers read from the power grid script", [s["layer"] for s in plan.straps] == ["metal4", "metal7"],
        plan.straps)
h.check("power straps reported as not fitting", level("Power straps") == "error", plan.checks)
h.shot("f1_straps_do_not_fit")
fix = next(c["fix"] for c in plan.checks if c["title"].startswith("Power straps"))
h.check("a utilization that fixes it is offered", fix and 1 <= fix["set"]["util"] < 50, fix)
plan.apply_fix(fix)
h.check("with the fix the straps fit", level("Power straps") == "ok" and plan.util.get_value() == fix["set"]["util"],
        plan.checks)
h.check("the plan is marked as not applied yet", plan.dirty and "Apply" in plan.state.get_text())
h.check("apply writes the plan", plan.apply() and h.saved(60))
cfg = (area / "designs/nangate45/counter/config.mk").read_text()
h.check("CORE_UTILIZATION saved in the config", f"CORE_UTILIZATION = {fix['set']['util']:g}" in cfg, cfg)
plan.run()
h.check("floorplan run started", h.wait(lambda: app.job is not None and app.job.running(), 30))
h.check("the floorplan stage passes with the planned size", h.wait(lambda: not app.job.running(), 600)
        and app.job.rc == 0, f"exit {app.job.rc}, log {app.job.log_path}")
h.check("floorplan stage is done", app.stage_done()["floorplan"])
app.show_page("floorplan")
h.check("the planner reads the finished run", h.wait(lambda: plan.last_run is not None, 60))
real = plan.last_run[1]
h.check("the real core is the size the planner drew", real and abs((real[2] - real[0]) - plan.plan["core_w"]) < 1.5,
        (real, plan.plan["core"]))
h.shot("f2_planned_and_built")

# ---------------- what-if: a fixed die ----------------
plan.set(mode="fixed")
h.check("switching to a fixed die starts from the planned die", abs(plan.s["die_w"] - plan.plan["die"][2]) < 0.2)
plan.set(die_w=20.0, die_h=20.0)
h.check("a die too small fails the checks with a fix", level("Power straps") == "error"
        and next(c["fix"] for c in plan.checks if c["title"].startswith("Power straps")) is not None)
plan.apply_fix(next(c["fix"] for c in plan.checks if c["title"].startswith("Power straps")))
h.check("growing the die fits the straps", level("Power straps") == "ok", (plan.s, plan.checks))
plan.refresh(force=True)
h.check("reset drops the what-if", plan.s["mode"] == "util" and not plan.dirty)

# ---------------- macros ----------------
h.check("macro design selected", h.select("nangate45", "test_rams", "config.mk"))
rc, out = hn.run_make(area, "designs/nangate45/test_rams/config.mk", "synth", env)
h.check("macro design synthesized", rc == 0, out[-800:])
app.on_config()
h.wait(lambda: app.inputs.effective.get("DESIGN_NAME") == "ram2" and not app.inputs.is_dirty(), 60)
h.pump(1.5)
app.show_page("floorplan")
plan.refresh(force=True)
h.check("fixed die read from the design", plan.s["mode"] == "fixed" and (plan.s["die_w"], plan.s["margin"])
        == (200, 10), plan.s)
h.check("macros found in the netlist", sorted(plan.masters) == ["ram0", "ram1"], plan.masters)
plan.place_macros()
h.check("both macros placed inside the core without problems", len(plan.placements) == 2
        and not [c for c in plan.checks if c["title"] == "Macro placement"], (plan.placements, plan.checks))
h.pump(0.5)


def canvas(x, y):
    scale, ox, oy = plan.view
    return NS(x=ox + x * scale, y=oy - y * scale, button=1)


a, b = plan.placements["ram0"], plan.placements["ram1"]
w, hgt = plan.sizes[plan.masters["ram0"]]
plan.on_press(plan.canvas, canvas(a[0] + w / 2, a[1] + hgt / 2))
plan.on_motion(plan.canvas, canvas(b[0] + w / 2 + 3, b[1] + hgt / 2 + 3))
plan.on_release()
h.check("dragging a macro onto another is reported", any("overlap" in c["detail"] for c in plan.checks),
        plan.checks)
h.shot("f3_macros_overlap")
plan.on_press(plan.canvas, canvas(plan.placements["ram0"][0] + 2, plan.placements["ram0"][1] + 2))
plan.on_motion(plan.canvas, canvas(120 + 2, 100 + 2))
plan.on_release()
h.check("dragged clear, the placement is legal again", not [c for c in plan.checks if c["title"] == "Macro placement"],
        plan.checks)
x, y, _o = plan.placements["ram0"]
h.check("a dragged macro sits on the placement grid", abs((y - plan.plan["core"][1]) / 1.4
                                                         - round((y - plan.plan["core"][1]) / 1.4)) < 1e-6, (x, y))
h.answer_next_dialog(lambda dlg: None)       # the Inputs page may ask to confirm; accept
h.check("apply writes the macro placement", plan.apply() and h.saved(60))
h.dialogs.clear()
tcl = area / "designs/nangate45/test_rams/macro_placement.tcl"
h.check("macro placement file has both macros", tcl.exists() and tcl.read_text().count("place_macro") == 2,
        tcl.read_text() if tcl.exists() else "missing")
h.check("the config points at it", "MACRO_PLACEMENT_TCL" in (area / "designs/nangate45/test_rams/config.mk").read_text())
plan.set(die_w=60.0, die_h=60.0)
plan.place_macros()
h.check("macros that do not fit are refused with a message", any("do not fit" in m for m in h.messages), h.messages)
h.shot("f4_macros")
h.done()
