"""GUI test of the Inputs pages: change every kind of input in the app, save, and check the files it wrote.

Works on throw-away copies of designs (names starting with test_), removed at the end. Run it off screen with
gui/tests/run_gui_tests.sh gui/tests/test_gui_inputs.py. Screenshots go to $SHOTS when it is set.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

FLOW = hn.KUL_FLOW
GCD, RAM = "test_gcd", "test_ram2"
NEW = "test_new"


def row_path(store, col, value):
    return next(str(r.path) for r in store if r[col] == value)


def main():
    hn.temp_design(FLOW, "nangate45", "gcd", GCD)
    hn.macro_design(FLOW, RAM)
    h = hn.Harness(FLOW, shots=os.environ.get("SHOTS"))
    try:
        run(h)
    finally:
        for name in (GCD, RAM, NEW):
            hn.remove_design(FLOW, "nangate45", name)
    h.done()


def run(h):
    inp, app = h.inp, h.app
    env = app.make_env()
    for name in (GCD, RAM):
        code, out = hn.run_make(FLOW, f"designs/nangate45/{name}/config.mk", "synth", env)
        h.check(f"{name} synthesizes", code == 0, out)
    h.check("gcd copy selected", h.select("nangate45", GCD, "config.mk"))
    ddir = FLOW / "designs" / "nangate45" / GCD
    cfg = ddir / "config.mk"
    pages = {p.name: p for p in inp.pages}

    # ---- Synthesis form: a check box becomes a pending setting, shown in All settings, and Revert drops it.
    synth = h.page("synth")
    box = synth.fields["ABC_AREA"][2]
    h.check("ABC_AREA shown on", box.get_active())
    box.set_active(False)
    h.check("unchecking makes ABC_AREA=0 pending", inp.pending.get("ABC_AREA") == "0", inp.pending)
    h.check("All settings shows it", any(r[0] == "ABC_AREA" and r[1] == "0" and r[8] for r in inp.store))
    h.check("Run is blocked while unsaved", not app.run_btn.get_sensitive())
    synth.revert_btn.clicked()
    h.pump(0.3)
    h.check("Revert drops it", "ABC_AREA" not in inp.pending and box.get_active())

    # ---- Placement form: bad values are refused, good ones saved into the config.
    place = h.page("place")
    entry, error = place.fields["PLACE_DENSITY"][2], place.fields["PLACE_DENSITY"][4]
    entry.set_text("2")
    h.check("density 2 refused", "PLACE_DENSITY" not in inp.pending and "at most 1" in error.get_text(),
            error.get_text())
    entry.set_text("0.65")
    h.check("density 0.65 pending", inp.pending.get("PLACE_DENSITY") == "0.65")
    h.shot("inputs_placement")
    place.save_btn.clicked()
    h.check("placement saved", h.saved() and "export PLACE_DENSITY = 0.65" in cfg.read_text(), cfg.read_text())
    h.check("make sees the new density", inp.effective.get("PLACE_DENSITY") == "0.65")

    # ---- Routing: a layer that does not exist is refused; per-layer capacity makes a design copy of the file.
    route = h.page("route")
    route.fields["MIN_ROUTING_LAYER"][2].get_child().set_text("metal99")
    h.check("unknown layer refused", "MIN_ROUTING_LAYER" not in inp.pending)
    route.fields["MIN_ROUTING_LAYER"][2].get_child().set_text("metal2")
    h.check("FASTROUTE note at the field", "Not used" in route.fields["ROUTING_LAYER_ADJUSTMENT"][6].get_text())
    route.on_adj_edit(None, row_path(route.adj_store, 0, "metal2-metal3"), "0.4", 1)
    route.on_adj_add()
    last = str(len(route.adj_store) - 1)
    route.on_adj_edit(None, last, "metal9", 0)
    route.on_adj_edit(None, last, "0.1", 1)
    h.check("routing page unsaved", route.is_dirty())
    h.shot("inputs_routing")
    route.save_btn.clicked()
    fr = ddir / "fastroute.tcl"
    h.check("fastroute.tcl written in the design", h.saved() and fr.exists(), inp.pending)
    text = fr.read_text() if fr.exists() else ""
    h.check("adjustments in it", "set_global_routing_layer_adjustment metal2-metal3 0.4" in text
            and "set_global_routing_layer_adjustment metal9 0.1" in text and "set_routing_layers" in text, text)
    h.check("FASTROUTE_TCL points at it", inp.effective.get("FASTROUTE_TCL", "").endswith(f"{GCD}/fastroute.tcl"),
            inp.effective.get("FASTROUTE_TCL"))
    h.check("platform file untouched", "metal2-metal3 0.5" in (FLOW / "platforms/nangate45/fastroute.tcl")
            .read_text())

    # ---- Timing: clock settings, design rules and exceptions go into the SDC block.
    timing = h.page("timing")
    h.check("timing lists both clocks", [r[0] for r in timing.clk_store] == ["core_clock", "vclk_core_clock"],
            [r[0] for r in timing.clk_store])
    timing.on_clock_edit(None, row_path(timing.clk_store, 0, "core_clock"), "0.02", 1)
    timing.rule_entries["driving_cell"].set_text("BUF_X2")
    timing.rule_entries["driving_pin"].set_text("Z")
    timing.rule_entries["load"].set_text("0.005")
    timing.rule_entries["max_fanout"].set_text("16")
    timing.on_add()
    timing.on_cell(None, "0", "reset", 4)
    timing.on_add()
    timing.on_cell(None, "1", "max_delay", 0)
    h.check("half-done exception flagged", "needs a value" in timing.problem_label.get_text(),
            timing.problem_label.get_text())
    timing.save_btn.clicked()
    h.pump(0.5)
    h.check("save refused while it is half done", any("problems" in m for m in h.messages) and inp.sdc_dirty(),
            h.messages[-1:])
    timing.on_cell(None, "1", "1.0", 1)
    timing.on_cell(None, "1", "", 3)  # no From: a max delay to the outputs
    timing.on_cell(None, "1", "ports", 7)
    timing.on_cell(None, "1", "resp_msg*", 8)
    h.check("no problems now", timing.problem_label.get_text() == "", timing.problem_label.get_text())
    h.check("outside lines listed", "set_clock_latency" in timing.outside_label.get_text())
    h.shot("inputs_timing")
    timing.save_btn.clicked()
    sdc = ddir / "constraint.sdc"
    h.check("SDC saved", h.saved())
    text = sdc.read_text()
    for line in ("set_clock_uncertainty -setup 0.02 [get_clocks {core_clock}]",
                 "set_driving_cell -lib_cell BUF_X2 -pin Z [all_inputs -no_clocks]",
                 "set_load 0.005 [all_outputs]", "set_max_fanout 16 [current_design]",
                 "set_false_path -from [get_ports {reset}]", "set_max_delay 1.0 -to [get_ports {resp_msg*}]"):
        h.check(f"SDC has: {line}", line in text, text[-800:])
    inp.stack.set_visible_child_name("sdc")
    inp.sdc_check.clicked()
    h.check("OpenROAD accepts the SDC", h.wait(lambda: "Checked against" in inp.check_label.get_text(), 180)
            and "ERROR" not in inp.check_label.get_text(), inp.check_label.get_text())

    # ---- Power grid: edit a strap, add a ring; gcd has its own PDN file, edited in place.
    power = h.page("pdn")
    h.check("standard cell grid shown", power.grid_cb.get_active_id() == "grid")
    power.on_edit(None, row_path(power.store, 2, "metal4"), "20.0", 4)
    power.on_add_ring()
    h.check("ring on the free layers below the top straps", any(r[1] == "ring" and r[2] == "metal5 metal6"
                                                                 for r in power.store), [list(r) for r in power.store])
    h.check("ring connected to the straps", any(r[1] == "vias" and r[2] == "metal7 metal6" for r in power.store)
            and any(r[1] == "vias" and r[2] == "metal4 metal5" for r in power.store))
    h.check("no PDN problems", "None." in power.problem_label.get_text(), power.problem_label.get_text())
    h.shot("inputs_power_grid")
    power.save_btn.clicked()
    pdn_file = ddir / "grid_strategy-M1-M4-M7.tcl"
    ok = h.saved()
    h.check("PDN saved in place", ok and "-pitch {20.0}" in pdn_file.read_text()
            and "add_pdn_ring -grid {grid} -layers {metal5 metal6}" in pdn_file.read_text()
            and "-offset {2} -extend_to_core_ring" in pdn_file.read_text(),
            f"saved={ok} dirty={[p.name for p in inp.pages if p.is_dirty()]} pending={inp.pending} "
            f"sdc_dirty={inp.sdc_dirty()} messages={h.messages[-2:]}\n" + pdn_file.read_text()[-600:])

    # ---- Floorplan: estimate from synthesis, then switch to a fixed die and core.
    floor = h.page("floorplan")
    h.check("estimate from synthesis area", "Synthesis area" in floor.estimate_label.get_text(),
            floor.estimate_label.get_text())
    floor.mode_buttons["fixed"].set_active(True)
    h.pump(0.3)
    h.check("fixed mode drops this config's utilization", "CORE_UTILIZATION" in inp.pending
            and inp.pending["CORE_UTILIZATION"] is None and floor.mode() == "fixed", inp.pending)
    floor.fields["DIE_AREA"][2].set_text("0 0 70 70")
    floor.fields["CORE_AREA"][2].set_text("5 5 65 65")
    h.shot("inputs_floorplan_gcd")
    floor.save_btn.clicked()
    text = cfg.read_text()
    h.check("die and core saved", h.saved() and "export DIE_AREA = 0 0 70 70" in text
            and "CORE_UTILIZATION" not in text, text)
    h.check("make sees one floorplan method", not inp.effective.get("CORE_UTILIZATION")
            and inp.effective.get("DIE_AREA") == "0 0 70 70")

    # ---- Macros: the RAM design's two macros, placed on the page and saved.
    h.check("macro design selected", h.select("nangate45", RAM, "config.mk"))
    floor = h.page("floorplan")
    h.check("both macros listed", h.wait(lambda: {r[0] for r in floor.store} == {"ram0", "ram1"}),
            [list(r) for r in floor.store])
    h.check("macro size from LEF", all(r[2] != "?" for r in floor.store))
    sel = floor.tv.get_selection()
    sel.select_all()
    floor.on_place()
    floor.on_xy(None, row_path(floor.store, 0, "ram0"), "20", 3)
    floor.on_xy(None, row_path(floor.store, 0, "ram0"), "20", 4)
    floor.on_xy(None, row_path(floor.store, 0, "ram1"), "100", 3)
    floor.on_xy(None, row_path(floor.store, 0, "ram1"), "20", 4)
    floor.on_orient(row_path(floor.store, 0, "ram1"), "MY")
    h.check("no macro problems", floor.problem_label.get_text() == "", floor.problem_label.get_text())
    floor.on_xy(None, row_path(floor.store, 0, "ram1"), "30", 3)
    h.check("overlap found", "overlap" in floor.problem_label.get_text())
    floor.on_xy(None, row_path(floor.store, 0, "ram1"), "100", 3)
    h.shot("inputs_floorplan_macros")
    floor.save_btn.clicked()
    mfile = FLOW / "designs" / "nangate45" / RAM / "macro_placement.tcl"
    h.check("macro file written", h.saved() and mfile.exists())
    text = mfile.read_text() if mfile.exists() else ""
    h.check("macro lines", "place_macro -macro_name {ram0} -location {20 20} -orientation R0" in text
            and "place_macro -macro_name {ram1} -location {100 20} -orientation MY" in text, text)
    h.check("MACRO_PLACEMENT_TCL set", inp.effective.get("MACRO_PLACEMENT_TCL", "").endswith("macro_placement.tcl"))

    # ---- Power grid of a design on the platform's file: the save makes a copy for the design.
    power = h.page("pdn")
    power.on_edit(None, row_path(power.store, 2, "metal7"), "1.2", 3)
    power.save_btn.clicked()
    copy = FLOW / "designs" / "nangate45" / RAM / "pdn.tcl"
    h.check("PDN copy made for the design", h.saved() and copy.exists()
            and "-layer {metal7} -width {1.2}" in copy.read_text())
    h.check("PDN_TCL points at the copy", inp.effective.get("PDN_TCL", "").endswith(f"{RAM}/pdn.tcl"))
    h.check("platform PDN untouched", "-layer {metal7} -width {1.40}" in
            (FLOW / "platforms/nangate45/grid_strategy-M1-M4-M7.tcl").read_text())

    # ---- Undo save takes the PDN change back (copy removed, PDN_TCL back to the platform's).
    inp.undo_save()
    h.check("undo removes the copy", h.wait(lambda: not copy.exists() and "platforms/nangate45" in
                                            inp.effective.get("PDN_TCL", "")), inp.effective.get("PDN_TCL"))

    # ---- New design from RTL files.
    src = FLOW / "designs" / "src" / "gcd" / "gcd.v"

    def fill(dlg):
        dlg.name.set_text(NEW)
        dlg.platform.set_active_id("nangate45")
        dlg.set_files([src])
        dlg.period.set_text("2.5")
        h.check("wizard finds the top module", dlg.top.get_active_id() == "gcd", dlg.top.get_active_id())
        h.check("wizard finds the clock", dlg.clock.get_active_id() == "clk", dlg.clock.get_active_id())
        h.shot("new_design", dlg)
    h.answer_next_dialog(fill)
    from gi.repository import GLib
    GLib.idle_add(app.on_new_design)
    made = FLOW / "designs" / "nangate45" / NEW / "config.mk"
    h.check("new design created and selected", h.wait(lambda: made.exists() and inp.cfg ==
                                                       f"designs/nangate45/{NEW}/config.mk", 60), inp.cfg)
    h.check("its clock is read", h.wait(lambda: "clk" in inp.clock_names()), inp.clock_names())
    h.check("its sources were copied", (FLOW / "designs" / "src" / NEW / "gcd.v").exists())
    pages["synth"].load()


if __name__ == "__main__":
    main()
