"""Verify section: regressions, waveforms, coverage, formal checks, the verification plan and sign-off.

Tests and formal tasks are stored in the project's flowrunner.json (see workspace.py). A regression runs the
tests in parallel, each seed as its own run (regress.py), and gives every run a verdict (sim.py); coverage of all
runs is merged (coverage.py). Results are kept in build/<project>/verify_results.json so they survive a restart
and the assistant can read them. The Sign-off tab rolls lint, design checks, CDC, tests, formal, coverage and the
verification plan into one verdict (signoff.py) and exports an HTML report and JUnit XML.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

import coverage  # noqa: E402
import dialogs  # noqa: E402
import eda_tools  # noqa: E402
import formal  # noqa: E402
import regress  # noqa: E402
import signoff  # noqa: E402
import sim  # noqa: E402
import theme  # noqa: E402
from input_page import MUTED, card, hint_label, markup  # noqa: E402
from runner import Console  # noqa: E402
from wave_view import WaveView  # noqa: E402

STATE_COLOR = {"pass": "success", "fail": "danger", "ran": "info", "unknown": "warning", "error": "danger",
               "running": "info", "timeout": "danger", "stopped": "warning", "not run": "text_muted",
               "no checks": "warning", "": "text_muted"}
TAB_WAVES, TAB_COVERAGE, TAB_OUTPUT, TAB_RUNS, TAB_PLAN, TAB_SIGNOFF = range(6)
COCOTB = "cocotb (Python)"


def state_markup(state, text=None):
    return f'<span foreground="{theme.color(STATE_COLOR.get(state, "text_muted"))}">●</span> ' \
           + GLib.markup_escape_text(text or state or "not run")


def table(store, columns, activated=None, height=120, expand=True):
    """A list in a scrolled window. columns: [(title, column index, "text" | "markup", extra cell properties)]."""
    tv = Gtk.TreeView(model=store)
    for title, index, kind, props in columns:
        cell = Gtk.CellRendererText(**props)
        tv.append_column(Gtk.TreeViewColumn(title, cell, **{kind: index}))
    if activated:
        tv.connect("row-activated", activated)
    sw = Gtk.ScrolledWindow(min_content_height=height, vexpand=expand)
    sw.add(tv)
    return tv, sw


def buttons(specs):
    """A row of buttons from [(label, handler, tooltip, suggested)]."""
    row = Gtk.Box(spacing=6)
    for label, handler, tip, suggested in specs:
        b = Gtk.Button(label=label, tooltip_text=tip)
        if suggested:
            b.get_style_context().add_class("suggested-action")
        b.connect("clicked", handler)
        row.pack_start(b, False, False, 0)
    return row


class VerifyPage:
    name, title = "verify", "Verify"

    def __init__(self, app):
        self.app = app
        self.results = {"tests": {}, "formal": {}}
        self.reg = None            # the regression that is running, or the last one
        self.wave_file = None
        self.widget = self.build()

    # ---------- layout ----------
    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=6)
        head.get_style_context().add_class("inputs-head")
        self.project_label = Gtk.Label(xalign=0)
        head.pack_start(self.project_label, False, False, 0)
        self.summary = Gtk.Label(xalign=1, hexpand=True)
        head.pack_end(self.summary, True, True, 0)
        outer.pack_start(head, False, False, 0)

        paned = Gtk.Paned(position=500)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)

        tests = card("Simulation tests", "A test is a testbench (Verilog, or Python with cocotb) run with the design "
                                         "sources. Run all is a regression: every test and seed, in parallel.")
        # name, how it runs, seeds, tags, verdict markup
        self.tstore = Gtk.ListStore(str, str, str, str, str)
        self.ttv, sw = table(self.tstore, [("Test", 0, "text", {"weight": 600}),
                                           ("Runs with", 1, "text", {"foreground": MUTED}),
                                           ("Seeds", 2, "text", {"foreground": MUTED}),
                                           ("Tags", 3, "text", {"foreground": MUTED}),
                                           ("Result", 4, "markup", {})], lambda *_: self.on_run_test(), 150)
        self.ttv.get_selection().connect("changed", lambda *_: self.on_test_selected())
        tests.pack_start(sw, True, True, 0)
        tests.pack_start(buttons([
            ("Run", lambda *_: self.on_run_test(), "Run the selected test (all its seeds)", False),
            ("Run all", lambda *_: self.run_regression(), "Run every test and seed: a regression", True),
            ("Run failed", lambda *_: self.run_failed(), "Run again only the tests that did not pass", False),
            ("Add…", lambda *_: self.on_edit_test(None), "Add a test", False),
            ("Edit…", lambda *_: self.on_edit_test(self.selected_test()), "Change the selected test", False),
            ("Remove", lambda *_: self.on_remove_test(), "Remove the selected test (its files stay)", False)]),
            False, False, 0)
        opts = Gtk.Box(spacing=8)
        opts.pack_start(Gtk.Label(label="Parallel runs"), False, False, 0)
        cpus = os.cpu_count() or 2
        self.jobs = Gtk.SpinButton.new_with_range(1, max(cpus, 1), 1)
        self.jobs.set_value(min(4, cpus))
        self.jobs.set_tooltip_text("How many builds and simulations run at the same time")
        opts.pack_start(self.jobs, False, False, 0)
        opts.pack_start(Gtk.Label(label="Tag"), False, False, 0)
        self.tag_cb = Gtk.ComboBoxText(tooltip_text="Run all runs only the tests with this tag")
        opts.pack_start(self.tag_cb, False, False, 0)
        self.stop_btn = Gtk.Button(label="Stop", sensitive=False, tooltip_text="Stop the regression")
        self.stop_btn.get_style_context().add_class("destructive-action")
        self.stop_btn.connect("clicked", lambda *_: self.reg and self.reg.stop())
        opts.pack_end(self.stop_btn, False, False, 0)
        tests.pack_start(opts, False, False, 0)
        self.cov_check = Gtk.CheckButton(label="Measure coverage: line, branch, toggle, cover points (Verilator)")
        tests.pack_start(self.cov_check, False, False, 0)
        left.pack_start(tests, True, True, 0)

        ftasks = card("Formal checks", "SymbiYosys proves the assertions of a property file: bmc looks for a "
                                       "failure up to the depth, prove proves them for every cycle, cover shows "
                                       "each cover can be reached.")
        self.fstore = Gtk.ListStore(str, str, str)
        self.ftv, sw = table(self.fstore, [("Task", 0, "text", {"weight": 600}),
                                           ("Mode", 1, "text", {"foreground": MUTED}),
                                           ("Result", 2, "markup", {})], lambda *_: self.on_run_formal(), 90, False)
        ftasks.pack_start(sw, False, False, 0)
        ftasks.pack_start(buttons([
            ("Run", lambda *_: self.on_run_formal(), "Run the selected task", False),
            ("Run all", lambda *_: self.run_all_formal(), "Run every formal task, one after another", False),
            ("Add…", lambda *_: self.on_edit_formal(None), "Add a formal task", False),
            ("Edit…", lambda *_: self.on_edit_formal(self.selected_formal()), "Change the selected task", False),
            ("Remove", lambda *_: self.on_remove_formal(), "Remove the selected task", False)]), False, False, 0)
        left.pack_start(ftasks, False, False, 0)
        paned.pack1(left, False, False)

        self.tabs = Gtk.Notebook(margin=14)
        for label, page in (("Waveforms", self.build_waves()), ("Coverage", self.build_coverage()),
                            ("Output", self.build_output()), ("Runs", self.build_runs()),
                            ("Plan", self.build_plan()), ("Sign-off", self.build_signoff())):
            self.tabs.append_page(page, Gtk.Label(label=label))
        self.tabs.connect("switch-page", lambda _nb, _page, n: n == TAB_SIGNOFF and self.refresh_signoff())
        paned.pack2(self.tabs, True, False)
        outer.pack_start(paned, True, True, 0)
        return outer

    def build_waves(self):
        self.waves = WaveView()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.pack_start(self.waves, True, True, 0)
        row = Gtk.Box(spacing=6)
        for label, tool in (("Open in GTKWave", "gtkwave"), ("Open in Surfer", "surfer")):
            b = Gtk.Button(label=label, tooltip_text=f"Open the waveform in {tool}, outside the app")
            b.connect("clicked", lambda _b, t=tool: self.open_external(t))
            row.pack_start(b, False, False, 0)
        b = Gtk.Button(label="Open file…")
        b.connect("clicked", self.on_open_wave)
        row.pack_start(b, False, False, 0)
        box.pack_start(row, False, False, 0)
        return box

    def build_coverage(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        top = Gtk.Box(spacing=8)
        self.cov_label = hint_label("Run tests with coverage on to see what the tests exercise.")
        top.pack_start(self.cov_label, True, True, 0)
        goals = Gtk.Button(label="Goals…", tooltip_text="The coverage each kind must reach for sign-off")
        goals.connect("clicked", self.on_goals)
        top.pack_end(goals, False, False, 0)
        box.pack_start(top, False, False, 0)
        # kind, covered, goal, hit / total, state markup
        self.kstore = Gtk.ListStore(str, str, str, str, str)
        _tv, sw = table(self.kstore, [("Kind", 0, "text", {"weight": 600}), ("Covered", 1, "text", {}),
                                      ("Goal", 2, "text", {"foreground": MUTED}), ("Hit / total", 3, "text", {}),
                                      ("", 4, "markup", {})], None, 150, False)
        box.pack_start(sw, False, False, 0)
        # file, points hit, points, percent, missed lines
        self.cstore = Gtk.ListStore(str, int, int, str, str)
        _tv, sw = table(self.cstore, [("File", 0, "text", {}), ("Hit", 1, "text", {}), ("Statements", 2, "text", {}),
                                      ("Covered", 3, "text", {}),
                                      ("Lines never run", 4, "text", {"ellipsize": Pango.EllipsizeMode.END})],
                        self.on_coverage_row, 90)
        box.pack_start(sw, True, True, 0)
        box.pack_start(hint_label("Not covered: cover points never hit and signal bits that never toggled. "
                                  "Double-click to open the place in the editor."), False, False, 0)
        # kind, what, file, line
        self.ustore = Gtk.ListStore(str, str, str, int)
        _tv, sw = table(self.ustore, [("Kind", 0, "text", {"foreground": MUTED}), ("Not covered", 1, "text", {}),
                                      ("File", 2, "text", {}), ("Line", 3, "text", {})], self.on_uncovered_row, 90)
        box.pack_start(sw, True, True, 0)
        return box

    def build_output(self):
        self.console = Console("Simulator and prover output")
        box = Gtk.Box(margin=8)
        box.pack_start(self.console, True, True, 0)
        return box

    def build_runs(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.runs_label = hint_label("Each test and seed is one run. Double-click a run for its waveform; Show log "
                                     "prints its output.")
        box.pack_start(self.runs_label, False, False, 0)
        # run id, seed, verdict markup, seconds, why
        self.rstore = Gtk.ListStore(str, str, str, str, str)
        self.rtv, sw = table(self.rstore, [("Run", 0, "text", {"weight": 600}), ("Seed", 1, "text", {}),
                                           ("Result", 2, "markup", {}), ("Time", 3, "text", {"foreground": MUTED}),
                                           ("Why", 4, "text", {"ellipsize": Pango.EllipsizeMode.END})],
                             lambda *_: self.on_run_row(False), 200)
        box.pack_start(sw, True, True, 0)
        box.pack_start(buttons([("Show log", lambda *_: self.on_run_row(True), "Print the run's output", False),
                                ("Open waveform", lambda *_: self.on_run_row(False), "Show the run's waveform",
                                 False)]), False, False, 0)
        self.history_label = hint_label("")
        box.pack_start(self.history_label, False, False, 0)
        return box

    def build_plan(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.pack_start(hint_label("The verification plan lists the features the design must be shown to have, and "
                                  "the tests, formal tasks and cover points that show each one. A feature is covered "
                                  "when all of them pass."), False, False, 0)
        # feature, checks, status markup, detail
        self.pstore = Gtk.ListStore(str, str, str, str)
        self.ptv, sw = table(self.pstore, [("Feature", 0, "text", {"weight": 600}),
                                           ("Checks", 1, "text", {"foreground": MUTED,
                                                                  "ellipsize": Pango.EllipsizeMode.END}),
                                           ("Status", 2, "markup", {}),
                                           ("Detail", 3, "text", {"ellipsize": Pango.EllipsizeMode.END})],
                             lambda *_: self.on_edit_plan(self.selected_plan()), 200)
        box.pack_start(sw, True, True, 0)
        box.pack_start(buttons([("Add…", lambda *_: self.on_edit_plan(None), "Add a feature to verify", False),
                                ("Edit…", lambda *_: self.on_edit_plan(self.selected_plan()), "Change it", False),
                                ("Remove", lambda *_: self.on_remove_plan(), "Remove the feature", False)]),
                       False, False, 0)
        return box

    def build_signoff(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=8)
        self.verdict = Gtk.Label(xalign=0)
        self.verdict.get_style_context().add_class("page-title")
        box.pack_start(self.verdict, False, False, 0)
        box.pack_start(hint_label("Sign-off needs every check run and clean: no lint, design or CDC errors (waived "
                                  "findings do not count), every test and proof passing, coverage at its goals, and "
                                  "every plan feature covered."), False, False, 0)
        self.tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                                 min_children_per_line=2, homogeneous=True, valign=Gtk.Align.START)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(self.tiles)
        box.pack_start(sw, True, True, 0)
        box.pack_start(buttons([
            ("Run everything", lambda *_: self.run_signoff(), "Lint, design checks, CDC, the regression and every "
                                                              "formal task, one after another", True),
            ("Export report…", lambda *_: self.export_report(), "Write the HTML report and JUnit XML", False),
            ("Open report", lambda *_: self.open_report(), "Open the last exported HTML report", False)]),
            False, False, 0)
        self.report_label = hint_label("")
        box.pack_start(self.report_label, False, False, 0)
        return box

    # ---------- project ----------
    def flow_changed(self):
        pass

    def results_file(self):
        p = self.app.project
        return p.build("sim").parent / "verify_results.json" if p else None

    def project_changed(self):
        p = self.app.project
        self.project_label.set_markup(f"Project <b>{GLib.markup_escape_text(p.name)}</b>" if p else
                                      markup("No project: make or clone one in the Project section.", MUTED))
        self.results = {"tests": {}, "formal": {}}
        path = self.results_file()
        if path and path.exists():
            try:
                self.results.update(json.loads(path.read_text()))
            except ValueError:
                pass
        if p and not p["tests"]:
            self.discover_tests()
        self.fill()
        self.show_coverage()

    def project_updated(self):
        self.fill()

    def discover_tests(self):
        """Register the project's testbench modules as tests the first time the project is opened here."""
        p = self.app.project
        mods = p.modules()
        found = []
        for f in p.testbenches():
            for m in mods.values():
                if str(m.file.relative_to(p.dir)) == f and not m.ports:
                    found.append({"name": m.name, "top": m.name, "files": [f], "simulator": "iverilog",
                                  "args": ""})
        if found:
            p["tests"] = found
            p.save()

    def save_results(self):
        path = self.results_file()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.results, indent=1))

    def runs_of(self, name):
        """Results of the runs of one test: {run id: result}."""
        return {k: r for k, r in self.results["tests"].items() if k == name or r.get("test") == name}

    def fill(self):
        p = self.app.project
        for store in (self.tstore, self.fstore, self.rstore, self.pstore):
            store.clear()
        tag = self.tag_cb.get_active_id()
        self.tag_cb.remove_all()
        self.tag_cb.append("", "(all tests)")
        if not p:
            self.summary.set_text("")
            return
        for t in sorted({t for test in p["tests"] for t in test.get("tags", [])}):
            self.tag_cb.append(t, t)
        self.tag_cb.set_active_id(tag or "")
        if self.tag_cb.get_active_id() is None:
            self.tag_cb.set_active_id("")
        for t in p["tests"]:
            runs = self.runs_of(t["name"])
            states = [r.get("state", "") for r in runs.values()]
            if not runs:
                shown = state_markup("")
            elif len(runs) == 1:
                r = next(iter(runs.values()))
                shown = state_markup(r.get("state", ""), r.get("why") and f"{r['state']}: {r['why']}"[:60])
            else:
                worst = next((s for s in ("running", "fail", "timeout", "error", "stopped", "ran") if s in states),
                             "pass")
                shown = state_markup(worst, f"{states.count('pass')} of {len(states)} seeds pass")
            how = "cocotb" if t.get("kind") == "cocotb" else t.get("simulator", "iverilog")
            self.tstore.append([t["name"], how, str(t.get("seeds", 1) or 1), " ".join(t.get("tags", [])), shown])
        for name, r in sorted(self.results["tests"].items()):
            self.rstore.append([name, str(r.get("seed") or ""), state_markup(r.get("state", "")),
                                f"{r['seconds']} s" if "seconds" in r else "", str(r.get("why", ""))])
        for t in p["formal"]:
            r = self.results["formal"].get(t["name"], {})
            self.fstore.append([t["name"], f"{t.get('mode', 'bmc')} {t.get('depth', 20)}",
                                state_markup(r.get("state", ""), r.get("why", "")[:60] or None)])
        for item, state, detail in signoff.plan_status(p["plan"], self.results):
            checks = ", ".join(item.get("tests", []) + item.get("formal", []) + item.get("cover", []))
            self.pstore.append([item["feature"], checks, state_markup(state), detail])
        t = regress.tally({k: r for k, r in self.results["tests"].items() if r.get("state") != "running"})
        self.summary.set_markup(markup(
            f"{t['total']} runs: {t['passed']} passed, {t['failed']} failed, {t['other']} other"
            if t["total"] else ("No tests yet: add one, or make a testbench in the RTL section." if not p["tests"]
                                else f"{len(p['tests'])} tests, not run yet"), MUTED))
        hist = regress.load_history(p)
        self.history_label.set_text("Last regressions: " + "   ".join(
            f"{h['when'][5:16]} {h['passed']}/{h['total']}" for h in hist[-6:][::-1]) if hist else "")

    # ---------- tests ----------
    def busy(self):
        return self.console.busy() or bool(self.reg and self.reg.running())

    def selected_test(self):
        model, it = self.ttv.get_selection().get_selected()
        p = self.app.project
        return next((t for t in p["tests"] if t["name"] == model[it][0]), None) if it and p else None

    def on_test_selected(self):
        t = self.selected_test()
        if t and not self.busy():
            for r in self.runs_of(t["name"]).values():
                waves = sim.waveforms(r["folder"]) if r.get("folder") else []
                if waves:
                    if waves[0] != self.wave_file:
                        self.show_wave(waves[0])
                    break

    def on_edit_test(self, test):
        p = self.app.project
        if not p:
            return
        tops = [m.name for m in p.modules().values() if not m.ports] or [""]
        sims = [s for s, _ in sim.available(self.app.rtl_tools())] or ["iverilog"]
        old = test or {"name": "", "top": tops[0], "files": p.testbenches()[:1], "simulator": sims[0], "args": ""}
        is_cocotb = old.get("kind") == "cocotb"

        def check(v):
            if not v["name"].replace("_", "").isalnum():
                return "Give the test a name: letters, digits and underscores."
            if any(t["name"] == v["name"] for t in p["tests"] if t is not test):
                return "A test with that name exists."
            if not v["top"]:
                return "Give the top module."
            if not (v["seeds"].isdigit() and int(v["seeds"]) >= 1):
                return "Seeds is a whole number, 1 or more."
            if not (v["seed"].isdigit() or v["seed"] == "random"):
                return "First seed is a whole number, or the word random."
            if not v["timeout"].isdigit():
                return "Timeout is in whole seconds."
            if v["how"] == COCOTB and not (v["files"].split() and (p.dir / v["files"].split()[0]).exists()):
                return "For cocotb, Files is the Python test module, such as tests/test_top.py."
            return None
        vals = dialogs.ask_fields(
            self.app, "Test" if test else "New test",
            [("name", "Name", old["name"], "text"),
             ("how", "Runs with", COCOTB if is_cocotb else old.get("simulator", "iverilog"), sims + [COCOTB]),
             ("top", "Top module", old["top"], "text"),
             ("files", "Files", old.get("module", "") if is_cocotb else " ".join(old.get("files", [])), "text"),
             ("args", "Run arguments (plusargs)", old.get("args", ""), "text"),
             ("seeds", "Seeds", str(old.get("seeds", 1)), "text"),
             ("seed", "First seed", str(old.get("seed", 1)), "text"),
             ("timeout", "Timeout (s)", str(old.get("timeout", 600)), "text"),
             ("tags", "Tags", " ".join(old.get("tags", [])), "text")],
            intro="Top module: the testbench module, or for cocotb the design's top. Files (relative to the project) "
                  "are the testbench files, or the cocotb Python module. With more than one seed, each seed is a "
                  "run that gets +seed=<n>; \"random\" as the first seed draws new seeds every regression. Tags "
                  "(such as smoke nightly) choose what Run all runs.", check=check)
        if not vals:
            return
        new = {"name": vals["name"], "top": vals["top"], "args": vals["args"], "seeds": int(vals["seeds"]),
               "seed": vals["seed"] if vals["seed"] == "random" else int(vals["seed"]),
               "timeout": int(vals["timeout"]), "tags": vals["tags"].split()}
        if vals["how"] == COCOTB:
            new.update(kind="cocotb", module=vals["files"].split()[0], simulator="iverilog")
        else:
            new.update(files=vals["files"].split(), simulator=vals["how"])
        p["tests"] = [new if t is test else t for t in p["tests"]] if test else p["tests"] + [new]
        p.save()
        self.fill()

    def on_remove_test(self):
        t, p = self.selected_test(), self.app.project
        if t:
            p["tests"] = [x for x in p["tests"] if x is not t]
            p.save()
            for k in list(self.runs_of(t["name"])):
                self.results["tests"].pop(k, None)
            self.save_results()
            self.fill()

    def on_run_test(self):
        t = self.selected_test()
        if t:
            self.run_tests([t], use_tag=False)

    def run_regression(self, then=None):
        """Run every test (with the chosen tag, if any) and every seed; then(results) when all are done."""
        p = self.app.project
        if p and p["tests"]:
            self.run_tests(list(p["tests"]), then)
        elif then:
            then(self.results["tests"])

    def run_failed(self):
        p = self.app.project
        if not p:
            return
        names = {r.get("test", k) for k, r in self.results["tests"].items() if r.get("state") != "pass"}
        tests = [t for t in p["tests"] if t["name"] in names or not self.runs_of(t["name"])]
        if tests:
            self.run_tests(tests, use_tag=False)
        else:
            self.app.set_status("Every test passed: nothing to run again.")

    def run_tests(self, tests, then=None, use_tag=True):
        """Run tests in parallel; then(results of all tests) when done. Returns False if it could not start."""
        p = self.app.project
        if not p or self.busy():
            self.app.set_status("A simulation or proof is still running." if p else "No project.")
            return False
        self.app.section_pages["rtl"].save_all()
        tag = self.tag_cb.get_active_id() if use_tag else ""
        runs = regress.expand(tests, [tag] if tag else None)
        if not runs:
            self.app.set_status("No test has that tag.")
            if then:
                then(self.results["tests"])
            return False
        names = {r["test"]["name"] for r in runs}
        self.results["tests"] = {k: r for k, r in self.results["tests"].items() if r.get("test", k) not in names}
        full = names == {t["name"] for t in p["tests"]}
        self.tabs.set_current_page(TAB_RUNS)
        self.stop_btn.set_sensitive(True)
        self.console.append(f"\nRegression: {len(runs)} run{'s' if len(runs) != 1 else ''}, "
                            f"{int(self.jobs.get_value())} at a time\n", "cmd")
        self.reg = regress.Regression(
            p, runs, jobs=int(self.jobs.get_value()), chosen=self.app.rtl_tools(),
            coverage=self.cov_check.get_active(),
            on_update=lambda run_id, result: GLib.idle_add(self.on_run_update, run_id, result),
            on_done=lambda results: GLib.idle_add(self.on_regression_done, results, then, full))
        self.reg.start()
        return True

    def on_run_update(self, run_id, result):
        self.results["tests"][run_id] = result
        if result.get("state") != "running":
            ok = result["state"] == "pass"
            self.console.append(f"{run_id}: {result['state']} ({result.get('why', '')}) after "
                                f"{result.get('seconds', 0)} s\n", "ok" if ok else "err")
            if not ok and result.get("log"):
                self.console.append(result["log"][-1500:] + "\n")
        self.fill()
        return False

    def on_regression_done(self, results, then, full):
        p = self.app.project
        self.stop_btn.set_sensitive(False)
        self.results["tests"].update(results)
        self.update_coverage()
        t = regress.tally(results)
        self.console.append(f">> {t['passed']} of {t['total']} runs passed in {self.reg.seconds} s\n",
                            "ok" if t["passed"] == t["total"] else "err")
        if full and p:
            regress.add_history(p, results, self.reg.seconds, self.results.get("coverage"))
        self.save_results()
        self.fill()
        self.app.set_status(f"Tests: {t['passed']} passed, {t['failed']} failed")
        # Show a waveform: a failing run's first, since that is the one to debug.
        order = sorted(results.values(), key=lambda r: r.get("state") == "pass")
        wave = next((w for r in order if r.get("folder") for w in sim.waveforms(r["folder"])[:1]), None)
        if wave:
            self.show_wave(wave)
            self.tabs.set_current_page(TAB_WAVES if len(results) == 1 else TAB_RUNS)
        self.refresh_signoff()
        if then:
            then(self.results["tests"])
        return False

    def selected_run(self):
        model, it = self.rtv.get_selection().get_selected()
        return (model[it][0], self.results["tests"].get(model[it][0])) if it else (None, None)

    def on_run_row(self, log):
        run_id, r = self.selected_run()
        if not r:
            return
        if log:
            self.console.append(f"\n---- {run_id} ----\n{r.get('log', '')}\n")
            self.tabs.set_current_page(TAB_OUTPUT)
            return
        waves = sim.waveforms(r["folder"]) if r.get("folder") else []
        if waves:
            self.show_wave(waves[0])
            self.tabs.set_current_page(TAB_WAVES)
        else:
            self.app.set_status(f"{run_id} left no waveform: the testbench needs $dumpfile and $dumpvars.")

    # ---------- waveforms ----------
    def show_wave(self, path, show=None):
        try:
            self.waves.load(path, show)
            self.wave_file = Path(path)
        except (ValueError, OSError) as e:
            self.waves.file_label.set_text(str(e))
            self.wave_file = Path(path)

    def on_open_wave(self, *_):
        dlg = Gtk.FileChooserDialog(title="Waveform", transient_for=self.app, action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        if self.app.project:
            dlg.set_current_folder(str(self.app.project.build("sim")))
        if dlg.run() == Gtk.ResponseType.OK:
            self.show_wave(dlg.get_filename())
            self.tabs.set_current_page(0)
        dlg.destroy()

    def open_external(self, tool):
        exe = eda_tools.find(tool, self.app.rtl_tools()) or shutil.which(tool)
        if not self.wave_file:
            self.app.message("Run a test first, or open a waveform file.")
        elif not exe:
            self.app.message(f"{tool} is not installed; install the RTL tools from Tools….")
        else:
            subprocess.Popen([exe, str(self.wave_file)], env=eda_tools.env(self.app.rtl_tools()),
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------- coverage ----------
    def update_coverage(self):
        """Merge the coverage of every run that has it, and show it."""
        p = self.app.project
        files = [Path(r["folder"]) / "coverage.dat" for r in self.results["tests"].values()
                 if r.get("folder") and (Path(r["folder"]) / "coverage.dat").exists()]
        if not files or not p:
            return
        cov = coverage.summarize(coverage.read(files, p.dir), p.sources())
        # "percent" and "files" are the line coverage, kept under their old names for readers of the results file.
        cov.update(percent=cov["line"]["percent"], files=cov["line"]["files"], runs=len(files))
        cov["toggle"]["missed"] = cov["toggle"]["missed"][:300]
        self.results["coverage"] = cov
        self.show_coverage()

    def show_coverage(self):
        p = self.app.project
        cov = self.results.get("coverage", {})
        for store in (self.kstore, self.cstore, self.ustore):
            store.clear()
        rtl = self.app.section_pages.get("rtl")
        if rtl:
            rtl.set_coverage(cov if cov.get("line", {}).get("total") else None)
        if not p or not cov.get("line", {}).get("total"):
            self.cov_label.set_text("Run tests with coverage on to see what the tests exercise.")
            return
        for kind, label, pct, goal, state in coverage.check_goals(cov, p["goals"]):
            k = cov[kind]
            if k["total"]:
                self.kstore.append([label, f"{pct:.1f}%", f"{goal:.0f}%" if goal else "no goal",
                                    f"{k['hit']} / {k['total']}",
                                    state_markup(state, {"pass": "goal met", "fail": "below goal"}.get(state, ""))
                                    if state != "info" else ""])
        for name, c in sorted(cov["line"]["files"].items()):
            missed = ", ".join(str(n) for n in c["missed"][:40]) + ("…" if len(c["missed"]) > 40 else "")
            self.cstore.append([name, c["hit"], c["total"], f"{100 * c['hit'] / c['total']:.0f}%", missed])
        for pt in cov["cover"]["points"]:
            if not pt["count"]:
                self.ustore.append(["cover point", pt["name"], pt["file"], pt["line"]])
        for text in cov["toggle"]["missed"]:
            what, _, where = text.partition("  (")
            file, _, line = where.rstrip(")").rpartition(":")
            self.ustore.append(["toggle", what, file, int(line) if line.isdigit() else 0])
        self.cov_label.set_markup(markup(
            f"Coverage of the design files, merged over {cov.get('runs', 1)} run"
            f"{'s' if cov.get('runs', 1) != 1 else ''}. Lines never run are marked in the RTL editor.", MUTED))

    def on_coverage_row(self, _tv, tpath, _col):
        name, missed = self.cstore[tpath][0], self.cstore[tpath][4]
        line = int(missed.split(",")[0]) if missed and missed.split(",")[0].strip().isdigit() else None
        self.app.show_section("rtl")
        self.app.section_pages["rtl"].open_file(self.app.project.dir / name, line)

    def on_uncovered_row(self, _tv, tpath, _col):
        file, line = self.ustore[tpath][2], self.ustore[tpath][3]
        if file:
            self.app.show_section("rtl")
            self.app.section_pages["rtl"].open_file(self.app.project.dir / file, line or None)

    def on_goals(self, *_):
        p = self.app.project
        if not p:
            return
        g = dict(p["goals"])
        vals = dialogs.ask_fields(
            self.app, "Coverage goals",
            [(kind, f"{label} (%)", str(g.get(kind, 0)), "text") for kind, label in coverage.KINDS],
            intro="The coverage of the design files each kind must reach for sign-off. 0 means the kind is measured "
                  "but not required.", ok="Save",
            check=lambda v: None if all(x.replace(".", "", 1).isdigit() and float(x) <= 100 for x in v.values())
            else "Each goal is a number from 0 to 100.")
        if vals:
            p["goals"] = {k: float(v) for k, v in vals.items()}
            p.save()
            self.show_coverage()
            self.refresh_signoff()

    # ---------- formal ----------
    def selected_formal(self):
        model, it = self.ftv.get_selection().get_selected()
        p = self.app.project
        return next((t for t in p["formal"] if t["name"] == model[it][0]), None) if it and p else None

    def on_edit_formal(self, task):
        p = self.app.project
        if not p:
            return
        tops = [m.name for m in p.modules().values() if m.name.endswith("_formal")] or [p.top()]
        old = task or {"name": "", "top": tops[0], "mode": "bmc", "depth": 20,
                      "files": [f for f in p.all_hdl() if "_formal." in f][:1]}
        vals = dialogs.ask_fields(
            self.app, "Formal task", [("name", "Name", old["name"], "text"), ("top", "Top module", old["top"], "text"),
                                      ("mode", "Mode", old["mode"], formal.MODES),
                                      ("depth", "Depth (cycles)", str(old["depth"]), "text"),
                                      ("files", "Property files", " ".join(old["files"]), "text")],
            intro="Property files are read with the design sources, with -formal. Make one from a module with "
                  "Properties… in the RTL section.",
            check=lambda v: None if v["name"] and v["depth"].isdigit() else "Give a name and a whole-number depth.")
        if not vals:
            return
        new = {"name": vals["name"], "top": vals["top"], "mode": vals["mode"], "depth": int(vals["depth"]),
               "files": vals["files"].split()}
        p["formal"] = [new if t is task else t for t in p["formal"]] if task else p["formal"] + [new]
        p.save()
        self.fill()

    def on_remove_formal(self):
        t, p = self.selected_formal(), self.app.project
        if t:
            p["formal"] = [x for x in p["formal"] if x is not t]
            p.save()
            self.fill()

    def on_run_formal(self, task=None, then=None):
        task = task or self.selected_formal()
        p = self.app.project
        if not task or not p:
            return
        if not eda_tools.find("sby", self.app.rtl_tools()):
            self.app.message("SymbiYosys is not installed; install the RTL tools from Tools….")
            return
        self.app.section_pages["rtl"].save_all()
        folder, cmd = formal.sby_command(p, task, self.app.rtl_tools())
        self.results["formal"][task["name"]] = {"state": "running"}
        self.fill()
        self.tabs.set_current_page(TAB_OUTPUT)

        def done(t):
            state, why = formal.verdict(t.text)
            failed = formal.failed_properties(t.text)
            if task.get("mode") == "cover" and state == "fail":
                why += ": a cover was not reached within the depth"
            self.results["formal"][task["name"]] = {"state": state, "why": why + (f"; failed {', '.join(failed)}"
                                                                                 if failed else ""),
                                                    "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                    "log": t.text[-6000:]}
            self.save_results()
            self.fill()
            trace = formal.traces(folder, task)
            if trace:
                self.show_wave(trace[0])
                self.tabs.set_current_page(TAB_WAVES)
            if then:
                then(self.results["formal"][task["name"]])
        self.console.run(cmd, cwd=str(folder), env=eda_tools.env(self.app.rtl_tools()), on_done=done,
                         label=f"Formal {task['name']}")

    def run_all_formal(self, then=None):
        """Run every formal task, one after another; then() at the end."""
        p = self.app.project
        todo = list(p["formal"]) if p and eda_tools.find("sby", self.app.rtl_tools()) else []

        def step(_result=None):
            if todo:
                self.on_run_formal(todo.pop(0), then=step)
            elif then:
                then()
        step()

    # ---------- verification plan ----------
    def selected_plan(self):
        model, it = self.ptv.get_selection().get_selected()
        p = self.app.project
        return next((i for i in p["plan"] if i["feature"] == model[it][0]), None) if it and p else None

    def on_edit_plan(self, item):
        p = self.app.project
        if not p:
            return
        old = item or {"feature": "", "description": "", "tests": [], "formal": [], "cover": []}
        tests = {t["name"] for t in p["tests"]}
        proofs = {t["name"] for t in p["formal"]}

        def check(v):
            if not v["feature"]:
                return "Name the feature."
            if any(i["feature"] == v["feature"] for i in p["plan"] if i is not item):
                return "The plan already has that feature."
            unknown = [n for n in v["tests"].split() if n not in tests] + \
                      [n for n in v["formal"].split() if n not in proofs]
            return f"Not a test or formal task of this project: {', '.join(unknown)}" if unknown else None
        vals = dialogs.ask_fields(
            self.app, "Plan feature",
            [("feature", "Feature", old["feature"], "text"),
             ("description", "What must hold", old.get("description", ""), "text"),
             ("tests", "Tests", " ".join(old.get("tests", [])), "text"),
             ("formal", "Formal tasks", " ".join(old.get("formal", [])), "text"),
             ("cover", "Cover points", " ".join(old.get("cover", [])), "text")],
            intro=f"Tests: {' '.join(sorted(tests)) or '(none yet)'}\nFormal tasks: "
                  f"{' '.join(sorted(proofs)) or '(none yet)'}\nCover points are the labels of `cover property` "
                  "statements in the design; they are measured when tests run with coverage.", check=check)
        if not vals:
            return
        new = {"feature": vals["feature"], "description": vals["description"], "tests": vals["tests"].split(),
               "formal": vals["formal"].split(), "cover": vals["cover"].split()}
        p["plan"] = [new if i is item else i for i in p["plan"]] if item else p["plan"] + [new]
        p.save()
        self.fill()
        self.refresh_signoff()

    def on_remove_plan(self):
        item, p = self.selected_plan(), self.app.project
        if item:
            p["plan"] = [i for i in p["plan"] if i is not item]
            p.save()
            self.fill()
            self.refresh_signoff()

    # ---------- sign-off ----------
    def signoff_state(self):
        """(items, verdict, findings) for the project now."""
        rtl = self.app.section_pages["rtl"]
        items, verdict = signoff.collect(self.app.project, rtl.findings, rtl.ran, self.results)
        return items, verdict, rtl.findings

    def refresh_signoff(self):
        p = self.app.project
        for child in self.tiles.get_children():
            self.tiles.remove(child)
        if not p:
            self.verdict.set_text("No project")
            return False
        items, verdict, _findings = self.signoff_state()
        color = theme.color("success" if verdict == "pass" else "danger")
        self.verdict.set_markup(f'<span foreground="{color}">'
                                f'{"Signed off" if verdict == "pass" else "Not signed off"}</span>')
        for i in items:
            state = {"pass": "pass", "fail": "fail"}.get(i["state"], "muted")
            tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for cls in ("card", "tile", f"tile-{state}"):
                tile.get_style_context().add_class(cls)
            name = Gtk.Label(label=i["name"], xalign=0)
            name.get_style_context().add_class("tile-name")
            row = Gtk.Box(spacing=8)
            value = Gtk.Label(label=i["value"], xalign=0)
            value.get_style_context().add_class("tile-value")
            chip = Gtk.Label(label={"pass": "pass", "fail": "fail", "info": "optional"}.get(i["state"], "not run"),
                             valign=Gtk.Align.CENTER)
            chip.get_style_context().add_class("chip")
            chip.get_style_context().add_class(f"chip-{state}")
            row.pack_start(value, False, False, 0)
            row.pack_start(chip, False, False, 0)
            detail = Gtk.Label(label=i["detail"], xalign=0, wrap=True, max_width_chars=34)
            detail.get_style_context().add_class("muted")
            for w in (name, row, detail):
                tile.pack_start(w, False, False, 0)
            self.tiles.add(tile)
        self.tiles.show_all()
        return False

    def run_signoff(self, then=None):
        """Run every check in order: lint, design checks, CDC, the regression, formal. then(verdict) at the end."""
        p = self.app.project
        if not p or self.busy():
            return
        rtl = self.app.section_pages["rtl"]
        self.cov_check.set_active(self.cov_check.get_active() or bool(eda_tools.find("verilator",
                                                                                     self.app.rtl_tools())))

        def finish():
            self.refresh_signoff()
            self.tabs.set_current_page(TAB_SIGNOFF)
            if then:
                then(self.signoff_state()[1])
        steps = [lambda nxt: rtl.run_lint(None, then=lambda _p: nxt()) or nxt(),
                 lambda nxt: rtl.run_design_checks(then=lambda _f: nxt()) or nxt(),
                 lambda nxt: rtl.run_cdc(then=lambda _r: nxt()) or nxt(),
                 lambda nxt: self.run_regression(then=lambda _r: nxt()),
                 lambda nxt: self.run_all_formal(then=nxt)]

        def advance():
            if steps:
                steps.pop(0)(advance)
            else:
                finish()
        advance()

    def export_report(self):
        """Write the HTML report, JUnit XML and the findings as JSON into build/<project>/reports/; the folder."""
        p = self.app.project
        if not p:
            return None
        items, verdict, findings = self.signoff_state()
        folder = p.build("reports")
        (folder / "signoff.html").write_text(signoff.html_report(p, items, verdict, findings, self.results,
                                                                 regress.load_history(p)))
        (folder / "junit.xml").write_text(signoff.junit(p, self.results))
        (folder / "findings.json").write_text(json.dumps({"verdict": verdict, "items": items, "findings": findings},
                                                         indent=1))
        self.report_label.set_text(f"Report written to {folder}: signoff.html, junit.xml, findings.json")
        self.app.set_status(f"Sign-off report written to {folder}")
        return folder

    def open_report(self):
        p = self.app.project
        path = p.build("reports") / "signoff.html" if p else None
        if not path or not path.exists():
            self.app.message("Export the report first.")
            return
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def is_dirty(self):
        return False
