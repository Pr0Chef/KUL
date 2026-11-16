"""The whole suite through the GUI, off screen, on three designs: everything a user does, from making a work
area to signing off a chip, done with the app's own buttons and dialogs.

  counter (new project):   write RTL in the editor, lint (clean, then a planted error), testbench, simulation
                           with Icarus and Verilator + coverage, waveforms, formal, a block diagram, and the
                           ASIC flow on nangate45 to the final GDS with LEC (RTL vs synthesis, synthesis vs final).
  blinky (cloned with git): FPGA builds for iCE40 (iCEBreaker), ECP5 (ULX3S) and Gowin (Tang Nano 9K).
  fifo (from a template):  a new file from the FIFO template, a generated testbench, lint and simulation.

Set SHOTS=<folder> to save screenshots. Takes about 5 to 10 minutes (the ASIC run is most of it).
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

import hdl  # noqa: E402
from new_design import NewDesignDialog  # noqa: E402

COUNTER = """// 8-bit counter with enable and a wrap flag.
module counter (input clk, input rst, input en, output reg [7:0] q, output wrap);
  assign wrap = &q;
  always @(posedge clk)
    if (rst) q <= 8'd0;
    else if (en) q <= q + 8'd1;
endmodule
"""
COUNTER_TB = """`timescale 1ns/1ps
module counter_tb;
  reg clk = 0, rst = 1, en = 0;
  wire [7:0] q; wire wrap;
  counter dut (.clk(clk), .rst(rst), .en(en), .q(q), .wrap(wrap));
  always #5 clk = ~clk;
  initial begin
    $dumpfile("counter_tb.vcd"); $dumpvars(0, counter_tb);
    repeat (2) @(posedge clk); @(negedge clk) begin rst = 0; en = 1; end  // change inputs away from the edge
    repeat (255) @(posedge clk); #1;
    if (q == 8'd255 && wrap) $display("PASS"); else $display("FAIL: q=%0d wrap=%b", q, wrap);
    $finish;
  end
endmodule
"""
FORMAL = """module counter_formal (input clk, input rst, input en);
  wire [7:0] q; wire wrap;
  counter dut (.clk(clk), .rst(rst), .en(en), .q(q), .wrap(wrap));
  reg past = 0;
  always @(posedge clk) past <= 1;
  always @(*) if (!past) assume(rst);
  always @(posedge clk) if (past && !$past(rst) && $past(en)) assert(q == $past(q) + 8'd1);
  always @(posedge clk) if (past && $past(rst)) assert(q == 0);
  always @(*) assert(wrap == (q == 8'hff));
endmodule
"""
BLINKY = """module blinky (input clk, output [3:0] led);
  reg [23:0] n = 0;
  always @(posedge clk) n <= n + 1;
  assign led = n[23:20];
endmodule
"""

shots = os.environ.get("SHOTS")
h = hn.Harness(hn.KUL_FLOW, shots=shots)
app = h.app
app.resize(1440, 900)
pp, rtl, ver, fp, dia = (app.section_pages[n] for n in ("project", "rtl", "verify", "fpga", "diagram"))
tmp = Path(tempfile.mkdtemp(prefix="suite-gui-"))


def fill(**values):
    """A dialog filler for FieldsDialog: set the named widgets."""
    def run(dlg):
        for key, value in values.items():
            w = dlg.widgets[key]
            if hasattr(w, "set_active_id") and not hasattr(w, "set_text"):
                w.set_active_id(value)
            elif hasattr(w, "set_active") and isinstance(value, bool):
                w.set_active(value)
            else:
                w.set_text(value)
    return run


def write_in_editor(path, text):
    rtl.open_file(path)
    rtl.current_view().set_text(text)
    rtl.save(Path(path))
    h.pump(0.3)


def lint_now():
    got = {}
    rtl.run_lint("verilator", then=lambda probs: got.setdefault("p", probs))
    h.wait(lambda: "p" in got, 120)
    return got.get("p")


# ---------------- Tools dialog ----------------
def look_at_tools(dlg):
    nb = next(c for c in dlg.get_content_area().get_children() if hasattr(c, "get_n_pages"))
    h.check("Tools dialog has the ASIC and RTL/FPGA tabs", nb.get_n_pages() == 2)
    return hn.Gtk.ResponseType.CANCEL


h.answer_next_dialog(look_at_tools)
app.choose_tools()
h.pump(0.5)

# ---------------- work area and a new project ----------------
app.show_section("project")
area = tmp / "area"
h.answer_next_dialog(fill(path=str(area)))
pp.on_new_area()
h.check("work area created and opened", h.wait(lambda: app.flow == area and (area / "Makefile").exists(), 30))
h.answer_next_dialog(fill(name="counter"))
pp.on_new_project()
h.check("project created and selected", h.wait(lambda: app.project and app.project.name == "counter", 10))
h.shot("01_project")

# ---------------- RTL, lint ----------------
app.show_section("rtl")
src = app.project.dir / "counter.v"
write_in_editor(src, COUNTER)
probs = lint_now()
h.check("clean RTL lints without errors", probs is not None and not [p for p in probs if p["severity"] == "error"],
        probs)
write_in_editor(src, COUNTER.replace("assign wrap = &q;", "assign wrap = &q"))
probs = lint_now() or []
errs = [p for p in probs if p["severity"] == "error"]
h.check("planted syntax error is found on its line", errs and errs[0]["file"] == "counter.v"
        and errs[0]["line"] in (3, 4), probs)
h.shot("02_rtl_lint_error")
write_in_editor(src, COUNTER)

# ---------------- testbench, simulation, coverage, waves ----------------
mod = app.project.modules()["counter"]
rtl.make_testbench(mod)
write_in_editor(app.project.dir / "tb" / "counter_tb.v", COUNTER_TB)
p = app.project
p["tests"] = p["tests"] + [{"name": "counter_vl", "top": "counter_tb", "files": ["tb/counter_tb.v"],
                            "simulator": "verilator", "args": ""}]
p.save()
app.project_updated(None)
app.show_section("verify")
ver.cov_check.set_active(True)
done = {}
ver.run_regression(then=lambda res: done.setdefault("r", dict(res)))
h.check("regression finished", h.wait(lambda: "r" in done, 300))
res = done.get("r", {})
h.check("Icarus test passes", res.get("counter_tb", {}).get("state") == "pass", res.get("counter_tb"))
h.check("Verilator test passes", res.get("counter_vl", {}).get("state") == "pass", res.get("counter_vl"))
h.check("waveform loaded", ver.wave_file is not None and ver.wave_file.exists())
cov = ver.results.get("coverage", {})
h.check("line coverage measured", cov.get("percent", 0) > 50, cov)
h.shot("03_verify_waves")

# ---------------- formal ----------------
(p.dir / "formal").mkdir(exist_ok=True)
(p.dir / "formal" / "counter_formal.sv").write_text(FORMAL)
p["formal"] = [{"name": "counter_prove", "top": "counter_formal", "mode": "prove", "depth": 10,
                "files": ["formal/counter_formal.sv"]}]
p.save()
app.project_updated(None)
ver.fill()
got = {}
ver.on_run_formal(p["formal"][0], then=lambda r: got.setdefault("r", r))
h.check("formal proof passes", h.wait(lambda: "r" in got, 300) and got["r"]["state"] == "pass", got.get("r"))

# ---------------- diagram ----------------
app.show_section("diagram")
h.answer_next_dialog(fill(module="counter"))
dia.on_from_rtl()
h.pump(0.5)
saved = list((p.dir / "docs").glob("*.diagram.json")) if (p.dir / "docs").exists() else []
h.check("block diagram drawn and saved", saved and len(dia.d.shapes) >= 4, saved)
h.shot("04_diagram")

# ---------------- ASIC: design from the project, flow to the end, LEC ----------------
app.show_section("project")


def asic_dialog(dlg):
    dlg.platform.set_active_id("nangate45")
    h.pump(0.2)
    dlg.top.set_active_id("counter")
    h.pump(0.2)
    dlg.clock.set_active_id("clk")
    dlg.period.set_text("5")
    dlg.util.set_value(5)       # a small counter: a bigger core so nangate45's power straps fit


def give_up_on_dialog():
    """If the dialog is still open (it shows an error and asks again), note the error and cancel it."""
    for w in hn.Gtk.Window.list_toplevels():
        if isinstance(w, NewDesignDialog) and w.get_visible():
            h.check("New design dialog accepted the answers", False, w.error.get_text())
            w.response(hn.Gtk.ResponseType.CANCEL)
    return False


h.answer_next_dialog(asic_dialog)
hn.GLib.timeout_add_seconds(30, give_up_on_dialog)
pp.on_asic()
cfg = "designs/nangate45/counter/config.mk"
h.check("ASIC design created from the project", h.wait(lambda: app.config_rel() == cfg and
                                                       app.vars.get("RESULTS_DIR"), 120), app.config_rel())
app.from_cb.set_active_id("continue")
app.to_cb.set_active_id("finish")
h.wait(lambda: app.run_btn.get_sensitive(), 30)
app.on_run()
h.check("ASIC run started", h.wait(lambda: app.job is not None, 10))
h.check("ASIC run reaches the final GDS", h.wait(lambda: not app.job.running(), 1800) and app.job.rc == 0,
        f"exit code {app.job.rc}, log {app.job.log_path}")
h.check("all stages done", all(app.stage_done().values()), app.stage_done())
app.show_page("reports")
h.pump(1)
h.shot("05_asic_reports")
app.show_page("lec")
h.pump(0.5)
got = {}
app.lec.run(["rtl_synth", "synth_final"], then=lambda r: got.setdefault("r", dict(r)))
h.check("LEC finished", h.wait(lambda: "r" in got, 600))
lec_res = got.get("r", {})
h.check("LEC RTL vs synthesis proven", lec_res.get("rtl_synth", {}).get("state") == "pass", lec_res)
h.check("LEC synthesis vs final proven", lec_res.get("synth_final", {}).get("state") == "pass", lec_res)
h.shot("06_lec")

# ---------------- clone a repository; FPGA on three families ----------------
repo = tmp / "blinky_repo"
repo.mkdir()
(repo / "blinky.v").write_text(BLINKY)
subprocess.run("git init -q && git add . && git -c user.name=t -c user.email=t@t commit -qm init", shell=True,
               cwd=repo, check=True)
app.show_section("project")
h.answer_next_dialog(fill(url=f"file://{repo}", name="blinky"))
pp.on_clone()
h.check("repository cloned into a project", h.wait(lambda: app.project and app.project.name == "blinky", 60))
app.show_section("fpga")
for board in ("iCEBreaker", "ULX3S (85F)", "Tang Nano 9K"):
    fp.board_cb.set_active_id(board)
    h.pump(0.3)
    fp.on_suggest()
    h.pump(0.3)
    got = {}
    fp.run_build(then=lambda last: got.setdefault("r", last))
    ok = h.wait(lambda: "r" in got, 600)
    last = got.get("r") or {}
    h.check(f"FPGA build for {board}", ok and last.get("state") == "pass" and last.get("timing_met"),
            {k: last.get(k) for k in ("state", "timing_met", "bitstream")})
h.shot("07_fpga")

# ---------------- a design from the FIFO template ----------------
app.show_section("project")
h.answer_next_dialog(fill(name="fifo"))
pp.on_new_project()
h.wait(lambda: app.project and app.project.name == "fifo", 10)
fname, text = hdl.template("Synchronous FIFO", "fifo")
for f in list(app.project.dir.glob("*.v")):
    f.unlink()
app.show_section("rtl")
rtl.new_file(fname, text)
app.project["top"] = "fifo"
app.project.save()
app.project_updated(None)
probs = lint_now()
h.check("FIFO template lints without errors", probs is not None and
        not [x for x in probs if x["severity"] == "error"], probs)
rtl.make_testbench(app.project.modules()["fifo"])
app.show_section("verify")
done = {}
ver.run_regression(then=lambda res: done.setdefault("r", dict(res)))
h.wait(lambda: "r" in done, 300)
state = done.get("r", {}).get("fifo_tb", {}).get("state")
h.check("generated FIFO testbench compiles and runs", state in ("pass", "ran"), done.get("r"))
h.done()
