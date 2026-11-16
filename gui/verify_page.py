"""Verify section: simulation tests, regressions, waveforms, coverage and formal property checks.

Tests and formal tasks are stored in the project's flowrunner.json (see workspace.py). Running all tests is a
regression: each test runs in turn and gets a pass / fail / ran verdict (sim.py); results are kept in
build/<project>/verify_results.json so they survive a restart and the assistant can read them. A test's waveform
opens in the built-in viewer; a formal failure opens its counterexample there too.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

import dialogs  # noqa: E402
import eda_tools  # noqa: E402
import formal  # noqa: E402
import sim  # noqa: E402
import theme  # noqa: E402
from input_page import MUTED, card, hint_label, markup  # noqa: E402
from runner import Console  # noqa: E402
from wave_view import WaveView  # noqa: E402

STATE_COLOR = {"pass": "success", "fail": "danger", "ran": "info", "unknown": "warning", "error": "danger",
               "running": "info", "": "text_muted"}


def state_markup(state, text=None):
    return f'<span foreground="{theme.color(STATE_COLOR.get(state, "text_muted"))}">●</span> ' \
           + GLib.markup_escape_text(text or state or "not run")


class VerifyPage:
    name, title = "verify", "Verify"

    def __init__(self, app):
        self.app = app
        self.results = {"tests": {}, "formal": {}}
        self.queue = []
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

        paned = Gtk.Paned(position=470)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)

        tests = card("Simulation tests", "A test is a testbench module and its files, run with the design "
                                         "sources. Generated testbenches print PASS or FAIL themselves.")
        # name, simulator, verdict markup, detail
        self.tstore = Gtk.ListStore(str, str, str, str)
        self.ttv = Gtk.TreeView(model=self.tstore)
        self.ttv.append_column(Gtk.TreeViewColumn("Test", Gtk.CellRendererText(weight=600), text=0))
        self.ttv.append_column(Gtk.TreeViewColumn("Simulator", Gtk.CellRendererText(foreground=MUTED), text=1))
        self.ttv.append_column(Gtk.TreeViewColumn("Result", Gtk.CellRendererText(), markup=2))
        self.ttv.connect("row-activated", lambda *_: self.on_run_test())
        self.ttv.get_selection().connect("changed", lambda *_: self.on_test_selected())
        sw = Gtk.ScrolledWindow(min_content_height=150, vexpand=True)
        sw.add(self.ttv)
        tests.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        for label, handler, tip, suggested in (
                ("Run", lambda *_: self.on_run_test(), "Run the selected test", False),
                ("Run all", lambda *_: self.run_regression(), "Run every test: a regression", True),
                ("Add…", lambda *_: self.on_edit_test(None), "Add a test", False),
                ("Edit…", lambda *_: self.on_edit_test(self.selected_test()), "Change the selected test", False),
                ("Remove", lambda *_: self.on_remove_test(), "Remove the selected test (its files stay)", False)):
            b = Gtk.Button(label=label, tooltip_text=tip)
            if suggested:
                b.get_style_context().add_class("suggested-action")
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        tests.pack_start(row, False, False, 0)
        self.cov_check = Gtk.CheckButton(label="Measure line coverage (Verilator)")
        tests.pack_start(self.cov_check, False, False, 0)
        left.pack_start(tests, True, True, 0)

        ftasks = card("Formal checks", "SymbiYosys proves the assertions of a property file: bmc looks for a "
                                       "failure up to the depth, prove proves them for every cycle, cover shows "
                                       "each cover can be reached.")
        self.fstore = Gtk.ListStore(str, str, str)
        self.ftv = Gtk.TreeView(model=self.fstore)
        self.ftv.append_column(Gtk.TreeViewColumn("Task", Gtk.CellRendererText(weight=600), text=0))
        self.ftv.append_column(Gtk.TreeViewColumn("Mode", Gtk.CellRendererText(foreground=MUTED), text=1))
        self.ftv.append_column(Gtk.TreeViewColumn("Result", Gtk.CellRendererText(), markup=2))
        self.ftv.connect("row-activated", lambda *_: self.on_run_formal())
        sw = Gtk.ScrolledWindow(min_content_height=90)
        sw.add(self.ftv)
        ftasks.pack_start(sw, False, False, 0)
        row = Gtk.Box(spacing=6)
        for label, handler in (("Run", lambda *_: self.on_run_formal()),
                               ("Add…", lambda *_: self.on_edit_formal(None)),
                               ("Edit…", lambda *_: self.on_edit_formal(self.selected_formal())),
                               ("Remove", lambda *_: self.on_remove_formal())):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        ftasks.pack_start(row, False, False, 0)
        left.pack_start(ftasks, False, False, 0)
        paned.pack1(left, False, False)

        self.tabs = Gtk.Notebook(margin=14)
        self.waves = WaveView()
        wbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        wbox.pack_start(self.waves, True, True, 0)
        row = Gtk.Box(spacing=6)
        self.wave_file = None
        for label, tool in (("Open in GTKWave", "gtkwave"), ("Open in Surfer", "surfer")):
            b = Gtk.Button(label=label, tooltip_text=f"Open the waveform in {tool}, outside the app")
            b.connect("clicked", lambda _b, t=tool: self.open_external(t))
            row.pack_start(b, False, False, 0)
        b = Gtk.Button(label="Open file…")
        b.connect("clicked", self.on_open_wave)
        row.pack_start(b, False, False, 0)
        wbox.pack_start(row, False, False, 0)
        self.tabs.append_page(wbox, Gtk.Label(label="Waveforms"))
        # file, lines hit, lines, percent, missed lines
        self.cstore = Gtk.ListStore(str, int, int, str, str)
        ctv = Gtk.TreeView(model=self.cstore)
        for i, title in enumerate(("File", "Hit", "Lines", "Covered", "Lines never run")):
            cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END if i == 4 else Pango.EllipsizeMode.NONE)
            ctv.append_column(Gtk.TreeViewColumn(title, cell, text=i))
        ctv.connect("row-activated", self.on_coverage_row)
        cbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.cov_label = hint_label("Run tests with line coverage on to see which lines the tests exercise.")
        cbox.pack_start(self.cov_label, False, False, 0)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(ctv)
        cbox.pack_start(sw, True, True, 0)
        self.tabs.append_page(cbox, Gtk.Label(label="Coverage"))
        self.console = Console("Simulator and prover output")
        obox = Gtk.Box(margin=8)
        obox.pack_start(self.console, True, True, 0)
        self.tabs.append_page(obox, Gtk.Label(label="Output"))
        paned.pack2(self.tabs, True, False)
        outer.pack_start(paned, True, True, 0)
        return outer

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

    def fill(self):
        p = self.app.project
        self.tstore.clear()
        self.fstore.clear()
        if not p:
            self.summary.set_text("")
            return
        for t in p["tests"]:
            r = self.results["tests"].get(t["name"], {})
            self.tstore.append([t["name"], t.get("simulator", "iverilog"),
                                state_markup(r.get("state", ""), r.get("why") and f"{r['state']}: {r['why']}"[:70]),
                                r.get("why", "")])
        for t in p["formal"]:
            r = self.results["formal"].get(t["name"], {})
            self.fstore.append([t["name"], f"{t.get('mode', 'bmc')} {t.get('depth', 20)}",
                                state_markup(r.get("state", ""), r.get("why", "")[:60] or None)])
        states = [self.results["tests"].get(t["name"], {}).get("state") for t in p["tests"]]
        passed, failed = states.count("pass"), states.count("fail")
        self.summary.set_markup(markup(
            f"{len(states)} tests: {passed} passed, {failed} failed, {len(states) - passed - failed} other"
            if states else "No tests yet: add one, or make a testbench in the RTL section.", MUTED))

    # ---------- tests ----------
    def selected_test(self):
        model, it = self.ttv.get_selection().get_selected()
        p = self.app.project
        return next((t for t in p["tests"] if t["name"] == model[it][0]), None) if it and p else None

    def on_test_selected(self):
        t = self.selected_test()
        if t:
            folder = sim.build_dir(self.app.project, t)
            waves = sim.waveforms(folder)
            if waves and waves[0] != self.wave_file:
                self.show_wave(waves[0])

    def on_edit_test(self, test):
        p = self.app.project
        if not p:
            return
        tops = [m.name for m in p.modules().values() if not m.ports] or [""]
        sims = [s for s, _ in sim.available(self.app.rtl_tools())] or ["iverilog"]
        old = test or {"name": "", "top": tops[0], "files": p.testbenches()[:1], "simulator": sims[0], "args": ""}

        def check(v):
            if not v["name"].replace("_", "").isalnum():
                return "Give the test a name: letters, digits and underscores."
            if test is None and any(t["name"] == v["name"] for t in p["tests"]):
                return "A test with that name exists."
            if not v["top"]:
                return "Give the testbench's top module."
            return None
        vals = dialogs.ask_fields(
            self.app, "Test" if test else "New test",
            [("name", "Name", old["name"], "text"), ("top", "Testbench module", old["top"], "text"),
             ("files", "Testbench files", " ".join(old["files"]), "text"),
             ("simulator", "Simulator", old.get("simulator", "iverilog"), sims),
             ("args", "Run arguments (plusargs)", old.get("args", ""), "text")],
            intro="Files are relative to the project folder and are compiled with the design sources. "
                  "Plusargs such as +seed=3 reach $test$plusargs and $value$plusargs.", check=check)
        if not vals:
            return
        new = {"name": vals["name"], "top": vals["top"], "files": vals["files"].split(),
               "simulator": vals["simulator"], "args": vals["args"]}
        p["tests"] = [new if t is test else t for t in p["tests"]] if test else p["tests"] + [new]
        p.save()
        self.fill()

    def on_remove_test(self):
        t, p = self.selected_test(), self.app.project
        if t:
            p["tests"] = [x for x in p["tests"] if x is not t]
            p.save()
            self.results["tests"].pop(t["name"], None)
            self.save_results()
            self.fill()

    def on_run_test(self):
        t = self.selected_test()
        if t:
            self.run_tests([t])

    def run_regression(self, then=None):
        """Run every test, one after another; then(results) when all are done."""
        p = self.app.project
        if p and p["tests"]:
            self.run_tests(list(p["tests"]), then)
        elif then:
            then(self.results["tests"])

    def run_tests(self, tests, then=None):
        if self.console.busy():
            self.app.set_status("A simulation or proof is still running.")
            return
        self.app.section_pages["rtl"].save_all()
        self.queue = list(tests)
        self.tabs.set_current_page(2)
        self.coverage_files = []
        self._next_test(then)

    def _next_test(self, then):
        p = self.app.project
        if not self.queue:
            self.save_results()
            self.fill()
            self.update_coverage()
            states = [self.results["tests"].get(t["name"], {}).get("state") for t in p["tests"]]
            self.app.set_status(f"Tests: {states.count('pass')} passed, {states.count('fail')} failed")
            if self.wave_file:
                self.tabs.set_current_page(0)
            if then:
                then(self.results["tests"])
            return
        test = dict(self.queue.pop(0))
        if self.cov_check.get_active() and test.get("simulator") == "verilator":
            test["coverage"] = True
        if not eda_tools.find(test.get("simulator", "iverilog"), self.app.rtl_tools()):
            self.results["tests"][test["name"]] = {"state": "error", "why": f"{test['simulator']} is not installed"}
            return self._next_test(then)
        folder, cmds = sim.commands(p, test, self.app.rtl_tools())
        for old in sim.waveforms(folder):
            old.unlink()
        if test.get("coverage"):
            cmds.append(sim.coverage_command(folder, self.app.rtl_tools()))
        self.results["tests"][test["name"]] = {"state": "running"}
        self.fill()
        started = time.time()

        def done(task, output):
            state, why = sim.verdict(task.rc, output)
            self.results["tests"][test["name"]] = {"state": state, "why": why, "seconds": round(time.time() - started,
                                                                                               1),
                                                   "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                   "log": output[-6000:]}
            if test.get("coverage") and (folder / "coverage.info").exists():
                self.coverage_files.append(folder / "coverage.info")
            waves = sim.waveforms(folder)
            if waves:
                self.show_wave(waves[0])
            self._next_test(then)
        self.console.run_all(cmds, cwd=str(folder), env=eda_tools.env(self.app.rtl_tools()), on_done=done,
                             label=f"Test {test['name']}")

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
        p = self.app.project
        files = getattr(self, "coverage_files", [])
        if not files or not p:
            return
        cov = sim.read_coverage(files, root=p.dir)
        design = set(p.sources())
        self.cstore.clear()
        for name in sorted(cov, key=lambda n: (n not in design, n)):
            c = cov[name]
            missed = ", ".join(str(n) for n in c["missed"][:40]) + ("…" if len(c["missed"]) > 40 else "")
            self.cstore.append([name, c["hit"], c["total"], f"{100 * c['hit'] / c['total']:.0f}%", missed])
        design_cov = {k: v for k, v in cov.items() if k in design}
        self.cov_label.set_markup(markup(f"Design line coverage: {sim.coverage_percent(design_cov):.1f}% over "
                                         f"{len(files)} test{'s' if len(files) != 1 else ''}. Double-click a file "
                                         "to open it at the first line never run.", MUTED))
        self.results["coverage"] = {"percent": round(sim.coverage_percent(design_cov), 1),
                                    "files": {k: {"hit": v["hit"], "total": v["total"], "missed": v["missed"][:200]}
                                              for k, v in design_cov.items()}}
        self.save_results()

    def on_coverage_row(self, _tv, tpath, _col):
        name, missed = self.cstore[tpath][0], self.cstore[tpath][4]
        line = int(missed.split(",")[0]) if missed and missed.split(",")[0].strip().isdigit() else None
        self.app.show_section("rtl")
        self.app.section_pages["rtl"].open_file(self.app.project.dir / name, line)

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
        self.tabs.set_current_page(2)

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
                self.tabs.set_current_page(0)
            if then:
                then(self.results["formal"][task["name"]])
        self.console.run(cmd, cwd=str(folder), env=eda_tools.env(self.app.rtl_tools()), on_done=done,
                         label=f"Formal {task['name']}")

    def is_dirty(self):
        return False

