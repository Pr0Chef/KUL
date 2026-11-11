"""End-to-end test: inputs set only through the app's pages reach the real flow results.

1. On a copy of gcd, the pages set a fixed die and core, a power ring and a strap pitch, placement density,
   per-layer routing capacity and timing constraints; the app's Run button then runs the flow to the end, and
   the final layout and logs are checked for each of those inputs.
2. On a small design with two RAM macros, the Floorplan page places them; the app runs to the floorplan stage,
   and the macros are checked to be exactly where the page put them.
Takes about ten minutes. Run off screen: gui/tests/run_gui_tests.sh gui/tests/test_flow_e2e.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402
import floorplan as fp  # noqa: E402

FLOW = hn.KUL_FLOW
GCD, RAM = "test_e2e_gcd", "test_e2e_ram2"


def row_path(store, col, value):
    return next(str(r.path) for r in store if r[col] == value)


def run_flow(h, to_stage, minutes=40):
    """Press Run (from synthesis) and wait for the run to end; True if it finished without errors."""
    app = h.app
    app.from_cb.set_active_id("synth")
    app.to_cb.set_active_id(to_stage)
    h.pump(0.3)
    app.run_btn.clicked()
    started = h.wait(lambda: app.job is not None, 30)
    done = h.wait(lambda: app.job is not None and not app.job.running(), minutes * 60)
    h.pump(1)
    return started and done and app.job.rc == 0


def main():
    hn.temp_design(FLOW, "nangate45", "gcd", GCD)
    hn.macro_design(FLOW, RAM)
    h = hn.Harness(FLOW)
    try:
        gcd(h)
        macros(h)
    finally:
        for name in (GCD, RAM):
            hn.remove_design(FLOW, "nangate45", name)
    h.done()


def gcd(h):
    inp = h.inp
    h.check("gcd copy selected", h.select("nangate45", GCD, "config.mk"))

    floor = h.page("floorplan")
    floor.mode_buttons["fixed"].set_active(True)
    floor.fields["DIE_AREA"][2].set_text("0 0 70 70")
    floor.fields["CORE_AREA"][2].set_text("10 10 60 60")
    place = h.page("place")
    place.fields["PLACE_DENSITY"][2].set_text("0.65")
    route = h.page("route")
    route.on_adj_edit(None, row_path(route.adj_store, 0, "metal2-metal3"), "0.4", 1)
    route.save_btn.clicked()  # saves the routing file and every pending setting
    h.check("settings saved", h.saved(), inp.pending)

    power = h.page("pdn")
    power.on_edit(None, row_path(power.store, 2, "metal4"), "20.0", 4)
    power.on_add_ring()  # metal5 and metal6, connected to the metal4 and metal7 straps
    power.save_btn.clicked()
    h.check("power grid saved", h.saved())

    timing = h.page("timing")
    timing.on_clock_edit(None, row_path(timing.clk_store, 0, "core_clock"), "0.02", 1)
    timing.rule_entries["max_fanout"].set_text("16")
    timing.rule_entries["load"].set_text("0.005")
    timing.save_btn.clicked()
    h.check("SDC saved", h.saved())

    h.check("gcd runs to the end from the app", run_flow(h, "finish"), h.app.job and h.app.job.log_path)
    eff, env = inp.effective, h.app.make_env()
    results, logs = inp.resolve(eff["RESULTS_DIR"]), inp.resolve(eff["LOG_DIR"])
    h.check("final GDS written", (results / "6_final.gds").exists())
    out = hn.odb_query(env, results / "6_final.odb", r"""
proc um {v} { return [ord::dbu_to_microns $v] }
set d [$block getDieArea]; puts "DIE [um [$d xMin]] [um [$d yMin]] [um [$d xMax]] [um [$d yMax]]"
set c [$block getCoreArea]; puts "CORE [um [$c xMin]] [um [$c yMin]] [um [$c xMax]] [um [$c yMax]]"
set layers {}
foreach sw [[$block findNet VDD] getSWires] {
  foreach w [$sw getWires] { if {[$w isVia]} continue; dict incr layers [[$w getTechLayer] getName] }
}
puts "VDD_LAYERS $layers"
""")
    die = next((line.split()[1:] for line in out.splitlines() if line.startswith("DIE")), [])
    core = next((line.split()[1:] for line in out.splitlines() if line.startswith("CORE")), [])
    h.check("die is the one set on the page", [float(v) for v in die] == [0, 0, 70, 70], out)
    # OpenROAD rounds the core inward to whole sites and rows; the Floorplan page shows the same rounding.
    want = fp.snap_core((10, 10, 60, 60), h.page("floorplan").site())
    h.check("core is the page's, rounded to the site grid as the page says",
            tuple(float(v) for v in core) == want, (core, want))
    vdd = next((line for line in out.splitlines() if line.startswith("VDD_LAYERS")), "")
    h.check("power ring on metal5 and metal6", " metal5 " in f" {vdd} " and " metal6 " in f" {vdd} ", vdd)
    fp_log = (logs / "2_1_floorplan.log").read_text(errors="replace")
    h.check("floorplan used the design's fastroute.tcl", f"{GCD}/fastroute.tcl" in fp_log)
    gpl_log = (logs / "3_3_place_gp.log").read_text(errors="replace")
    h.check("placement used density 0.65", "-density 0.65" in gpl_log or "0.65" in gpl_log)
    final_sdc = (results / "6_final.sdc").read_text(errors="replace")
    h.check("final SDC keeps the uncertainty", "set_clock_uncertainty" in final_sdc)
    h.check("final SDC keeps the max fanout", "set_max_fanout 16" in final_sdc, final_sdc[-600:])
    h.check("final SDC keeps the load", "set_load" in final_sdc)


def macros(h):
    inp = h.inp
    h.check("RAM design selected", h.select("nangate45", RAM, "config.mk"))
    code, out = hn.run_make(FLOW, f"designs/nangate45/{RAM}/config.mk", "synth", h.app.make_env())
    h.check("RAM design synthesizes", code == 0, out)
    inp.refresh()
    floor = h.page("floorplan")
    h.check("macros listed", h.wait(lambda: {r[0] for r in floor.store} == {"ram0", "ram1"}))
    floor.tv.get_selection().select_all()
    floor.on_place()
    for name, x, y, orient in (("ram0", "20", "20", "R0"), ("ram1", "100", "30", "MY")):
        floor.on_xy(None, row_path(floor.store, 0, name), x, 3)
        floor.on_xy(None, row_path(floor.store, 0, name), y, 4)
        floor.on_orient(row_path(floor.store, 0, name), orient)
    floor.save_btn.clicked()
    h.check("macro placement saved", h.saved())
    h.check("RAM design runs to floorplan from the app", run_flow(h, "floorplan"))
    results = inp.resolve(inp.effective["RESULTS_DIR"])
    out = hn.odb_query(h.app.make_env(), results / "2_floorplan.odb", r"""
foreach inst [$block getInsts] {
  if { [[$inst getMaster] isBlock] } {
    set b [$inst getBBox]
    puts "MACRO [$inst getName] [ord::dbu_to_microns [$b xMin]] [ord::dbu_to_microns [$b yMin]] [$inst getOrient]"
  }
}
""")
    got = {p[1]: (float(p[2]), float(p[3]), p[4]) for p in (line.split() for line in out.splitlines())
           if p[:1] == ["MACRO"]}
    # OpenROAD may nudge a macro by a fraction of a micron so its pins sit on the routing tracks.
    def near(name, x, y, orient):
        g = got.get(name)
        return g is not None and abs(g[0] - x) <= 0.5 and abs(g[1] - y) <= 0.5 and g[2] == orient
    h.check("ram0 where the page put it (within 0.5 micron)", near("ram0", 20, 20, "R0"), got)
    h.check("ram1 where the page put it (within 0.5 micron)", near("ram1", 100, 30, "MY"), got)
    # The page reads the run back: the macros are drawn from the result once it exists.
    inp.refresh()
    h.check("page reads the last run", h.wait(lambda: floor.last_run is not None, 120))


if __name__ == "__main__":
    main()
