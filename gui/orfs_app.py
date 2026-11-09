#!/usr/bin/env python3
"""ORFS Flow Runner: a desktop app to run OpenROAD-flow-scripts designs from RTL to GDS.

Pick a platform, design and config from the designs in the ORFS flow directory, the number of
CPU cores, and the stage range, then run. The Inputs tab edits the design's constraints, flow settings
and files; reports, logs and debug information have their own tabs.

Usage: python3 gui/orfs_app.py [--flow DIR]   (or orfs-flow-runner, when installed from the .deb)

Needs GTK 3 through PyGObject (on Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flow_gui import CHECK_COLUMNS, METRICS, evaluate, load_metrics, step_info  # noqa: E402
from inputs_tab import InputsTab  # noqa: E402

VERSION = "1.1.0"
APP_ID = "orfs-flow-runner"
GUI_DIR = Path(__file__).resolve().parent
ROOT = GUI_DIR.parent
# Per-user files, so the app also works when installed read-only under /opt.
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP_ID / "settings.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_ID
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / APP_ID
RUN_LOGS = DATA_DIR / "run_logs"
# Tools the flow needs: name, environment variable ORFS reads, and the flag that prints the version.
TOOLS = [("openroad", "OPENROAD_EXE", "-version"), ("yosys", "YOSYS_EXE", "-V"), ("klayout", None, "-v")]
# Where tools usually are when they are not on PATH (an app menu starts with a short PATH).
TOOL_DIRS = ["/usr/local/bin", "/usr/bin", "/opt/openroad/bin", str(Path.home() / ".local" / "bin")]

# ORFS stages in order: name, make target, clean target, result that marks the stage done, log prefix.
STAGES = [
    ("synth", "synth", "clean_synth", "1_synth.odb", "1_"),
    ("floorplan", "floorplan", "clean_floorplan", "2_floorplan.odb", "2_"),
    ("place", "place", "clean_place", "3_place.odb", "3_"),
    ("cts", "cts", "clean_cts", "4_cts.odb", "4_"),
    ("route", "route", "clean_route", "5_route.odb", "5_"),
    ("finish", "finish", "clean_finish", "6_final.odb", "6_"),
]
STAGE_NAMES = [s[0] for s in STAGES]
# Databases that `make gui_<target>` opens.
GUI_TARGETS = [("floorplan", "2_floorplan.odb"), ("place", "3_place.odb"), ("cts", "4_cts.odb"),
               ("grt", "5_1_grt.odb"), ("route", "5_route.odb"), ("final", "6_final.odb")]
MAKE_VARS = ["PLATFORM", "DESIGN_NAME", "DESIGN_NICKNAME", "FLOW_VARIANT", "RESULTS_DIR", "LOG_DIR",
             "REPORTS_DIR", "SDC_FILE", "CORE_UTILIZATION", "PLACE_DENSITY", "SYNTH_HIERARCHICAL",
             "ADDITIONAL_LEFS", "VERILOG_FILES"]

ISSUE_RE = re.compile(r"\[(ERROR|WARNING) ([A-Z]+-\d+)\]")
ERR_LINE = re.compile(r"\[ERROR|\bError\b|error:|\*\*\* |FAILED|Killed")
WARN_LINE = re.compile(r"\[WARNING|\bWarning\b")
COLORS = {"pass": "#2e9e5b", "fail": "#e0533f", "warn": "#c88a00", "run": "#3a7bd5", "muted": "#8a8f98"}

CSS = b"""
.side { padding: 12px; }
.section-title { font-weight: bold; margin-top: 8px; }
.plan { font-family: monospace; font-size: 9pt; padding: 8px; border-radius: 6px;
        background-color: alpha(currentColor, 0.06); }
.statusbar { padding: 4px 10px; border-top: 1px solid alpha(currentColor, 0.15); }
"""


def default_flow():
    for cand in (os.environ.get("ORFS_FLOW"), ROOT / "OpenROAD-flow-scripts" / "flow", ROOT / "orfs" / "flow",
                 Path.home() / "OpenROAD-flow-scripts" / "flow"):
        if cand and (Path(cand) / "Makefile").exists():
            return Path(cand).resolve()
    return (ROOT / "orfs" / "flow").resolve()


def discover(flow):
    """{platform: {"available": bool, "designs": {design: [config files]}}} from flow/designs."""
    out = {}
    ddir = flow / "designs"
    if not ddir.is_dir():
        return out
    for pdir in sorted(ddir.iterdir()):
        if not pdir.is_dir() or pdir.name == "src":
            continue
        designs = {}
        for d in sorted(pdir.iterdir()):
            cfgs = sorted(p.name for p in d.glob("config*.mk")) if d.is_dir() else []
            if cfgs:
                designs[d.name] = cfgs
        if designs:
            out[pdir.name] = {"available": (flow / "platforms" / pdir.name / "config.mk").exists(),
                              "designs": designs}
    return out


def tool_path(name, tools):
    """The tool chosen in Tools…, else the one on PATH, else one in a usual install folder, else None."""
    chosen = (tools or {}).get(name)
    if chosen:
        return chosen if os.access(chosen, os.X_OK) else None
    return shutil.which(name) or shutil.which(name, path=os.pathsep.join(TOOL_DIRS))


def flow_env(tools):
    env = dict(os.environ)
    dirs = []
    for name, var, _ in TOOLS:
        path = tool_path(name, tools)
        if not path:
            continue
        if str(Path(path).parent) not in dirs:
            dirs.append(str(Path(path).parent))
        # ORFS only uses tools from PATH when these are set; otherwise it looks under tools/install.
        if var and ((tools or {}).get(name) or var not in env):
            env[var] = path
    # Put the tools' folders first so make, OpenROAD and KLayout also find helpers that sit next to them.
    env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    return env


def read_tail(path, limit=4 << 20):
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - limit))
        data = f.read().decode(errors="replace")
    if size > limit:
        data = f"[showing the last {limit >> 20} MB of {size >> 20} MB]\n" + data.split("\n", 1)[-1]
    return data


def meminfo():
    vals = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            vals[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return vals.get("MemTotal", 0), vals.get("MemAvailable", 0)


def group_rss(pgid):
    """Resident memory of all processes in a process group, and their names."""
    rss, names = 0, []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            if os.getpgid(int(p)) != pgid:
                continue
            with open(f"/proc/{p}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss += int(line.split()[1]) * 1024
            names.append(Path(f"/proc/{p}/comm").read_text().strip())
        except (OSError, ProcessLookupError, ValueError):
            continue
    return rss, names


def fmt_dur(s):
    if not s:
        return "–"
    s = int(s)
    h, m = divmod(s, 3600)
    m, s = divmod(m, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def fmt_bytes(b):
    if b >= 1 << 30:
        return f"{b / (1 << 30):.1f} GB"
    if b >= 1 << 20:
        return f"{b / (1 << 20):.0f} MB"
    return f"{b / 1024:.0f} KB" if b >= 1024 else f"{b} B"


def colored(text, state):
    return f'<span foreground="{COLORS.get(state, COLORS["muted"])}">{GLib.markup_escape_text(str(text))}</span>'


def make_textview():
    tv = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR)
    tv.set_left_margin(8)
    tv.set_top_margin(6)
    buf = tv.get_buffer()
    buf.create_tag("err", foreground=COLORS["fail"])
    buf.create_tag("warn", foreground=COLORS["warn"])
    buf.create_tag("info", foreground=COLORS["run"])
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    sw.add(tv)
    return tv, sw


def tag_lines(buf, start_line, text):
    """Colour error and warning lines of text inserted at start_line."""
    for i, line in enumerate(text.split("\n")):
        tag = "err" if ERR_LINE.search(line) else "warn" if WARN_LINE.search(line) else \
            "info" if line.startswith(">>") else None
        if tag:
            it = buf.get_iter_at_line(start_line + i)
            end = it.copy()
            end.forward_to_line_end()
            buf.apply_tag_by_name(tag, it, end)


def set_text(tv, text, scroll_end=False):
    buf = tv.get_buffer()
    buf.set_text(text)
    tag_lines(buf, 0, text)
    if scroll_end:
        GLib.idle_add(lambda: tv.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 1) and False)


class Job:
    """One flow run: a shell command in its own process group, output teed to a log file."""

    def __init__(self, cmd, cwd, env, log_path, on_exit):
        self.cmd, self.log_path, self.on_exit = cmd, log_path, on_exit
        self.started, self.rc = time.time(), None
        self.pending = []
        self.lock = threading.Lock()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log_path, "wb")
        self.log.write(f">> {cmd}\n>> cwd: {cwd}\n".encode())
        self.proc = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for chunk in iter(lambda: self.proc.stdout.read1(65536), b""):
            self.log.write(chunk)
            self.log.flush()
            with self.lock:
                self.pending.append(chunk)
        self.rc = self.proc.wait()
        tail = f"\n>> exit code {self.rc} after {fmt_dur(time.time() - self.started)}\n".encode()
        self.log.write(tail)
        self.log.close()
        with self.lock:
            self.pending.append(tail)
        GLib.idle_add(self.on_exit, self)

    def drain(self):
        with self.lock:
            data, self.pending = b"".join(self.pending), []
        return data.decode(errors="replace")

    def running(self):
        return self.proc.poll() is None

    def stop(self):
        if not self.running():
            return
        os.killpg(self.proc.pid, signal.SIGTERM)

        def force():
            if self.running():
                os.killpg(self.proc.pid, signal.SIGKILL)
            return False
        GLib.timeout_add_seconds(15, force)


class App(Gtk.Window):
    def __init__(self, flow):
        super().__init__(title="ORFS Flow Runner")
        w, h = 1280, 820
        mon = Gdk.Display.get_default().get_primary_monitor() or Gdk.Display.get_default().get_monitor(0)
        if mon:
            area = mon.get_workarea()
            w, h = min(w, int(area.width * 0.95)), min(h, int(area.height * 0.95))
        self.set_default_size(w, h)
        self.set_icon_name(APP_ID if Gtk.IconTheme.get_default().has_icon(APP_ID) else "applications-engineering")
        self.flow = flow
        self.platforms = {}
        self.vars = {}
        self.vars_gen = 0
        self.job = None
        self.inputs_unsaved = False
        self.settings = self.load_settings()
        self.connect("delete-event", self.on_close)

        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), prov,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        hb = Gtk.HeaderBar(show_close_button=True, title="ORFS Flow Runner")
        self.set_titlebar(hb)
        self.hb = hb
        flow_btn = Gtk.Button(label="Flow folder…", tooltip_text="Choose the ORFS flow directory")
        flow_btn.connect("clicked", self.choose_flow)
        hb.pack_start(flow_btn)
        tools_btn = Gtk.Button(label="Tools…", tooltip_text="Choose the OpenROAD, Yosys and KLayout programs")
        tools_btn.connect("clicked", self.choose_tools)
        hb.pack_start(tools_btn)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=380)
        paned.pack1(self.build_side(), False, False)
        self.nb = Gtk.Notebook()
        self.inputs = InputsTab(self, MAKE_VARS, CACHE_DIR / "backups")
        self.pages = {"inputs": self.inputs.widget, "console": self.build_console(), "reports": self.build_reports(),
                      "logs": self.build_logs(), "debug": self.build_debug()}
        for key, label in (("inputs", "Inputs"), ("console", "Run output"), ("reports", "Reports"),
                           ("logs", "Logs"), ("debug", "Debug")):
            self.nb.append_page(self.pages[key], Gtk.Label(label=label))
        self.nb.connect("switch-page", lambda nb, page, n: GLib.idle_add(self.refresh_tab, n))
        paned.pack2(self.nb, True, False)

        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.get_style_context().add_class("statusbar")
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        vbox.pack_start(paned, True, True, 0)
        vbox.pack_start(self.status, False, False, 0)
        self.add(vbox)

        self.load_flow(flow)
        GLib.timeout_add(250, self.tick_output)
        GLib.timeout_add_seconds(3, self.tick_status)
        threading.Thread(target=self.probe_env, daemon=True).start()
        if not (flow / "Makefile").exists():
            GLib.idle_add(self.ask_for_flow)

    def ask_for_flow(self):
        self.message("No ORFS flow folder found yet. Choose the flow/ folder of your OpenROAD-flow-scripts "
                     "checkout (it can be changed later with Flow folder…).")
        self.choose_flow()
        return False

    # ---------- settings ----------
    def load_settings(self):
        try:
            return json.loads(SETTINGS.read_text())
        except (OSError, ValueError):
            return {}

    def save_settings(self):
        s = {"flow": str(self.flow), "platform": self.platform_cb.get_active_id(),
             "design": self.design_cb.get_active_id(), "config": self.config_cb.get_active_id(),
             "cores": self.cores_cb.get_active_id(), "from": self.from_cb.get_active_id(),
             "to": self.to_cb.get_active_id(), "tools": self.tools()}
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(s, indent=1))
        except OSError:
            pass

    def tools(self):
        return self.settings.get("tools") or {}

    def choose_tools(self, *_):
        dlg = Gtk.Dialog(title="Tools", transient_for=self, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        grid.attach(Gtk.Label(label="Pick each program, or leave it on Automatic to use the one on PATH\n"
                                    f"(or in {', '.join(TOOL_DIRS)}).", xalign=0), 0, 0, 3, 1)
        pickers = {}
        for row, (name, _, _) in enumerate(TOOLS, 1):
            chosen = self.tools().get(name)
            found = tool_path(name, {})
            pick = Gtk.FileChooserButton(title=f"Choose {name}", action=Gtk.FileChooserAction.OPEN, hexpand=True)
            if chosen:
                pick.set_filename(chosen)
            auto = Gtk.Button(label="Automatic",
                              tooltip_text=f"Use {found}" if found else f"{name} was not found on PATH")
            auto.connect("clicked", lambda _b, w=pick: w.unselect_all())
            grid.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
            grid.attach(pick, 1, row, 1, 1)
            grid.attach(auto, 2, row, 1, 1)
            pickers[name] = pick
        dlg.get_content_area().add(grid)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            tools = {n: w.get_filename() for n, w in pickers.items() if w.get_filename()}
            bad = [p for p in tools.values() if not os.access(p, os.X_OK) or Path(p).is_dir()]
            if bad:
                self.message(f"Not a program: {', '.join(bad)}")
            else:
                self.settings["tools"] = tools
                self.save_settings()
                threading.Thread(target=self.probe_env, daemon=True).start()
                self.on_config()
        dlg.destroy()

    # ---------- left panel ----------
    def build_side(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.get_style_context().add_class("side")
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)

        def combo_with_store():
            store = Gtk.ListStore(str, str, bool)
            cb = Gtk.ComboBox.new_with_model(store)
            cell = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
            cb.pack_start(cell, True)
            cb.add_attribute(cell, "text", 1)
            cb.add_attribute(cell, "sensitive", 2)
            cb.set_id_column(0)
            return cb, store

        self.platform_cb, self.platform_store = combo_with_store()
        self.design_cb, self.design_store = combo_with_store()
        self.config_cb = Gtk.ComboBoxText()
        self.cores_cb = Gtk.ComboBoxText()
        ncpu = os.cpu_count() or 1
        for n in range(1, ncpu + 1):
            self.cores_cb.append(str(n), f"{n} of {ncpu}" + ("  (all)" if n == ncpu else ""))
        self.from_cb = Gtk.ComboBoxText()
        self.from_cb.append("continue", "Continue (reuse finished stages)")
        self.to_cb = Gtk.ComboBoxText()
        for name in STAGE_NAMES:
            self.from_cb.append(name, f"Re-run from {name}")
            self.to_cb.append(name, name)

        rows = [("Platform", self.platform_cb), ("Design", self.design_cb), ("Config", self.config_cb),
                ("CPU cores", self.cores_cb), ("From stage", self.from_cb), ("To stage", self.to_cb)]
        for i, (label, w) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, i, 1, 1)
            w.set_hexpand(True)
            grid.attach(w, 1, i, 1, 1)
        box.pack_start(grid, False, False, 0)

        self.platform_cb.connect("changed", self.on_platform)
        self.design_cb.connect("changed", self.on_design)
        self.config_cb.connect("changed", self.on_config)
        for cb in (self.cores_cb, self.from_cb, self.to_cb):
            cb.connect("changed", lambda *_: (self.update_plan(), self.save_settings()))

        t = Gtk.Label(label="Plan", xalign=0)
        t.get_style_context().add_class("section-title")
        box.pack_start(t, False, False, 0)
        self.plan_label = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True,
                                    wrap_mode=Pango.WrapMode.WORD_CHAR)
        self.plan_label.get_style_context().add_class("plan")
        box.pack_start(self.plan_label, False, False, 0)

        btns = Gtk.Box(spacing=8)
        self.run_btn = Gtk.Button(label="Run")
        self.run_btn.get_style_context().add_class("suggested-action")
        self.run_btn.connect("clicked", self.on_run)
        self.stop_btn = Gtk.Button(label="Stop", sensitive=False)
        self.stop_btn.get_style_context().add_class("destructive-action")
        self.stop_btn.connect("clicked", self.on_stop)
        btns.pack_start(self.run_btn, True, True, 0)
        btns.pack_start(self.stop_btn, True, True, 0)
        box.pack_start(btns, False, False, 4)

        t = Gtk.Label(label="Stages", xalign=0)
        t.get_style_context().add_class("section-title")
        box.pack_start(t, False, False, 0)
        sgrid = Gtk.Grid(column_spacing=12, row_spacing=4)
        self.stage_labels = {}
        for i, name in enumerate(STAGE_NAMES):
            st, tm = Gtk.Label(xalign=0), Gtk.Label(xalign=1, hexpand=True)
            sgrid.attach(Gtk.Label(label=name, xalign=0), 0, i, 1, 1)
            sgrid.attach(st, 1, i, 1, 1)
            sgrid.attach(tm, 2, i, 1, 1)
            self.stage_labels[name] = (st, tm)
        box.pack_start(sgrid, False, False, 0)

        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(box)
        return sw

    # ---------- tabs ----------
    def build_console(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=8, margin=6)
        self.console_title = Gtk.Label(label="No run started yet.", xalign=0, hexpand=True,
                                       ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.follow_console = Gtk.CheckButton(label="Follow", active=True)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.console.get_buffer().set_text(""))
        bar.pack_start(self.console_title, True, True, 0)
        bar.pack_start(self.follow_console, False, False, 0)
        bar.pack_start(clear, False, False, 0)
        self.console, sw = make_textview()
        box.pack_start(bar, False, False, 0)
        box.pack_start(sw, True, True, 0)
        return box

    def file_browser(self, groups_label):
        """List of files on the left, viewer with a line filter on the right."""
        store = Gtk.TreeStore(str, str, str, str)  # label, size, kind, path
        tree = Gtk.TreeView(model=store, headers_visible=False)
        tree.append_column(Gtk.TreeViewColumn("File", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE),
                                              markup=0))
        tree.get_column(0).set_expand(True)
        tree.append_column(Gtk.TreeViewColumn("Size", Gtk.CellRendererText(xalign=1, foreground=COLORS["muted"]),
                                              text=1))
        lsw = Gtk.ScrolledWindow(width_request=260)
        lsw.add(tree)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=8, margin=6)
        title = Gtk.Label(label=f"Pick a {groups_label}", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        flt = Gtk.SearchEntry(placeholder_text="Show only lines matching…", hexpand=True)
        issues = Gtk.CheckButton(label="Errors & warnings only")
        bar.pack_start(title, False, False, 0)
        bar.pack_start(flt, True, True, 0)
        bar.pack_start(issues, False, False, 0)
        tv, sw = make_textview()
        right.pack_start(bar, False, False, 0)
        right.pack_start(sw, True, True, 0)

        paned = Gtk.Paned(position=300)
        paned.pack1(lsw, False, False)
        paned.pack2(right, True, False)
        fb = {"store": store, "tree": tree, "title": title, "filter": flt, "issues": issues, "tv": tv,
              "text": "", "path": None, "widget": paned, "bar": bar}

        def render(*_):
            lines = fb["text"].split("\n")
            if issues.get_active():
                lines = [ln for ln in lines if ERR_LINE.search(ln) or WARN_LINE.search(ln) or "violation" in ln]
            q = flt.get_text().lower()
            if q:
                lines = [ln for ln in lines if q in ln.lower()]
            set_text(tv, "\n".join(lines))

        def on_select(sel):
            model, it = sel.get_selected()
            if it is None or not model[it][3]:
                return
            self.open_in_browser(fb, Path(model[it][3]))

        tree.get_selection().connect("changed", on_select)
        flt.connect("search-changed", render)
        issues.connect("toggled", render)
        fb["render"] = render
        return fb

    def open_in_browser(self, fb, path, scroll_end=False):
        fb["path"] = path
        fb["title"].set_text(path.name)
        try:
            fb["text"] = read_tail(path)
        except OSError as e:
            fb["text"] = f"Could not read {path}: {e}"
        fb["render"]()
        if scroll_end:
            tv = fb["tv"]
            GLib.idle_add(lambda: tv.scroll_to_iter(tv.get_buffer().get_end_iter(), 0, False, 0, 1) and False)

    def fill_files(self, fb, groups):
        store = fb["store"]
        store.clear()
        for title, files in groups:
            parent = store.append(None, [f"<b>{GLib.markup_escape_text(title)}</b> ({len(files)})", "", "", ""])
            for p in files:
                store.append(parent, [GLib.markup_escape_text(p.name), fmt_bytes(p.stat().st_size), "", str(p)])
        fb["tree"].expand_all()

    def build_reports(self):
        stack = Gtk.Stack()
        sw_bar = Gtk.StackSwitcher(stack=stack, halign=Gtk.Align.CENTER, margin=6)

        # QoR checks: summary of the latest stage plus a metric x stage table.
        checks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        self.checks_summary = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=6,
                                          column_spacing=8, row_spacing=8, homogeneous=True)
        self.checks_title = Gtk.Label(xalign=0)
        self.checks_title.get_style_context().add_class("section-title")
        checks.pack_start(self.checks_title, False, False, 0)
        checks.pack_start(self.checks_summary, False, False, 0)
        t = Gtk.Label(label="Metrics by stage (from the ORFS metrics JSON; red fails)", xalign=0)
        t.get_style_context().add_class("section-title")
        checks.pack_start(t, False, False, 6)
        self.metrics_box = Gtk.Box()
        checks.pack_start(self.metrics_box, True, True, 0)
        csw = Gtk.ScrolledWindow()
        csw.add(checks)
        stack.add_titled(csw, "checks", "QoR checks")

        self.reports_fb = self.file_browser("report")
        stack.add_titled(self.reports_fb["widget"], "files", "Report files")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.pack_start(sw_bar, False, False, 0)
        box.pack_start(stack, True, True, 0)
        return box

    def build_logs(self):
        self.logs_fb = self.file_browser("log")
        self.follow_log = Gtk.CheckButton(label="Follow", active=True,
                                          tooltip_text="Reload the open log every few seconds while it grows")
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Rescan logs")
        refresh.connect("clicked", lambda *_: self.refresh_logs())
        self.logs_fb["bar"].pack_start(self.follow_log, False, False, 0)
        self.logs_fb["bar"].pack_start(refresh, False, False, 0)
        return self.logs_fb["widget"]

    def build_debug(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)

        def title(text):
            t = Gtk.Label(label=text, xalign=0)
            t.get_style_context().add_class("section-title")
            box.pack_start(t, False, False, 0)

        title("Actions")
        acts = Gtk.Box(spacing=8)
        dry = Gtk.Button(label="Dry run (make -n)", tooltip_text="Show the commands make would run for the plan")
        dry.connect("clicked", self.on_dry_run)
        self.gui_cb = Gtk.ComboBoxText()
        self.gui_btn = Gtk.Button(label="Open in OpenROAD GUI")
        self.gui_btn.connect("clicked", self.on_open_gui)
        folder = Gtk.Button(label="Open results folder")
        folder.connect("clicked", lambda *_: self.open_folder("RESULTS_DIR"))
        copy = Gtk.Button(label="Copy command")
        copy.connect("clicked", self.on_copy)
        for w in (dry, self.gui_cb, self.gui_btn, folder, copy):
            acts.pack_start(w, False, False, 0)
        box.pack_start(acts, False, False, 0)

        title("Environment")
        self.env_store = Gtk.ListStore(str, str)
        box.pack_start(self.kv_view(self.env_store), False, False, 0)

        title("Resolved make variables for this config")
        self.vars_store = Gtk.ListStore(str, str)
        box.pack_start(self.kv_view(self.vars_store), False, False, 0)

        title("Errors and warnings in this design's logs (double-click to show them in Logs)")
        self.issues_store = Gtk.ListStore(str, str, int, str, str)  # markup, code, count, file, path
        tv = Gtk.TreeView(model=self.issues_store)
        tv.append_column(Gtk.TreeViewColumn("Severity", Gtk.CellRendererText(), markup=0))
        tv.append_column(Gtk.TreeViewColumn("Message ID", Gtk.CellRendererText(), text=1))
        tv.append_column(Gtk.TreeViewColumn("Count", Gtk.CellRendererText(xalign=1), text=2))
        tv.append_column(Gtk.TreeViewColumn("Most in", Gtk.CellRendererText(), text=3))
        tv.connect("row-activated", self.on_issue)
        isw = Gtk.ScrolledWindow(min_content_height=160)
        isw.add(tv)
        box.pack_start(isw, False, False, 0)

        title("Debug output")
        self.debug_tv, dsw = make_textview()
        dsw.set_min_content_height(220)
        box.pack_start(dsw, True, True, 0)

        sw = Gtk.ScrolledWindow()
        sw.add(box)
        return sw

    def kv_view(self, store):
        tv = Gtk.TreeView(model=store, headers_visible=False)
        tv.append_column(Gtk.TreeViewColumn("Name", Gtk.CellRendererText(weight=600), text=0))
        val = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, family="monospace")
        col = Gtk.TreeViewColumn("Value", val, text=1)
        col.set_expand(True)
        tv.append_column(col)
        tv.set_tooltip_column(1)
        return tv

    # ---------- flow and selection ----------
    def choose_flow(self, *_):
        dlg = Gtk.FileChooserDialog(title="Choose the ORFS flow directory", parent=self,
                                    action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        dlg.set_filename(str(self.flow))
        if dlg.run() == Gtk.ResponseType.OK:
            path = Path(dlg.get_filename())
            if not (path / "Makefile").exists() and (path / "flow" / "Makefile").exists():
                path = path / "flow"
            if (path / "Makefile").exists():
                self.load_flow(path)
                self.save_settings()
            else:
                self.message(f"{path} has no ORFS Makefile. Pick the flow/ folder of an ORFS checkout.")
        dlg.destroy()

    def load_flow(self, flow):
        self.flow = flow
        self.hb.set_subtitle(str(flow))
        self.platforms = discover(flow)
        self.platform_store.clear()
        for name, p in self.platforms.items():
            n = len(p["designs"])
            label = f"{name}  ({n} design{'s' if n != 1 else ''})" if p["available"] else f"{name}  (PDK not installed)"
            self.platform_store.append([name, label, p["available"]])
        ncfg = sum(len(c) for p in self.platforms.values() for c in p["designs"].values())
        self.set_status(f"{len(self.platforms)} platforms, {ncfg} design configs in {flow / 'designs'}")
        s = self.settings
        self.cores_cb.set_active_id(s.get("cores") or str(os.cpu_count() or 1))
        self.from_cb.set_active_id(s.get("from") or "continue")
        self.to_cb.set_active_id(s.get("to") or "finish")
        avail = [n for n, p in self.platforms.items() if p["available"]]
        want = s.get("platform") if s.get("platform") in avail else (avail[0] if avail else None)
        if want:
            self.platform_cb.set_active_id(want)
        else:
            self.update_plan()

    def on_platform(self, *_):
        p = self.platforms.get(self.platform_cb.get_active_id())
        self.design_store.clear()
        if not p:
            return
        for d, cfgs in p["designs"].items():
            self.design_store.append([d, d + (f"  ({len(cfgs)} configs)" if len(cfgs) > 1 else ""), True])
        want = self.settings.get("design")
        self.design_cb.set_active_id(want if want in p["designs"] else next(iter(p["designs"])))

    def on_design(self, *_):
        p = self.platforms.get(self.platform_cb.get_active_id())
        d = self.design_cb.get_active_id()
        self.config_cb.remove_all()
        if not p or d not in p["designs"]:
            return
        for c in p["designs"][d]:
            self.config_cb.append(c, c)
        want = self.settings.get("config")
        self.config_cb.set_active_id(want if want in p["designs"][d] else p["designs"][d][0])

    def on_config(self, *_):
        cfg = self.config_rel()
        self.vars = {}
        self.update_plan()
        self.inputs.config_changed(cfg)
        if not cfg:
            return
        self.settings.update(platform=self.platform_cb.get_active_id(), design=self.design_cb.get_active_id(),
                             config=self.config_cb.get_active_id())
        self.save_settings()
        self.vars_gen += 1
        gen = self.vars_gen
        self.set_status(f"Reading make variables for {cfg}…")
        threading.Thread(target=self.fetch_vars, args=(gen, cfg), daemon=True).start()

    def select_config(self, name):
        """Re-read the designs folder and select config `name` of the current design."""
        self.platforms = discover(self.flow)
        self.settings["config"] = name
        self.on_design()

    def reload_designs(self):
        """Pick up config files added or removed in the app, keeping the selection."""
        found = discover(self.flow)
        if found != self.platforms:
            self.select_config(self.config_cb.get_active_id())

    def make_env(self):
        return flow_env(self.tools())

    def inputs_dirty(self, dirty):
        """Unsaved inputs: keep the selection and block runs until they are saved or reverted."""
        if dirty == self.inputs_unsaved:
            return
        self.inputs_unsaved = dirty
        tip = "Save or revert the changes in Inputs first" if dirty else None
        for cb in (self.platform_cb, self.design_cb, self.config_cb):
            cb.set_sensitive(not dirty)
            cb.set_tooltip_text(tip)
        self.update_plan()

    def inputs_saved(self, verb, what, stage):
        """After a save in Inputs: re-read the results folders and point From stage at the first stale stage."""
        eff = self.inputs.effective
        self.vars_gen += 1
        self.on_vars(self.vars_gen, {k: eff[k] for k in MAKE_VARS if k in eff}, None)
        if verb == "Restored":
            return f"Restored {what} as it was before the last save. From stage is unchanged."
        if not stage:
            return (f"{verb} {what}. No flow variable changed and no stage reads this file directly, so From stage "
                    "is unchanged.")
        i = STAGE_NAMES.index(stage)
        has = self.stage_has_output()
        built = [s for s in STAGE_NAMES[i:] if has[s]]
        if not built:
            return f"{verb} {what}. Nothing from {stage} on is built yet, so the next run uses the new inputs."
        frm = self.from_cb.get_active_id()
        if frm == "continue" or STAGE_NAMES.index(frm) > i:
            self.from_cb.set_active_id(stage)
        return (f"{verb} {what}. {', '.join(built)} {'was' if len(built) == 1 else 'were'} built with the old "
                f"inputs, so From stage is now “Re-run from {stage}”.")

    def config_rel(self):
        p, d, c = self.platform_cb.get_active_id(), self.design_cb.get_active_id(), self.config_cb.get_active_id()
        return f"designs/{p}/{d}/{c}" if p and d and c else None

    def fetch_vars(self, gen, cfg):
        try:
            out = subprocess.run(["make", f"DESIGN_CONFIG={cfg}"] + [f"print-{v}" for v in MAKE_VARS],
                                 cwd=self.flow, env=flow_env(self.tools()), capture_output=True, text=True, timeout=60)
            vals, err = {}, None
            for line in out.stdout.splitlines():
                k, sep, v = line.partition(": ")
                if sep and k in MAKE_VARS:
                    vals[k] = v.strip()
            if out.returncode != 0:
                err = (out.stderr or out.stdout).strip()[-1500:]
        except (OSError, subprocess.TimeoutExpired) as e:
            vals, err = {}, str(e)
        GLib.idle_add(self.on_vars, gen, vals, err)

    def on_vars(self, gen, vals, err):
        if gen != self.vars_gen:
            return False
        self.vars = vals
        self.vars_store.clear()
        for k in MAKE_VARS:
            if k in vals:
                self.vars_store.append([k, vals[k] or "(empty)"])
        if err:
            self.vars_store.append(["make error", err.splitlines()[-1] if err else ""])
            set_text(self.debug_tv, f"make print-* failed for {self.config_rel()}:\n{err}\n")
            self.set_status("Could not read the make variables; see Debug")
        else:
            self.set_status(f"{self.config_rel()}: results in {vals.get('RESULTS_DIR', '?')}")
        self.update_plan()
        self.tick_status()
        self.refresh_tab(self.nb.get_current_page())
        return False

    def dir_of(self, var):
        v = self.vars.get(var)
        return (self.flow / v).resolve() if v else None

    # ---------- plan and run ----------
    def stage_done(self):
        res = self.dir_of("RESULTS_DIR")
        return {s[0]: bool(res and (res / s[3]).exists()) for s in STAGES}

    def stage_has_output(self):
        """Stages with any results or logs, finished or not: what their clean target deletes."""
        res, logs = self.dir_of("RESULTS_DIR"), self.dir_of("LOG_DIR")
        return {s[0]: any(d and d.is_dir() and any(d.glob(s[4] + "*")) for d in (res, logs)) for s in STAGES}

    def plan(self):
        """(commands, notes, error) for the current selection."""
        cfg = self.config_rel()
        if not cfg:
            return [], [], "Pick a design."
        if self.inputs_unsaved:
            return [], [], "Inputs has unsaved changes: save or revert them first."
        plat = self.platforms.get(self.platform_cb.get_active_id(), {})
        if not plat.get("available"):
            return [], [], "This platform's PDK is not in the flow directory."
        frm, to = self.from_cb.get_active_id(), self.to_cb.get_active_id()
        if not frm or not to:
            return [], [], "Pick the stages."
        ti = STAGE_NAMES.index(to)
        base = ["make", f"DESIGN_CONFIG={cfg}", f"NUM_CORES={self.cores_cb.get_active_id()}"]
        cmds, notes = [], []
        done = self.stage_done()
        if frm == "continue":
            todo = [s for s in STAGE_NAMES[: ti + 1] if not done[s]]
            notes.append(f"Runs {', '.join(todo)}." if todo else f"Everything up to {to} is already built; "
                         "make will only redo steps whose inputs changed.")
        else:
            fi = STAGE_NAMES.index(frm)
            if fi > ti:
                return [], [], "“From” comes after “To”."
            cleans = [s[2] for s in STAGES[fi:]]
            cmds.append(base + cleans)
            has = self.stage_has_output()
            wiped = [s + ("" if done[s] else " (partial)") for s in STAGE_NAMES[fi:] if has[s]]
            if wiped:
                notes.append(f"Deletes the results, logs and reports of: {', '.join(wiped)}.")
            missing = [s for s in STAGE_NAMES[:fi] if not done[s]]
            if missing:
                notes.append(f"Not built yet, so make will run them first: {', '.join(missing)}.")
            notes.append(f"Runs {', '.join(STAGE_NAMES[fi: ti + 1])}.")
        cmds.append(base + [STAGES[ti][1]])
        return cmds, notes, None

    def command_line(self, cmds):
        return " && ".join(shlex.join(c) for c in cmds)

    def update_plan(self):
        cmds, notes, err = self.plan()
        if err:
            self.plan_label.set_markup(colored(err, "warn"))
        else:
            text = GLib.markup_escape_text(self.command_line(cmds).replace(" && ", " &&\n"))
            self.plan_label.set_markup(text + "\n\n" + "\n".join(GLib.markup_escape_text(n) for n in notes))
        busy = bool(self.job and self.job.running())
        self.run_btn.set_sensitive(not err and not busy)
        self.stop_btn.set_sensitive(busy)
        self.inputs.set_busy(busy)

    def on_run(self, *_):
        cmds, notes, err = self.plan()
        if err or (self.job and self.job.running()):
            return
        if self.from_cb.get_active_id() != "continue" and any(n.startswith("Deletes") for n in notes):
            if not self.confirm("Delete earlier results and re-run?", "\n".join(notes)):
                return
        cmd = self.command_line(cmds)
        nick = self.vars.get("DESIGN_NICKNAME") or self.design_cb.get_active_id()
        variant = self.vars.get("FLOW_VARIANT") or "base"
        log = RUN_LOGS / (f"{self.platform_cb.get_active_id()}_{nick}_{variant}_{self.from_cb.get_active_id()}-"
                          f"{self.to_cb.get_active_id()}_{time.strftime('%Y%m%d_%H%M%S')}.log")
        try:
            self.job = Job(cmd, str(self.flow), flow_env(self.tools()), log, self.on_job_exit)
        except OSError as e:
            self.message(f"Could not start the run: {e}")
            return
        buf = self.console.get_buffer()
        buf.set_text("")
        self.console_title.set_text(f"{self.config_rel()}  ·  log: {log}")
        self.show_page("console")
        self.update_plan()
        self.tick_status()

    def on_stop(self, *_):
        if self.job and self.job.running() and self.confirm(
                "Stop the run?", "The step that is running is killed; its partial log is kept as a .tmp.log."):
            self.job.stop()
            self.set_status("Stop sent")

    def on_job_exit(self, job):
        self.tick_output()
        self.update_plan()
        self.tick_status()
        self.refresh_tab(self.nb.get_current_page())
        state = "finished" if job.rc == 0 else f"failed (exit code {job.rc})"
        self.set_status(f"Run {state} after {fmt_dur(time.time() - job.started)}. Log: {job.log_path}")
        return False

    def tick_output(self):
        if self.job:
            text = self.job.drain()
            if text:
                buf = self.console.get_buffer()
                start = buf.get_line_count() - 1
                buf.insert(buf.get_end_iter(), text)
                tag_lines(buf, start, text)
                # Keep the view fast: the full output is in the run log.
                if buf.get_line_count() > 30000:
                    buf.delete(buf.get_start_iter(), buf.get_iter_at_line(buf.get_line_count() - 25000))
                if self.follow_console.get_active():
                    self.console.scroll_to_mark(buf.get_insert(), 0, False, 0, 0)
                    buf.place_cursor(buf.get_end_iter())
        return True

    def tick_status(self):
        logs = self.dir_of("LOG_DIR")
        files = sorted(logs.glob("*.log")) if logs and logs.is_dir() else []
        busy = bool(self.job and self.job.running())
        done = self.stage_done()
        # The first stage that is not done is the one a run is working on. Synthesis writes its log in place
        # rather than as a .tmp.log, so a .tmp.log alone does not show it running.
        current_stage = next((n for n in STAGE_NAMES if not done[n]), None)
        for name, _, _, _, prefix in STAGES:
            steps = [step_info(p) for p in files if p.name.startswith(prefix)]
            if done[name]:
                state, color = "done", "pass"
            elif busy and name == current_stage and any(s["mtime"] >= self.job.started for s in steps):
                state, color = "running", "run"
            elif any(s["tmp"] for s in steps):
                state, color = ("running", "run") if busy else ("interrupted", "fail")
            elif steps:
                state, color = "partial", "warn"
            else:
                state, color = "not built", "muted"
            st, tm = self.stage_labels[name]
            st.set_markup(colored(state, color))
            total = sum(s["elapsed_s"] or 0 for s in steps)
            peak = max((s["peak_mb"] or 0 for s in steps), default=0)
            tm.set_markup(colored(f"{fmt_dur(total)}" + (f" · {peak / 1024:.1f} GB" if peak else ""), "muted"))
        if busy:
            rss, names = group_rss(self.job.proc.pid)
            _, avail = meminfo()
            # The step being written: a .tmp.log, else the newest log written since the run started.
            current = max((p for p in files if p.name.endswith(".tmp.log")), key=lambda p: p.stat().st_mtime,
                          default=None) or max((p for p in files if p.stat().st_mtime >= self.job.started),
                                               key=lambda p: p.stat().st_mtime, default=None)
            tools = sorted({n for n in names if n in ("openroad", "yosys", "klayout")})
            step = current.name.replace(".tmp.log", "").replace(".log", "") if current else ""
            self.set_status(f"Running {fmt_dur(time.time() - self.job.started)}"
                            + (f" · step {step}" if step else "")
                            + (f" · {', '.join(tools)}" if tools else "")
                            + f" · using {fmt_bytes(rss)} · {fmt_bytes(avail)} free")
            if self.current_page() == "logs" and self.follow_log.get_active():
                self.follow_open_log()
        self.gui_cb_fill()
        return True

    def gui_cb_fill(self):
        res = self.dir_of("RESULTS_DIR")
        have = [t for t, odb in GUI_TARGETS if res and (res / odb).exists()]
        if have != getattr(self, "_gui_have", None):
            self._gui_have = have
            self.gui_cb.remove_all()
            for t in have:
                self.gui_cb.append(t, t)
            if have:
                self.gui_cb.set_active_id(have[-1])
            else:
                self.gui_cb.append("", "nothing built yet")
                self.gui_cb.set_active(0)
            self.gui_btn.set_sensitive(bool(have))

    # ---------- tab contents ----------
    def current_page(self, n=None):
        page = self.nb.get_nth_page(self.nb.get_current_page() if n is None else n)
        return next((k for k, w in self.pages.items() if w is page), None)

    def show_page(self, key):
        self.nb.set_current_page(self.nb.page_num(self.pages[key]))

    def refresh_tab(self, n):
        page = self.current_page(n)
        if page == "reports":
            self.refresh_reports()
        elif page == "logs":
            self.refresh_logs()
        elif page == "debug":
            self.refresh_issues()
        return False

    def refresh_reports(self):
        rep = self.dir_of("REPORTS_DIR")
        files = sorted(p for p in rep.rglob("*") if p.is_file()) if rep and rep.is_dir() else []
        self.fill_files(self.reports_fb, [("Reports", files)])
        self.refresh_checks()

    def refresh_checks(self):
        logs = self.dir_of("LOG_DIR")
        cols, data = [], {}
        for name, fname in CHECK_COLUMNS:
            m = load_metrics(logs / fname) if logs else None
            if m is not None:
                cols.append(name)
                data[name] = m
        for child in self.checks_summary.get_children():
            self.checks_summary.remove(child)
        for child in self.metrics_box.get_children():
            self.metrics_box.remove(child)
        if not cols:
            self.checks_title.set_text("No metrics yet: run at least synthesis.")
            return
        last = cols[-1]
        self.checks_title.set_text(f"Sign-off checks at the latest stage with metrics: {last}")
        tiles = []
        for label, key, kind, scale in METRICS:
            v = data[last].get(key) if isinstance(key, str) else None
            if kind != "info" and isinstance(v, (int, float)):
                tiles.append((label, round(v * scale, 3), evaluate(kind, v)))
        res, rep = self.dir_of("RESULTS_DIR"), self.dir_of("REPORTS_DIR")
        drc = rep / "5_route_drc.rpt" if rep else None
        if drc and drc.exists():
            n = sum(1 for ln in drc.read_text(errors="replace").splitlines() if ln.startswith("violation type"))
            tiles.append(("DRC report violations", n, "pass" if n == 0 else "fail"))
        gds = res and any(res.glob("6_final.gds*"))
        tiles.append(("Final GDS", "written" if gds else "not yet", "pass" if gds else "muted"))
        for label, value, state in tiles:
            b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=6)
            b.pack_start(Gtk.Label(label=label, xalign=0), False, False, 0)
            v = Gtk.Label(xalign=0)
            v.set_markup(f'<span size="x-large" weight="bold">{GLib.markup_escape_text(f"{value:,}" if isinstance(value, (int, float)) else str(value))}</span>  '
                         + colored(state if state != "muted" else "pending", state))
            b.pack_start(v, False, False, 0)
            frame = Gtk.Frame()
            frame.add(b)
            self.checks_summary.add(frame)

        store = Gtk.ListStore(*([str] * (len(cols) + 1)))
        for label, key, kind, scale in METRICS:
            keys = key if isinstance(key, tuple) else (key,)
            row, any_val = [GLib.markup_escape_text(label)], False
            for c in cols:
                v = next((data[c][k] for k in keys if k in data[c]), None)
                if isinstance(v, (int, float)):
                    any_val = True
                    shown = v * scale
                    shown = f"{shown:,.3f}".rstrip("0").rstrip(".") if isinstance(shown, float) else f"{shown:,}"
                    s = evaluate(kind, v)
                    row.append(colored(shown, s) if s != "info" else GLib.markup_escape_text(shown))
                else:
                    row.append(colored("–", "muted"))
            if any_val:
                store.append(row)
        tv = Gtk.TreeView(model=store, hexpand=True)
        for i, name in enumerate(["Metric"] + cols):
            cell = Gtk.CellRendererText(xalign=0 if i == 0 else 1)
            col = Gtk.TreeViewColumn(name, cell, markup=i)
            col.set_expand(True)
            col.set_alignment(0 if i == 0 else 1)
            tv.append_column(col)
        self.metrics_box.pack_start(tv, True, True, 0)
        self.checks_summary.show_all()
        self.metrics_box.show_all()

    def refresh_logs(self):
        logs = self.dir_of("LOG_DIR")
        stage = sorted(logs.glob("*")) if logs and logs.is_dir() else []
        runs = sorted(RUN_LOGS.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if RUN_LOGS.is_dir() else []
        self.fill_files(self.logs_fb, [("Stage logs", [p for p in stage if p.is_file()]), ("Run logs", runs)])

    def follow_open_log(self):
        fb = self.logs_fb
        p = fb["path"]
        if p and p.exists() and time.time() - p.stat().st_mtime < 30:
            self.open_in_browser(fb, p, scroll_end=True)

    def refresh_issues(self):
        logs = self.dir_of("LOG_DIR")
        files = sorted(logs.glob("*.log")) if logs and logs.is_dir() else []

        def scan():
            counts = {}
            for p in files:
                try:
                    with open(p, errors="replace") as f:
                        for line in f:
                            for sev, code in ISSUE_RE.findall(line):
                                c = counts.setdefault((sev, code), {})
                                c[p] = c.get(p, 0) + 1
                except OSError:
                    continue
            GLib.idle_add(self.on_issues, counts)
        threading.Thread(target=scan, daemon=True).start()

    def on_issues(self, counts):
        self.issues_store.clear()
        order = sorted(counts.items(), key=lambda kv: (kv[0][0] != "ERROR", -sum(kv[1].values())))
        for (sev, code), per_file in order:
            top = max(per_file, key=per_file.get)
            self.issues_store.append([colored(sev, "fail" if sev == "ERROR" else "warn"), code,
                                      sum(per_file.values()), top.name, str(top)])
        return False

    def on_issue(self, tv, path, _col):
        row = self.issues_store[path]
        self.show_page("logs")
        self.open_in_browser(self.logs_fb, Path(row[4]))
        self.logs_fb["filter"].set_text(row[1])

    # ---------- debug actions ----------
    def probe_env(self):
        rows = [("Flow directory", str(self.flow))]
        tools = self.tools()
        for tool, _, flag in TOOLS:
            path = tool_path(tool, tools)
            if not path:
                rows.append((tool, f"NOT FOUND: {tools[tool]} is not a program" if tools.get(tool)
                             else "NOT FOUND on PATH; choose it with Tools…"))
                continue
            try:
                out = subprocess.run([path, flag], capture_output=True, text=True, timeout=20,
                                     env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
                ver = (out.stdout or out.stderr).strip().splitlines()
                rows.append((tool, f"{ver[0] if ver else '?'}  ({path})"))
            except (OSError, subprocess.TimeoutExpired) as e:
                rows.append((tool, f"{path}: {e}"))
        try:
            yhelp = subprocess.run([tool_path("yosys", tools) or "yosys", "-p", "help read_slang"],
                                   capture_output=True, text=True, timeout=20).stdout
            rows.append(("yosys read_slang", "available" if "read_slang" in yhelp and "No such command" not in yhelp
                         else "missing (needed for slang designs such as cva6)"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        total, avail = meminfo()
        rows.append(("CPU cores", str(os.cpu_count())))
        rows.append(("Memory", f"{fmt_bytes(avail)} available of {fmt_bytes(total)}"))
        rows.append(("Run logs", str(RUN_LOGS)))
        try:
            rev = subprocess.run(["git", "-C", str(self.flow), "log", "-1", "--format=%h %s"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if rev:
                rows.append(("Flow git commit", rev))
        except (OSError, subprocess.TimeoutExpired):
            pass
        GLib.idle_add(self.on_env, rows)

    def on_env(self, rows):
        self.env_store.clear()
        for k, v in rows:
            self.env_store.append([k, v])
        return False

    def on_dry_run(self, *_):
        cmds, _, err = self.plan()
        if err:
            return self.message(err)
        target = cmds[-1]
        set_text(self.debug_tv, f"$ {shlex.join(target[:1] + ['-n'] + target[1:])}\n(running…)\n")

        def work():
            try:
                out = subprocess.run(target[:1] + ["-n"] + target[1:], cwd=self.flow, env=flow_env(self.tools()),
                                     capture_output=True, text=True, timeout=120)
                text = out.stdout + out.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                text = str(e)
            note = "" if len(cmds) == 1 else ("Note: the plan cleans stages first, so the real run does more "
                                              "than this dry run shows.\n\n")
            GLib.idle_add(lambda: set_text(self.debug_tv, f"$ {shlex.join(target[:1] + ['-n'] + target[1:])}\n"
                                           + note + (text.strip() or "(nothing to do)")) and False)
        threading.Thread(target=work, daemon=True).start()

    def on_open_gui(self, *_):
        target = self.gui_cb.get_active_id()
        cfg = self.config_rel()
        if not target or not cfg:
            return
        cmd = ["make", f"DESIGN_CONFIG={cfg}", f"gui_{target}"]
        log = CACHE_DIR / "openroad_gui.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "wb") as f:
            subprocess.Popen(cmd, cwd=self.flow, env=flow_env(self.tools()), stdout=f, stderr=subprocess.STDOUT,
                             start_new_session=True)
        set_text(self.debug_tv, f"$ {shlex.join(cmd)}\nStarted. OpenROAD's output goes to {log}\n")
        self.set_status(f"Opening {target} in the OpenROAD GUI…")

    def open_folder(self, var):
        d = self.dir_of(var)
        if not d or not d.exists():
            return self.message("That folder does not exist yet. Run the flow first.")
        Gtk.show_uri_on_window(self, d.as_uri(), Gdk.CURRENT_TIME)

    def on_copy(self, *_):
        cmds, _, err = self.plan()
        if err:
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(f"cd {shlex.quote(str(self.flow))} && "
                                                            + self.command_line(cmds), -1)
        self.set_status("Command copied")

    # ---------- helpers ----------
    def set_status(self, text):
        self.status.set_text(text)

    def message(self, text):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.INFO,
                              buttons=Gtk.ButtonsType.OK, text=text)
        d.run()
        d.destroy()

    def confirm(self, title, detail):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.OK_CANCEL, text=title)
        d.format_secondary_text(detail)
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        return ok

    def on_close(self, *_):
        if self.inputs.is_dirty() and not self.confirm("Quit without saving the changes in Inputs?",
                                                        "They are lost if you quit now."):
            return True
        if self.job and self.job.running():
            if not self.confirm("A run is still going. Stop it and quit?",
                                "The flow is stopped; finished stages are kept."):
                return True
            self.job.stop()
        Gtk.main_quit()
        return False


def main():
    ap = argparse.ArgumentParser(prog=APP_ID, description=__doc__.splitlines()[0])
    ap.add_argument("--flow", help="ORFS flow directory (default: last used, $ORFS_FLOW, "
                                   "./OpenROAD-flow-scripts/flow, ./orfs/flow, then ~/OpenROAD-flow-scripts/flow)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = ap.parse_args()
    # Matches StartupWMClass in the .desktop file, so the dock groups the window under its menu entry.
    GLib.set_prgname(APP_ID)
    saved = None
    try:
        saved = json.loads(SETTINGS.read_text()).get("flow")
    except (OSError, ValueError):
        pass
    flow = Path(args.flow).resolve() if args.flow else \
        Path(saved) if saved and (Path(saved) / "Makefile").exists() else default_flow()
    if (flow / "flow" / "Makefile").exists() and not (flow / "Makefile").exists():
        flow = flow / "flow"
    win = App(flow)
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
