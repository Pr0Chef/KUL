"""Floorplan page of the Inputs tab: die and core size, a drawn preview, and macro positions.

The page sets the floorplan the way ORFS reads it (see floorplan.py): either from utilization or as a fixed die and
core. ORFS stops if both are set, so switching the mode empties the other mode's variables. Macros can be dragged
in the preview or typed in the table; their positions are saved in MACRO_PLACEMENT_TCL. If the floorplan stage has
run, the real die, core and macro positions of that run are read from its result and drawn in grey for reference.
"""

import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import floorplan as fp  # noqa: E402
import lef  # noqa: E402
from input_page import ERROR, MUTED, hint_label, markup, section  # noqa: E402
from stage_forms import FormPage  # noqa: E402

UTILIZATION = [("CORE_UTILIZATION", "Core utilization (%)", "float", (1.0, 100.0)),
               ("CORE_ASPECT_RATIO", "Aspect ratio (height / width)", "float", (0.05, 20.0)),
               ("CORE_MARGIN", "Core to die margin (microns)", "text", None)]
FIXED = [("DIE_AREA", "Die area: x1 y1 x2 y2 (microns)", "text", None),
         ("CORE_AREA", "Core area: x1 y1 x2 y2 (microns)", "text", None)]
MACROS = [("RTLMP_MIN_CHANNEL_SIZE", "Smallest gap between macros (microns)", "text", None)]
# Reads the last floorplan result: die, core, and every macro with its position, orientation and size.
ODB_QUERY = r"""
read_db {%s}
set block [ord::get_db_block]
proc um {v} { return [ord::dbu_to_microns $v] }
set d [$block getDieArea]
puts "DIE [um [$d xMin]] [um [$d yMin]] [um [$d xMax]] [um [$d yMax]]"
set c [$block getCoreArea]
puts "CORE [um [$c xMin]] [um [$c yMin]] [um [$c xMax]] [um [$c yMax]]"
foreach inst [$block getInsts] {
  set m [$inst getMaster]
  if { [$m isBlock] } {
    set b [$inst getBBox]
    set size "[um [$m getWidth]] [um [$m getHeight]]"
    puts "MACRO [list [$inst getName]] [$m getName] [um [$b xMin]] [um [$b yMin]] [$inst getOrient] $size"
  }
}
"""
# Table columns.
NAME, MASTER, SIZE, X, Y, ORIENT, BY, EDITABLE = range(8)


def read_last_run(openroad, odb):
    """(die, core, {instance: (master, x, y, orient, w, h)}) from a floorplan result, or None if it can't be read."""
    try:
        out = subprocess.run([openroad, "-no_init", "-no_splash", "-exit"], input=ODB_QUERY % odb,
                             capture_output=True, text=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    die = core = None
    macros = {}
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["DIE"]:
            die = tuple(float(v) for v in parts[1:5])
        elif parts[:1] == ["CORE"]:
            core = tuple(float(v) for v in parts[1:5])
        elif parts[:1] == ["MACRO"] and len(parts) >= 8:
            name = fp.plain_name(parts[1])
            macros[name] = (parts[2], float(parts[3]), float(parts[4]), parts[5], float(parts[6]), float(parts[7]))
    return (die, core, macros) if die else None


class FloorplanPage(FormPage):
    name, title = "floorplan", "Floorplan"
    intro = ("The die is the chip's outline; the core inside it holds the rows of standard cells and the macros. "
             "Size them from a target utilization, or give the die and core exactly.")
    groups = []

    def build(self):
        self.mode_buttons = {}
        self.placements = {}      # macros placed on this page: {instance: (x, y, orient)}
        self.fixed_by_hand = {}   # place_macro lines outside the page's block
        self.macro_master = {}    # instance: master
        self.sizes = {}           # master: (w, h)
        self.last_run = None
        self.macro_dirty = False
        self.drag = None
        return super().build()

    def extra(self, body):
        # The mode switch goes first, then the two sets of fields, then macros and the preview.
        mode = Gtk.Box(spacing=12)
        util = Gtk.RadioButton.new_with_label(None, "Size from utilization")
        fixed = Gtk.RadioButton.new_with_label_from_widget(util, "Fixed die and core")
        for key, b in (("util", util), ("fixed", fixed)):
            b.connect("toggled", self.on_mode, key)
            mode.pack_start(b, False, False, 0)
            self.mode_buttons[key] = b
        body.pack_start(section("Die and core"), False, False, 0)
        body.pack_start(mode, False, False, 0)
        self.util_grid, self.fixed_grid = Gtk.Grid(column_spacing=12, row_spacing=2), Gtk.Grid(column_spacing=12,
                                                                                                row_spacing=2)
        row = 0
        for var, label, kind, limits in UTILIZATION:
            row = self.add_field(self.util_grid, row, var, label, kind, limits)
        row = 0
        for var, label, kind, limits in FIXED:
            row = self.add_field(self.fixed_grid, row, var, label, kind, limits)
        body.pack_start(self.util_grid, False, False, 0)
        body.pack_start(self.fixed_grid, False, False, 0)
        self.estimate_label = hint_label("")
        body.pack_start(self.estimate_label, False, False, 0)

        body.pack_start(section("Macros"), False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        row = 0
        for var, label, kind, limits in MACROS:
            row = self.add_field(grid, row, var, label, kind, limits)
        body.pack_start(grid, False, False, 0)
        self.macro_note = hint_label("")
        body.pack_start(self.macro_note, False, False, 0)

        self.area = Gtk.DrawingArea(height_request=300, width_request=300, vexpand=True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.POINTER_MOTION_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-release-event", self.on_release)
        frame = Gtk.Frame()
        frame.add(self.area)

        # instance, master, size, x, y, orientation, placed by, editable
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, bool)
        self.tv = Gtk.TreeView(model=self.store)
        self.tv.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.tv.append_column(Gtk.TreeViewColumn("Macro", Gtk.CellRendererText(), text=NAME))
        self.tv.append_column(Gtk.TreeViewColumn("Cell", Gtk.CellRendererText(), text=MASTER))
        self.tv.append_column(Gtk.TreeViewColumn("Size", Gtk.CellRendererText(), text=SIZE))
        for col, title in ((X, "X"), (Y, "Y")):
            cell = Gtk.CellRendererText(xalign=1)
            cell.connect("edited", self.on_xy, col)
            self.tv.append_column(Gtk.TreeViewColumn(title, cell, text=col, editable=EDITABLE))
        orients = Gtk.ListStore(str)
        for o in fp.ORIENTS:
            orients.append([o])
        cell = Gtk.CellRendererCombo(model=orients, text_column=0, has_entry=False)
        cell.connect("changed", lambda _c, path, it: self.on_orient(path, orients[it][0]))
        self.tv.append_column(Gtk.TreeViewColumn("Orientation", cell, text=ORIENT, editable=EDITABLE))
        self.tv.append_column(Gtk.TreeViewColumn("Placed by", Gtk.CellRendererText(foreground=MUTED), text=BY))
        self.tv.get_selection().connect("changed", lambda *_: self.area.queue_draw())
        # The preview and the macro table sit beside the fields (see wrap_body), always in view.
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=6)
        side.pack_start(frame, True, True, 0)
        side.pack_start(hint_label(
            "Drag a macro placed on this page to move it. Grey dashed outlines are the last floorplan run; "
            "colored bars on the die edges are the pin regions set on the IO pins page."), False, False, 0)
        sw = Gtk.ScrolledWindow(height_request=150)
        sw.add(self.tv)
        side.pack_start(sw, False, False, 0)
        btns = Gtk.Box(spacing=6)
        for label, handler, tip in (
                ("Place here", self.on_place, "Fix the selected macros where they are drawn (their last-run "
                                              "position, or the core's lower-left corner)"),
                ("Let the flow place", self.on_unplace, "Remove the selected macros' positions, so the flow's "
                                                        "macro placer places them")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            btns.pack_start(b, False, False, 0)
        side.pack_start(btns, False, False, 0)
        self.side = side
        self.problem_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.pack_start(self.problem_label, False, False, 0)

    def wrap_body(self, body):
        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=480)
        paned.pack1(body, True, False)
        paned.pack2(self.side, True, False)
        return paned

    # ---------- loading ----------
    def mode(self):
        die, core = (self.value(v).strip() for v in ("DIE_AREA", "CORE_AREA"))
        return "fixed" if die and core else "util"

    def load(self):
        super().load()
        self._filling = True
        mode = self.mode()
        self.mode_buttons[mode].set_active(True)
        self._filling = False
        self.util_grid.set_visible(mode == "util")
        self.fixed_grid.set_visible(mode == "fixed")
        self.load_macros()
        self.update_estimate()

    def forget(self):
        self.macro_dirty = False

    def macro_file(self):
        value = self.tab.effective.get("MACRO_PLACEMENT_TCL", "")
        return self.tab.resolve(value) if value else None

    def load_macros(self):
        eff = self.tab.effective
        lefs = [self.tab.resolve(p) for p in eff.get("ADDITIONAL_LEFS", "").split()]
        self.sizes = lef.macro_sizes(lefs)
        if not self.macro_dirty:
            path = self.macro_file()
            try:
                self.macro_text = path.read_text(errors="replace") if path and path.exists() else ""
                self.macro_mtime = path.stat().st_mtime if path and path.exists() else None
            except OSError:
                self.macro_text, self.macro_mtime = "", None
            self.placements, self.fixed_by_hand, self.other_lines = fp.parse(self.macro_text)
        # Macro instances from the synthesized netlist (a macro is an instance of a cell from ADDITIONAL_LEFS).
        netlist = self.tab.synth_file(eff, "1_synth.v", "1_2_yosys.v")
        self.macro_master = {}
        if netlist and self.sizes:
            from inputs_tab import netlist_names
            _, insts, _ = netlist_names(netlist, eff.get("DESIGN_NAME", ""), list(self.sizes))
            masters = self.netlist_masters(netlist, insts)
            self.macro_master.update(masters)
        self.start_last_run_read()
        self.fill_table()

    def netlist_masters(self, netlist, insts):
        """{instance: master} for the macro instances (netlist_names only returns their names)."""
        wanted, out = set(insts), {}
        try:
            with open(netlist, errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2 and parts[0] in self.sizes and parts[1].lstrip("\\") in wanted:
                        out[parts[1].lstrip("\\")] = parts[0]
        except OSError:
            pass
        return out

    def start_last_run_read(self):
        """Read the last floorplan result in the background (OpenROAD takes a moment to start)."""
        res = self.tab.effective.get("RESULTS_DIR")
        odb = None
        if res:
            for name in ("2_2_floorplan_macro.odb", "2_floorplan.odb", "2_1_floorplan.odb"):
                p = self.tab.resolve(res) / name
                if p.exists():
                    odb = p
                    break
        self.last_run = None
        if not odb:
            return
        openroad = self.tab.app.make_env().get("OPENROAD_EXE", "openroad")
        cfg = self.tab.cfg

        def work():
            result = read_last_run(openroad, str(odb))
            GLib.idle_add(self.on_last_run, cfg, odb, result)
        threading.Thread(target=work, daemon=True).start()

    def on_last_run(self, cfg, odb, result):
        if cfg != self.tab.cfg or not result:
            return False
        self.last_run = result
        self.last_run_file = Path(odb).name
        for name, (master, *_rest) in result[2].items():
            self.macro_master.setdefault(name, master)
        self.fill_table()
        self.update_estimate()
        return False

    # ---------- table and checks ----------
    def fill_table(self):
        names = sorted(set(self.macro_master) | set(self.placements) | set(self.fixed_by_hand))
        sel = self.selected()
        self.store.clear()
        for name in names:
            master = self.macro_master.get(name, "")
            size = self.sizes.get(master)
            if name in self.placements:
                x, y, o = self.placements[name]
                by, editable = "this page", True
            elif name in self.fixed_by_hand:
                x, y, o = self.fixed_by_hand[name]
                by, editable = "hand-written line in the file", False
            else:
                last = (self.last_run or (None, None, {}))[2].get(name)
                x, y, o = (last[1], last[2], last[3]) if last else ("", "", "")
                by, editable = "the flow" + (" (last run shown)" if last else ""), False
            self.store.append([name, master, f"{fp.fmt(size[0])} x {fp.fmt(size[1])}" if size else "?",
                               fp.fmt(x) if x != "" else "", fp.fmt(y) if y != "" else "", o, by, editable])
        for row in self.store:
            if row[NAME] in sel:
                self.tv.get_selection().select_path(row.path)
        n = len(names)
        path = self.macro_file()
        netlist = self.tab.synth_file(self.tab.effective, "1_synth.v", "1_2_yosys.v")
        self.macro_note.set_markup(markup(
            ("This design has no macros: ADDITIONAL_LEFS names no macro LEF." if not n and not self.sizes
             else "Run synthesis to list the macros." if not n and not netlist
             else "No macros in the netlist (none of its cells come from ADDITIONAL_LEFS)." if not n else
             f"{n} macros. Positions set here are saved in "
             f"{self.tab.short(path) if path else 'a new macro_placement.tcl in the design folder'} "
             "(MACRO_PLACEMENT_TCL); the flow's macro placer places the rest."), MUTED))
        self.show_problems()
        self.area.queue_draw()

    def site(self):
        """(width, height) of the placement site, from the tech or cell LEF; None if not found."""
        eff = self.tab.effective
        lefs = [self.tab.resolve(p) for p in (eff.get("TECH_LEF", "") + " " + eff.get("SC_LEF", "")).split()]
        return lef.site_size(lefs, eff.get("PLACE_SITE", "")) if eff.get("PLACE_SITE") else None

    def core(self):
        """The core box to draw and check against: the fixed one as the flow rounds it, else the estimate, else
        the last run's."""
        if self.mode() == "fixed":
            return fp.snap_core(fp.parse_box(self.value("CORE_AREA")), self.site())
        est = self.estimate()
        if self.last_run and self.last_run[1] and not est:
            return self.last_run[1]
        return est[1] if est else None

    def die(self):
        if self.mode() == "fixed":
            return fp.parse_box(self.value("DIE_AREA"))
        est = self.estimate()
        return est[0] if est else (self.last_run[0] if self.last_run else None)

    def estimate(self):
        """(die, core) estimated from the synthesis area and the utilization settings, or None."""
        try:
            util = float(self.value("CORE_UTILIZATION"))
            aspect = float(self.value("CORE_ASPECT_RATIO") or 1)
            margin = float((self.value("CORE_MARGIN") or "1").split()[0])
        except ValueError:
            return None
        area = self.synth_area()
        return fp.estimate(area, util, aspect, margin) if area and util > 0 and aspect > 0 else None

    def synth_area(self):
        rep = self.tab.effective.get("REPORTS_DIR")
        if not rep:
            return None
        try:
            return fp.synth_area((self.tab.resolve(rep) / "synth_stat.txt").read_text(errors="replace"))
        except OSError:
            return None

    def update_estimate(self):
        if self.mode() == "fixed":
            die, core = self.die(), self.core()
            given = fp.parse_box(self.value("CORE_AREA"))
            text = "Give the die and core as four numbers each: lower-left x and y, then upper-right x and y."
            if die and core:
                text = "The die is used as given."
                text += (" The core too: it falls on whole sites and rows." if core == given else
                         f" The flow rounds the core inward to whole sites and rows, so it will be from "
                         f"({fp.fmt(core[0])}, {fp.fmt(core[1])}) to ({fp.fmt(core[2])}, {fp.fmt(core[3])}).")
            if die and core and not (die[0] <= core[0] and die[1] <= core[1] and core[2] <= die[2]
                                     and core[3] <= die[3]):
                text = "The core must be inside the die."
        else:
            area, est = self.synth_area(), self.estimate()
            if est:
                d = est[0]
                text = (f"Synthesis area {area:.0f} µm². The flow will make the die about {d[2]:.1f} x {d[3]:.1f} "
                        "µm (estimate; rows are rounded to the site). Macro positions need the exact core, so "
                        "place macros with a fixed die and core.")
            else:
                text = "Run synthesis to see the size this makes."
        if self.last_run:
            d, c = self.last_run[0], self.last_run[1]
            text += (f"\nLast floorplan run ({self.last_run_file}): die {fp.fmt(d[2] - d[0])} x {fp.fmt(d[3] - d[1])}"
                     f" µm, core from ({fp.fmt(c[0])}, {fp.fmt(c[1])}) to ({fp.fmt(c[2])}, {fp.fmt(c[3])}).")
        self.estimate_label.set_markup(markup(text, MUTED))
        self.area.queue_draw()

    def channel(self):
        words = (self.value("RTLMP_MIN_CHANNEL_SIZE") or "").split()
        try:
            vals = [float(w) for w in words]
        except ValueError:
            return (0.0, 0.0)
        return (vals[0], vals[-1]) if vals else (0.0, 0.0)

    def show_problems(self):
        probs = fp.problems(self.placements, self.sizes, self.macro_master, self.core(), self.channel())
        if self.placements and self.mode() != "fixed":
            probs.insert(0, "Macros are placed here but the floorplan is sized from utilization: the core may end "
                            "up a different size than drawn. Use a fixed die and core.")
        self.problem_label.set_markup(markup("\n".join(probs), ERROR) if probs else "")

    # ---------- edits ----------
    def on_mode(self, button, key):
        if self._filling or not button.get_active():
            return
        # ORFS stops when both methods are set, so the other method's variables are emptied.
        if key == "fixed":
            est = self.estimate() or (self.last_run[:2] if self.last_run else None)
            die, core = est if est else ((0, 0, 100, 100), (2, 2, 98, 98))
            self.set_pending("DIE_AREA", " ".join(fp.fmt(v) for v in die))
            self.set_pending("CORE_AREA", " ".join(fp.fmt(v) for v in core))
            self.clear("CORE_UTILIZATION")
        else:
            self.set_pending("CORE_UTILIZATION", self.tab.effective.get("CORE_UTILIZATION") or "50")
            self.clear("DIE_AREA")
            self.clear("CORE_AREA")
        self.load()
        self.tab.settings_changed(source=self)

    def clear(self, var):
        """Make var empty for the flow: drop this config's line when nothing else sets it, else set it empty."""
        elsewhere = any(v == var and p != self.tab.cfg_path() for v, p, _ in self.tab.chain) \
            or var in self.tab.platform_vars()
        if var in self.here() and not elsewhere:
            self.tab.pending[var] = None
        elif self.tab.effective.get(var):
            self.tab.pending[var] = ""

    def set_pending(self, var, value):
        here = self.here()
        saved = here[var] if var in here else self.tab.effective.get(var, "")
        if value == saved:
            self.tab.pending.pop(var, None)
        else:
            self.tab.pending[var] = value

    def on_change(self, var, value):
        super().on_change(var, value)
        if not self._filling:
            self.update_estimate()
            self.show_problems()

    def selected(self):
        model, paths = self.tv.get_selection().get_selected_rows()
        return {model[p][NAME] for p in paths}

    def macro_changed(self):
        self.macro_dirty = True
        self.fill_table()
        self.tab.dirty()

    def on_xy(self, _cell, path, text, col):
        name = self.store[path][NAME]
        try:
            v = float(text)
        except ValueError:
            self.tab.app.message(f"'{text}' is not a number.")
            return
        x, y, o = self.placements[name]
        self.placements[name] = (v, y, o) if col == X else (x, v, o)
        self.macro_changed()

    def on_orient(self, path, orient):
        name = self.store[path][NAME]
        x, y, _ = self.placements[name]
        self.placements[name] = (x, y, orient)
        self.macro_changed()

    def on_place(self, *_):
        core = self.core() or (0, 0, 0, 0)
        last = (self.last_run or (None, None, {}))[2]
        for name in self.selected():
            if name in self.placements or name in self.fixed_by_hand:
                continue
            got = last.get(name)
            self.placements[name] = (got[1], got[2], got[3]) if got else (core[0], core[1], "R0")
        self.macro_changed()

    def on_unplace(self, *_):
        for name in self.selected():
            self.placements.pop(name, None)
        self.macro_changed()

    # ---------- preview ----------
    def view(self, width, height):
        """(scale, x offset, y offset) that fit the die (or core, or last run) in the drawing, y pointing up."""
        box = self.die() or self.core() or (0, 0, 100, 100)
        pad = 16
        bw, bh = max(box[2] - box[0], 1e-6), max(box[3] - box[1], 1e-6)
        s = min((width - 2 * pad) / bw, (height - 2 * pad) / bh)
        ox = pad + (width - 2 * pad - bw * s) / 2 - box[0] * s
        oy = height - pad - (height - 2 * pad - bh * s) / 2 + box[1] * s
        return s, ox, oy

    def to_screen(self, x, y):
        s, ox, oy = self._view
        return ox + x * s, oy - y * s

    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        self._view = self.view(w, h)
        s = self._view[0]
        cr.set_source_rgb(0.98, 0.98, 0.97)
        cr.paint()

        def rect(box, fill=None, stroke=None, dash=False, width=1.0):
            x1, y1 = self.to_screen(box[0], box[3])
            x2, y2 = self.to_screen(box[2], box[1])
            cr.rectangle(x1, y1, x2 - x1, y2 - y1)
            if fill:
                cr.set_source_rgba(*fill)
                cr.fill_preserve()
            cr.set_source_rgba(*(stroke or (0, 0, 0, 0)))
            cr.set_line_width(width)
            cr.set_dash([4, 3] if dash else [])
            cr.stroke()

        if self.last_run:
            rect(self.last_run[0], stroke=(0.6, 0.6, 0.6, 1), dash=True)
            rect(self.last_run[1], stroke=(0.7, 0.7, 0.7, 1), dash=True)
        die, core = self.die(), self.core()
        if die:
            rect(die, fill=(0.93, 0.94, 0.96, 1), stroke=(0.2, 0.2, 0.25, 1), width=1.5)
        if core:
            rect(core, fill=(1, 1, 1, 1), stroke=(0.35, 0.55, 0.85, 1))
        if die:
            self.draw_pins(cr, die)
        sel = self.selected()
        last = (self.last_run or (None, None, {}))[2]
        for name, master in sorted(self.macro_master.items()):
            size = self.sizes.get(master)
            if not size:
                continue
            if name in self.placements or name in self.fixed_by_hand:
                x, y, o = self.placements.get(name) or self.fixed_by_hand[name]
                mw, mh = fp.macro_size(size, o)
                box = (x, y, x + mw, y + mh)
                if name in self.placements:
                    cx, cy = self.channel()
                    if cx or cy:
                        rect((box[0] - cx, box[1] - cy, box[2] + cx, box[3] + cy), stroke=(0.9, 0.55, 0.1, 0.8),
                             dash=True)
                    color = (0.95, 0.6, 0.2, 0.85) if name in sel else (0.3, 0.5, 0.9, 0.75)
                else:
                    color = (0.55, 0.55, 0.6, 0.75)
                rect(box, fill=color, stroke=(0.1, 0.1, 0.2, 1))
                self.label(cr, name, box, s)
            elif name in last:
                _, x, y, o, mw, mh = last[name]
                mw, mh = fp.macro_size((mw, mh), o)
                rect((x, y, x + mw, y + mh), fill=(0.8, 0.8, 0.8, 0.35), stroke=(0.6, 0.6, 0.6, 1), dash=True)

    def label(self, cr, text, box, s):
        x1, y1 = self.to_screen(box[0], box[3])
        width = (box[2] - box[0]) * s
        if width < 30:
            return
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(10)
        shown = text if len(text) * 6 < width else text[: max(int(width / 6) - 1, 1)] + "…"
        cr.move_to(x1 + 3, y1 + 12)
        cr.show_text(shown)

    def draw_pins(self, cr, die):
        """Pin regions from the IO pins page as colored bars just outside the die edge."""
        colors = {"left": (0.85, 0.3, 0.3), "right": (0.3, 0.65, 0.3), "top": (0.55, 0.35, 0.8),
                  "bottom": (0.2, 0.6, 0.75)}
        for row in getattr(self.tab, "io_rows", []):
            edge, interval = row.get("edge"), row.get("interval") or "*"
            if edge not in colors:
                continue
            lo, hi = (die[1], die[3]) if edge in ("left", "right") else (die[0], die[2])
            if interval != "*":
                try:
                    a, b = (float(v) for v in interval.split("-"))
                    lo, hi = (die[1] + a, die[1] + b) if edge in ("left", "right") else (die[0] + a, die[0] + b)
                except ValueError:
                    pass
            cr.set_source_rgb(*colors[edge])
            cr.set_line_width(4)
            cr.set_dash([])
            if edge in ("left", "right"):
                x = die[0] if edge == "left" else die[2]
                sx, sy1 = self.to_screen(x, lo)
                _, sy2 = self.to_screen(x, hi)
                sx += -4 if edge == "left" else 4
                cr.move_to(sx, sy1)
                cr.line_to(sx, sy2)
            else:
                y = die[1] if edge == "bottom" else die[3]
                sx1, sy = self.to_screen(lo, y)
                sx2, _ = self.to_screen(hi, y)
                sy += 4 if edge == "bottom" else -4
                cr.move_to(sx1, sy)
                cr.line_to(sx2, sy)
            cr.stroke()

    # ---------- dragging ----------
    def macro_at(self, px, py):
        if not hasattr(self, "_view"):
            return None
        s, ox, oy = self._view
        x, y = (px - ox) / s, (oy - py) / s
        for name, (mx, my, o) in self.placements.items():
            size = self.sizes.get(self.macro_master.get(name, ""))
            if size:
                w, h = fp.macro_size(size, o)
                if mx <= x <= mx + w and my <= y <= my + h:
                    return name, x - mx, y - my
        return None

    def on_press(self, _w, event):
        hit = self.macro_at(event.x, event.y)
        if hit and not self.tab.busy:
            self.drag = hit
            sel = self.tv.get_selection()
            sel.unselect_all()
            for row in self.store:
                if row[NAME] == hit[0]:
                    sel.select_path(row.path)
        return True

    def on_motion(self, _w, event):
        if not self.drag:
            return False
        name, dx, dy = self.drag
        s, ox, oy = self._view
        x, y = (event.x - ox) / s - dx, (oy - event.y) / s - dy
        _, _, o = self.placements[name]
        # Rounded to 0.1 micron so the file stays readable; the flow snaps macros to its own grid.
        self.placements[name] = (round(x, 1), round(y, 1), o)
        self.area.queue_draw()
        return True

    def on_release(self, *_):
        if self.drag:
            self.drag = None
            self.macro_changed()
        return True

    # ---------- state ----------
    def is_dirty(self):
        return super().is_dirty() or self.macro_dirty

    def save(self):
        for var in ("DIE_AREA", "CORE_AREA"):
            value = self.value(var)
            if self.mode() == "fixed" and not fp.parse_box(value):
                self.tab.app.message(f"{var} must be four numbers: x1 y1 x2 y2, with x2 > x1 and y2 > y1.")
                return
        if not self.macro_dirty:
            self.tab.on_settings_save()
            return
        probs = fp.problems(self.placements, self.sizes, self.macro_master, self.core(), self.channel())
        if probs and not self.tab.app.confirm("The macro placement has problems", "\n".join(probs)
                                              + "\n\nSave anyway?"):
            return
        header = "# Macro placement, read by the floorplan stage (MACRO_PLACEMENT_TCL).\n"
        text = fp.set_block(self.macro_text, self.placements, header=header)
        path, var_value = self.tab.own_file("MACRO_PLACEMENT_TCL", "macro_placement.tcl")
        mtime = self.macro_mtime if path == self.macro_file() else None
        if self.tab.save_with_settings([(path, text, mtime)],
                                       {"MACRO_PLACEMENT_TCL": var_value} if var_value else {}):
            self.macro_dirty = False

    def revert(self):
        self.macro_dirty = False
        super().revert()
