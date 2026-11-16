"""LEC tab of the ASIC section: equivalence checks between the design's RTL and its netlists (see lec.py).

Each check is a row with its verdict. Run all runs them in turn; a stage check first has OpenROAD write that
stage's netlist. Logs, scripts and netlists go to <results folder>/lec/, and the verdicts to lec_results.json
there, so they are kept with the run they checked.
"""

import json
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import eda_tools  # noqa: E402
import lec  # noqa: E402
from input_page import MUTED, card, hint_label, markup, page_header  # noqa: E402
from runner import Console  # noqa: E402
from verify_page import state_markup  # noqa: E402


class LecPage:
    def __init__(self, app):
        self.app = app
        self.results = {}
        self.queue = []
        self.widget = self.build()

    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(page_header("Logic equivalence (LEC)", "Proves the netlists of the flow do exactly what "
                                     "the RTL does: synthesis, timing repair and buffering must never change the "
                                     "function."), False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=20, margin_end=20,
                       margin_top=12, margin_bottom=16)
        checks = card("Checks", "Registers and ports are matched by name and the logic between them is proven "
                                "equal with Yosys (SAT, then induction). Netlist cells get their function from "
                                "the platform's liberty files.")
        # id, check, verdict markup, detail
        self.store = Gtk.ListStore(str, str, str, str)
        tv = Gtk.TreeView(model=self.store)
        tv.append_column(Gtk.TreeViewColumn("Check", Gtk.CellRendererText(weight=600), text=1))
        tv.append_column(Gtk.TreeViewColumn("Result", Gtk.CellRendererText(), markup=2))
        tv.append_column(Gtk.TreeViewColumn("Detail", Gtk.CellRendererText(foreground=MUTED), text=3))
        self.tv = tv
        checks.pack_start(tv, False, False, 0)
        row = Gtk.Box(spacing=8)
        self.stage_cb = Gtk.ComboBoxText(tooltip_text="The stage to compare with synthesis")
        for name, _ in lec.STAGE_DBS:
            self.stage_cb.append(name, name)
        self.stage_cb.set_active_id("route")
        for label, handler, suggested in (("Run all", lambda *_: self.run_all(), True),
                                          ("Run selected", lambda *_: self.run_selected(), False)):
            b = Gtk.Button(label=label)
            if suggested:
                b.get_style_context().add_class("suggested-action")
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        row.pack_start(Gtk.Label(label="Stage for the stage check:"), False, False, 8)
        row.pack_start(self.stage_cb, False, False, 0)
        checks.pack_start(row, False, False, 0)
        self.unproven = hint_label("")
        checks.pack_start(self.unproven, False, False, 0)
        body.pack_start(checks, False, False, 0)
        self.console = Console("Yosys and OpenROAD output")
        holder = card()
        holder.pack_start(self.console, True, True, 0)
        body.pack_start(holder, True, True, 0)
        outer.pack_start(body, True, True, 0)
        return outer

    def info(self):
        return lec.inputs(self.app.inputs.effective, self.app.inputs.resolve)

    def folder(self):
        res = self.info()["results"]
        if not res:
            return None
        (res / "lec").mkdir(parents=True, exist_ok=True)
        return res / "lec"

    def refresh(self):
        """The config changed or a run finished: read the saved verdicts and say which checks can run."""
        folder = self.folder()
        self.results = {}
        if folder and (folder / "lec_results.json").exists():
            try:
                self.results = json.loads((folder / "lec_results.json").read_text())
            except ValueError:
                pass
        self.fill()

    def fill(self):
        info = self.info()
        self.store.clear()
        for cid, label in lec.CHECKS:
            r = self.results.get(cid, {})
            why = lec.missing(cid, info)
            stage = f" ({r['stage']})" if r.get("stage") else ""
            self.store.append([cid, label + stage, state_markup(r.get("state", "") if not why or r else "",
                                                                r.get("summary") or ("cannot run yet" if why
                                                                                     else None)),
                               why or r.get("when", "")])

    def selected(self):
        model, it = self.tv.get_selection().get_selected()
        return model[it][0] if it else None

    def run_selected(self):
        cid = self.selected()
        if cid:
            self.run([cid])

    def run_all(self, then=None):
        self.run([cid for cid, _ in lec.CHECKS], then)

    def run(self, ids, then=None):
        if self.console.busy():
            return
        yosys = eda_tools.find("yosys", self.app.rtl_tools()) or self.app.make_env().get("YOSYS_EXE")
        if not yosys:
            self.app.message("Yosys is not installed.")
            return
        self.queue, self.then, self.yosys = list(ids), then, yosys
        self._next()

    def _next(self):
        if not self.queue:
            self.unproven.set_markup("")
            bad = [r for r in self.results.values() if r.get("unproven")]
            if bad:
                self.unproven.set_markup(markup("Unproven points: " + ", ".join(bad[0]["unproven"][:12]), MUTED))
            if self.then:
                self.then(self.results)
            return
        cid = self.queue.pop(0)
        info, folder = self.info(), self.folder()
        why = lec.missing(cid, info)
        if why:
            self.results[cid] = {"state": "error", "summary": why}
            self.fill()
            return self._next()
        libs = [str(p) for p in info["libs"]]
        steps = []
        stage = None
        if cid == "rtl_synth":
            gold = lec.read_rtl([str(f) for f in info["rtl"]], info["defines"],
                                [str(self.app.inputs.resolve(i)) for i in info["incs"]])
            gate = lec.read_netlist(info["synth"], libs)
        elif cid == "synth_final":
            gold, gate = lec.read_netlist(info["synth"], libs), lec.read_netlist(info["final"], libs)
        else:
            stage = self.stage_cb.get_active_id()
            odb = info["results"] / dict(lec.STAGE_DBS)[stage]
            if not odb.exists():
                self.results[cid] = {"state": "error", "summary": f"Run the flow to {stage} first."}
                self.fill()
                return self._next()
            netlist = folder / f"{stage}.v"
            (folder / f"{stage}_netlist.tcl").write_text(lec.stage_netlist_script(odb, netlist))
            openroad = self.app.make_env().get("OPENROAD_EXE", "openroad")
            steps.append([openroad, "-no_init", "-no_splash", "-exit", str(folder / f"{stage}_netlist.tcl")])
            gold, gate = lec.read_netlist(info["synth"], libs), lec.read_netlist(netlist, libs)
        (folder / f"{cid}.ys").write_text(lec.script(info["top"], gold, gate))
        steps.append([self.yosys, "-q", "-l", str(folder / f"{cid}.log"), str(folder / f"{cid}.ys")])
        self.results[cid] = {"state": "running"}
        self.fill()

        def done(task, _output):
            log = (folder / f"{cid}.log").read_text(errors="replace") if (folder / f"{cid}.log").exists() \
                else task.text
            state, summary, names = lec.verdict(log + task.text)
            self.results[cid] = {"state": state, "summary": summary, "unproven": names[:50],
                                 "when": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": stage}
            (folder / "lec_results.json").write_text(json.dumps(self.results, indent=1))
            self.fill()
            self.app.set_status(f"LEC {dict(lec.CHECKS)[cid]}: {summary}")
            self._next()
        self.console.run_all(steps, cwd=str(folder), env=self.app.make_env(), on_done=done,
                             label=dict(lec.CHECKS)[cid])

