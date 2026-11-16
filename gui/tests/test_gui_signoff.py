"""RTL sign-off and verification management through the GUI, off screen, on a two-clock design.

Checks: lint with all linters, the design checks and CDC on clean RTL; a planted latch and a planted unsynchronized
crossing are found on their lines; a waiver stops a finding counting and removing it brings it back; a regression
with seeds in parallel on Verilator, Icarus and cocotb; merged coverage against goals with missed lines marked in
the editor; the verification plan; "Run everything" reaching a signed-off verdict; the HTML and JUnit reports;
filelist export and import; go to definition and instance insertion; and the assistant's view of all of it.

Set SHOTS=<folder> to save screenshots. About two minutes.
"""

import os
import sys
import tempfile
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

import agent  # noqa: E402
import designcheck as dc  # noqa: E402
import regress  # noqa: E402
import workspace as ws  # noqa: E402

SYNC2 = """// Two-flop synchronizer.
module sync2 (input clk, input d, output q);
  reg a, b;
  always @(posedge clk) begin
    a <= d;
    b <= a;
  end
  assign q = b;
endmodule
"""
TOP = """// A counter in the clk_a domain; its wrap flag is synchronized into clk_b.
module top (input clk_a, input clk_b, input rst, input en, output reg [3:0] cnt, output wrap_b);
  reg wrap_a;
  always @(posedge clk_a)
    if (rst) cnt <= 4'd0;
    else if (en) cnt <= cnt + 4'd1;
  always @(posedge clk_a)
    if (rst) wrap_a <= 1'b0;
    else wrap_a <= (cnt == 4'hf);
  sync2 u_sync (.clk(clk_b), .d(wrap_a), .q(wrap_b));
`ifdef VERILATOR
  cover_wrap: cover property (@(posedge clk_a) cnt == 4'hf);
`endif
endmodule
"""
# The same design with the synchronizer replaced by one flop, and a latch.
BAD_SYNC = "  reg single;\n  always @(posedge clk_b) single <= wrap_a;\n  assign wrap_b = single;\n"
TB = """`timescale 1ns/1ps
module top_tb;
  reg clk_a = 0, clk_b = 0, rst = 1, en = 0;
  wire [3:0] cnt; wire wrap_b;
  integer seed, n, i;
  top dut (.clk_a(clk_a), .clk_b(clk_b), .rst(rst), .en(en), .cnt(cnt), .wrap_b(wrap_b));
  always #5 clk_a = ~clk_a;
  always #7 clk_b = ~clk_b;
  initial begin
    if (!$value$plusargs("seed=%d", seed)) seed = 1;
    $dumpfile("top_tb.vcd"); $dumpvars(0, top_tb);
    n = 20 + (seed % 8);
    repeat (2) @(negedge clk_a);
    rst = 0;
    @(negedge clk_a) en = 1;                 // one cycle with en low, for coverage of the hold branch
    for (i = 0; i < n; i = i + 1) @(negedge clk_a);
    if (cnt == n[3:0]) $display("PASS seed=%0d", seed); else $display("FAIL cnt=%0d n=%0d", cnt, n);
    $finish;
  end
endmodule
"""

tmp = Path(tempfile.mkdtemp(prefix="signoff-gui-"))
area = tmp / "area"
ws.create_work_area(area, hn.KUL_FLOW)
proj = ws.new_project(area, "dual", "top")
for f in proj.dir.glob("*.v"):
    f.unlink()
(proj.dir / "sync2.v").write_text(SYNC2)
(proj.dir / "top.v").write_text(TOP)
(proj.dir / "tb").mkdir()
(proj.dir / "tb" / "top_tb.v").write_text(TB)
proj["top"] = "top"
proj["tests"] = [{"name": "rand", "top": "top_tb", "files": ["tb/top_tb.v"], "simulator": "verilator", "seeds": 3,
                  "timeout": 120, "tags": ["nightly"]},
                 {"name": "ivl", "top": "top_tb", "files": ["tb/top_tb.v"], "simulator": "iverilog",
                  "tags": ["smoke"]}]
proj["goals"] = {"line": 90, "toggle": 0, "cover": 100}
proj.save()

h = hn.Harness(area, shots=os.environ.get("SHOTS"))
app = h.app
app.resize(1440, 900)
app.set_project("dual")
h.pump(0.5)
rtl, ver, pp = (app.section_pages[n] for n in ("rtl", "verify", "project"))
p = app.project


def wait_for(start, seconds=180):
    """Start something that takes a `then` callback; wait for it and return what it was called with."""
    got = {}
    started = start(lambda *a: got.setdefault("r", a[0] if a else None))
    h.wait(lambda: "r" in got, seconds)
    return got.get("r"), started, "r" in got


def errors(source):
    return [f for f in rtl.findings if f["source"] == source and f["severity"] == "error" and not f.get("waived")]


# ---------------- RTL checks on the clean design ----------------
app.show_section("rtl")
_r, _s, done = wait_for(lambda then: rtl.run_all_checks(then=then))
h.check("lint, design checks and CDC all ran", done and all(rtl.ran.values()), rtl.ran)
h.check("clean design has no errors", not [f for f in rtl.findings if f["severity"] == "error"], rtl.findings)
c = rtl.cdc_result or {}
h.check("CDC sees two clock domains", sorted(c.get("domains", {})) == ["clk_a", "clk_b"], c.get("domains"))
h.check("CDC accepts the two-flop synchronizer", [x["kind"] for x in c.get("crossings", [])] == ["synchronized"],
        c.get("crossings"))

# ---------------- planted problems are found; a waiver stops one counting ----------------
bad = TOP.replace("  sync2 u_sync (.clk(clk_b), .d(wrap_a), .q(wrap_b));\n", BAD_SYNC).replace(
    "output wrap_b);", "output wrap_b, output reg lat);\n  always @(*) if (en) lat = cnt[0];")
(p.dir / "top.v").write_text(bad)
rtl.reload_file(p.dir / "top.v")
result, _s, done = wait_for(lambda then: rtl.run_cdc(then=then))
cdc_errors = errors("cdc")
h.check("unsynchronized crossing found", done and len(cdc_errors) == 1 and cdc_errors[0]["code"] == "CDC-NOSYNC"
        and cdc_errors[0]["file"] == "top.v", rtl.findings)
h.check("crossing is reported on its line", cdc_errors and bad.splitlines()[cdc_errors[0]["line"] - 1].strip()
        .startswith("always @(posedge clk_b) single"), cdc_errors)
latches = [f for f in rtl.findings if f["code"] == "LATCH"]
h.check("inferred latch found", len(latches) == 1 and latches[0]["file"] == "top.v", rtl.findings)
h.shot("s1_rtl_findings")
p["waivers"] = p["waivers"] + [dc.waiver_for(cdc_errors[0], "wrap_a is quasi-static in this mode")]
p.save()
rtl.after_waivers()
h.check("waived finding no longer counts", not errors("cdc") and dc.counts(rtl.findings)[2] == 1,
        dc.counts(rtl.findings))
items, verdict, _f = ver.signoff_state()
h.check("gate shows CDC clean once waived", next(i for i in items if i["key"] == "cdc")["state"] == "pass", items)
p["waivers"] = []
p.save()
rtl.after_waivers()
h.check("removing the waiver brings the error back", len(errors("cdc")) == 1)
h.check("gate fails on the CDC error", ver.signoff_state()[1] == "fail")
(p.dir / "top.v").write_text(TOP)
rtl.reload_file(p.dir / "top.v")

# ---------------- cocotb test, plan ----------------
made = rtl.make_cocotb(p.modules()["top"])
h.check("cocotb test written and registered", made and made.exists()
        and any(t.get("kind") == "cocotb" for t in p["tests"]), p["tests"])
p["plan"] = [{"feature": "Counts and wraps", "description": "cnt follows the enabled cycles and wraps at 15",
              "tests": ["rand", "ivl"], "formal": [], "cover": ["cover_wrap"]},
             {"feature": "Runs from Python", "description": "cocotb can drive the top", "tests": ["top_cocotb"],
              "formal": [], "cover": []}]
p.save()
app.project_updated(None)

# ---------------- run everything: the sign-off gate ----------------
app.show_section("verify")
ver.jobs.set_value(4)
verdict, _s, done = wait_for(lambda then: ver.run_signoff(then=then), 600)
runs = ver.results["tests"]
h.check("sign-off run finished", done)
h.check("each seed is its own run", sorted(runs) == ["ivl", "rand.s1", "rand.s2", "rand.s3", "top_cocotb"],
        sorted(runs))
h.check("every run passed", all(r["state"] == "pass" for r in runs.values()),
        {k: (r["state"], r.get("why")) for k, r in runs.items()})
h.check("seeds reached the testbench", "seed=2" in runs.get("rand.s2", {}).get("log", ""),
        runs.get("rand.s2", {}).get("log", "")[-300:])
cov = ver.results.get("coverage", {})
h.check("coverage merged over the Verilator runs", cov.get("runs") == 3 and cov["line"]["percent"] >= 90
        and cov["toggle"]["total"] > 0, {k: v.get("percent") for k, v in cov.items() if isinstance(v, dict)})
h.check("cover point hit", [(pt["name"], pt["count"] > 0) for pt in cov.get("cover", {}).get("points", [])]
        == [("cover_wrap", True)], cov.get("cover"))
items, verdict2, _f = ver.signoff_state()
h.check("signed off", verdict == "pass" and verdict2 == "pass", [(i["name"], i["value"], i["state"]) for i in items])
h.check("plan features covered", next(i for i in items if i["key"] == "plan")["value"] == "2 / 2 features", items)
h.check("regression history kept", len(regress.load_history(p)) == 1)
ver.tabs.set_current_page(5)
h.shot("s2_signoff")
ver.tabs.set_current_page(3)
h.shot("s3_runs")
ver.tabs.set_current_page(1)
h.shot("s4_coverage")

# ---------------- reports ----------------
folder = ver.export_report()
html = (folder / "signoff.html").read_text()
h.check("HTML report written", "PASS" in html and "Counts and wraps" in html and "rand.s2" in html)
suite = ElementTree.parse(folder / "junit.xml").getroot()
h.check("JUnit report lists every run", suite.get("tests") == "5" and suite.get("failures") == "0"
        and len(suite.findall("testcase")) == 5, suite.attrib)

# ---------------- a goal that is not met fails the gate; a coverage hole is marked in the editor ----------------
p["goals"] = {"line": 90, "toggle": 100, "cover": 100}
p.save()
h.check("an unmet coverage goal fails the gate", cov["toggle"]["percent"] < 100 and ver.signoff_state()[1] == "fail",
        cov["toggle"]["percent"])
p["goals"] = {"line": 90, "toggle": 0, "cover": 100}
p.save()
app.show_section("rtl")
rtl.open_file(p.dir / "top.v")
ver.results["coverage"]["line"]["files"]["top.v"]["missed"] = [6]
rtl.set_coverage(ver.results["coverage"])
view = rtl.current_view()
tag = view.buffer.get_tag_table().lookup("uncovered")
h.check("line no test ran is tinted in the editor", view.buffer.get_iter_at_line(5).has_tag(tag)
        and not view.buffer.get_iter_at_line(2).has_tag(tag))

# ---------------- navigation, instances, filelists ----------------
text = view.text()
view.buffer.place_cursor(view.buffer.get_iter_at_offset(text.index("sync2 u_sync") + 2))
rtl.go_to_definition()
h.pump(0.3)
h.check("F12 opens the module under the cursor", rtl.current_path().name == "sync2.v", rtl.current_path())
inst = rtl.instance_text(p.modules()["sync2"])
h.check("instance template names every port", "sync2 u_sync2 (" in inst and ".clk(clk)" in inst and ".q(q)" in inst,
        inst)
flist = ws.write_filelist(p)
h.check("filelist exported", flist.read_text().splitlines()[-2:] == ["sync2.v", "top.v"], flist.read_text())
p["sources"] = ["top.v"]
p.save()
pp.import_filelist(str(flist))
h.check("filelist imported", app.project["sources"] == ["sync2.v", "top.v"], app.project["sources"])

# ---------------- the assistant sees it ----------------
tb = agent.Toolbox(app)
names = {d["name"] for d in agent.Toolbox.definitions()}
h.check("assistant has the sign-off tools", {"run_checks", "run_signoff"} <= names)
h.check("assistant reads the gate", '"verdict": "pass"' in tb.get_results("signoff"), tb.get_results("signoff")[:300])
h.check("assistant reads coverage and plan", "cover_wrap" in tb.get_results("coverage")
        and "Counts and wraps" in tb.get_results("plan"))
h.check("assistant reads CDC", "synchronized" in tb.get_results("cdc"))
h.done()
