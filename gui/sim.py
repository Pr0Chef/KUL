"""Simulation of a project's tests: the commands for each simulator, the verdict, waveforms and coverage.

A test is a testbench module plus the files that hold it: {"name", "top", "files", "simulator", "args",
"coverage"}; regress.py adds seeds, timeouts and tags. A cocotb test ({"kind": "cocotb", "top": the design top,
"module": a Python file}) drives the design from Python instead of a Verilog testbench. It is compiled together with the project's design sources and run in its own build folder,
<work area>/build/<project>/sim/<test>/, so waveform dumps and logs of different tests never mix.

Verdict rule (the same for both simulators): a test fails when the simulator exits with an error, or its output
has a line with FAIL, ERROR, $error, $fatal or a failed assertion; it passes when it prints PASS; otherwise it
"ran" (no verdict line). Generated testbenches print PASS or FAIL themselves.
"""

import re
from pathlib import Path

import eda_tools

SIMULATORS = [("iverilog", "Icarus Verilog"), ("verilator", "Verilator")]
FAIL_RE = re.compile(r"\bFAIL(ED|URE)?\b|^\s*ERROR\b|%Error|\$error|\$fatal|Assertion (failed|violation)|"
                     r"\berror:|\bFATAL\b", re.M)
PASS_RE = re.compile(r"\bPASS(ED)?\b|\bALL TESTS PASSED\b|\bTEST PASSED\b", re.I)


def available(chosen=None):
    return [(tool, label) for tool, label in SIMULATORS
            if eda_tools.find(tool, chosen) and (tool != "iverilog" or eda_tools.find("vvp", chosen))]


def build_dir(project, test):
    return project.build("sim") / re.sub(r"[^\w\-]", "_", test["name"])


def commands(project, test, chosen=None):
    """(folder to run in, [compile command, run command]) for a test."""
    out = build_dir(project, test)
    out.mkdir(parents=True, exist_ok=True)
    design = project.paths(project.sources())
    bench = [f for f in project.paths(test.get("files", [])) if f not in design]
    files = design + bench
    defines = [f"-D{d}" for d in project["defines"] + test.get("defines", [])]
    args = (test.get("args") or "").split()
    top = test["top"]
    if test.get("kind") == "cocotb":
        return out, cocotb_commands(project, test, out, design, chosen)
    if test.get("simulator", "iverilog") == "verilator":
        exe = eda_tools.find("verilator", chosen)
        # --assert: SystemVerilog assertions and cover properties are checked, not ignored.
        build = [exe, "--binary", "--timing", "--trace", "--assert", "-sv", "-Wno-fatal", "-Wno-WIDTH", "-Wno-UNUSED",
                 "-Wno-DECLFILENAME", "-Wno-PINCONNECTEMPTY", "-Wno-TIMESCALEMOD", "--top-module", top, "--Mdir", str(out / "obj_dir"),
                 "-o", "sim", "-j", "0"] + project.include_flags("-I") + defines
        if test.get("coverage"):
            build.append("--coverage")
        return out, [build + files, [str(out / "obj_dir" / "sim")] + args]
    exe, vvp = eda_tools.find("iverilog", chosen), eda_tools.find("vvp", chosen)
    build = [exe, "-g2012", "-o", str(out / "sim.vvp"), "-s", top] + project.include_flags("-I") + defines
    return out, [build + files, [vvp, "-n", str(out / "sim.vvp")] + args]


COCOTB_RUNNER = """# Written by ORFS Flow Runner: builds and runs a cocotb test with Icarus Verilog.
import subprocess, sys
from pathlib import Path
from xml.etree import ElementTree
from cocotb_tools.runner import get_runner

step, top, module, here = sys.argv[1], sys.argv[2], sys.argv[3], Path(__file__).parent
sources = [Path(f) for f in {sources!r}]
runner = get_runner("icarus")
# The runner keeps the source list in memory, so the test step builds too (nothing is recompiled then).
runner.build(sources=sources, hdl_toplevel=top, build_dir=here, always=step == "build", waves=True,
             timescale=("1ns", "1ps"), includes={includes!r}, defines={defines!r})
if step == "build":
    sys.exit(0)
sys.path.insert(0, {tests_dir!r})
results = runner.test(hdl_toplevel=top, test_module=module, build_dir=here, test_dir=Path.cwd(), waves=True,
                      results_xml=str(Path.cwd() / "results.xml"))
for fst in set(here.glob("*.fst")) | set(Path.cwd().glob("*.fst")):
    subprocess.run(["fst2vcd", "-f", str(fst), "-o", str(Path.cwd() / (fst.stem + ".vcd"))], check=False)
    fst.unlink(missing_ok=True)
cases = list(ElementTree.parse(results).iter("testcase"))
bad = [c.get("name") for c in cases if c.find("failure") is not None or c.find("error") is not None]
print("FAIL: " + ", ".join(bad) if bad or not cases else f"PASS ({{len(cases)}} cocotb tests)")
sys.exit(1 if bad or not cases else 0)
"""


def cocotb_commands(project, test, out, design, chosen=None):
    """[build, run] for a cocotb test: Python coroutines (test["module"], a file in the project) drive the design
    top through Icarus Verilog. The suite's own Python runs it, since cocotb is installed there."""
    module = Path(test["module"])
    includes = [f[2:] for f in project.include_flags("-I")]
    defines = {d.split("=")[0]: (d.split("=", 1)[1] if "=" in d else 1) for d in project["defines"]}
    script = out / "run_cocotb.py"
    script.write_text(COCOTB_RUNNER.format(sources=design, includes=includes, defines=defines,
                                           tests_dir=str((project.dir / module).parent)))
    py = eda_tools.find("tabbypy3", chosen) or "python3"
    return [[py, str(script), "build", test["top"], module.stem], [py, str(script), "test", test["top"], module.stem]]


def verdict(rc, output, kind=""):
    """("pass" | "fail" | "ran", why) for a finished simulation."""
    if kind == "cocotb":
        # cocotb prints its own table (with "FAIL=0" in it), so only the runner's last line and exit code count.
        last = next((ln for ln in reversed(output.splitlines()) if ln.startswith(("PASS (", "FAIL: "))), "")
        return ("pass", last) if rc == 0 and last.startswith("PASS") else ("fail", last or f"exit code {rc}")
    if rc != 0:
        return "fail", f"exit code {rc}"
    bad = FAIL_RE.search(output)
    if bad:
        start, end = output.rfind("\n", 0, bad.start()) + 1, output.find("\n", bad.end())
        line = output[start:end if end >= 0 else len(output)]
        return "fail", line.strip()[:160] or bad.group(0)
    if PASS_RE.search(output):
        return "pass", "printed PASS"
    return "ran", "finished without a PASS or FAIL line"


def waveforms(folder):
    """Waveform dumps a simulation left in its folder, newest first."""
    found = [p for p in Path(folder).glob("*") if p.suffix.lower() in (".vcd", ".fst") and p.is_file()]
    return sorted(found, key=lambda p: -p.stat().st_mtime)


# ---------------------------------------------------------------- coverage (Verilator)
def coverage_command(folder, chosen=None):
    """Turns Verilator's coverage.dat in a test's folder into coverage.info (lcov format)."""
    exe = eda_tools.find("verilator_coverage", chosen) or str(Path(eda_tools.find("verilator", chosen)).parent
                                                              / "verilator_coverage")
    return [exe, "--write-info", str(Path(folder) / "coverage.info"), str(Path(folder) / "coverage.dat")]


def read_coverage(info_paths, root=None):
    """Line coverage from lcov files, merged over tests: {file: {"hit": n, "total": n, "missed": [lines]}}."""
    lines = {}   # file: {line: count}
    for info in info_paths:
        current = None
        try:
            text = Path(info).read_text(errors="replace")
        except OSError:
            continue
        for row in text.splitlines():
            if row.startswith("SF:"):
                current = row[3:].strip()
                if root and current.startswith(str(root)):
                    current = current[len(str(root)):].lstrip("/")
                lines.setdefault(current, {})
            elif row.startswith("DA:") and current is not None:
                number, count = row[3:].split(",")[:2]
                lines[current][int(number)] = lines[current].get(int(number), 0) + int(float(count))
    out = {}
    for name, counts in lines.items():
        if counts:
            out[name] = {"hit": sum(c > 0 for c in counts.values()), "total": len(counts),
                         "missed": sorted(n for n, c in counts.items() if c == 0)}
    return out


def coverage_percent(cov):
    total = sum(f["total"] for f in cov.values())
    return 100.0 * sum(f["hit"] for f in cov.values()) / total if total else 0.0
