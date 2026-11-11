"""Unit tests for the parts of ORFS Flow Runner that read and write design files. No display is needed.

Run from the repository root:  python3 -m unittest discover -s gui/tests -p 'test_units.py' -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

GUI = Path(__file__).resolve().parents[1]
FLOW = GUI.parent / "orfs" / "flow"
sys.path.insert(0, str(GUI))

import fastroute  # noqa: E402
import floorplan as fp  # noqa: E402
import io_pins  # noqa: E402
import lef  # noqa: E402
import new_design  # noqa: E402
import pdn  # noqa: E402
import sdc_clocks as sc  # noqa: E402
import sdc_timing as st  # noqa: E402
from inputs_tab import set_mk_vars  # noqa: E402
from stage_forms import check, first_sentence  # noqa: E402

NANGATE = FLOW / "platforms" / "nangate45"
NANGATE_TECH = NANGATE / "lef" / "NangateOpenCellLibrary.tech.lef"
NANGATE_LAYERS = [f"metal{i}" for i in range(1, 11)]


class LefTest(unittest.TestCase):
    def test_routing_layers_in_order(self):
        layers = lef.routing_layers(NANGATE_TECH)
        self.assertEqual([x["name"] for x in layers], NANGATE_LAYERS)
        self.assertEqual(layers[0]["direction"], "horizontal")
        self.assertAlmostEqual(layers[1]["pitch"], 0.19)

    def test_macro_sizes_skip_standard_cells(self):
        sizes = lef.macro_sizes(sorted((NANGATE / "lef").glob("fakeram45_*.lef")))
        self.assertEqual(sizes["fakeram45_32x32"], (19.57, 40.6))
        self.assertEqual(lef.macro_sizes([NANGATE / "lef" / "NangateOpenCellLibrary.macro.mod.lef"]), {})

    def test_site_size(self):
        self.assertEqual(lef.site_size([NANGATE_TECH], "FreePDK45_38x28_10R_NP_162NW_34O"), (0.19, 1.4))
        self.assertIsNone(lef.site_size([NANGATE_TECH], "nope"))

    def test_missing_file_is_empty(self):
        self.assertEqual(lef.routing_layers("/no/such.lef"), [])


class TimingBlockTest(unittest.TestCase):
    def model(self):
        m = st.empty()
        m["clocks"]["core_clock"] = {"setup_uncertainty": "0.05", "hold_uncertainty": "0.02", "transition": "0.1",
                                     "source_latency": "0.2", "network_latency": "0.1"}
        m["rules"] = {"driving_cell": "BUF_X2", "driving_pin": "Z", "load": "0.01", "max_transition": "0.5",
                      "max_fanout": "20", "max_capacitance": "0.2"}
        m["exceptions"] = [
            {"type": "false_path", "value": "", "check": "", "from": ("ports", "reset"), "through": None,
             "to": None},
            {"type": "multicycle", "value": "2", "check": "setup", "from": ("clocks", "core_clock"),
             "through": ("pins", "u1/A u2/*"), "to": ("cells", "r*")},
            {"type": "max_delay", "value": "1.5", "check": "", "from": None, "through": None,
             "to": ("ports", "out*")},
            {"type": "min_delay", "value": "0.1", "check": "", "from": ("ports", "in"), "through": None,
             "to": None}]
        return m

    def test_round_trip(self):
        text = "create_clock -name core_clock -period 1 [get_ports clk]\n"
        out = st.set_block(text, self.model())
        back, unknown = st.parse(out)
        self.assertEqual(back, self.model())
        self.assertEqual(unknown, [])
        self.assertTrue(out.startswith(text))

    def test_rewrite_and_remove_keep_other_lines(self):
        text = "a\n" + "\n".join(st.render(self.model())) + "\nb\n"
        changed = self.model()
        changed["rules"]["max_fanout"] = "10"
        out = st.set_block(text, changed)
        self.assertIn("set_max_fanout 10 [current_design]", out)
        self.assertEqual(out.count(st.BEGIN), 1)
        self.assertEqual(st.set_block(out, st.empty()), "a\nb\n")

    def test_outside_lines_listed(self):
        text = "set_clock_latency 0.07 [get_clocks c]\n" + "\n".join(st.render(self.model()))
        self.assertEqual(st.outside(text), [(1, "set_clock_latency 0.07 [get_clocks c]")])

    def test_problems(self):
        self.assertEqual(st.problems(self.model(), ["core_clock"]), [])
        bad = self.model()
        bad["exceptions"][1]["value"] = "1.5"
        bad["exceptions"].append({"type": "false_path", "value": "", "check": "", "from": None, "through": None,
                                  "to": None})
        bad["rules"]["max_fanout"] = "2.5"
        probs = st.problems(bad, ["core_clock"])
        self.assertTrue(any("whole number" in p for p in probs))
        self.assertTrue(any("at least one of From" in p for p in probs))
        self.assertTrue(any("Max fanout" in p for p in probs))
        self.assertTrue(st.problems(self.model(), []))  # unknown clock

    def test_incomplete_exception_is_a_comment_and_comes_back(self):
        m = st.empty()
        m["exceptions"] = [{"type": "max_delay", "value": "", "check": "", "from": ("ports", ""), "through": None,
                            "to": None},
                           {"type": "false_path", "value": "", "check": "", "from": None, "through": None,
                            "to": None}]
        text = st.set_block("", m)
        self.assertTrue(all(ln.startswith(st.INCOMPLETE) for ln in text.split("\n")
                            if ln.startswith("set_") or ln.startswith("#  ") or "max_delay" in ln))
        self.assertNotIn("\nset_max_delay", text)
        self.assertEqual(st.parse(text), (m, []))
        self.assertTrue(st.problems(m, []))

    def test_unknown_line_in_block_reported(self):
        text = f"{st.BEGIN}\nset_false_path -from [all_inputs]\n{st.END}\n"
        self.assertEqual(st.parse(text)[1], ["set_false_path -from [all_inputs]"])


class FloorplanTest(unittest.TestCase):
    def test_estimate_matches_orfs_formula(self):
        die, core = fp.estimate(1000, 50, 2, 1)
        width, height = core[2] - core[0], core[3] - core[1]
        self.assertAlmostEqual(width * height, 2000)
        self.assertAlmostEqual(height / width, 2)
        self.assertAlmostEqual(die[2] - core[2], 1)

    def test_parse_box(self):
        self.assertEqual(fp.parse_box("0 0 10 20"), (0, 0, 10, 20))
        self.assertIsNone(fp.parse_box("0 0 10"))
        self.assertIsNone(fp.parse_box("10 0 0 20"))

    def test_macro_block_round_trip_with_escaped_names(self):
        placed = {"gen_sram[0].ram": (10.0, 20.5, "R0"), "b": (100.125, 5.0, "MX")}
        text = fp.set_block("# hand line\nplace_macro -macro_name {fixed} -location {1 2} -orientation R0\n",
                            placed)
        self.assertIn(r"{gen_sram\[0\].ram}", text)
        mine, theirs, other = fp.parse(text)
        self.assertEqual(mine, placed)
        self.assertEqual(theirs, {"fixed": (1.0, 2.0, "R0")})
        self.assertEqual(other, [])
        self.assertNotIn(fp.BEGIN, fp.set_block(text, {}))

    def test_problems(self):
        sizes, masters = {"M": (50, 50)}, {"a": "M", "b": "M"}
        self.assertEqual(fp.problems({"a": (0, 0, "R0"), "b": (60, 0, "R0")}, sizes, masters, (0, 0, 200, 200)), [])
        self.assertIn("a and b overlap",
                      fp.problems({"a": (0, 0, "R0"), "b": (40, 0, "R0")}, sizes, masters, (0, 0, 200, 200))[0])
        self.assertIn("halo", fp.problems({"a": (0, 0, "R0"), "b": (52, 0, "R0")}, sizes, masters,
                                          (0, 0, 200, 200), (5, 5))[0])
        self.assertIn("not inside", fp.problems({"a": (180, 0, "R0")}, sizes, masters, (0, 0, 200, 200))[0])

    def test_rotated_size(self):
        self.assertEqual(fp.macro_size((10, 20), "R90"), (20, 10))
        self.assertEqual(fp.macro_size((10, 20), "MX"), (10, 20))

    def test_core_rounded_to_sites_and_rows(self):
        # Measured in OpenROAD: CORE_AREA 10 10 60 60 on nangate45 gives this core.
        self.assertEqual(fp.snap_core((10, 10, 60, 60), (0.19, 1.4)), (10.07, 11.2, 59.85, 58.8))
        self.assertEqual(fp.snap_core((0.19, 1.4, 1.9, 14), (0.19, 1.4)), (0.19, 1.4, 1.9, 14.0))
        self.assertEqual(fp.snap_core((1, 2, 3, 4), None), (1, 2, 3, 4))

    def test_synth_area(self):
        self.assertEqual(fp.synth_area("   Chip area for module '\\gcd': 628.292000\n"), 628.292)
        self.assertIsNone(fp.synth_area(""))


class PdnTest(unittest.TestCase):
    def setUp(self):
        self.text = (NANGATE / "grid_strategy-M1-M4-M7.tcl").read_text()

    def test_untouched_script_is_unchanged(self):
        self.assertEqual(pdn.rebuild(self.text, pdn.parse(self.text)), self.text)

    def test_grids_and_problems(self):
        cmds = pdn.parse(self.text)
        self.assertEqual([g for g, _ in pdn.grids(cmds)], ["grid", "CORE_macro_grid_1", "CORE_macro_grid_2"])
        self.assertEqual(pdn.problems(cmds, NANGATE_LAYERS), [])

    def test_edit_add_and_delete(self):
        cmds = pdn.parse(self.text)
        stripes = [c for c in cmds if c["cmd"] == "add_pdn_stripe"]
        pdn.set_opt(stripes[1], "pitch", "40")
        stripes[2]["deleted"] = True
        ring = pdn.new_command("add_pdn_ring", "grid", layers="metal5 metal6", widths="2", spacings="1",
                               core_offsets="2")
        cmds.append(ring)
        pdn.connect_ring(cmds, ring, lef.routing_layers(NANGATE_TECH))
        out = pdn.rebuild(self.text, cmds)
        self.assertIn("-layer {metal4} -width {0.48} -pitch {40} -offset {2}", out)
        self.assertNotIn("-layer {metal7} -width {1.40}", out)
        lines = out.split("\n")
        ring = next(i for i, ln in enumerate(lines) if ln.startswith("add_pdn_ring"))
        self.assertIn("-grid {grid}", lines[ring - 1])  # added after the grid's last command
        again = pdn.parse(out)
        self.assertEqual(pdn.problems(again, NANGATE_LAYERS), [])

    def test_add_ring_connects_it(self):
        # The form that passed OpenROAD's IR drop connectivity check on gcd: straps extended to the ring, and
        # vias from each strap layer to the ring layer that crosses it.
        layers = lef.routing_layers(NANGATE_TECH)
        text = (FLOW / "designs/nangate45/gcd/grid_strategy-M1-M4-M7.tcl").read_text()
        cmds = pdn.parse(text)
        self.assertEqual(pdn.ring_layers_for(cmds, "grid", layers), "metal5 metal6")
        ring = pdn.new_command("add_pdn_ring", "grid", layers="metal5 metal6", widths="2", spacings="1",
                               core_offsets="2")
        cmds.append(ring)
        self.assertTrue(any("not be powered" in p for p in pdn.problems(cmds, NANGATE_LAYERS)))
        pdn.connect_ring(cmds, ring, layers)
        self.assertEqual(pdn.problems(cmds, NANGATE_LAYERS), [])
        out = pdn.rebuild(text, cmds)
        self.assertIn("-layer {metal4} -width {0.48} -pitch {28.0} -offset {2} -extend_to_core_ring", out)
        self.assertIn("add_pdn_connect -grid {grid} -layers {metal4 metal5}", out)
        self.assertIn("add_pdn_connect -grid {grid} -layers {metal7 metal6}", out)
        self.assertNotIn("-layer {metal1} -width {0.17} -pitch {2.4} -offset {0} -followpins -extend", out)

    def test_problems_found(self):
        cmds = pdn.parse(self.text)
        stripe = next(c for c in cmds if c["cmd"] == "add_pdn_stripe" and pdn.get(c, "layer") == "metal4")
        pdn.set_opt(stripe, "pitch", "0.1")
        pdn.set_opt(stripe, "layer", "metal99")
        probs = pdn.problems(cmds, NANGATE_LAYERS)
        self.assertTrue(any("not a routing layer" in p for p in probs))
        self.assertTrue(any("larger than the width" in p for p in probs))

    def test_words_keep_braces_and_brackets(self):
        self.assertEqual(pdn.words("a -x {b c} [d e] f"), ["a", "-x", "b c", "[d e]", "f"])

    def test_every_platform_script_round_trips(self):
        for path in (FLOW / "platforms").glob("*/**/*.tcl"):
            text = path.read_text(errors="replace")
            if "add_pdn_stripe" in text:
                with self.subTest(path=path.name):
                    self.assertEqual(pdn.rebuild(text, pdn.parse(text)), text)


class FastrouteTest(unittest.TestCase):
    def test_round_trip_and_edit(self):
        text = (NANGATE / "fastroute.tcl").read_text()
        rows = fastroute.parse(text)
        self.assertEqual(fastroute.rebuild(text, rows), text)
        rows[0]["value"] = "0.3"
        rows.append({"layers": "metal8", "value": "0.1"})
        out = fastroute.rebuild(text, rows)
        self.assertIn("set_global_routing_layer_adjustment metal2-metal3 0.3", out)
        self.assertIn("set_routing_layers -signal", out)
        self.assertEqual(len(fastroute.parse(out)), 3)

    def test_problems(self):
        self.assertEqual(fastroute.problems([{"layers": "metal2-metal3", "value": "0.5"}], NANGATE_LAYERS), [])
        probs = fastroute.problems([{"layers": "metal2-metalX", "value": "1.5"}], NANGATE_LAYERS)
        self.assertEqual(len(probs), 2)


class FormCheckTest(unittest.TestCase):
    def test_kinds(self):
        self.assertIsNone(check("int", "", None))
        self.assertIsNone(check("int", "5", (0, 10)))
        self.assertEqual(check("int", "5.5", None), "must be a whole number")
        self.assertEqual(check("float", "x", None), "must be a number")
        self.assertEqual(check("float", "1.5", (0.0, 1.0)), "must be at most 1")
        self.assertEqual(check("layer", "metal99", None, NANGATE_LAYERS), "is not a routing layer of this platform")
        self.assertIsNone(check("layer", "metal3", None, NANGATE_LAYERS))
        self.assertEqual(check("bool", "yes", None), "must be 0 or 1")

    def test_first_sentence(self):
        self.assertEqual(first_sentence("One thing. Two."), "One thing.")


class ConfigTest(unittest.TestCase):
    def test_set_mk_vars_replaces_appends_and_removes(self):
        text = "export A = 1\nexport B ?= 2\n"
        out = set_mk_vars(text, {"A": "3", "B": None, "C": "x # y"})
        self.assertIn("export A = 3", out)
        self.assertNotIn("B ?=", out)
        self.assertIn("export C = x \\# y", out)

    def test_empty_value_overrides(self):
        self.assertIn("export CORE_UTILIZATION = \n", set_mk_vars("", {"CORE_UTILIZATION": ""}))
        # "?=" would not override a value set before, so an empty value is written with "=".
        self.assertEqual(set_mk_vars("export CORE_UTILIZATION ?= 55\n", {"CORE_UTILIZATION": ""}),
                         "export CORE_UTILIZATION = \n")


class NewDesignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "top.v").write_text(
            "module leaf(input a, output y); assign y = a; endmodule\n"
            "module top(input clk, input rst, input d, output q);\n"
            "  reg r; always @(posedge clk) r <= d;\n  leaf u_leaf (.a(r), .y(q));\nendmodule\n")
        (self.tmp / "tb.v").write_text("module top_tb; top dut (.clk(1'b0)); endmodule\n")

    def test_top_is_the_module_nothing_uses(self):
        self.assertEqual(new_design.top_candidates([self.tmp / "top.v"]), ["top"])
        self.assertEqual(new_design.top_candidates([self.tmp / "top.v", self.tmp / "tb.v"]), ["top_tb"])
        self.assertEqual(set(new_design.modules([self.tmp / "top.v"])), {"leaf", "top"})

    def test_create_writes_config_sdc_and_copies(self):
        flow = self.tmp / "flow"
        cfg = new_design.create(flow, "demo", "nangate45", [self.tmp / "top.v"], "top", "clk", "5", 40)
        self.assertEqual(cfg, flow / "designs" / "nangate45" / "demo" / "config.mk")
        text = cfg.read_text()
        self.assertIn("export DESIGN_NAME = top", text)
        self.assertIn("$(DESIGN_HOME)/src/demo/top.v", text)
        self.assertIn("export CORE_UTILIZATION = 40", text)
        sdc = (cfg.parent / "constraint.sdc").read_text()
        self.assertIn("set clk_period 5", sdc)
        self.assertTrue((flow / "designs" / "src" / "demo" / "top.v").exists())
        res = sc.read_sdc(sdc, [("input", "clk"), ("input", "d"), ("output", "q")])
        self.assertEqual([c["name"] for c in res["clocks"]], ["clk"])
        with self.assertRaises(ValueError):
            new_design.create(flow, "demo", "nangate45", [self.tmp / "top.v"], "top", "clk", "5", 40)

    def test_create_refuses_bad_input(self):
        flow = self.tmp / "flow"
        for args in (("9bad", ["x"], "5"), ("ok", [], "5"), ("ok2", [self.tmp / "top.v"], "fast")):
            with self.assertRaises(ValueError):
                new_design.create(flow, args[0], "nangate45", args[1], "top", "clk", args[2], 50)

    def test_virtual_clock_without_clock_port(self):
        sdc = new_design.sdc_text("comb", "", "2")
        self.assertIn("create_clock -name $clk_name -period $clk_period\n", sdc)
        self.assertIn("[all_inputs]", sdc)


class IoPinsTest(unittest.TestCase):
    def test_render_parse(self):
        rows = [{"spec": "a[3:0]", "edge": "left", "interval": "0-50", "group": True, "order": True},
                {"spec": "b", "edge": "top", "interval": "*", "group": False, "order": False}]
        text = io_pins.set_block("", rows)
        self.assertEqual(io_pins.managed_rows(text), rows)


if __name__ == "__main__":
    unittest.main()
