"""Inputs tab of ORFS Flow Runner: edit a design's timing constraints, flow settings and files in the app.

Everything is saved into the design's own files (its config*.mk, SDC and Tcl files), so a run from the app
and one from make use the same inputs. Every save keeps a backup of the file it replaced.
"""

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

ASSIGN_RE = re.compile(r"^(\s*)(export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*(\?=|::=|:=|\+=|=)\s*(.*)$")
INCLUDE_RE = re.compile(r"^\s*-?include\s+(.+?)\s*$")
SDC_SET_RE = re.compile(r"^(\s*set\s+)([A-Za-z_]\w*)(\s+)(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(\s*(?:;\s*)?(?:#.*)?)$")
SDC_PERIOD_RE = re.compile(r"^(\s*create_clock\b.*?-period\s+)(-?\d+(?:\.\d+)?)(.*)$")
SDC_TEXT_RE = re.compile(r"^(\s*set\s+)([A-Za-z_]\w*)(\s+)([A-Za-z_][\w.\[\]/]*)(\s*(?:;\s*)?(?:#.*)?)$")
NAME_RE = re.compile(r"[A-Za-z_][\w.\[\]/]*")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
STA_MSG_RE = re.compile(r"\[(WARNING|ERROR) (STA|ORD|ODB)-\d+\].*")
MANAGED = "# Set in ORFS Flow Runner"
# ORFS stage names in variables.json, mapped to the app's stages.
STAGE_OF = {"synth": "synth", "floorplan": "floorplan", "place": "place", "cts": "cts", "grt": "route",
            "route": "route", "final": "finish", "generate_abstract": "finish"}
COMMON = ["SYNTH_HIERARCHICAL", "SYNTH_MINIMUM_KEEP_SIZE", "ABC_AREA",
          "CORE_UTILIZATION", "CORE_ASPECT_RATIO", "CORE_MARGIN", "DIE_AREA", "CORE_AREA", "IO_CONSTRAINTS",
          "MACRO_PLACEMENT_TCL", "RTLMP_MIN_CHANNEL_SIZE", "PDN_TCL", "PLACE_DENSITY", "PLACE_PINS_ARGS",
          "GPL_TIMING_DRIVEN", "GPL_ROUTABILITY_DRIVEN", "TNS_END_PERCENT", "SETUP_SLACK_MARGIN",
          "HOLD_SLACK_MARGIN", "SKIP_CTS_REPAIR_TIMING", "MIN_ROUTING_LAYER", "MAX_ROUTING_LAYER",
          "ROUTING_LAYER_ADJUSTMENT", "DETAILED_ROUTE_END_ITERATION"]
GROUPS = [("common", "Common settings"), ("synth", "Synthesis"), ("floorplan", "Floorplan"),
          ("place", "Placement"), ("cts", "Clock tree"), ("route", "Routing"), ("finish", "Finishing"),
          ("here", "Set in this config"), ("all", "All variables")]
TEXT_SUFFIXES = {".mk", ".sdc", ".tcl", ".v", ".sv", ".vh", ".svh", ".json", ".cfg", ".txt", ".py", ".yaml",
                 ".yml", ".md", ".def"}
MAX_EDIT = 4 << 20
# New-file templates: label, file name, variable that points to it, header.
TEMPLATES = [
    ("IO pin constraints", "io_constraints.tcl", "IO_CONSTRAINTS",
     "# IO pin constraints, read by the floorplan stage (IO_CONSTRAINTS).\n"
     "# Regions are left, right, top or bottom, with * for the whole edge or a range in microns (bottom:0-50).\n"
     "# set_io_pin_constraint -direction INPUT -region left:*\n"
     "# set_io_pin_constraint -direction OUTPUT -region right:*\n"
     "# set_io_pin_constraint -region top:* -pin_names {clk_i rst_ni}\n"
     "# place_pin -pin_name clk_i -layer metal4 -location {0 100} -force_to_die_boundary\n"),
    ("Macro placement", "macro_placement.tcl", "MACRO_PLACEMENT_TCL",
     "# Macro placement, read by the floorplan stage (MACRO_PLACEMENT_TCL).\n"
     "# One line per macro: lower-left corner in microns, orientation R0, R180, MX or MY.\n"),
    ("Empty Tcl file", "custom.tcl", None, ""),
    ("Empty SDC file", "constraint_extra.sdc", None, ""),
]
CSS_MUTED = "#8a8f98"
CHANGED = "#3a7bd5"


def logical_lines(lines):
    """Join backslash continuations: [(first index, last index, text)]."""
    out, i = [], 0
    while i < len(lines):
        j, parts = i, [lines[i].rstrip("\n")]
        while parts[-1].endswith("\\") and j + 1 < len(lines):
            parts[-1] = parts[-1][:-1].rstrip()
            j += 1
            parts.append(lines[j].rstrip("\n").strip())
        out.append((i, j, " ".join(p for p in parts if p)))
        i = j + 1
    return out


def mk_entries(text):
    """Assignments and includes of a makefile in order: ("set", var, info) or ("include", raw, None)."""
    out = []
    for first, last, line in logical_lines(text.splitlines(keepends=True)):
        if line.lstrip().startswith("#"):
            continue
        m = INCLUDE_RE.match(line)
        if m:
            out.append(("include", m.group(1), None))
            continue
        m = ASSIGN_RE.match(line)
        if m:
            out.append(("set", m.group(3), {"first": first, "last": last, "indent": m.group(1),
                                            "export": m.group(2) or "", "op": m.group(4),
                                            "value": m.group(5).strip()}))
    return out


def expand(raw, here, subst):
    s = raw.replace("$(dir $(lastword $(MAKEFILE_LIST)))", str(here.parent) + "/")
    for k, v in subst.items():
        s = s.replace(f"$({k})", v)
    return None if "$(" in s else Path(s)


def config_chain(path, subst, seen=None):
    """Assignments in the order make reads them, following includes: [(var, file, info)]."""
    seen = set() if seen is None else seen
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    out = []
    for kind, name, info in mk_entries(path.read_text(errors="replace")):
        if kind == "include":
            inc = expand(name, path, subst)
            if inc:
                out += config_chain(inc.resolve(), subst, seen)
        else:
            out.append((name, path, info))
    return out


def set_mk_vars(text, changes):
    """Apply {var: value or None} to a config's text; None removes this file's assignment of var."""
    lines = text.splitlines(keepends=True)
    last = {}
    for kind, var, info in mk_entries(text):
        if kind == "set":
            last[var] = info
    edits, append = [], []
    for var, val in changes.items():
        if val is not None:
            val = re.sub(r"(?<!\\)#", r"\\#", val.strip())
        if var in last:
            a = last[var]
            new = [] if val is None else [f"{a['indent']}{a['export']}{var} {a['op']} {val}\n"]
            edits.append((a["first"], a["last"], new))
        elif val is not None:
            append.append(f"export {var} = {val}\n")
    for first, end, new in sorted(edits, reverse=True):
        lines[first:end + 1] = new
    if append:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if not any(ln.strip() == MANAGED for ln in lines):
            lines += ["\n", MANAGED + "\n"]
        lines += append
    return "".join(lines)


def sdc_fields(text):
    """Values the quick settings can change: [(line number, kind, name, value)], kind set, text or period."""
    out = []
    for i, line in enumerate(text.split("\n")):
        m = SDC_SET_RE.match(line)
        if m:
            out.append((i, "set", m.group(2), m.group(4)))
            continue
        m = SDC_TEXT_RE.match(line)
        if m:
            out.append((i, "text", m.group(2), m.group(4)))
            continue
        m = SDC_PERIOD_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            out.append((i, "period", "create_clock -period", m.group(2)))
    return out


def sdc_set_line(line, kind, value):
    if kind in ("set", "text"):
        rx = SDC_SET_RE if kind == "set" else SDC_TEXT_RE
        return rx.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + value + m.group(5), line)
    return SDC_PERIOD_RE.sub(lambda m: m.group(1) + value + m.group(3), line)


def sdc_code(text):
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("#"))


def sdc_unused(text):
    """Variables the SDC sets that no command in it uses."""
    code = sdc_code(text)
    names = re.findall(r"^\s*set\s+([A-Za-z_]\w*)\s", code, re.M)
    return [n for n in dict.fromkeys(names) if not re.search(r"\$\{?" + re.escape(n) + r"\b", code)]


def sdc_clock(text):
    """How the delay commands should name the clock: $clk_name, the create_clock -name, or None."""
    code = sdc_code(text)
    if re.search(r"^\s*set\s+clk_name\s", code, re.M):
        return "$clk_name"
    m = re.search(r"create_clock\b.*?-name\s+(\S+)", code)
    return m.group(1) if m else None


def sdc_period(text):
    for _, kind, name, value in sdc_fields(text):
        if kind == "period" or (kind == "set" and name in ("clk_period", "clock_period")):
            return float(value)
    return None


def fmt_num(x):
    return f"{x:.4g}"


def freq_text(period, unit):
    try:
        p = float(period) * (1e-12 if unit == "ps" else 1e-9)
    except ValueError:
        return ""
    if p <= 0:
        return ""
    f = 1 / p
    return f"= {f / 1e9:.3g} GHz" if f >= 0.9995e9 else f"= {f / 1e6:.4g} MHz"


def dump_vars(flow, cfg, names, env):
    """Every variable's value as make resolves it for this config, in one make call."""
    expr = "orfs_runner_dump: ; @: $(foreach v,$(ORFS_RUNNER_VARS),$(info $(v)=$($(v))))"
    out = subprocess.run(["make", "--no-print-directory", f"DESIGN_CONFIG={cfg}", f"--eval={expr}",
                          "orfs_runner_dump", "ORFS_RUNNER_VARS=" + " ".join(sorted(names))],
                         cwd=flow, env=env, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or out.stdout).strip()[-2000:] or f"make exited with {out.returncode}")
    known, vals = set(names), {}
    for line in out.stdout.splitlines():
        k, sep, v = line.partition("=")
        if sep and k in known:
            vals[k] = v.strip()
    return vals


def netlist_names(netlist, top, macros):
    """Top-level ports and macro instances in a synthesized netlist."""
    ports, insts, in_top = [], [], False
    cell_re = re.compile(r"^\s*(" + "|".join(map(re.escape, macros)) + r")\s+(\S+)\s*\(") if macros else None
    try:
        with open(netlist, errors="replace") as f:
            for line in f:
                if line.startswith("module "):
                    in_top = line.split()[1].split("(")[0] == top
                elif in_top:
                    m = re.match(r"\s*(input|output|inout)\s+(?:wire\s+)?(?:\[[^\]]+\]\s*)?([^\s;]+)\s*;", line)
                    if m:
                        ports.append((m.group(1), m.group(2).lstrip("\\")))
                    if cell_re:
                        m = cell_re.match(line)
                        if m:
                            insts.append(m.group(2).lstrip("\\"))
    except OSError:
        pass
    return ports, insts


def section(text):
    t = Gtk.Label(label=text, xalign=0)
    t.get_style_context().add_class("section-title")
    return t


def edit_view():
    tv = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE)
    tv.set_left_margin(8)
    tv.set_top_margin(6)
    sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
    sw.add(tv)
    return tv, sw


class InputsTab:
    def __init__(self, app, make_vars, backups):
        self.app, self.make_vars, self.backups = app, make_vars, backups
        self.cfg = None          # designs/<platform>/<design>/<config>.mk
        self.effective = {}      # every variable as make resolves it for this config
        self.chain = []          # (var, file, info) in the order make reads them
        self.pending = {}        # unsaved flow settings: {var: value, or None to remove}
        self.sdc_path = self.sdc_mtime = self.file_path = self.file_mtime = None
        self.last_save = []      # (path, backup or None if the save created it)
        self.busy = False
        self.gen = 0
        self._sdc_quiet = False
        self._sdc_timer = None
        self.variables = {}
        self.ports = None        # top-level ports from the synthesized netlist, once synthesis has run
        self.widget = self.build()

    # ---------- layout ----------
    def build(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.stack = Gtk.Stack()
        bar = Gtk.Box(spacing=8, margin=6)
        self.cfg_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        new_cfg = Gtk.Button(label="New config…", tooltip_text="Make a new config of this design, based on this one,"
                                                               " with its own results folder")
        new_cfg.connect("clicked", self.on_new_config)
        bar.pack_start(self.cfg_label, True, True, 0)
        bar.pack_start(Gtk.StackSwitcher(stack=self.stack), False, False, 0)
        bar.pack_start(new_cfg, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.info = Gtk.InfoBar(show_close_button=True)
        self.info_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.info.get_content_area().add(self.info_label)
        self.undo_btn = self.info.add_button("Undo save", 1)
        self.info.connect("response", self.on_info)
        self.info.set_no_show_all(True)
        self.info_label.show()
        box.pack_start(self.info, False, False, 0)

        self.stack.add_titled(self.build_sdc(), "sdc", "Constraints")
        self.stack.add_titled(self.build_settings(), "settings", "Flow settings")
        self.stack.add_titled(self.build_files(), "files", "Files")
        box.pack_start(self.stack, True, True, 0)
        return box

    def build_sdc(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        row = Gtk.Box(spacing=8)
        self.sdc_label = Gtk.Label(xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.sdc_copy = Gtk.Button(label="Use a separate copy for this config")
        self.sdc_copy.connect("clicked", self.on_sdc_copy)
        row.pack_start(Gtk.Label(label="SDC file", xalign=0), False, False, 0)
        row.pack_start(self.sdc_label, True, True, 0)
        row.pack_start(self.sdc_copy, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.sdc_shared = Gtk.Label(xalign=0, wrap=True)
        box.pack_start(self.sdc_shared, False, False, 0)
        # Settings and checks above, the file below, with a divider to give either more room.
        # Quick settings on the left, input and output delays and the OpenROAD check on the right.
        upper = Gtk.Box(spacing=24, margin_end=6)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left.pack_start(section("Quick settings"), False, False, 0)
        self.sdc_grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        left.pack_start(self.sdc_grid, False, False, 0)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        upper.pack_start(left, False, False, 0)
        upper.pack_start(right, True, True, 0)
        right.pack_start(section("Input and output delays"), False, False, 0)
        self.io_label = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=50)
        right.pack_start(self.io_label, False, False, 0)
        acts = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=8, row_spacing=6,
                           max_children_per_line=3)
        self.io_add = Gtk.Button(label="Add input and output delays",
                                 tooltip_text="Add set_input_delay on every input except the clock and "
                                              "set_output_delay on every output, using input_delay and output_delay")
        self.io_add.connect("clicked", self.on_io_add)
        self.io_ports = Gtk.Button(label="Delay for specific ports…")
        self.io_ports.connect("clicked", self.on_io_ports)
        self.sdc_check = Gtk.Button(label="Check with OpenROAD",
                                    tooltip_text="Read this SDC (with unsaved changes) into OpenROAD with the "
                                                 "synthesized design and list names that don't match")
        self.sdc_check.connect("clicked", self.on_sdc_check)
        for w in (self.io_add, self.io_ports, self.sdc_check):
            acts.add(w)
        right.pack_start(acts, False, False, 0)
        self.check_label = Gtk.Label(xalign=0, wrap=True, selectable=True, max_width_chars=50)
        right.pack_start(self.check_label, False, False, 0)
        usw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        usw.add(upper)
        lower = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        lower.pack_start(section("File contents"), False, False, 4)
        self.sdc_tv, sw = edit_view()
        self.sdc_tv.get_buffer().connect("changed", self.on_sdc_text)
        lower.pack_start(sw, True, True, 0)
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=330, wide_handle=True)
        paned.pack1(usw, True, False)
        paned.pack2(lower, True, False)
        box.pack_start(paned, True, True, 0)
        btns = Gtk.Box(spacing=8)
        self.sdc_state = Gtk.Label(xalign=0, hexpand=True)
        self.sdc_revert = Gtk.Button(label="Revert")
        self.sdc_revert.connect("clicked", lambda *_: self.load_sdc(force=True))
        self.sdc_save = Gtk.Button(label="Save")
        self.sdc_save.get_style_context().add_class("suggested-action")
        self.sdc_save.connect("clicked", self.on_sdc_save)
        for w in (self.sdc_state, self.sdc_revert, self.sdc_save):
            btns.pack_start(w, w is self.sdc_state, w is self.sdc_state, 0)
        box.pack_start(btns, False, False, 0)
        return box

    def build_settings(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        row = Gtk.Box(spacing=8)
        self.group_cb = Gtk.ComboBoxText()
        for gid, label in GROUPS:
            self.group_cb.append(gid, label)
        self.group_cb.set_active_id("common")
        self.search = Gtk.SearchEntry(placeholder_text="Find a variable or a word in its description…", hexpand=True)
        self.group_cb.connect("changed", lambda *_: self.filter.refilter())
        self.search.connect("search-changed", lambda *_: self.filter.refilter())
        row.pack_start(self.group_cb, False, False, 0)
        row.pack_start(self.search, True, True, 0)
        box.pack_start(row, False, False, 0)

        # name, value in this config, value make uses, set in, stage, tooltip, weight, editable, unsaved,
        # search text
        self.store = Gtk.ListStore(str, str, str, str, str, str, int, bool, bool, str)
        self.filter = self.store.filter_new()
        self.filter.set_visible_func(self.visible)
        tv = Gtk.TreeView(model=self.filter, tooltip_column=5)
        tv.append_column(Gtk.TreeViewColumn("Variable", Gtk.CellRendererText(), text=0, weight=6))
        val = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END, family="monospace", foreground=CHANGED)
        val.connect("edited", self.on_value_edited)
        col = Gtk.TreeViewColumn("Value in this config", val, text=1, editable=7, foreground_set=8)
        col.set_resizable(True)
        col.set_min_width(220)
        tv.append_column(col)
        eff = Gtk.TreeViewColumn("Value the flow uses", Gtk.CellRendererText(
            ellipsize=Pango.EllipsizeMode.END, family="monospace", foreground=CSS_MUTED), text=2)
        eff.set_resizable(True)
        eff.set_expand(True)
        tv.append_column(eff)
        tv.append_column(Gtk.TreeViewColumn("Set in", Gtk.CellRendererText(foreground=CSS_MUTED), text=3))
        tv.append_column(Gtk.TreeViewColumn("Stage", Gtk.CellRendererText(foreground=CSS_MUTED), text=4))
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)

        btns = Gtk.Box(spacing=8)
        hint = Gtk.Label(xalign=0, hexpand=True, wrap=True,
                         label="Click a value to change it; hover a row for its description. An empty value removes "
                               "it from this config, so the included file's or the default value applies.")
        self.set_revert = Gtk.Button(label="Revert")
        self.set_revert.connect("clicked", lambda *_: (self.pending.clear(), self.fill_settings(), self.dirty()))
        self.set_save = Gtk.Button(label="Save")
        self.set_save.get_style_context().add_class("suggested-action")
        self.set_save.connect("clicked", self.on_settings_save)
        btns.pack_start(hint, True, True, 0)
        btns.pack_start(self.set_revert, False, False, 0)
        btns.pack_start(self.set_save, False, False, 0)
        box.pack_start(btns, False, False, 0)
        return box

    def build_files(self):
        self.fstore = Gtk.TreeStore(str, str)  # markup, path
        tree = Gtk.TreeView(model=self.fstore, headers_visible=False)
        tree.append_column(Gtk.TreeViewColumn("File", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.MIDDLE),
                                              markup=0))
        tree.get_selection().connect("changed", self.on_file_pick)
        self.ftree = tree
        lsw = Gtk.ScrolledWindow(width_request=240, vexpand=True)
        lsw.add(tree)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        left.pack_start(lsw, True, True, 0)
        new = Gtk.Button(label="New file…", tooltip_text="Add a file to the design folder, for example IO pin "
                                                         "constraints or macro placement")
        new.connect("clicked", self.on_new_file)
        left.pack_start(new, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin=6)
        self.file_title = Gtk.Label(label="Pick a file", xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE,
                                    selectable=True)
        right.pack_start(self.file_title, False, False, 0)
        self.file_tv, sw = edit_view()
        self.file_tv.get_buffer().connect("modified-changed", lambda *_: self.dirty())
        right.pack_start(sw, True, True, 0)
        btns = Gtk.Box(spacing=8)
        self.file_state = Gtk.Label(xalign=0, hexpand=True)
        self.file_revert = Gtk.Button(label="Revert")
        self.file_revert.connect("clicked", lambda *_: self.open_file(self.file_path, force=True))
        self.file_save = Gtk.Button(label="Save")
        self.file_save.get_style_context().add_class("suggested-action")
        self.file_save.connect("clicked", self.on_file_save)
        btns.pack_start(self.file_state, True, True, 0)
        btns.pack_start(self.file_revert, False, False, 0)
        btns.pack_start(self.file_save, False, False, 0)
        right.pack_start(btns, False, False, 0)

        paned = Gtk.Paned(position=260)
        paned.pack1(left, False, False)
        paned.pack2(right, True, False)
        return paned

    # ---------- state ----------
    def sdc_dirty(self):
        return self.sdc_tv.get_buffer().get_modified()

    def file_dirty(self):
        return self.file_tv.get_buffer().get_modified()

    def is_dirty(self):
        return bool(self.pending) or self.sdc_dirty() or self.file_dirty()

    def dirty(self):
        """Update the save buttons and tell the app whether there are unsaved inputs."""
        idle = not self.busy
        why = "Wait for the run to finish before changing its inputs." if self.busy else None
        for btn, on in ((self.sdc_save, self.sdc_dirty()), (self.file_save, self.file_dirty()),
                        (self.set_save, bool(self.pending))):
            btn.set_sensitive(on and idle)
            btn.set_tooltip_text(why)
        self.sdc_revert.set_sensitive(self.sdc_dirty())
        self.file_revert.set_sensitive(self.file_dirty())
        self.set_revert.set_sensitive(bool(self.pending))
        n = len(self.pending)
        self.set_save.set_label(f"Save {n} change{'s' if n != 1 else ''}" if n else "Save")
        self.sdc_state.set_markup(self.state_markup(self.sdc_dirty()))
        shared = self.file_path in getattr(self, "shared_files", ()) and not self.file_dirty()
        self.file_state.set_markup(colored_text("Shared with other designs: a change here affects them too.",
                                                "#c88a00") if shared else
                                   self.state_markup(self.file_dirty()) if self.file_path else "")
        self.app.inputs_dirty(self.is_dirty())

    def state_markup(self, dirty):
        return '<span foreground="#c88a00">Unsaved changes</span>' if dirty else \
            f'<span foreground="{CSS_MUTED}">Saved</span>'

    def set_busy(self, busy):
        if busy != self.busy:
            self.busy = busy
            self.dirty()

    def show_info(self, text, kind=Gtk.MessageType.INFO, undo=False):
        self.info.set_message_type(kind)
        self.info_label.set_text(text)
        self.undo_btn.set_visible(undo and bool(self.last_save))
        self.info.show()

    def on_info(self, bar, resp):
        if resp == 1:
            self.undo_save()
        bar.hide()

    # ---------- loading ----------
    def config_changed(self, cfg):
        """The app selected another config (or re-read the current one)."""
        if cfg != self.cfg:
            self.pending.clear()
            self.info.hide()
            self.last_save = []
            if self.file_path and not self.file_dirty():
                self.file_path = None
        self.cfg = cfg
        self.cfg_label.set_markup(f"<b>{GLib.markup_escape_text(cfg)}</b>" if cfg else "Pick a design")
        self.refresh()

    def refresh(self, then=None):
        if not self.cfg:
            return
        if not self.variables:
            try:
                self.variables = json.loads((self.app.flow / "scripts" / "variables.json").read_text())
            except (OSError, ValueError):
                self.variables = {}
        self.gen += 1
        gen, cfg, flow, env = self.gen, self.cfg, self.app.flow, self.app.make_env()
        cfg_path = (flow / cfg).resolve()
        names = set(self.variables) | set(self.make_vars) | {"DESIGN_HOME", "DESIGN_NAME", "PLATFORM"}
        names |= {v for v, _, _ in config_chain(cfg_path, {"DESIGN_HOME": str(flow / "designs")})}

        def work():
            try:
                eff = dump_vars(flow, cfg, names, env)
                err = None
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
                eff, err = {}, str(e)
            subst = {"DESIGN_HOME": eff.get("DESIGN_HOME") or str(flow / "designs"),
                     "PLATFORM": eff.get("PLATFORM", ""), "DESIGN_NAME": eff.get("DESIGN_NAME", ""),
                     "DESIGN_NICKNAME": eff.get("DESIGN_NICKNAME", ""), "FLOW_HOME": str(flow)}
            chain = config_chain(cfg_path, subst)
            ports = self.read_ports(eff)
            # Which configs of this design use the same SDC file.
            shared = []
            sdc = eff.get("SDC_FILE")
            if sdc:
                for other in sorted(cfg_path.parent.glob("config*.mk")):
                    if other == cfg_path:
                        continue
                    try:
                        o = dump_vars(flow, str(other.relative_to(flow)), ["SDC_FILE"], env)
                        if o.get("SDC_FILE") == sdc:
                            shared.append(other.name)
                    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
                        pass
            GLib.idle_add(self.on_loaded, gen, eff, chain, shared, ports, err, then)
        threading.Thread(target=work, daemon=True).start()

    def on_loaded(self, gen, eff, chain, shared, ports, err, then):
        if gen != self.gen:
            return False
        if err:
            self.show_info("make could not read this config, so the inputs can't be shown. If a save caused "
                           f"this, use Undo save.\n\n{err.splitlines()[-1] if err else ''}",
                           Gtk.MessageType.ERROR, undo=True)
            if then:
                then(None)
            return False
        self.effective, self.chain, self.shared, self.ports = eff, chain, shared, ports
        self.fill_settings()
        self.load_sdc()
        self.fill_files()
        self.dirty()
        if then:
            then(eff)
        return False

    def cfg_path(self):
        return (self.app.flow / self.cfg).resolve()

    def synth_file(self, eff, *names):
        res = eff.get("RESULTS_DIR")
        if not res:
            return None
        return next((p for p in (self.resolve(res) / n for n in names) if p.exists()), None)

    def read_ports(self, eff):
        """Top-level ports from the synthesized netlist, [(direction, name)]; None before synthesis."""
        netlist = self.synth_file(eff, "1_synth.v", "1_2_yosys.v")
        if not netlist:
            return None
        key = (netlist, netlist.stat().st_mtime)
        if getattr(self, "_ports_key", None) != key:
            self._ports_key, self._ports = key, netlist_names(netlist, eff.get("DESIGN_NAME", ""), [])[0]
        return self._ports

    def design_dir(self):
        return self.cfg_path().parent

    def resolve(self, value):
        p = Path(value)
        return (p if p.is_absolute() else self.app.flow / p).resolve()

    def unit(self):
        return "ps" if self.effective.get("PLATFORM") == "asap7" else "ns"

    # ---------- constraints ----------
    def load_sdc(self, force=False):
        sdc = self.effective.get("SDC_FILE")
        path = self.resolve(sdc) if sdc else None
        if not force and self.sdc_dirty() and path == self.sdc_path:
            return
        self.sdc_path = path
        text = ""
        if path and path.is_file():
            text = path.read_text(errors="replace")
            self.sdc_mtime = path.stat().st_mtime
            self.sdc_label.set_text(self.short(path))
        else:
            self.sdc_mtime = None
            self.sdc_label.set_text(f"{sdc} (not found)" if sdc else "SDC_FILE is not set")
        buf = self.sdc_tv.get_buffer()
        self._sdc_quiet = True
        buf.set_text(text)
        buf.set_modified(False)
        self._sdc_quiet = False
        self.fill_sdc_fields(text)
        shared = getattr(self, "shared", [])
        inside = bool(path) and self.design_dir() in path.parents
        if shared:
            self.sdc_shared.set_markup(colored_text(
                f"Also used by {', '.join(shared)}: saving here changes those configs too. Use a separate copy to "
                "change only this one.", "#c88a00"))
        elif path and not inside:
            self.sdc_shared.set_markup(colored_text("This file is outside the design folder.", "#c88a00"))
        else:
            self.sdc_shared.set_markup(colored_text("Only this config uses this file.", CSS_MUTED))
        self.sdc_copy.set_sensitive(bool(path) and (bool(shared) or not inside))
        self.dirty()

    def port_names(self, direction=None):
        return [n for d, n in (self.ports or []) if direction in (None, d)]

    def field_note(self, kind, name, value, unused):
        """Unit, frequency or port check shown next to a quick setting: (text, colour or None)."""
        unit = self.unit()
        if kind == "period" or name in ("clk_period", "clock_period", "period"):
            return f"{unit}  {freq_text(value, unit)}", None
        if name in unused:
            return "set but not used", "#c88a00"
        if kind == "text" and "port" in name:
            if self.ports is None:
                return "port names are known once synthesis has run", None
            if value not in self.port_names():
                return f"not a port of {self.effective.get('DESIGN_NAME', 'the design')}", "#e0533f"
            d = next(d for d, n in self.ports if n == value)
            return f"{d} port", None
        if kind == "set" and ("delay" in name or "uncertainty" in name or "latency" in name):
            return unit, None
        return "", None

    def fill_sdc_fields(self, text):
        for child in self.sdc_grid.get_children():
            self.sdc_grid.remove(child)
        fields = sdc_fields(text)
        if not fields:
            self.sdc_grid.attach(Gtk.Label(label="No settings found; edit the file below.", xalign=0), 0, 0, 1, 1)
        unused = sdc_unused(text)
        for row, (line_no, kind, name, value) in enumerate(fields):
            if kind == "text" and "port" in name and self.ports:
                # Clock ports are inputs; the list also lets you type a name.
                combo = Gtk.ComboBoxText.new_with_entry()
                for n in self.port_names("input"):
                    combo.append_text(n)
                entry = combo.get_child()
                entry.set_text(value)
                widget = combo
            else:
                entry = widget = Gtk.Entry(text=value, width_chars=12 if kind != "text" else 20)
            note = Gtk.Label(xalign=0)
            self.set_note(note, *self.field_note(kind, name, value, unused))
            entry.connect("changed", self.on_sdc_field, line_no, kind, name, note)
            self.sdc_grid.attach(Gtk.Label(label=name, xalign=0), 0, row, 1, 1)
            self.sdc_grid.attach(widget, 1, row, 1, 1)
            self.sdc_grid.attach(note, 2, row, 1, 1)
        self.sdc_grid.show_all()
        self.update_io(text)

    def set_note(self, label, text, color):
        label.set_markup(colored_text(text, color or CSS_MUTED))

    def update_io(self, text):
        code = sdc_code(text)
        defined = set(re.findall(r"^\s*set\s+([A-Za-z_]\w*)\s", code, re.M))
        lines = []
        for kind in ("input", "output"):
            var = f"{kind}_delay"
            if f"set_{kind}_delay" in code:
                lines.append(colored_text(f"{kind.capitalize()}s: delays set with set_{kind}_delay.", CSS_MUTED))
            elif var in defined:
                lines.append(colored_text(f"{var} is set, but no set_{kind}_delay uses it: {kind} paths are not "
                                          "timed.", "#c88a00"))
            else:
                lines.append(colored_text(f"No set_{kind}_delay: {kind} paths are not timed.", "#c88a00"))
        self.io_label.set_markup("\n".join(lines))
        clock = sdc_clock(text)
        missing = "set_input_delay" not in code or "set_output_delay" not in code
        self.io_add.set_sensitive(bool(clock) and missing)
        self.io_add.set_tooltip_text(None if clock else "Define the clock (create_clock -name) first")
        self.io_ports.set_sensitive(bool(clock) and bool(self.ports))
        self.io_ports.set_tooltip_text(None if self.ports else "Port names are known once synthesis has run")
        odb = self.synth_file(self.effective, "1_synth.odb")
        self.sdc_check.set_sensitive(bool(odb))
        self.sdc_check.set_tooltip_text(self.sdc_check.get_tooltip_text() if odb else
                                        "Needs the synthesized design: run synthesis first")

    def on_sdc_field(self, entry, line_no, kind, name, note):
        value = entry.get_text().strip()
        if not (NAME_RE if kind == "text" else NUMBER_RE).fullmatch(value):
            entry.get_style_context().add_class("error")
            return
        entry.get_style_context().remove_class("error")
        buf = self.sdc_tv.get_buffer()
        self.set_note(note, *self.field_note(kind, name, value, sdc_unused(
            buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))))
        start = buf.get_iter_at_line(line_no)
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        line = buf.get_text(start, end, False)
        new = sdc_set_line(line, kind, value)
        if new != line:
            self._sdc_quiet = True
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_line(line_no), new)
            self._sdc_quiet = False
            self.dirty()

    def on_sdc_text(self, buf):
        if self._sdc_quiet:
            return
        self.dirty()
        # Re-read the quick settings once typing pauses.
        if self._sdc_timer:
            GLib.source_remove(self._sdc_timer)

        def reparse():
            self._sdc_timer = None
            self.fill_sdc_fields(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
            return False
        self._sdc_timer = GLib.timeout_add(500, reparse)

    def sdc_text(self):
        buf = self.sdc_tv.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def append_sdc(self, lines):
        """Add lines at the end of the SDC editor (not saved yet) and re-read the quick settings."""
        buf = self.sdc_tv.get_buffer()
        text = self.sdc_text()
        buf.insert(buf.get_end_iter(), ("" if text.endswith("\n") or not text else "\n") + "\n".join(lines) + "\n")
        self.fill_sdc_fields(self.sdc_text())
        self.check_label.set_text("")

    def on_io_add(self, *_):
        text = self.sdc_text()
        code, clock, period = sdc_code(text), sdc_clock(text), sdc_period(text)
        defined = set(re.findall(r"^\s*set\s+([A-Za-z_]\w*)\s", code, re.M))
        lines = ["", "# Input and output delays (added in ORFS Flow Runner)"]
        for kind, ports in (("input", "[all_inputs -no_clocks]"), ("output", "[all_outputs]")):
            if f"set_{kind}_delay" in code:
                continue
            if f"{kind}_delay" not in defined:
                # Without a value in the file, start from 20% of the clock period, as ORFS's own SDCs do.
                lines.append(f"set {kind}_delay {fmt_num(0.2 * period) if period else 0}")
            lines.append(f"set_{kind}_delay ${kind}_delay -clock {clock} {ports}")
        self.append_sdc(lines)

    def on_io_ports(self, *_):
        dlg = Gtk.Dialog(title="Delay for specific ports", transient_for=self.app, modal=True,
                         default_width=520, default_height=520)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        row = Gtk.Box(spacing=8)
        kind = Gtk.ComboBoxText()
        kind.append("input", "Inputs (set_input_delay)")
        kind.append("output", "Outputs (set_output_delay)")
        value = Gtk.Entry(width_chars=10, activates_default=True)
        unit = Gtk.Label(label=self.unit())
        row.pack_start(kind, False, False, 0)
        row.pack_start(Gtk.Label(label="Delay"), False, False, 0)
        row.pack_start(value, False, False, 0)
        row.pack_start(unit, False, False, 0)
        box.pack_start(row, False, False, 0)
        find = Gtk.SearchEntry(placeholder_text="Filter ports…")
        box.pack_start(find, False, False, 0)
        store = Gtk.ListStore(bool, str, str)  # chosen, name, direction
        for d, n in self.ports or []:
            if d in ("input", "output") and n != self.clock_port():
                store.append([False, n, d])
        flt = store.filter_new()
        flt.set_visible_func(lambda m, it, _d: m[it][2] == kind.get_active_id()
                             and find.get_text().lower() in m[it][1].lower())
        tv = Gtk.TreeView(model=flt, headers_visible=False)
        tick = Gtk.CellRendererToggle()

        def toggled(_c, path):
            it = flt.convert_iter_to_child_iter(flt.get_iter(path))
            store[it][0] = not store[it][0]
        tick.connect("toggled", toggled)
        tv.append_column(Gtk.TreeViewColumn("", tick, active=0))
        tv.append_column(Gtk.TreeViewColumn("Port", Gtk.CellRendererText(), text=1))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(tv)
        box.pack_start(sw, True, True, 0)
        note = Gtk.Label(xalign=0, wrap=True, label="The line is added after the others, so it replaces the general "
                                                    "delay for these ports. Save to keep it.")
        box.pack_start(note, False, False, 0)

        def on_kind(*_):
            var = f"{kind.get_active_id()}_delay"
            value.set_text(f"${var}" if re.search(rf"^\s*set\s+{var}\s", self.sdc_text(), re.M) else "0")
            flt.refilter()
        kind.connect("changed", on_kind)
        find.connect("search-changed", lambda *_: flt.refilter())
        kind.set_active_id("input")
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_content_area().add(box)
        dlg.show_all()
        while dlg.run() == Gtk.ResponseType.OK:
            names = [r[1] for r in store if r[0] and r[2] == kind.get_active_id()]
            v = value.get_text().strip()
            if not names:
                self.app.message("Tick at least one port.")
                continue
            if not (NUMBER_RE.fullmatch(v) or re.fullmatch(r"\$\w+|\[expr .*\]", v)):
                self.app.message("The delay is a number, a $variable or an [expr …].")
                continue
            self.append_sdc([f"set_{kind.get_active_id()}_delay {v} -clock {sdc_clock(self.sdc_text())} "
                             f"[get_ports {{{' '.join(names)}}}]"])
            break
        dlg.destroy()

    def clock_port(self):
        for _, kind, name, value in sdc_fields(self.sdc_text()):
            if kind == "text" and "port" in name:
                return value
        return None

    def on_sdc_check(self, *_):
        odb = self.synth_file(self.effective, "1_synth.odb")
        if not odb:
            return
        text = self.sdc_text()
        work_dir = self.backups.parent
        work_dir.mkdir(parents=True, exist_ok=True)
        sdc, tcl = work_dir / "sdc_check.sdc", work_dir / "sdc_check.tcl"
        sdc.write_text(text)
        tcl.write_text(
            f"read_db {{{odb}}}\n"
            f"if {{[catch {{read_sdc {{{sdc}}}}} err]}} {{ puts \"ORFS_RUNNER_ERROR $err\" }}\n"
            "foreach c [all_clocks] { puts \"ORFS_RUNNER_CLOCK [get_name $c] [get_property $c period] "
            "[llength [get_property $c sources]]\" }\n")
        env = self.app.make_env()
        env.update({k: v for k, v in self.effective.items() if f"env({k})" in text})
        exe = env.get("OPENROAD_EXE") or "openroad"
        self.check_label.set_markup(colored_text("Checking with OpenROAD…", CSS_MUTED))
        self.sdc_check.set_sensitive(False)
        unused = sdc_unused(text)

        def work():
            try:
                out = subprocess.run([exe, "-no_init", "-exit", str(tcl)], cwd=self.app.flow, env=env,
                                     capture_output=True, text=True, timeout=300)
                log = out.stdout + out.stderr
            except (OSError, subprocess.TimeoutExpired) as e:
                log = f"ORFS_RUNNER_ERROR {e}"
            GLib.idle_add(self.on_sdc_checked, log, unused, odb)
        threading.Thread(target=work, daemon=True).start()

    def on_sdc_checked(self, log, unused, odb):
        self.sdc_check.set_sensitive(True)
        unit = self.unit()
        problems, clocks = [], []
        for line in log.splitlines():
            if line.startswith("ORFS_RUNNER_ERROR "):
                problems.append(("#e0533f", "Error: " + line.split(" ", 1)[1]))
            elif line.startswith("ORFS_RUNNER_CLOCK "):
                _, name, period, nsrc = line.split()
                clocks.append(f"clock {name}, period {fmt_num(float(period))} {unit} "
                              f"{freq_text(period, unit)}, on {nsrc} port{'s' if nsrc != '1' else ''}")
            else:
                m = STA_MSG_RE.search(line)
                if m and "ORD-0012" not in line:
                    problems.append(("#e0533f" if m.group(1) == "ERROR" else "#c88a00", m.group(0)))
        problems = list(dict.fromkeys(problems))
        out = [colored_text(f"Checked against {odb.name}: " + ("; ".join(clocks) if clocks else "no clock defined"),
                            CSS_MUTED if clocks else "#e0533f")]
        if not clocks:
            problems.append(("#e0533f", "No clock: every path is untimed."))
        for n in unused:
            problems.append(("#c88a00", f"{n} is set but never used."))
        out += [colored_text(t, c) for c, t in problems[:20]]
        if len(problems) > 20:
            out.append(colored_text(f"… and {len(problems) - 20} more", CSS_MUTED))
        if not problems:
            out.append(colored_text("No problems: every port and clock name matched.", "#2e9e5b"))
        self.check_label.set_markup("\n".join(out))
        return False

    def on_sdc_save(self, *_):
        buf = self.sdc_tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if self.sdc_path and self.write([(self.sdc_path, text, self.sdc_mtime)]):
            buf.set_modified(False)
            self.after_save([self.sdc_path])

    def on_sdc_copy(self, *_):
        if not self.sdc_path:
            return
        stem = re.sub(r"^config_?", "", Path(self.cfg).stem) or "base"
        new = self.design_dir() / f"constraint_{stem}.sdc"
        n = 2
        while new.exists():
            new = self.design_dir() / f"constraint_{stem}_{n}.sdc"
            n += 1
        buf = self.sdc_tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        rel = f"$(DESIGN_HOME)/$(PLATFORM)/{self.design_dir().name}/{new.name}"
        cfg = self.cfg_path()
        if not self.app.confirm(f"Copy the SDC to {new.name}?",
                                f"{new.name} gets the text shown here (with any unsaved changes), and {cfg.name} "
                                f"is set to use it (SDC_FILE). {self.sdc_path.name} stays as it is for the other "
                                "configs."):
            return
        cfg_text = set_mk_vars(cfg.read_text(errors="replace"), {"SDC_FILE": rel})
        if self.write([(new, text, None), (cfg, cfg_text, None)]):
            buf.set_modified(False)
            self.after_save([new, cfg])

    # ---------- flow settings ----------
    def stage_of(self, var):
        stages = [STAGE_OF.get(s) for s in (self.variables.get(var, {}).get("stages") or [])]
        order = ["synth", "floorplan", "place", "cts", "route", "finish"]
        known = sorted((s for s in stages if s), key=order.index)
        return known[0] if known else "synth"

    def fill_settings(self):
        here = {}
        appends = set()
        cfg = self.cfg_path() if self.cfg else None
        set_in = {}
        for var, path, info in self.chain:
            set_in[var] = path
            if path == cfg:
                if info["op"] == "+=":
                    appends.add(var)
                here[var] = info["value"]
        names = sorted(set(self.variables) | set(here))
        self.store.clear()
        for var in names:
            meta = self.variables.get(var, {})
            desc = (meta.get("description") or "").strip()
            default = meta.get("default")
            tip = desc + (f"\n\nORFS default: {default}" if default not in (None, "") else "")
            if var in appends:
                tip += "\n\nThis config adds to the value with +=; change it in Files."
            src = set_in.get(var)
            where = "this config" if src == cfg else src.name if src else \
                "default" if self.effective.get(var) else ""
            stage = self.stage_of(var) if var in self.variables else ""
            changed = var in self.pending
            value = (self.pending[var] or "") if changed else here.get(var, "")
            self.store.append([var, value, self.effective.get(var, ""), where, stage,
                               GLib.markup_escape_text(tip or var), 700 if (var in here or changed) else 400,
                               var not in appends, changed, f"{var} {desc}".lower()])
        self.filter.refilter()

    def visible(self, model, it, _data):
        group = self.group_cb.get_active_id() or "all"
        var, stage, where = model[it][0], model[it][4], model[it][3]
        q = self.search.get_text().strip().lower()
        if q:
            return q in model[it][9]
        if group == "common":
            return var in COMMON or (where == "this config" and var in self.variables)
        if group == "here":
            return where == "this config" or var in self.pending
        return group == "all" or stage == group

    def on_value_edited(self, _cell, fpath, text):
        it = self.filter.convert_iter_to_child_iter(self.filter.get_iter(fpath))
        var = self.store[it][0]
        here = next((info["value"] for v, p, info in reversed(self.chain)
                     if v == var and p == self.cfg_path()), None)
        new = text.strip()
        if new == (here or ""):
            self.pending.pop(var, None)
        else:
            self.pending[var] = new or None
        self.store[it][1] = new
        self.store[it][6] = 700 if (new or here) else 400
        self.store[it][8] = var in self.pending
        self.dirty()

    def on_settings_save(self, *_):
        cfg = self.cfg_path()
        text = set_mk_vars(cfg.read_text(errors="replace"), self.pending)
        if self.write([(cfg, text, None)]):
            self.pending.clear()
            self.after_save([cfg])

    # ---------- files ----------
    def short(self, path):
        try:
            return str(path.relative_to(self.app.flow))
        except ValueError:
            return str(path)

    def fill_files(self):
        ddir = self.design_dir()
        mine = sorted(p for p in ddir.iterdir() if p.is_file() and p.suffix in TEXT_SUFFIXES
                      and p.stat().st_size <= MAX_EDIT)
        # Files outside the design folder that this config points to (SDC, Tcl and the like).
        used = []
        for var, val in sorted(self.effective.items()):
            for word in val.split():
                p = self.resolve(word) if ("/" in word and Path(word).suffix in TEXT_SUFFIXES) else None
                if p and p.is_file() and ddir not in p.parents and p not in used and p.stat().st_size <= MAX_EDIT \
                        and var not in ("VERILOG_FILES", "VERILOG_INCLUDE_DIRS"):
                    used.append(p)
        self.shared_files = set(used)
        self.fstore.clear()
        for title, files in ((f"Design folder ({ddir.name})", mine),
                             ("Shared with other designs", used)):
            if not files:
                continue
            parent = self.fstore.append(None, [f"<b>{GLib.markup_escape_text(title)}</b>", ""])
            for p in files:
                label = GLib.markup_escape_text(p.name)
                if p == self.cfg_path():
                    label = f"<b>{label}</b>"
                elif p.parent != ddir:
                    label += "  " + colored_text(self.short(p.parent), CSS_MUTED)
                self.fstore.append(parent, [label, str(p)])
        self.ftree.expand_all()
        if self.file_path and self.file_path.exists() and not self.file_dirty():
            self.open_file(self.file_path, force=True)

    def on_file_pick(self, sel):
        model, it = sel.get_selected()
        if it is None or not model[it][1]:
            return
        path = Path(model[it][1])
        if path == self.file_path:
            return
        if self.file_dirty() and not self.app.confirm(f"Discard your changes to {self.file_path.name}?",
                                                      "They have not been saved."):
            return
        self.open_file(path, force=True)

    def open_file(self, path, force=False):
        if not path or (not force and self.file_dirty()):
            return
        self.file_path = path
        try:
            text = path.read_text(errors="replace")
            self.file_mtime = path.stat().st_mtime
        except OSError as e:
            text, self.file_mtime = f"Could not read {path}: {e}", None
        self.file_title.set_text(self.short(path))
        buf = self.file_tv.get_buffer()
        buf.set_text(text)
        buf.set_modified(False)
        self.dirty()

    def on_file_save(self, *_):
        buf = self.file_tv.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        if self.file_path and self.write([(self.file_path, text, self.file_mtime)]):
            buf.set_modified(False)
            self.after_save([self.file_path])

    def on_new_file(self, *_):
        if not self.cfg:
            return
        if self.file_dirty() and not self.app.confirm(f"Discard your changes to {self.file_path.name}?",
                                                      "The new file opens in the editor in its place."):
            return
        dlg = Gtk.Dialog(title="New file", transient_for=self.app, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        kind = Gtk.ComboBoxText()
        for i, (label, *_rest) in enumerate(TEMPLATES):
            kind.append(str(i), label)
        name = Gtk.Entry(activates_default=True, hexpand=True)
        use = Gtk.CheckButton()
        grid.attach(Gtk.Label(label="Kind", xalign=0), 0, 0, 1, 1)
        grid.attach(kind, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="File name", xalign=0), 0, 1, 1, 1)
        grid.attach(name, 1, 1, 1, 1)
        grid.attach(use, 1, 2, 1, 1)
        grid.attach(Gtk.Label(label=f"Saved in {self.short(self.design_dir())}", xalign=0), 1, 3, 1, 1)

        def on_kind(*_):
            _label, fname, var, _head = TEMPLATES[int(kind.get_active_id())]
            name.set_text(fname)
            use.set_visible(bool(var))
            use.set_label(f"Use it in {Path(self.cfg).name} (sets {var})" if var else "")
            use.set_active(bool(var) and not self.effective.get(var))
        kind.connect("changed", on_kind)
        kind.set_active(0)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_content_area().add(grid)
        dlg.show_all()
        on_kind()
        while dlg.run() == Gtk.ResponseType.OK:
            fname = name.get_text().strip()
            path = self.design_dir() / fname
            if not re.fullmatch(r"[\w.+-]+", fname) or fname.startswith("."):
                self.app.message("Use a plain file name: letters, digits, dot, dash or underscore.")
                continue
            if path.exists():
                self.app.message(f"{fname} already exists. Open it from the list instead.")
                continue
            _label, _f, var, head = TEMPLATES[int(kind.get_active_id())]
            files = [(path, head + self.template_body(var), None)]
            if var and use.get_active():
                cfg = self.cfg_path()
                rel = f"$(DESIGN_HOME)/$(PLATFORM)/{self.design_dir().name}/{fname}"
                files.append((cfg, set_mk_vars(cfg.read_text(errors="replace"), {var: rel}), None))
            if self.write(files):
                self.file_path = None
                self.after_save([p for p, _, _ in files], open_path=path)
            break
        dlg.destroy()

    def template_body(self, var):
        """Port or macro names from the synthesized netlist, as commented lines to start from."""
        res = self.effective.get("RESULTS_DIR")
        netlist = next((p for p in (self.resolve(res) / n for n in ("1_synth.v", "1_2_yosys.v")) if p.exists()),
                       None) if res else None
        if not netlist or var not in ("IO_CONSTRAINTS", "MACRO_PLACEMENT_TCL"):
            return "" if var not in ("IO_CONSTRAINTS", "MACRO_PLACEMENT_TCL") else \
                "#\n# Run synthesis first to have this file list the design's names.\n"
        macros = [Path(p).stem for p in self.effective.get("ADDITIONAL_LEFS", "").split()]
        ports, insts = netlist_names(netlist, self.effective.get("DESIGN_NAME", ""), macros)
        if var == "IO_CONSTRAINTS":
            lines = [f"#\n# Ports of {self.effective.get('DESIGN_NAME')} in {netlist.name}:"]
            for d in ("input", "output", "inout"):
                names = [n for k, n in ports if k == d]
                if names:
                    lines.append(f"# {d}s: " + " ".join(names))
            return "\n".join(lines) + "\n"
        if not insts:
            return "#\n# No macros found in the netlist.\n"
        # OpenROAD keeps the brackets of flattened names escaped, so the names are written that way.
        lines = [f"#\n# The {len(insts)} macros in {netlist.name}:"]
        lines += ["# place_macro -macro_name {" + n.replace("[", "\\[").replace("]", "\\]")
                  + "} -location {0 0} -orientation R0" for n in insts]
        return "\n".join(lines) + "\n"

    # ---------- new config ----------
    def on_new_config(self, *_):
        if not self.cfg:
            return
        base = self.cfg_path()
        dlg = Gtk.Dialog(title="New config", transient_for=self.app, modal=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        name = Gtk.Entry(activates_default=True, hexpand=True, placeholder_text="for example util50")
        grid.attach(Gtk.Label(label="Name", xalign=0), 0, 0, 1, 1)
        grid.attach(name, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label=f"Starts as a copy of {base.name}'s settings (it includes {base.name}) and keeps "
                                    "its results in a separate folder.\nChange it in Inputs once it is created.",
                              xalign=0, wrap=True, max_width_chars=60), 0, 1, 2, 1)
        dlg.set_default_response(Gtk.ResponseType.OK)
        dlg.get_content_area().add(grid)
        dlg.show_all()
        while dlg.run() == Gtk.ResponseType.OK:
            n = name.get_text().strip()
            path = base.parent / f"config_{n}.mk"
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_]*", n):
                self.app.message("Use letters, digits and underscores, starting with a letter or digit.")
                continue
            if path.exists():
                self.app.message(f"{path.name} already exists.")
                continue
            text = (f"# Created in ORFS Flow Runner from {base.name}.\n"
                    f"include $(dir $(lastword $(MAKEFILE_LIST))){base.name}\n"
                    f"export FLOW_VARIANT = {n}\n")
            if self.write([(path, text, None)]):
                saved = self.last_save
                self.app.select_config(path.name)
                self.last_save = saved
                self.show_info(f"Created {path.name}. Its results go to a separate {n} folder; change its "
                               "settings here before running it.", undo=True)
            break
        dlg.destroy()

    # ---------- saving ----------
    def write(self, files):
        """Write [(path, text, mtime when read)], backing up what they replace; False if the user cancels."""
        for path, _, mtime in files:
            if mtime is not None and path.exists() and path.stat().st_mtime != mtime:
                if not self.app.confirm(f"{path.name} changed on disk since it was opened here",
                                        "Save anyway and replace those changes?"):
                    return False
        stamp = time.strftime("%Y%m%d_%H%M%S")
        saved = []
        try:
            for path, text, _ in files:
                bak = None
                if path.exists():
                    bak = self.backups / stamp / self.short(path).replace("/", "__")
                    bak.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, bak)
                path.write_text(text if text.endswith("\n") or not text else text + "\n")
                saved.append((path, bak))
        except OSError as e:
            self.show_info(f"Could not save: {e}", Gtk.MessageType.ERROR)
            return False
        self.last_save = saved
        return True

    def after_save(self, paths, open_path=None, verb="Saved"):
        """Re-read the config, then tell the app which stage the change affects."""
        before = dict(self.effective)

        def report(after):
            if after is None:
                return
            changed = [v for v in sorted(set(before) | set(after)) if before.get(v) != after.get(v)]
            strs = {str(p.resolve()) for p in paths}
            uses = [v for v, val in after.items() if any(str(self.resolve(w)) in strs for w in val.split() if "/" in w)]
            names = list(dict.fromkeys(changed + uses))
            stage = min((self.stage_of(v) for v in names if v in self.variables),
                        key=["synth", "floorplan", "place", "cts", "route", "finish"].index, default=None)
            what = ", ".join(p.name for p in paths)
            detail = f" Changed: {', '.join(changed[:6])}{'…' if len(changed) > 6 else ''}." if changed else ""
            self.show_info(self.app.inputs_saved(verb, what, stage) + detail, undo=verb == "Saved")
            if open_path:
                self.stack.set_visible_child_name("files")
                self.open_file(open_path, force=True)
        self.app.reload_designs()
        self.refresh(then=report)

    def undo_save(self):
        restored = []
        for path, bak in self.last_save:
            try:
                if bak:
                    shutil.copy2(bak, path)
                elif path.exists():
                    path.unlink()
                restored.append(path)
            except OSError as e:
                self.show_info(f"Could not restore {path.name}: {e}", Gtk.MessageType.ERROR)
                return
        self.last_save = []
        for buf in (self.sdc_tv.get_buffer(), self.file_tv.get_buffer()):
            buf.set_modified(False)
        if self.file_path and not self.file_path.exists():
            self.file_path = None
            self.file_title.set_text("Pick a file")
            self.file_tv.get_buffer().set_text("")
        self.after_save(restored, verb="Restored")


def colored_text(text, color):
    return f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>'
