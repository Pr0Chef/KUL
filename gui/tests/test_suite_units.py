"""Unit tests for the RTL, verification, FPGA, LEC, diagram and assistant modules. No display is needed.

Tests marked "tool" run the real open-source tools (Icarus, Verilator, Yosys, SymbiYosys) and are skipped when
OSS CAD Suite is not installed. Run from the repository root:
    python3 -m unittest discover -s gui/tests -p 'test_suite_units.py' -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

GUI = Path(__file__).resolve().parents[1]
FLOW = GUI.parent / "orfs" / "flow"
sys.path.insert(0, str(GUI))

import agent  # noqa: E402
import cdc  # noqa: E402
import coverage  # noqa: E402
import designcheck as dc  # noqa: E402
import diagram as dg  # noqa: E402
import eda_tools  # noqa: E402
import floorplan as fp  # noqa: E402
import formal  # noqa: E402
import fplan  # noqa: E402
import fpga  # noqa: E402
import hdl  # noqa: E402
import lec  # noqa: E402
import lint  # noqa: E402
import regress  # noqa: E402
import signoff  # noqa: E402
import sim  # noqa: E402
import vcd  # noqa: E402
import workspace as ws  # noqa: E402
from runner import run_sync  # noqa: E402

COUNTER = """module cnt #(parameter W = 4) (input clk, input rst_n, input en, output reg [W-1:0] q,
                              output wrap);
  assign wrap = &q;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) q <= 0;
    else if (en) q <= q + 1'b1;
endmodule
"""
TOP = """module top (input clk, input rst_n, output [3:0] led);
  wire w;
  cnt #(.W(4)) u_cnt (.clk(clk), .rst_n(rst_n), .en(1'b1), .q(led), .wrap(w));
endmodule
"""
HAVE_TOOLS = all(eda_tools.find(t) for t in ("iverilog", "verilator", "yosys", "sby"))


def mods_of(text):
    return {m.name: m for m in hdl.parse_text(text)}


def project_with(files):
    area = Path(tempfile.mkdtemp(prefix="suite-units-")) / "area"
    ws.create_work_area(area, FLOW)
    p = ws.new_project(area, "demo", "top")
    for name in list(p.dir.iterdir()):
        if name.suffix in (".v", ".sv"):
            name.unlink()
    for name, text in files.items():
        (p.dir / name).parent.mkdir(parents=True, exist_ok=True)
        (p.dir / name).write_text(text)
    p["top"] = "top"
    p.save()
    return ws.Project(area, "demo")


class HdlTest(unittest.TestCase):
    def setUp(self):
        self.mods = mods_of(COUNTER + TOP)

    def test_ports_params_instances(self):
        cnt = self.mods["cnt"]
        self.assertEqual([n for _, n, _ in cnt.ports], ["clk", "rst_n", "en", "q", "wrap"])
        self.assertEqual(cnt.params[0][0], "W")
        self.assertEqual(self.mods["top"].instances[0][:2], ("cnt", "u_cnt"))

    def test_tops_and_hierarchy(self):
        self.assertEqual(hdl.tops(self.mods), ["top"])
        tree = hdl.hierarchy(self.mods, "top")
        self.assertEqual(tree["children"][0]["module"], "cnt")

    def test_clock_and_active_low_reset(self):
        self.assertEqual(hdl.clock_and_reset(self.mods["cnt"]), ("clk", "rst_n", True))

    def test_testbench_and_formal_wrapper_parse(self):
        tb = hdl.testbench(self.mods["cnt"])
        self.assertIn("localparam W", tb)
        self.assertIn("$dumpfile", tb)
        wrapper = hdl.formal_wrapper(self.mods["cnt"])
        self.assertIn("module cnt_formal", wrapper)
        self.assertIn("cnt_formal", mods_of(wrapper))

    def test_templates_parse(self):
        for label in hdl.TEMPLATES:
            if label == "Empty file":
                continue
            fname, text = hdl.template(label, "thing")
            self.assertTrue(any("thing" in name for name in mods_of(text)), label)


class WorkspaceTest(unittest.TestCase):
    def test_work_area_and_project(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP, "tb/top_tb.v": "module top_tb; top t(); endmodule\n"})
        self.assertTrue(ws.is_flow(p.flow))
        self.assertEqual(ws.project_names(p.flow), ["demo"])
        self.assertEqual(sorted(p.sources()), ["cnt.v", "top.v"])
        self.assertEqual(p.testbenches(), ["tb/top_tb.v"])
        self.assertEqual(p.top_module().name, "top")

    def test_bad_names_and_clone_command(self):
        p = project_with({"top.v": TOP})
        with self.assertRaises(ValueError):
            ws.new_project(p.flow, "1bad")
        with self.assertRaises(ValueError):
            ws.new_project(p.flow, "demo")
        self.assertEqual(ws.repo_name("https://github.com/x/picorv32.git"), "picorv32")
        self.assertIn("--depth", " ".join(ws.clone_command("https://e.org/r.git", "/tmp/r")))


class LintParseTest(unittest.TestCase):
    def test_verilator(self):
        out = "%Warning-WIDTH: /w/demo/top.v:3:20: Operator ASSIGN expects 4 bits\n%Error: /w/demo/cnt.v:9:1: syntax\n"
        probs = lint.parse("verilator", out, root="/w/demo")
        self.assertEqual([(p["file"], p["line"], p["severity"], p["code"]) for p in probs],
                         [("top.v", 3, "warning", "WIDTH"), ("cnt.v", 9, "error", "")])
        self.assertEqual(lint.summary(probs), "1 error, 1 warning")

    def test_iverilog_and_yosys(self):
        self.assertEqual(lint.parse("iverilog", "cnt.v:4: syntax error\n")[0]["line"], 4)
        y = lint.parse("yosys", "ERROR: syntax error, unexpected ';' at cnt.v:7.3-7.4\n")
        self.assertEqual((y[0]["file"], y[0]["line"]), ("cnt.v", 7))


class VerdictTest(unittest.TestCase):
    def test_sim(self):
        self.assertEqual(sim.verdict(0, "PASS\n")[0], "pass")
        self.assertEqual(sim.verdict(0, "ERROR: q mismatch\n")[0], "fail")
        self.assertEqual(sim.verdict(1, "")[0], "fail")
        self.assertEqual(sim.verdict(0, "done\n")[0], "ran")

    def test_formal(self):
        self.assertEqual(formal.verdict("SBY 1:2 [x] DONE (PASS, rc=0)")[0], "pass")
        self.assertEqual(formal.verdict("DONE (FAIL, rc=2)")[0], "fail")
        self.assertEqual(formal.verdict("crashed")[0], "error")

    def test_lec(self):
        self.assertEqual(lec.verdict("Equivalence successfully proven!\nOf those cells 12 are proven")[0], "pass")
        state, summary, names = lec.verdict("Trying to prove $equiv for \\q: failed.\n"
                                            "Of those cells 10 are proven and 2 are unproven.")
        self.assertEqual(state, "fail")
        self.assertEqual(names, ["\\q"])
        self.assertIn("2 of 12", summary)


class VcdTest(unittest.TestCase):
    def test_parse_and_format(self):
        text = ("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n$var wire 4 \" q [3:0] $end\n"
                "$upscope $end\n$enddefinitions $end\n#0\n0!\nb0 \"\n#5\n1!\nb1010 \"\n#10\n0!\n")
        f = Path(tempfile.mkdtemp()) / "w.vcd"
        f.write_text(text)
        w = vcd.parse(f)
        q = next(s for p, s in w.signals.items() if p.endswith("q"))
        self.assertEqual(q.value_at(7), "1010")
        self.assertEqual(vcd.format_value("1010", 4, "hex"), "a")
        self.assertEqual(vcd.format_value("1010", 4, "dec"), "10")
        self.assertEqual(w.end, 10)


class FpgaTest(unittest.TestCase):
    def setUp(self):
        self.top = mods_of(TOP)["top"]

    def test_pins_and_check(self):
        s = fpga.defaults("ULX3S (85F)")
        s["pins"] = fpga.suggest_pins(self.top, "ULX3S (85F)")
        self.assertEqual(s["pins"]["clk"], "G2")
        self.assertEqual(len([b for b in s["pins"] if b.startswith("led")]), 4)
        self.assertIsNone(fpga.check(s, self.top))
        s["pins"]["led[1]"] = s["pins"]["led[0]"]
        self.assertIn("twice", fpga.check(s, self.top))

    def test_constraint_formats(self):
        for board, needle in (("iCEBreaker", "set_io clk 35"), ("ULX3S (85F)", 'LOCATE COMP "clk" SITE "G2"'),
                              ("Tang Nano 9K", 'IO_LOC "clk" 52')):
            s = fpga.defaults(board)
            s["pins"] = {"clk": fpga.BOARDS[board]["clock_pin"]}
            self.assertIn(needle, fpga.constraints(s, "clk"), board)


class DiagramTest(unittest.TestCase):
    def test_json_round_trip_and_from_rtl(self):
        d = dg.Diagram()
        a, b = d.add_shape("block", 0, 0, "fetch"), d.add_shape("block", 300, 0, "decode")
        d.connect(a["id"], b["id"], "instr", bus=32)
        again = dg.Diagram.from_json(d.to_json())
        self.assertEqual((len(again.shapes), len(again.edges)), (2, 1))
        blocks = dg.from_rtl(mods_of(COUNTER + TOP), "top")
        self.assertTrue(any(s["label"].startswith("u_cnt") for s in blocks.shapes))
        out = Path(tempfile.mkdtemp())
        dg.export(blocks, out / "b.svg")
        dg.export(blocks, out / "b.png")
        self.assertTrue((out / "b.svg").stat().st_size > 500 and (out / "b.png").stat().st_size > 500)


class AgentTest(unittest.TestCase):
    def test_tool_definitions(self):
        defs = agent.Toolbox.definitions()
        names = {d["name"] for d in defs}
        for want in ("get_overview", "read_file", "edit_file", "run_tests", "run_lec", "run_fpga", "run_asic",
                     "draw_diagram"):
            self.assertIn(want, names)
        for d in defs:
            self.assertTrue(d["eager_input_streaming"])
            self.assertFalse(d["input_schema"]["additionalProperties"])

    def test_check_input(self):
        schema = agent.Toolbox.specs()["read_file"]["schema"]
        self.assertIsNone(agent.check_input(schema, {"path": "a.v", "first_line": 3}))
        self.assertIn("missing", agent.check_input(schema, {}))
        self.assertIn("integer", agent.check_input(schema, {"path": "a", "first_line": True}))
        self.assertIn("unknown", agent.check_input(schema, {"path": "a", "x": 1}))
        lint_schema = agent.Toolbox.specs()["run_lint"]["schema"]
        self.assertIn("one of", agent.check_input(lint_schema, {"tool": "vcs"}))

    def test_paths_stay_inside(self):
        p = project_with({"top.v": TOP})

        class App:
            flow, project = p.flow, p

            def config_rel(self):
                return None
        tb = agent.Toolbox(App())
        self.assertEqual(tb.path("top.v"), (p.dir / "top.v").resolve())
        self.assertEqual(tb.path("designs/src/demo/top.v"), (p.dir / "top.v").resolve())
        with self.assertRaises(agent.ToolError):
            tb.path("../../../../etc/passwd")
        with self.assertRaises(agent.ToolError):
            tb.path("designs/nangate45/gcd/config.mk", write=True)
        self.assertTrue(tb.path("designs/src/demo/flowrunner.json").exists())

    def test_diff_and_clip(self):
        self.assertIn("+b", agent.diff_text("a\n", "b\n", "f.v"))
        self.assertLess(len(agent.clip("x" * 200_000)), 70_000)


BAD = """module sync2 (input clk, input d, output q);
  reg a, b;
  always @(posedge clk) begin a <= d; b <= a; end
  assign q = b;
endmodule
module top (input clk_a, input clk_b, input rst_n, input [3:0] din, input sel, output reg [3:0] dout,
            output reg lat, output w2, output loop_o, output reg single, output good, output reg [3:0] busx);
  reg [3:0] ra;
  reg flag_a;
  wire undriven;
  wire loop;
  always @(posedge clk_a or negedge rst_n) if (!rst_n) ra <= 0; else ra <= din;
  always @(posedge clk_a) flag_a <= sel;
  always @(posedge clk_b) dout <= ra & {4{sel}};
  always @(posedge clk_b) single <= flag_a;
  sync2 u_sync (.clk(clk_b), .d(flag_a), .q(good));
  reg [3:0] m1;
  always @(posedge clk_b) begin m1 <= ra; busx <= m1; end
  always @(*) if (sel) lat = din[0];
  assign w2 = din[1];
  assign w2 = din[2];
  assign loop = loop ^ din[3];
  assign loop_o = loop | undriven;
endmodule
"""


class DiagramEditTest(unittest.TestCase):
    def setUp(self):
        self.d = dg.Diagram()
        self.a = self.d.add_shape("module", 0, 0, w=100, h=60)["id"]
        self.b = self.d.add_shape("register", 300, 200, w=80, h=40)["id"]
        self.e = self.d.connect(self.a, self.b, "data", bus=8)["id"]

    def test_orthogonal_routing_and_sides(self):
        pts = self.d.edge_points(self.d.edge(self.e))
        self.assertEqual((pts[0], pts[-1]), ((100, 30), (300, 220)))
        self.assertTrue(all(p[0] == q[0] or p[1] == q[1] for p, q in zip(pts, pts[1:])))
        self.d.edge(self.e).update(src_side="bottom", dst_side="top", points=[[50, 120], [340, 120]])
        pts = self.d.edge_points(self.d.edge(self.e))
        self.assertEqual((pts[0], pts[-1]), ((50, 60), (340, 200)))
        self.assertTrue(all(p[0] == q[0] or p[1] == q[1] for p, q in zip(pts, pts[1:])))

    def test_copy_paste_group_align(self):
        made = self.d.duplicate({self.a, self.b})
        self.assertEqual((len(self.d.shapes), len(self.d.edges)), (4, 2))
        copy = self.d.edge(next(i for i in made if self.d.edge(i)))
        self.assertTrue({copy["src"], copy["dst"]} <= set(made))
        gid = self.d.group({self.a, self.b})
        self.assertTrue(gid)
        self.assertEqual(self.d.with_groups({self.a}), {self.a, self.b})
        self.d.align({self.a, self.b}, "left")
        self.assertEqual(self.d.shape(self.a)["x"], self.d.shape(self.b)["x"])
        self.d.reorder({self.a}, front=True)
        self.assertEqual(self.d.shapes[-1]["id"], self.a)
        self.d.remove({self.a})
        self.assertFalse(any(e["src"] == self.a or e["dst"] == self.a for e in self.d.edges))

    def test_every_kind_draws_and_exports(self):
        d = dg.Diagram()
        for i, kind in enumerate(dg.KINDS):
            d.add_shape(kind, (i % 6) * 200, (i // 6) * 150)
        out = Path(tempfile.mkdtemp())
        for ext in ("png", "svg", "pdf", "drawio"):
            dg.export(d, out / f"all.{ext}")
            self.assertGreater((out / f"all.{ext}").stat().st_size, 1000, ext)
        from xml.etree import ElementTree
        cells = list(ElementTree.parse(out / "all.drawio").iter("mxCell"))
        self.assertEqual(sum(c.get("vertex") == "1" for c in cells), len(dg.KINDS))


class FloorplanPlanTest(unittest.TestCase):
    PDN = """define_pdn_grid -name {grid} -voltage_domains {CORE}
add_pdn_stripe -grid {grid} -layer {metal1} -width {0.17} -pitch {2.8} -offset {0} -followpins
add_pdn_stripe -grid {grid} -layer {metal4} -width {0.48} -pitch {56.0} -offset {2}
add_pdn_stripe -grid {grid} -layer {metal7} -width {1.40} -pitch {40.0} -offset {2}
define_pdn_grid -macro -default -name CORE_macro_grid_1 -voltage_domains {CORE}
add_pdn_stripe -grid {CORE_macro_grid_1} -layer {metal5} -width {0.93} -pitch {100.0} -offset {2}
"""
    LAYERS = [{"name": "metal4", "direction": "vertical", "pitch": 0.28},
              {"name": "metal7", "direction": "horizontal", "pitch": 0.8}]

    def test_straps_and_fix(self):
        straps = fplan.straps(self.PDN)
        self.assertEqual([s["layer"] for s in straps], ["metal4", "metal7"])   # rails and macro grids left out
        self.assertAlmostEqual(fplan.strap_need(straps[0]), 30.48)
        p = fplan.plan("util", 90.0, 50, 1.0, 2.0)                               # a 13.4 µm core
        checks = fplan.check(p, 90.0, strap_list=straps, layers=self.LAYERS)
        bad = next(c for c in checks if c["title"] == "Power straps do not fit")
        fixed = fplan.plan("util", 90.0, bad["fix"]["set"]["util"], 1.0, 2.0)
        self.assertGreaterEqual(fixed["core_w"], 30.48)
        self.assertEqual(next(c["level"] for c in fplan.check(fixed, 90.0, strap_list=straps, layers=self.LAYERS)
                              if c["title"].startswith("Power straps")), "ok")

    def test_fixed_die_utilization_and_grow(self):
        p = fplan.plan("fixed", 5000.0, margin=5, die_w=60, die_h=60, site=(0.19, 1.4))
        self.assertGreater(p["utilization"], 100)
        fix = next(c["fix"] for c in fplan.check(p, 5000.0, mode="fixed") if c["level"] == "error")
        grown = fplan.plan("fixed", 5000.0, margin=5, die_w=fix["set"]["die_w"], die_h=fix["set"]["die_h"])
        self.assertAlmostEqual(grown["utilization"], 60, delta=1)

    def test_pins_rows_and_macros(self):
        p = fplan.plan("fixed", 100.0, margin=2, die_w=20, die_h=20, site=(0.19, 1.4))
        checks = fplan.check(p, 100.0, layers=self.LAYERS, pins=200, pin_layers=["metal4", "metal7"],
                             site=(0.19, 1.4), mode="fixed")
        self.assertEqual(next(c["level"] for c in checks if "IO pins" in c["title"]), "error")
        placed = fplan.pack_macros({"m0": (20, 40), "m1": (20, 40), "m2": (30, 10)}, (2, 2, 100, 100), (4, 4))
        self.assertEqual(set(placed), {"m0", "m1", "m2"})
        sizes = {"M": (20, 40), "S": (30, 10)}
        masters = {"m0": "M", "m1": "M", "m2": "S"}
        self.assertEqual(fp.problems(placed, sizes, masters, (2, 2, 100, 100), (4, 4)), [])
        with self.assertRaises(ValueError):
            fplan.pack_macros({"big": (200, 10)}, (0, 0, 100, 100))


class WaiverTest(unittest.TestCase):
    def test_waiver_matches_by_code_file_and_message_not_line(self):
        f = {"source": "lint", "severity": "warning", "code": "WIDTH", "file": "a.v", "line": 12,
             "message": "Operator ASSIGN expects 4 bits but got 8 on 'q'"}
        w = dc.waiver_for(f, "truncation intended")
        moved = dict(f, line=40)
        other = dict(f, message="Operator ASSIGN expects 4 bits but got 8 on 'r'")
        found = dc.apply_waivers([moved, other, dict(f, file="b.v")], [w])
        self.assertEqual([x.get("waived") for x in found], ["truncation intended", None, None])
        self.assertEqual(dc.counts(found), (0, 2, 1))
        self.assertEqual(dc.unused_waivers([other], [w]), [w])


class CoverageTest(unittest.TestCase):
    def dat(self, rows):
        f = Path(tempfile.mkdtemp()) / "coverage.dat"
        f.write_text("# SystemC::Coverage-3\n" + "".join(
            "C '\x01f\x02%s\x01l\x02%d\x01n\x021\x01t\x02%s\x01o\x02%s\x01h\x02tb.dut' %d\n" % r for r in rows))
        return f

    def test_merge_kinds_and_goals(self):
        a = self.dat([("d.v", 3, "line", "block", 5), ("d.v", 4, "line", "else", 0), ("d.v", 9, "user", "c_wrap", 0),
                      ("d.v", 1, "toggle", "q[0]:0->1", 1), ("tb.v", 2, "line", "block", 1)])
        b = self.dat([("d.v", 4, "line", "else", 2), ("d.v", 9, "user", "c_wrap", 3),
                      ("d.v", 1, "toggle", "q[0]:1->0", 0)])
        one = coverage.summarize(coverage.read([a]), ["d.v"])
        self.assertEqual((one["line"]["hit"], one["line"]["total"], one["line"]["files"]["d.v"]["missed"]),
                         (1, 2, [4]))
        both = coverage.summarize(coverage.read([a, b]), ["d.v"])
        self.assertEqual(both["line"]["percent"], 100.0)
        self.assertEqual(both["cover"]["points"][0]["count"], 3)
        self.assertEqual((both["toggle"]["hit"], both["toggle"]["total"]), (1, 2))
        states = {k: s for k, _l, _p, _g, s in coverage.check_goals(both, {"line": 90, "toggle": 100, "cover": 100})}
        self.assertEqual(states, {"line": "pass", "branch": "info", "toggle": "fail", "cover": "pass"})
        self.assertEqual(coverage.missed_lines(one, "d.v"), [4])


class RegressTest(unittest.TestCase):
    def test_expand_seeds_and_tags(self):
        tests = [{"name": "a", "seeds": 3, "seed": 10, "tags": ["nightly"]}, {"name": "b", "tags": ["smoke"]},
                 {"name": "c", "seeds": 2, "seed": "random"}]
        runs = regress.expand(tests)
        self.assertEqual([r["id"] for r in runs[:4]], ["a.s10", "a.s11", "a.s12", "b"])
        self.assertEqual(len({r["seed"] for r in runs[4:]}), 2)
        self.assertEqual([r["id"] for r in regress.expand(tests, ["smoke"])], ["b"])
        self.assertEqual(regress.tally({"x": {"state": "pass"}, "y": {"state": "timeout"}, "z": {"state": "ran"}}),
                         {"total": 3, "passed": 1, "failed": 1, "other": 1})


class SignoffTest(unittest.TestCase):
    def setUp(self):
        self.p = project_with({"top.v": TOP})
        self.p["tests"] = [{"name": "t1"}, {"name": "t2", "seeds": 2}]
        self.p["plan"] = [{"feature": "f", "tests": ["t2"], "formal": [], "cover": ["c"]}]
        self.cov = {"line": {"hit": 9, "total": 10, "percent": 90.0, "files": {}},
                    "branch": {"hit": 0, "total": 0, "percent": 0.0, "files": {}},
                    "toggle": {"hit": 1, "total": 2, "percent": 50.0, "missed": []},
                    "cover": {"hit": 1, "total": 1, "percent": 100.0, "points": [{"name": "c", "count": 2,
                                                                                    "file": "top.v", "line": 3}]}}
        self.results = {"tests": {"t1": {"state": "pass"}, "t2.s1": {"state": "pass", "test": "t2"},
                                  "t2.s2": {"state": "pass", "test": "t2"}}, "formal": {}, "coverage": self.cov}
        self.ran = {"lint": True, "design": True, "cdc": True}

    def verdict(self, findings=(), ran=None, results=None):
        return signoff.collect(self.p, list(findings), ran or self.ran, results or self.results)[1]

    def test_gate(self):
        self.assertEqual(self.verdict(), "pass")
        err = {"source": "cdc", "severity": "error", "code": "CDC-NOSYNC", "file": "top.v", "line": 1, "message": "m"}
        self.assertEqual(self.verdict([err]), "fail")
        self.assertEqual(self.verdict([dict(err, waived="ok")]), "pass")
        self.assertEqual(self.verdict([dict(err, severity="warning")]), "pass")
        self.assertEqual(self.verdict(ran={"lint": True, "design": False, "cdc": True}), "fail")   # not run
        failing = dict(self.results, tests=dict(self.results["tests"], **{"t2.s2": {"state": "fail", "test": "t2"}}))
        self.assertEqual(self.verdict(results=failing), "fail")
        self.assertEqual(signoff.plan_status(self.p["plan"], failing)[0][1], "fail")
        self.p["goals"] = {"line": 95}
        self.assertEqual(self.verdict(), "fail")

    def test_reports(self):
        items, verdict = signoff.collect(self.p, [], self.ran, self.results)
        page = signoff.html_report(self.p, items, verdict, [], self.results, [])
        self.assertIn("PASS", page)
        self.assertIn("t2.s2", page)
        from xml.etree import ElementTree
        suite = ElementTree.fromstring(signoff.junit(self.p, dict(self.results, tests={
            "t1": {"state": "fail", "why": "q <b> mismatch", "log": "x & y"}})))
        self.assertEqual((suite.get("tests"), suite.get("failures")), ("1", "1"))


class FilelistTest(unittest.TestCase):
    def test_round_trip_and_nested(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP, "inc/defs.vh": "`define X 1\n"})
        p["include_dirs"], p["defines"] = ["inc"], ["WIDTH=8", "FAST"]
        p.save()
        flist = ws.write_filelist(p)
        data = ws.read_filelist(flist)
        self.assertEqual([Path(f).name for f in data["files"]], ["cnt.v", "top.v"])
        self.assertEqual(data["defines"], ["WIDTH=8", "FAST"])
        outer = p.dir / "all.f"
        outer.write_text(f"// wrapper\n-f {flist.name}\n+define+EXTRA  # trailing\n/outside/x.v\n")
        p["sources"], p["defines"], p["include_dirs"] = ["top.v"], [], []
        outside = ws.apply_filelist(p, outer)
        self.assertEqual((p["sources"], p["defines"], p["include_dirs"]),
                         (["cnt.v", "top.v"], ["WIDTH=8", "FAST", "EXTRA"], ["inc"]))
        self.assertEqual(outside, ["/outside/x.v"])


@unittest.skipUnless(HAVE_TOOLS, "OSS CAD Suite is not installed")
class DesignCheckRunTest(unittest.TestCase):
    """Yosys on a design with one of every problem: the design checks and the CDC classes."""

    @classmethod
    def setUpClass(cls):
        cls.p = project_with({"top.v": BAD})
        folder, cmd = dc.command(cls.p)
        rc, out = run_sync(cmd, cwd=str(folder), env=eda_tools.env())
        cls.findings = dc.parse((folder / "checks.log").read_text() + out, root=cls.p.dir)
        cls.cdc = cdc.analyze(dc.load_netlist(cls.p), root=cls.p.dir)

    def test_design_checks(self):
        codes = {f["code"]: f for f in self.findings}
        self.assertEqual(set(codes), {"LATCH", "MULTIDRIVEN", "UNDRIVEN", "COMBLOOP"})
        self.assertEqual((codes["LATCH"]["file"], BAD.splitlines()[codes["LATCH"]["line"] - 1].strip()),
                         ("top.v", "always @(*) if (sel) lat = din[0];"))
        self.assertEqual(codes["COMBLOOP"]["severity"], "error")

    def test_cdc_classes(self):
        self.assertEqual(set(self.cdc["domains"]), {"clk_a", "clk_b"})
        kinds = {}
        for x in self.cdc["crossings"]:
            kinds.setdefault(x["kind"], set()).add(x["to"].split("[")[0])
        self.assertEqual(kinds, {"synchronized": {"u_sync.a"}, "CDC-BUS": {"m1"}, "CDC-NOSYNC": {"single"},
                                 "CDC-LOGIC": {"dout"}})
        by_code = {f["code"]: f for f in self.cdc["findings"]}
        self.assertEqual({c: f["severity"] for c, f in by_code.items()},
                         {"CDC-BUS": "warning", "CDC-NOSYNC": "error", "CDC-LOGIC": "error"})
        line = BAD.splitlines()[by_code["CDC-NOSYNC"]["line"] - 1]
        self.assertIn("single <= flag_a", line)

    def test_single_clock_design_is_clean(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP})
        folder, cmd = dc.command(p)
        run_sync(cmd, cwd=str(folder), env=eda_tools.env())
        result = cdc.analyze(dc.load_netlist(p))
        self.assertEqual((list(result["domains"]), result["findings"]), (["clk"], []))
        self.assertIn("One clock domain", cdc.summary(result))


@unittest.skipUnless(HAVE_TOOLS, "OSS CAD Suite is not installed")
class RegressionRunTest(unittest.TestCase):
    """The regression engine with real simulators: seeds in parallel, a timeout, coverage, cocotb."""

    TB = """`timescale 1ns/1ps
module top_tb;
  reg clk = 0, rst_n = 0; wire [3:0] led; integer seed, n;
  top dut (.clk(clk), .rst_n(rst_n), .led(led));
  always #5 clk = ~clk;
  initial begin
    if (!$value$plusargs("seed=%d", seed)) seed = 1;
    $dumpfile("top_tb.vcd"); $dumpvars(0, top_tb);
    n = 4 + seed;
    #12 rst_n = 1; repeat (n) @(posedge clk); #1;
    if (seed == 3) forever #100;
    if (led == n[3:0]) $display("PASS seed=%0d", seed); else $display("FAIL: led=%0d", led);
    $finish;
  end
endmodule
"""

    def test_seeds_timeout_coverage_and_cocotb(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP, "tb/top_tb.v": self.TB})
        (p.dir / "tests").mkdir()
        (p.dir / "tests" / "test_cnt.py").write_text(hdl.cocotb_test(p.modules()["cnt"]))
        tests = [{"name": "rand", "top": "top_tb", "files": ["tb/top_tb.v"], "simulator": "verilator", "seeds": 3,
                  "timeout": 4},
                 {"name": "ivl", "top": "top_tb", "files": ["tb/top_tb.v"], "simulator": "iverilog"},
                 {"name": "py", "kind": "cocotb", "top": "cnt", "module": "tests/test_cnt.py"}]
        seen = []
        reg = regress.Regression(p, regress.expand(tests), jobs=4, coverage=True,
                                 on_update=lambda i, r: seen.append((i, r["state"]))).start()
        self.assertTrue(reg.wait(600))
        states = {k: r["state"] for k, r in reg.results.items()}
        self.assertEqual(states, {"rand.s1": "pass", "rand.s2": "pass", "rand.s3": "timeout", "ivl": "pass",
                                  "py": "pass"}, {k: r.get("why") for k, r in reg.results.items()})
        self.assertIn("seed=2", reg.results["rand.s2"]["log"])
        self.assertIn(("rand.s1", "running"), seen)
        self.assertTrue(sim.waveforms(reg.results["py"]["folder"]))            # cocotb's FST, converted
        cov = coverage.summarize(coverage.read(reg.coverage_files(), p.dir), p.sources())
        self.assertGreater(cov["line"]["percent"], 50)
        self.assertGreater(cov["toggle"]["total"], 4)
        self.assertEqual(len(regress.add_history(p, reg.results, reg.seconds, cov)), 1)
        self.assertEqual(regress.load_history(p)[0]["failed_runs"], ["rand.s3"])


@unittest.skipUnless(HAVE_TOOLS, "OSS CAD Suite is not installed")
class ToolRunTest(unittest.TestCase):
    """The real tools on a small design: simulation, coverage, lint, formal and LEC."""

    TB = """`timescale 1ns/1ps
module top_tb;
  reg clk = 0, rst_n = 0; wire [3:0] led;
  top dut (.clk(clk), .rst_n(rst_n), .led(led));
  always #5 clk = ~clk;
  initial begin
    $dumpfile("top_tb.vcd"); $dumpvars(0, top_tb);
    #12 rst_n = 1; repeat (5) @(posedge clk); #1;
    if (led == 5) $display("PASS"); else $display("FAIL: led=%0d", led);
    $finish;
  end
endmodule
"""

    def run_steps(self, folder, cmds):
        out = ""
        for c in cmds:
            rc, text = run_sync(c, cwd=str(folder), env=eda_tools.env(), timeout=300)
            out += text
            if rc:
                return rc, out
        return 0, out

    def test_simulate_both_simulators(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP, "tb/top_tb.v": self.TB})
        for simulator in ("iverilog", "verilator"):
            test = {"name": f"t_{simulator}", "top": "top_tb", "simulator": simulator, "files": ["tb/top_tb.v"]}
            folder, cmds = sim.commands(p, test)
            rc, out = self.run_steps(folder, cmds)
            self.assertEqual(sim.verdict(rc, out)[0], "pass", out[-1500:])
            self.assertTrue(sim.waveforms(folder), simulator)

    def test_lint_clean_and_dirty(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP})
        rc, out = run_sync(lint.command("verilator", p), cwd=str(p.dir), env=eda_tools.env())
        self.assertFalse([x for x in lint.parse("verilator", out, p.dir) if x["severity"] == "error"], out)
        (p.dir / "top.v").write_text(TOP.replace("wire w;", "wire w"))
        rc, out = run_sync(lint.command("verilator", p), cwd=str(p.dir), env=eda_tools.env())
        errs = [x for x in lint.parse("verilator", out, p.dir) if x["severity"] == "error"]
        self.assertTrue(errs and errs[0]["file"] == "top.v", out)

    def test_formal_prove_and_fail(self):
        prop = COUNTER.replace("endmodule", """`ifdef FORMAL
  reg past = 0; always @(posedge clk) past <= 1;
  always @(*) if (!past) assume(!rst_n);
  always @(posedge clk) if (past) assert({1'b0, q} != BAD);
`endif
endmodule""").replace("parameter W = 4", "parameter W = 4, parameter BAD = 5'd16")
        p = project_with({"cnt.v": prop})
        p["top"] = "cnt"
        p.save()
        for mode, bad, want in (("prove", "5'd16", "pass"), ("bmc", "5'd3", "fail")):
            (p.dir / "cnt.v").write_text(prop.replace("5'd16", bad))
            task = {"name": f"f_{mode}", "top": "cnt", "mode": mode, "depth": 12}
            folder, cmd = formal.sby_command(p, task)
            rc, out = run_sync(cmd, cwd=str(folder), env=eda_tools.env(), timeout=300)
            self.assertEqual(formal.verdict(out)[0], want, out[-1500:])

    def test_lec_rtl_vs_generic_netlist(self):
        p = project_with({"cnt.v": COUNTER, "top.v": TOP})
        folder = Path(tempfile.mkdtemp())
        net = folder / "net.v"
        rc, out = run_sync(["yosys", "-q", "-p", f"read_verilog {p.dir}/cnt.v {p.dir}/top.v; synth -top top; "
                            f"write_verilog -noattr {net}"], env=eda_tools.env())
        self.assertEqual(rc, 0, out)
        gold = lec.read_rtl([str(p.dir / "cnt.v"), str(p.dir / "top.v")])
        good = folder / "good.ys"
        good.write_text(lec.script("top", gold, lec.read_netlist(net, [])))
        rc, out = run_sync(["yosys", "-q", "-l", str(folder / "good.log"), str(good)], env=eda_tools.env())
        self.assertEqual(lec.verdict((folder / "good.log").read_text())[0], "pass", out[-1500:])
        # Break the netlist: tie one LED low. The check must fail.
        text = net.read_text()
        broken = folder / "broken.v"
        broken.write_text(text.replace("endmodule", "") .rsplit("module top", 1)[0] + "module top" +
                          text.rsplit("module top", 1)[1].replace("assign led[0] =", "assign led[0] = 1'b0 & "))
        bad = folder / "bad.ys"
        bad.write_text(lec.script("top", gold, lec.read_netlist(broken, [])))
        run_sync(["yosys", "-q", "-l", str(folder / "bad.log"), str(bad)], env=eda_tools.env())
        self.assertNotEqual(lec.verdict((folder / "bad.log").read_text())[0], "pass")


if __name__ == "__main__":
    unittest.main()
