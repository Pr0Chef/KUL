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
import diagram as dg  # noqa: E402
import eda_tools  # noqa: E402
import formal  # noqa: E402
import fpga  # noqa: E402
import hdl  # noqa: E402
import lec  # noqa: E402
import lint  # noqa: E402
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
