"""The design assistant: Claude with tools that read and drive every section of the app.

Agent.send() runs on a worker thread. It streams Claude's reply and runs the tools Claude asks for through a
bridge to the GTK thread (see agent_page.Bridge): reads run straight away, edits and runs wait for the user's
approval unless they allowed them for the chat. Toolbox holds the tools; each returns text for Claude.

The SDK (`anthropic`) is not part of the system Python here, so it lives in DATA_DIR/pylib and install_command()
puts it there with uv or pip. Credentials come from ANTHROPIC_API_KEY, an `ant auth login` profile, or the key
saved in the assistant's settings.
"""

import base64
import difflib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import eda_tools
import hdl
import workspace as ws

PYLIB = eda_tools.DATA_DIR / "pylib"
MODELS = [("claude-opus-5-5", "Claude Opus 5.5 (best)"), ("claude-sonnet-5-5", "Claude Sonnet 5.5 (faster)"),
          ("claude-haiku-5-5", "Claude Haiku 5.5 (cheapest)")]
EFFORTS = ["low", "medium", "high", "xhigh", "max"]
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.1, 0.5)}  # $/MTok
MAX_READ = 60_000          # characters of a file or log handed to Claude at once
LONG_RUN = 6 * 3600        # seconds a run tool waits for its result (an ASIC flow can take hours)

SYSTEM = """You are the design assistant inside ORFS Flow Runner, a desktop EDA suite built on open-source tools. \
It has these sections, which share one project (a folder of RTL in the work area):
- Project: the work area, projects, cloning repos, the top module, include folders, defines, git.
- RTL: a Verilog/SystemVerilog editor with lint (Verilator, Icarus, Yosys), design checks (latches, loops, \
drivers), clock-domain-crossing analysis, waivers, and testbench/cocotb/formal templates.
- Diagrams: a draw.io-style editor for architecture and block diagrams, saved in the project's docs folder.
- Verify: regressions in parallel with seeds and tags (Icarus, Verilator, cocotb), coverage (line, branch, \
toggle, cover points) against goals, waveforms, formal checks with SymbiYosys, a verification plan, and a \
sign-off gate with an HTML/JUnit report.
- FPGA: Yosys + nextpnr builds for iCE40, ECP5 and Gowin boards, pin constraints, timing, programming.
- ASIC: the OpenROAD flow (synth, floorplan, place, cts, route, finish), its inputs (config.mk, SDC), a \
Floorplan planner (area budget, checks such as power straps and pins fitting, macro placement), \
reports, sign-off checks and logical equivalence (LEC) between RTL, the synthesized and the final netlist.

How to work:
- Look before you act: call get_overview first in a new chat, read files before editing them, and base \
every claim about the design on what the tools return.
- Prefer small, exact edits (edit_file) over rewriting whole files. Keep RTL synthesizable unless it is a \
testbench, and follow the style of the existing code.
- After changing RTL, lint it; after changing behaviour, run the relevant tests or formal checks; report what \
passed and what did not, with the real numbers.
- For sign-off work: findings are fixed in the RTL, not waived, unless the user says a finding is acceptable \
(then record the waiver with their reason). Close coverage holes by adding stimulus or tests for the lines, \
branches and cover points never hit, not by lowering goals. A clock domain crossing needs a synchronizer that \
fits the signal: two flops for a level, gray code or a handshake for a bus.
- Runs can take long and edits change the user's files: the user approves them, and may deny one. If a tool \
is denied, do not retry it; explain and ask.
- Paths are relative to the project folder unless they start with the work area's own folders (designs/, \
results/, reports/, logs/).
- For a task of three or more steps, show the steps with update_tasks and keep them current. When a test \
fails, read the waveform around the failure (read_waveform) before changing RTL. After drawing a diagram or \
changing the floorplan, look at it (look) and fix what reads badly. Ask the user (ask_user) only for choices \
that are theirs; remember what they tell you about the project (remember).
- The <app_state> with each message says what the user is looking at and, at the start of a chat, the project \
overview and its notes: use it instead of asking for it again.
- Answer concisely, in plain words, for a hardware engineer. Use short code blocks for RTL or constraints."""


# ---------------------------------------------------------------- the SDK
def load_sdk():
    """The anthropic module, or None when it is not installed."""
    if PYLIB.is_dir() and str(PYLIB) not in sys.path:
        sys.path.insert(0, str(PYLIB))
    try:
        import anthropic
        return anthropic
    except ImportError:
        return None


def install_command():
    """Command that installs the SDK into PYLIB for this Python, or None when neither uv nor pip is here."""
    uv = shutil.which("uv") or next((str(p) for p in [Path.home() / ".local/bin/uv", Path.home() / ".cargo/bin/uv"]
                                     if p.exists()), None)
    if uv:
        return [uv, "pip", "install", "--python", sys.executable, "--target", str(PYLIB), "--upgrade", "anthropic"]
    if os.system(f"{sys.executable} -m pip --version >/dev/null 2>&1") == 0:
        return [sys.executable, "-m", "pip", "install", "--target", str(PYLIB), "--upgrade", "anthropic"]
    return None


def has_credentials(key=""):
    """True when some credential is set; an `ant auth login` profile counts too (the SDK reads it)."""
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "anthropic"
    return bool(key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or os.environ.get("ANTHROPIC_PROFILE") or (cfg.is_dir() and any(cfg.rglob("*.json"))))


# ---------------------------------------------------------------- input checks
def check_input(schema, args):
    """Why args do not fit the tool's schema, or None. Streamed tool input can arrive truncated."""
    if not isinstance(args, dict):
        return "input is not an object"
    for key in schema.get("required", []):
        if key not in args:
            return f"missing {key}"
    kinds = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict, "number": (int, float)}
    for key, value in args.items():
        spec = schema.get("properties", {}).get(key)
        if spec is None:
            return f"unknown field {key}"
        want = kinds.get(spec.get("type"))
        if want and (not isinstance(value, want) or (spec.get("type") == "integer" and isinstance(value, bool))):
            return f"{key} should be a {spec['type']}"
        if "enum" in spec and value not in spec["enum"]:
            return f"{key} should be one of {', '.join(map(str, spec['enum']))}"
    return None


def diff_text(old, new, name):
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{name}",
                                 f"b/{name}", n=2)
    text = "".join(lines)
    return text if len(text) < 20_000 else text[:20_000] + "\n… (diff cut)\n"


def clip(text, limit=MAX_READ):
    return text if len(text) <= limit else text[: limit // 3] + f"\n… ({len(text) - limit} characters cut) …\n" + \
        text[-2 * limit // 3:]


class ToolError(Exception):
    """A tool failed in a way Claude should hear about (the text goes back as an error result)."""


# ---------------------------------------------------------------- the tools
def tool(kind, description, properties=None, required=()):
    """Mark a Toolbox method as a tool. kind: read (runs at once), edit or run (needs approval)."""
    def mark(fn):
        fn.tool = {"kind": kind, "description": description,
                   "schema": {"type": "object", "properties": properties or {}, "required": list(required),
                              "additionalProperties": False}}
        return fn
    return mark


S = {"type": "string"}
I = {"type": "integer"}


class Toolbox:
    """The tools, run on the GTK thread. Run tools take done(text) and call it when their run ends."""

    def __init__(self, app):
        self.app = app
        self.last_backup = None      # (path, text before or None) of the last file write

    def restore(self, backup):
        """Undo a file write: put the file back as it was (or remove it if the write created it)."""
        f, old = backup
        if old is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(old)
        self.app.section_pages["rtl"].reload_file(f)
        self.app.project_updated(None)
        return self.show(f)

    @classmethod
    def specs(cls):
        return {name: fn.tool for name, fn in vars(cls).items() if hasattr(fn, "tool")}

    @classmethod
    def definitions(cls):
        return [{"name": name, "description": t["description"], "input_schema": t["schema"],
                 "eager_input_streaming": True} for name, t in cls.specs().items()]

    # ---------- helpers ----------
    def project(self, need=True):
        p = self.app.project
        if need and not p:
            raise ToolError("No project is selected. Ask the user to pick or create one (create_project).")
        return p

    def design_dir(self):
        cfg = self.app.config_rel() if self.app.flow else None
        return (self.app.flow / cfg).parent.resolve() if cfg else None

    def roots(self, write):
        flow = self.app.flow.resolve() if self.app.flow else None
        roots = [r for r in (self.app.project.dir.resolve() if self.app.project else None, self.design_dir()) if r]
        if not write and flow:
            roots.append(flow)
            src = ws.orfs_source(self.app.flow)
            if src:
                roots.append(Path(src).resolve())
        return roots

    def path(self, rel, write=False):
        """Resolve a path from Claude: relative to the project (or the work area for designs/ results/ …) and
        inside the folders it may touch. Writes stay in the project and the selected ASIC design."""
        if not self.app.flow:
            raise ToolError("No work area is open.")
        rel = rel.strip()
        base = self.app.project.dir if self.app.project else self.app.flow
        if re.match(r"(designs|results|reports|logs|objects)/", rel):
            base = self.app.flow
        target = (base / rel).resolve()
        roots = self.roots(write)
        if not any(target == r or target.is_relative_to(r) for r in roots):
            where = "the project or the selected ASIC design" if write else "the work area"
            raise ToolError(f"{rel} is outside {where}.")
        return target

    def show(self, rel):
        base = self.app.project.dir if self.app.project else self.app.flow
        try:
            return str(rel.relative_to(base.resolve()))
        except ValueError:
            return str(rel)

    # ---------- reading ----------
    @tool("read", "Summary of the open work area, project (top, files, tests, formal tasks, FPGA target), the "
                  "selected ASIC design and its stage status, and which tools are installed. Call it first.")
    def get_overview(self):
        a = self.app
        out = {"work_area": str(a.flow) if a.flow else None,
               "projects": ws.project_names(a.flow) if a.flow else [],
               "section_shown": a.sections.get_visible_child_name()}
        p = a.project
        if p:
            out["project"] = {"name": p.name, "folder": str(p.dir), "top": p.top(),
                              "design_files": [str(f) for f in p.sources()],
                              "testbenches": [str(f) for f in p.testbenches()],
                              "include_dirs": p["include_dirs"], "defines": p["defines"],
                              "tests": p["tests"], "formal": p["formal"], "fpga": p["fpga"], "notes": p["notes"]}
        cfg = a.config_rel() if a.flow else None
        if cfg:
            out["asic"] = {"config": cfg, "design_folder": str(self.design_dir()),
                           "stages_done": a.stage_done(), "running": bool(a.job and a.job.running()),
                           "make_vars": {k: v for k, v in a.vars.items() if k in ("PLATFORM", "DESIGN_NAME",
                                                                                  "SDC_FILE", "CORE_UTILIZATION",
                                                                                  "PLACE_DENSITY", "VERILOG_FILES",
                                                                                  "RESULTS_DIR", "REPORTS_DIR",
                                                                                  "LOG_DIR")}}
        out["rtl_tools"] = {t: bool(eda_tools.find(t, a.rtl_tools())) for t in
                            ("iverilog", "verilator", "yosys", "sby", "nextpnr-ice40", "nextpnr-ecp5",
                             "nextpnr-himbaechel", "gtkwave")}
        return json.dumps(out, indent=1, default=str)

    @tool("read", "List files under a folder (default: the project folder), up to 300 entries.",
          {"path": {**S, "description": "Folder, relative to the project. Empty for the project itself."}})
    def list_files(self, path=""):
        root = self.path(path or ".")
        if not root.is_dir():
            raise ToolError(f"{path} is not a folder.")
        files = []
        for f in sorted(root.rglob("*")):
            if any(part.startswith(".") for part in f.relative_to(root).parts) or not f.is_file():
                continue
            files.append(f"{f.relative_to(root)}  ({f.stat().st_size} bytes)")
            if len(files) >= 300:
                files.append("… more not shown")
                break
        return "\n".join(files) or "(empty)"

    @tool("read", "Read a text file (RTL, testbench, SDC, config.mk, a log or report), with line numbers. Long "
                  "files: give first_line/last_line.",
          {"path": S, "first_line": I, "last_line": I}, ["path"])
    def read_file(self, path, first_line=1, last_line=0):
        f = self.path(path)
        if not f.is_file():
            raise ToolError(f"{path} does not exist.")
        lines = f.read_text(errors="replace").splitlines()
        last = last_line or len(lines)
        chosen = lines[max(first_line, 1) - 1: last]
        text = "\n".join(f"{n:5} {ln}" for n, ln in enumerate(chosen, max(first_line, 1)))
        return f"{self.show(f)}: {len(lines)} lines\n" + clip(text)

    @tool("read", "Search the project's files (or a folder) for a regular expression; returns file:line: text.",
          {"pattern": S, "path": S}, ["pattern"])
    def search(self, pattern, path=""):
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Bad pattern: {e}")
        root = self.path(path or ".")
        hits = []
        for f in sorted(root.rglob("*") if root.is_dir() else [root]):
            if not f.is_file() or f.stat().st_size > 5_000_000 or "/." in str(f):
                continue
            for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{self.show(f)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n… more matches not shown"
        return "\n".join(hits) or "No matches."

    @tool("read", "The project's modules: ports with widths, parameters, submodule instances, clocks and resets, "
                  "and the hierarchy under the top.")
    def get_hierarchy(self):
        p = self.project()
        mods = p.modules()
        out = []
        for m in mods.values():
            clk, rst, low = hdl.clock_and_reset(m)
            out.append({"module": m.name, "file": self.show(Path(m.file).resolve()), "line": m.line,
                        "testbench": hdl.is_testbench(m),
                        "ports": [f"{d} {m.width_text(b, n)}{n}" for d, n, b in m.ports],
                        "params": m.params, "instances": m.instances, "clock": clk, "reset": rst,
                        "reset_active_low": low})
        tree = hdl.hierarchy(mods, p.top()) if p.top() in mods else None
        return json.dumps({"top": p.top(), "modules": out, "hierarchy": tree}, indent=1, default=str)

    @tool("read", "Latest results the app holds. findings: lint, design-check and CDC findings with waivers "
                  "marked; cdc: clock domains and every crossing; tests: each regression run; coverage: line, "
                  "branch, toggle and cover points against the goals; plan: the verification plan's status; "
                  "signoff: the gate's items and verdict; formal, lec, fpga, asic. Logs are cut to their end.",
          {"kind": {**S, "enum": ["findings", "lint", "cdc", "tests", "coverage", "plan", "signoff", "formal", "lec",
                                  "fpga", "asic"]}}, ["kind"])
    def get_results(self, kind):
        pages = self.app.section_pages
        rtl, verify = pages["rtl"], pages["verify"]
        if kind in ("findings", "lint"):
            found = [f for f in rtl.findings if kind == "findings" or f.get("source") == "lint"]
            not_run = [k for k, done in rtl.ran.items() if not done]
            return json.dumps({"not_run_yet": not_run, "findings": found[:150]}, indent=1)
        if kind == "cdc":
            return json.dumps(rtl.cdc_result, indent=1) if rtl.cdc_result else "CDC has not been run (run_checks)."
        if kind == "coverage":
            cov = verify.results.get("coverage")
            if not cov:
                return "No coverage yet: run_tests with coverage on (Verilator tests)."
            brief = {k: {"percent": v["percent"], "hit": v["hit"], "total": v["total"]} for k, v in cov.items()
                     if isinstance(v, dict) and "total" in v}
            return json.dumps({"goals": self.project()["goals"], "kinds": brief,
                               "lines_never_run": {f: d["missed"][:60] for f, d in cov["line"]["files"].items()
                                                   if d["missed"]},
                               "cover_points": cov["cover"]["points"][:80],
                               "bits_never_toggled": cov["toggle"]["missed"][:60]}, indent=1)
        if kind == "plan":
            import signoff
            rows = signoff.plan_status(self.project()["plan"], verify.results)
            return json.dumps([{**item, "status": state, "detail": detail} for item, state, detail in rows],
                              indent=1) if rows else "The project has no verification plan (set_project(plan=…))."
        if kind == "signoff":
            self.project()
            items, verdict, _f = verify.signoff_state()
            return json.dumps({"verdict": verdict, "items": items}, indent=1)
        if kind in ("tests", "formal"):
            res = verify.results.get(kind, {})
            # Passing runs need no log; failing ones keep the end of theirs.
            return json.dumps({k: {**v, "log": "" if v.get("state") == "pass" else clip(v.get("log", ""), 2500)}
                               for k, v in res.items()}, indent=1) if res else f"No {kind} results yet."
        if kind == "lec":
            res = self.app.lec.results
            return json.dumps(res, indent=1) if res else "No LEC results yet."
        if kind == "fpga":
            last = pages["fpga"].last
            return json.dumps(last, indent=1, default=str) if last else "No FPGA build yet."
        return self.asic_metrics()

    def asic_metrics(self):
        import orfs_app as oa
        logs = self.app.dir_of("LOG_DIR")
        rows = {}
        for name, fname in oa.CHECK_COLUMNS:
            m = oa.load_metrics(logs / fname) if logs else None
            if m is not None:
                rows[name] = {label: m.get(key) for label, key, _k, _s in oa.METRICS if isinstance(key, str)
                              and m.get(key) is not None}
        if not rows:
            return "No ASIC metrics yet: run at least synthesis."
        return json.dumps({"stages_done": self.app.stage_done(), "metrics_by_stage": rows}, indent=1, default=str)

    @tool("read", "Signal values from a simulation waveform. Without signals: the signals in the file. With "
                  "signals: each one's value at every time one of them changes, between from_time and to_time "
                  "(simulation time units), to see what happened around a failure. run: a run id from the "
                  "regression (default: the waveform shown in Verify).",
          {"run": S, "signals": {"type": "array", "items": S}, "from_time": I, "to_time": I})
    def read_waveform(self, run="", signals=(), from_time=0, to_time=0):
        import sim
        import vcd
        verify = self.app.section_pages["verify"]
        path = verify.wave_file
        if run:
            r = verify.results["tests"].get(run)
            waves = sim.waveforms(r["folder"]) if r and r.get("folder") else []
            if not waves:
                raise ToolError(f"Run {run} has no waveform. Runs: {', '.join(verify.results['tests']) or 'none'}.")
            path = waves[0]
        if not path or not Path(path).exists():
            raise ToolError("No waveform yet: run a test whose testbench has $dumpfile and $dumpvars.")
        try:
            w = vcd.parse(path)
        except (ValueError, OSError) as e:
            raise ToolError(f"Could not read {path}: {e}")
        if not signals:
            names = list(w.signals)
            return f"{path}: {len(names)} signals, time 0 to {w.end} x {w.timescale}\n" + "\n".join(
                f"{n} [{w.signals[n].width}]" for n in names[:400])
        picked = [next((n for n in w.signals if n == s or n.endswith("." + s)), None) for s in signals]
        missing = [s for s, n in zip(signals, picked) if n is None]
        if missing:
            raise ToolError(f"No such signals: {', '.join(missing)}. Call read_waveform without signals to list them.")
        end = to_time or w.end
        times = sorted({t for n in picked for t in w.signals[n].times if from_time <= t <= end} | {from_time})
        note = ""
        if len(times) > 300:
            note = f"\n({len(times)} change times; the first 300 are shown. Narrow from_time and to_time.)"
            times = times[:300]
        rows = ["time  " + "  ".join(signals)]
        for t in times:
            rows.append(f"{t}  " + "  ".join(vcd.format_value(w.signals[n].value_at(t), w.signals[n].width) or "-"
                                             for n in picked))
        return f"{path} (time unit {w.timescale})\n" + "\n".join(rows) + note

    @tool("read", "The floorplan plan of the selected ASIC design (the Floorplan tab): settings, the core and die "
                  "they give, and every check with its fix.")
    def get_floorplan(self):
        plan = self.planner()
        return json.dumps({"settings": plan.s, "cell_area_from_synthesis": plan.area_from_synth,
                           "plan": plan.plan, "checks": plan.checks, "macros": plan.masters,
                           "placements": plan.placements, "applied_to_design": not plan.dirty}, indent=1)

    def planner(self):
        plan = self.app.planner
        if not (self.app.flow and self.app.config_rel()):
            raise ToolError("No ASIC design is selected.")
        plan.refresh()
        return plan

    @tool("read", "Look at a picture of the current diagram (what=diagram) or of the floorplan plan "
                  "(what=floorplan), to judge the layout the way the user sees it.",
          {"what": {**S, "enum": ["diagram", "floorplan"]}}, ["what"])
    def look(self, what):
        import tempfile
        out = Path(tempfile.mkdtemp(prefix="flowrunner-look-")) / f"{what}.png"
        if what == "diagram":
            import diagram as dg
            d = self.app.section_pages["diagram"].d
            if not d.shapes:
                raise ToolError("The diagram is empty.")
            dg.render_png(d, out)
            return {"text": f"The diagram: {len(d.shapes)} shapes, {len(d.edges)} arrows.", "image": out}
        plan = self.planner()
        if not plan.plan:
            raise ToolError("There is no floorplan plan yet (no cell area).")
        plan.render_png(out)
        return {"text": "The floorplan plan: die (outer box), core (blue), power straps, macros (green).",
                "image": out}

    @tool("read", "The diagram open in the Diagrams section as JSON (shapes with ids, kinds, boxes, labels and "
                  "styles; arrows), to change it and write it back with draw_diagram.")
    def read_diagram(self):
        page = self.app.section_pages["diagram"]
        return json.dumps({"file": page.path.name if page.path else None, "title": page.d.title,
                           "shapes": page.d.shapes, "edges": page.d.edges}, indent=1)

    @tool("read", "Show the user your plan for a task of several steps, and keep it current: give the whole list "
                  "each time, with each step's status. Use it for work of three or more steps.",
          {"tasks": {"type": "array", "items": {"type": "object"}}}, ["tasks"])
    def update_tasks(self, tasks):
        clean = [{"text": str(t.get("text", "")), "status": t.get("status", "pending")
                  if t.get("status") in ("pending", "doing", "done") else "pending"}
                 for t in tasks if isinstance(t, dict)]
        self.tasks = clean
        return "Shown."

    @tool("ask", "Ask the user a question when the work depends on a choice only they can make (which of two "
                 "fixes, a target, a trade-off). Give two to four short options; the user can also type another "
                 "answer. Do not use it for things you can find out with the other tools.",
          {"question": S, "options": {"type": "array", "items": S}}, ["question", "options"])
    def ask_user(self, question, options):
        raise NotImplementedError      # answered by the panel, see Agent.run_tool

    @tool("read", "Keep a fact about this project for later chats (a convention, a decision, something the user "
                  "told you): it is saved in the project and shown to you at the start of every chat.",
          {"note": S}, ["note"])
    def remember(self, note):
        p = self.project()
        p["notes"] = (p["notes"] + "\n" if p["notes"] else "") + "- " + note.strip()
        p.save()
        return "Saved in the project's notes."

    # ---------- editing ----------
    def write(self, f, text, verb):
        doc = self.app.section_pages["rtl"].doc_of(f)
        if doc and doc["view"].buffer.get_modified():
            raise ToolError(f"{self.show(f)} has unsaved edits in the RTL editor; ask the user to save them first.")
        f.parent.mkdir(parents=True, exist_ok=True)
        # What the file was before, so the panel can offer Undo for this edit.
        self.last_backup = (f, f.read_text(errors="replace") if f.exists() else None)
        f.write_text(text)
        self.app.section_pages["rtl"].reload_file(f)
        design = self.design_dir()
        if design and f.is_relative_to(design) and not self.app.inputs_unsaved:
            self.app.on_config()       # the ASIC Inputs tab re-reads config.mk and the SDC
        self.app.project_updated(None)
        return f"{verb} {self.show(f)} ({len(text.splitlines())} lines)."

    @tool("edit", "Replace one exact piece of text in a file. old_text must occur exactly once; include enough "
                  "context to make it unique.", {"path": S, "old_text": S, "new_text": S},
          ["path", "old_text", "new_text"])
    def edit_file(self, path, old_text, new_text):
        f = self.path(path, write=True)
        if not f.is_file():
            raise ToolError(f"{path} does not exist; use write_file to create it.")
        text = f.read_text(errors="replace")
        n = text.count(old_text)
        if n != 1:
            raise ToolError(f"old_text occurs {n} times in {path}; it must occur exactly once.")
        return self.write(f, text.replace(old_text, new_text, 1), "Edited")

    @tool("edit", "Create a file or replace all of it (RTL, testbench, SDC, constraints, docs).",
          {"path": S, "content": S}, ["path", "content"])
    def write_file(self, path, content):
        f = self.path(path, write=True)
        return self.write(f, content, "Wrote" if f.exists() else "Created")

    @tool("edit", "Change project settings. Give only the fields to change. tests: list of {name, top, "
                  "simulator (iverilog|verilator), files, args?, seeds?, seed? (number or \"random\"), timeout?, "
                  "tags?} or cocotb tests {name, kind: \"cocotb\", top: design top, module: python file}; formal: "
                  "list of {name, top, mode (bmc|prove|cover), depth, files?}; fpga: {board, family, device, "
                  "package, mhz, io_standard, pins: {port[bit]: pin}, top}; plan: the verification plan, list of "
                  "{feature, description, tests: [names], formal: [names], cover: [cover point labels]}; goals: "
                  "coverage goals in percent {line, branch, toggle, cover}; waivers: accepted findings, list of "
                  "{source (lint|design|cdc), code, file, match (part of the message), reason}.",
          {"top": S, "include_dirs": {"type": "array"}, "defines": {"type": "array"}, "tests": {"type": "array"},
           "formal": {"type": "array"}, "fpga": {"type": "object"}, "notes": S, "plan": {"type": "array"},
           "goals": {"type": "object"}, "waivers": {"type": "array"}})
    def set_project(self, **changes):
        p = self.project()
        for key, value in changes.items():
            p[key] = value
        p.save()
        self.app.project_updated(None)
        for page in self.app.section_pages.values():
            page.project_changed()
        return f"Saved {', '.join(changes)} in {p.name}/flowrunner.json."

    @tool("edit", "Create a new project in the work area with a starter module, and select it.",
          {"name": S, "top": S}, ["name"])
    def create_project(self, name, top=""):
        if not self.app.flow:
            raise ToolError("No work area is open.")
        try:
            ws.new_project(self.app.flow, name, top or None)
        except (ValueError, OSError) as e:
            raise ToolError(str(e))
        self.app.section_pages["project"].fill_projects()
        self.app.set_project(name)
        return f"Created project {name} in {ws.src_dir(self.app.flow) / name}."

    @tool("edit", "Generate a self-checking testbench for a module (tb/<module>_tb.v) and add it as a test.",
          {"module": S}, ["module"])
    def make_testbench(self, module):
        mods = self.project().modules()
        if module not in mods:
            raise ToolError(f"No module {module}.")
        self.app.section_pages["rtl"].make_testbench(mods[module])
        return f"Wrote tb/{module}_tb.v and added test {module}_tb. Fill in its stimulus and checks."

    @tool("edit", "Generate a formal wrapper with starter assertions for a module (formal/<module>_formal.sv) and "
                  "add it as a formal task.", {"module": S}, ["module"])
    def make_formal(self, module):
        mods = self.project().modules()
        if module not in mods:
            raise ToolError(f"No module {module}.")
        self.app.section_pages["rtl"].make_formal(mods[module])
        return f"Wrote formal/{module}_formal.sv and added a formal task. Replace the starter properties."

    @tool("edit", "Draw a diagram and save it in the project's docs folder. Either from_rtl (a block diagram of "
                  "that module) or shapes and edges. shapes: [{id, kind, label, x, y, w?, h?, color (blue|green|"
                  "amber|red|grey|purple|white), fill?, stroke? (hex), bold?, font?, align?, dashed?}]. kinds: "
                  "block rounded ellipse diamond text note hexagon parallelogram triangle cylinder cloud container "
                  "| terminator process decision data document | module port register mux adder fifo memory "
                  "state clock bus | and or not xor nand nor buffer. edges: [{src, dst, label?, dashed?, bus?, "
                  "style (orthogonal|straight|curved), head, tail (arrow|open|diamond|dot|none), src_side, "
                  "dst_side (left|right|top|bottom), points: [[x, y]]}] using the shape ids. Lay shapes out left "
                  "to right on a 10 px grid, about 200 px apart; then look(diagram) to check the result.",
          {"name": S, "from_rtl": S, "shapes": {"type": "array"}, "edges": {"type": "array"}}, ["name"])
    def draw_diagram(self, name, from_rtl="", shapes=(), edges=()):
        import diagram as dg
        from diagram_page import SUFFIX
        p = self.project()
        if from_rtl:
            mods = p.modules()
            if from_rtl not in mods:
                raise ToolError(f"No module {from_rtl}.")
            d = dg.from_rtl(mods, from_rtl)
        else:
            d, ids = dg.Diagram(), {}
            for s in shapes:
                kind = s.get("kind", "block")
                if kind not in dg.KINDS:
                    raise ToolError(f"Unknown shape kind {kind}.")
                style = {k: s[k] for k in ("fill", "stroke", "bold", "font", "align", "dashed", "text_color")
                         if k in s}
                made = d.add_shape(kind, s.get("x", 0), s.get("y", 0), s.get("label", ""), s.get("w"), s.get("h"),
                                   s.get("color", "blue") if s.get("color", "blue") in dg.COLORS else "blue",
                                   **style)
                ids[str(s.get("id", made["id"]))] = made["id"]
            for e in edges:
                src, dst = ids.get(str(e.get("src"))), ids.get(str(e.get("dst")))
                if src is None or dst is None:
                    raise ToolError(f"Edge {e} names a shape that is not in shapes.")
                style = {k: e[k] for k in ("style", "head", "tail", "src_side", "dst_side", "points", "color")
                         if k in e}
                d.connect(src, dst, e.get("label", ""), bool(e.get("dashed")), bus=int(e.get("bus", 0)), **style)
            d.title = name
        page = self.app.section_pages["diagram"]
        path = page.folder() / (re.sub(r"[^\w-]", "_", name) + SUFFIX)
        page.d, page.undo_stack = d, []
        page.save(path)
        page.resize_area()
        return f"Saved {self.show(path.resolve())} with {len(d.shapes)} shapes and {len(d.edges)} connections."

    @tool("edit", "Change the floorplan plan of the selected ASIC design and, with apply, write it into the "
                  "design. mode: util (size from utilization) or fixed (die_w x die_h). place_macros gives the "
                  "macros a first legal placement. Returns the plan and its checks; fix errors before applying.",
          {"mode": {**S, "enum": ["util", "fixed"]}, "util": {"type": "number"}, "aspect": {"type": "number"},
           "margin": {"type": "number"}, "die_w": {"type": "number"}, "die_h": {"type": "number"},
           "place_macros": {"type": "boolean"}, "apply": {"type": "boolean"}})
    def set_floorplan(self, place_macros=False, apply=False, **settings):
        plan = self.planner()
        if settings:
            plan.set(**settings)
            plan.fill()
        if place_macros:
            plan.place_macros()
        applied = plan.apply() if apply else False
        return json.dumps({"settings": plan.s, "plan": plan.plan, "checks": plan.checks,
                           "applied_to_design": bool(applied)}, indent=1)

    # ---------- runs ----------
    @tool("run", "Lint the project's design files; returns the problems with file and line.",
          {"tool": {**S, "enum": ["verilator", "iverilog", "yosys", "all"]}})
    def run_lint(self, done, tool=""):
        self.project()
        rtl = self.app.section_pages["rtl"]
        if not rtl.run_lint(tool or None, then=lambda problems: done(
                f"{rtl.count_text(problems)}\n" + json.dumps(problems[:80], indent=1))):
            done("Lint did not start: no linter is installed, or the project has no design files.")

    @tool("run", "Run the design checks (latches, combinational loops, multiple drivers, undriven nets) and the "
                  "clock domain crossing analysis; returns the findings and the crossings. Lint is run_lint.")
    def run_checks(self, done):
        self.project()
        rtl = self.app.section_pages["rtl"]

        def report(result):
            found = [f for f in rtl.findings if f.get("source") in ("design", "cdc")]
            done(json.dumps({"findings": found[:120], "cdc": rtl.cdc_result and
                             {"domains": rtl.cdc_result["domains"],
                              "crossings": rtl.cdc_result["crossings"][:80]}}, indent=1))
        if not rtl.run_cdc(then=report):
            done("The checks did not start: Yosys is missing, a check is running, or there are no design files.")

    @tool("run", "Run simulation tests in parallel (all of them when names is empty), every seed of each, with "
                  "coverage when coverage is true (Verilator tests). Returns each run's verdict, failing logs, "
                  "and the coverage percentages.",
          {"names": {"type": "array", "items": S}, "coverage": {"type": "boolean"}})
    def run_tests(self, done, names=(), coverage=False):
        p = self.project()
        tests = [t for t in p["tests"] if not names or t["name"] in names]
        if not tests:
            raise ToolError("No such tests. Add one with make_testbench or set_project(tests=…).")
        page = self.app.section_pages["verify"]
        if page.busy():
            raise ToolError("Something is already running in Verify.")
        wanted = {t["name"] for t in tests}
        if coverage:
            page.cov_check.set_active(True)

        def report(res):
            runs = {k: {**r, "log": "" if r.get("state") == "pass" else clip(r.get("log", ""), 2500)}
                    for k, r in res.items() if r.get("test", k) in wanted}
            cov = page.results.get("coverage") or {}
            done(json.dumps({"runs": runs, "coverage": {k: v["percent"] for k, v in cov.items()
                                                        if isinstance(v, dict) and v.get("total")}}, indent=1))
        if page.run_tests(tests, then=report, use_tag=False) is False:
            raise ToolError("The tests did not start.")

    @tool("run", "Run everything sign-off needs, in order: lint, design checks, CDC, the regression with "
                  "coverage, and every formal task. Returns the gate's items and verdict. Can take minutes.")
    def run_signoff(self, done):
        self.project()
        page = self.app.section_pages["verify"]
        if page.busy():
            raise ToolError("Something is already running in Verify.")

        def report(verdict):
            items, _v, _f = page.signoff_state()
            folder = page.export_report()
            done(json.dumps({"verdict": verdict, "items": items, "report": str(folder / "signoff.html")}, indent=1))
        page.run_signoff(then=report)

    @tool("run", "Run one formal task (SymbiYosys) and return its verdict, failed properties and log end.",
          {"name": S}, ["name"])
    def run_formal(self, done, name):
        p = self.project()
        task = next((t for t in p["formal"] if t["name"] == name), None)
        if not task:
            raise ToolError(f"No formal task {name}.")
        page = self.app.section_pages["verify"]
        if page.busy():
            raise ToolError("Something is already running in Verify.")
        page.on_run_formal(task, then=lambda r: done(json.dumps({**r, "log": clip(r.get("log", ""), 3000)},
                                                                indent=1)))

    @tool("run", "Run logical equivalence checks on the selected ASIC design (RTL vs synthesized netlist, "
                  "synthesized vs final). Needs the flow to have run.",
          {"checks": {"type": "array", "items": {**S, "enum": ["rtl_synth", "synth_final", "synth_stage"]}}})
    def run_lec(self, done, checks=()):
        page = self.app.lec
        if page.console.busy():
            raise ToolError("An LEC check is already running.")
        page.refresh()
        page.run(list(checks) or ["rtl_synth", "synth_final"], then=lambda res: done(json.dumps(res, indent=1)))

    @tool("run", "Build the FPGA bitstream for the project's FPGA target (set it with set_project(fpga=…)); "
                  "returns timing (Fmax vs target) and utilization.")
    def run_fpga(self, done):
        self.project()
        page = self.app.section_pages["fpga"]
        if page.console.busy():
            raise ToolError("An FPGA build is already running.")
        if page.run_build(then=lambda last: done(json.dumps(last, indent=1, default=str))) is False:
            done("The build did not start: check the FPGA target, pins and installed tools (get_overview).")

    @tool("run", "Run the ASIC flow on the selected design from one stage to another (from_stage 'continue' "
                  "keeps finished stages). Can take minutes to hours. Returns the exit status and the sign-off "
                  "metrics.",
          {"from_stage": {**S, "enum": ["continue", "synth", "floorplan", "place", "cts", "route", "finish"]},
           "to_stage": {**S, "enum": ["synth", "floorplan", "place", "cts", "route", "finish"]}},
          ["to_stage"])
    def run_asic(self, done, to_stage, from_stage="continue"):
        a = self.app
        if a.job and a.job.running():
            raise ToolError("An ASIC run is already going.")
        a.from_cb.set_active_id(from_stage)
        a.to_cb.set_active_id(to_stage)
        _cmds, _notes, err = a.plan()
        if err:
            raise ToolError(err)
        a.show_section("asic")
        a.on_run(confirmed=True)
        if not (a.job and a.job.running()):
            raise ToolError("The run did not start; see the ASIC console.")
        job = a.job

        def poll():
            if job.running():
                return True
            tail = clip(Path(job.log_path).read_text(errors="replace")[-4000:], 4000)
            done(f"Run {'finished' if job.rc == 0 else f'failed (exit code {job.rc})'}. Log: {job.log_path}\n"
                 f"{tail}\n{self.asic_metrics()}")
            return False
        from gi.repository import GLib
        GLib.timeout_add_seconds(5, poll)


# ---------------------------------------------------------------- the conversation
class Agent:
    """One chat with Claude. send() runs on a worker thread and talks to the UI only through bridge:
    bridge.call(fn, *args) runs fn on the GTK thread and returns its value; bridge.start(fn, *args) runs a run
    tool there and waits for its done(text); bridge.approve(name, summary, detail) asks the user;
    bridge.event(kind, payload) shows progress: text, thinking, tool, tool_done, status, error."""

    def __init__(self, toolbox, model=MODELS[0][0], effort="high", api_key="", client=None):
        self.toolbox, self.model, self.effort = toolbox, model, effort
        self.api_key, self.client = api_key, client
        self.messages = []
        self.cancelled = threading.Event()
        self.allowed = set()      # tool kinds the user allowed for the rest of this chat
        self.backups = {}         # undo token: (path, text before) of each file write this chat
        self.usage = {"input": 0, "output": 0, "cached": 0}

    def make_client(self):
        if self.client is None:
            anthropic = load_sdk()
            if anthropic is None:
                raise RuntimeError("The Claude SDK is not installed; use Install in the assistant's settings.")
            self.client = anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
        return self.client

    def reset(self, keep_allowed=False):
        self.messages, self.backups = [], {}
        self.usage = {"input": 0, "output": 0, "cached": 0}
        if not keep_allowed:
            self.allowed = set()

    def undo(self, token):
        """Put back the file an edit changed; the file's name, or None if the edit is not known any more."""
        backup = self.backups.pop(token, None)
        return self.toolbox.restore(backup) if backup else None

    def cost(self):
        """A rough cost of this chat in US dollars, from the token counts and the model's list price."""
        price_in, price_out = PRICES.get(self.model, (4.0, 20.0))
        u = self.usage
        return (u["input"] * price_in + u["cached"] * price_in * 0.1 + u["output"] * price_out) / 1e6

    def send(self, text, context, bridge):
        """Add the user's message and loop until Claude stops asking for tools. Returns the final text."""
        self.cancelled.clear()
        content = [{"type": "text", "text": text}]
        if context:
            content.append({"type": "text", "text": f"<app_state>{context}</app_state>"})
        self.messages.append({"role": "user", "content": content})
        try:
            return self._loop(bridge)
        except Exception as e:  # noqa: BLE001 - the chat must survive any failure; the user sees what went wrong
            self._tidy()
            bridge.event("error", self.explain(e))
            return ""

    def explain(self, e):
        anthropic = load_sdk()
        if anthropic:
            if isinstance(e, anthropic.AuthenticationError):
                return "Claude rejected the credentials: set an API key in the assistant's settings, or run " \
                       "`ant auth login`."
            if isinstance(e, anthropic.PermissionDeniedError):
                return f"This key may not use {self.model}: {e.message}"
            if isinstance(e, anthropic.RateLimitError):
                return "Rate limited by the API; wait a minute and send again."
            if isinstance(e, anthropic.APIStatusError):
                return f"API error {e.status_code}: {e.message}"
            if isinstance(e, anthropic.APIConnectionError):
                return "Could not reach the Claude API; check the network."
        return f"{type(e).__name__}: {e}"

    def _tidy(self):
        """Leave the history valid after a failure: drop a last assistant turn whose tools never answered."""
        if self.messages and self.messages[-1]["role"] == "assistant":
            self.messages.pop()

    def _loop(self, bridge):
        client = self.make_client()
        tools = Toolbox.definitions()
        specs = Toolbox.specs()
        final, json_retries = "", 0
        while True:
            if self.cancelled.is_set():
                bridge.event("status", "Stopped.")
                return final
            bridge.event("status", "Thinking…")
            try:
                with client.beta.messages.stream(
                        model=self.model, max_tokens=64000, system=SYSTEM, messages=self.messages, tools=tools,
                        thinking={"type": "adaptive", "display": "summarized"},
                        output_config={"effort": self.effort}, cache_control={"type": "ephemeral"},
                        betas=["server-side-fallback-2026-07-01"], fallbacks="default") as stream:
                    for event in stream:
                        if self.cancelled.is_set():
                            break
                        if event.type == "text":
                            bridge.event("text", event.text)
                        elif event.type == "thinking":
                            bridge.event("thinking", event.thinking)
                    if self.cancelled.is_set():
                        bridge.event("status", "Stopped.")
                        return final
                    response = stream.get_final_message()
                json_retries = 0
            except ValueError:
                # Tool input the SDK could not parse at all: no tool_use id to answer, so ask again (bounded).
                json_retries += 1
                if json_retries > 2:
                    raise
                continue
            self.messages.append({"role": "assistant", "content": response.content})
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.usage["input"] += (getattr(usage, "input_tokens", 0) or 0) + \
                    (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                self.usage["cached"] += getattr(usage, "cache_read_input_tokens", 0) or 0
                self.usage["output"] += getattr(usage, "output_tokens", 0) or 0
                bridge.event("usage", dict(self.usage, cost=self.cost()))
            final = "".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "pause_turn":
                continue
            uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "refusal":
                bridge.event("error", "Claude declined this request.")
                self._tidy()
                return final
            if not uses:
                bridge.event("status", "")
                return final
            if response.stop_reason == "max_tokens":
                self._tidy()
                raise RuntimeError("The reply was cut off in the middle of a tool call; ask for a smaller step.")
            results = [self.run_tool(b, specs, bridge) for b in uses]
            self.messages.append({"role": "user", "content": results})

    def run_tool(self, block, specs, bridge):
        def result(text, error=False, image=None, undo=None):
            bridge.event("tool_done", {"id": block.id, "name": block.name, "text": text, "error": error,
                                       "undo": undo})
            content = text
            if image:
                data = base64.standard_b64encode(Path(image).read_bytes()).decode()
                content = [{"type": "text", "text": text},
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]
            return {"type": "tool_result", "tool_use_id": block.id, "content": content, "is_error": error}
        spec = specs.get(block.name)
        if spec is None:
            return result(f"Unknown tool {block.name}.", True)
        bad = check_input(spec["schema"], block.input)
        if bad:
            return result(json.dumps({"INVALID_JSON": bad}), True)
        bridge.event("tool", {"id": block.id, "name": block.name, "input": block.input, "kind": spec["kind"]})
        if self.cancelled.is_set():
            return result("Stopped by the user before it ran.", True)
        if spec["kind"] == "ask":
            answer = bridge.ask(block.input["question"], [str(o) for o in block.input["options"]][:4])
            return result(f"The user answered: {answer}" if answer else "The user gave no answer; decide yourself "
                          "and say what you assumed.")
        if spec["kind"] not in ("read", "ask") and spec["kind"] not in self.allowed:
            detail = self.preview(block.name, block.input)
            answer = bridge.approve(block.name, spec["kind"], detail)
            if answer == "always":
                self.allowed.add(spec["kind"])
            elif answer != "yes":
                return result("The user denied this. Do not retry it; ask what they want instead.", True)
        fn = getattr(self.toolbox, block.name)
        started = time.time()
        self.toolbox.last_backup = None
        try:
            if spec["kind"] == "run":
                text = bridge.start(fn, **block.input)
            else:
                text = bridge.call(fn, **block.input)
        except ToolError as e:
            return result(str(e), True)
        except Exception as e:  # noqa: BLE001 - a bug in a tool should not end the chat; Claude hears what failed
            return result(f"{type(e).__name__}: {e}", True)
        if spec["kind"] == "run":
            text = f"(took {time.time() - started:.0f} s)\n{text}"
        if block.name == "update_tasks":
            bridge.event("tasks", self.toolbox.tasks)
        image = None
        if isinstance(text, dict):
            text, image = text["text"], text.get("image")
        undo = None
        if self.toolbox.last_backup:
            undo = f"u{len(self.backups)}"
            self.backups[undo] = self.toolbox.last_backup
        return result(text, image=image, undo=undo)

    def preview(self, name, args):
        """What the user sees before approving: a diff for edits, the request for everything else."""
        tb = self.toolbox
        try:
            if name == "edit_file":
                f = tb.path(args["path"], write=True)
                old = f.read_text(errors="replace")
                return diff_text(old, old.replace(args["old_text"], args["new_text"], 1), args["path"])
            if name == "write_file":
                f = tb.path(args["path"], write=True)
                return diff_text(f.read_text(errors="replace") if f.exists() else "", args["content"], args["path"])
        except (ToolError, OSError) as e:
            return f"({e})"
        if not args:
            return "With the settings shown in its section."
        if name == "run_asic" and args.get("from_stage", "continue") != "continue":
            return json.dumps(args, indent=1) + f"\nThis deletes the results of {args['from_stage']} and the " \
                                                "stages after it, then runs them again."
        return json.dumps(args, indent=1)
