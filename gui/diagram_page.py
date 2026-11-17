"""Diagrams section: a diagram editor in the style of draw.io, saved with the project.

Left: the shape palette (general, flowchart, digital design, logic gates); click a shape to add it, or drag it
onto the canvas. Middle: the canvas. Right: the format panel for what is selected (colors, line, text, arrow
style, arrange and align).

On the canvas: click selects, Shift+click adds to the selection, dragging empty space draws a selection box.
Drag a selection to move it (it snaps to the grid and to the edges and centers of other shapes). A selected shape
has eight resize handles. Hovering a shape shows four blue connection points: drag from one to another shape to
draw an arrow. A selected arrow shows its waypoints: drag one, or drag the middle of a segment to add one.
Double-click (or F2) edits a label in place. Middle-drag or Space+drag pans, the wheel scrolls, Ctrl+wheel zooms.
Keys: Delete, Ctrl+Z / Ctrl+Y, Ctrl+A, Ctrl+C / X / V, Ctrl+D duplicate, Ctrl+G / Ctrl+Shift+G group and ungroup,
arrows nudge, Ctrl+S saves. From RTL draws the block diagram of a module. Export writes PNG, SVG, PDF or a
draw.io file.
"""

import math
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk  # noqa: E402

import dialogs  # noqa: E402
import diagram as dg  # noqa: E402
import hdl  # noqa: E402
import theme  # noqa: E402
from input_page import MUTED, markup  # noqa: E402

SUFFIX = ".diagram.json"
HANDLES = {"nw": (0, 0), "n": (0.5, 0), "ne": (1, 0), "e": (1, 0.5), "se": (1, 1), "s": (0.5, 1), "sw": (0, 1),
           "w": (0, 0.5)}
HIT = 7            # pixels within which a handle or connection point is grabbed
ACCENT = (0.24, 0.35, 1)
ALIGN = [("left", "⇤", "Align left edges"), ("center", "↔", "Align centers"), ("right", "⇥", "Align right edges"),
         ("top", "⤒", "Align top edges"), ("middle", "↕", "Align middles"), ("bottom", "⤓", "Align bottom edges"),
         ("hspace", "⋯", "Space evenly across"), ("vspace", "⋮", "Space evenly down"),
         ("width", "W", "Same width as the first"), ("height", "H", "Same height as the first")]


def rgba(hex_color):
    c = Gdk.RGBA()
    c.parse(hex_color)
    return c


def hex_of(color):
    return "#%02x%02x%02x" % (round(color.red * 255), round(color.green * 255), round(color.blue * 255))


class DiagramPage:
    name, title = "diagram", "Diagrams"

    def __init__(self, app):
        self.app = app
        self.d = dg.Diagram()
        self.path = None
        self.dirty = False
        self.selected = set()
        self.drag = None          # what a mouse drag is doing: {"kind": move | resize | connect | box | pan | point}
        self.hover = None         # id of the shape under the pointer
        self.pointer = (0, 0)     # in diagram coordinates
        self.guides = []          # alignment guide lines shown while moving: ("v", x) or ("h", y)
        self.zoom, self.ox, self.oy = 1.0, 40.0, 40.0
        self.grid, self.snap = True, True
        self.space = False        # Space is held: drag pans
        self.clipboard = None
        self.editing = None       # id of the item whose label is being edited in place
        self.undo_stack, self.redo_stack = [], []
        self._filling = False
        self.widget = self.build()

    # ---------- layout ----------
    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("inputs-head")
        self.file_cb = Gtk.ComboBoxText(tooltip_text="The project's diagrams")
        self.file_cb.connect("changed", self.on_pick_file)
        bar.pack_start(self.file_cb, False, False, 0)

        def button(label, handler, tip=None, box=bar):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            box.pack_start(b, False, False, 0)
            return b
        button("New…", self.on_new, "Start a new diagram")
        button("From RTL…", self.on_from_rtl, "Draw the block diagram of a module of the project")
        save = button("Save", lambda *_: self.save(), "Save the diagram in the project's docs folder (Ctrl+S)")
        save.get_style_context().add_class("suggested-action")
        button("Export…", self.on_export, "Write a PNG, SVG, PDF or draw.io file")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        button("Undo", lambda *_: self.undo(), "Undo (Ctrl+Z)")
        button("Redo", lambda *_: self.redo(), "Redo (Ctrl+Y)")
        button("Delete", lambda *_: self.delete_selected(), "Delete the selection (Delete)")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        button("−", lambda *_: self.set_zoom(self.zoom / 1.25), "Zoom out")
        self.zoom_label = Gtk.Label(label="100%", width_chars=5)
        bar.pack_start(self.zoom_label, False, False, 0)
        button("+", lambda *_: self.set_zoom(self.zoom * 1.25), "Zoom in")
        button("Fit", lambda *_: self.fit(), "Show the whole diagram")
        self.state = Gtk.Label(xalign=1, hexpand=True)
        bar.pack_end(self.state, True, True, 0)
        outer.pack_start(bar, False, False, 0)

        body = Gtk.Box()
        body.pack_start(self.build_palette(), False, False, 0)
        self.area = Gtk.DrawingArea(can_focus=True, hexpand=True, vexpand=True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                             Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK |
                             Gdk.EventMask.KEY_RELEASE_MASK | Gdk.EventMask.SCROLL_MASK |
                             Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("key-press-event", self.on_key)
        self.area.connect("key-release-event", self.on_key_release)
        self.area.connect("scroll-event", self.on_scroll)
        self.area.drag_dest_set(Gtk.DestDefaults.ALL, [Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP,
                                                                           0)], Gdk.DragAction.COPY)
        self.area.connect("drag-data-received", self.on_drop)
        # The label editor floats over the canvas, on the item being edited.
        self.overlay = Gtk.Overlay()
        self.overlay.add(self.area)
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, justification=Gtk.Justification.CENTER,
                                  left_margin=4, right_margin=4, top_margin=3, bottom_margin=3, no_show_all=True)
        self.entry.get_style_context().add_class("diagram-edit")
        self.entry.connect("key-press-event", self.on_entry_key)
        self.entry.connect("focus-out-event", lambda *_: self.commit_edit() or False)
        self.overlay.add_overlay(self.entry)
        self.overlay.connect("get-child-position", self.on_entry_position)
        body.pack_start(self.overlay, True, True, 0)
        body.pack_start(self.build_format(), False, False, 0)
        outer.pack_start(body, True, True, 0)
        return outer

    def build_palette(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=8)
        hint = Gtk.Label(xalign=0, wrap=True, max_width_chars=22)
        hint.set_markup(markup("Click a shape to add it, or drag it onto the canvas.", MUTED))
        box.pack_start(hint, False, False, 4)
        for category, items in dg.PALETTE:
            exp = Gtk.Expander(label=category, expanded=True)
            exp.get_style_context().add_class("palette-title")
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4,
                               min_children_per_line=4, row_spacing=2, column_spacing=2)
            for kind, name in items:
                icon = Gtk.DrawingArea(width_request=34, height_request=28)
                icon.connect("draw", self.draw_icon, kind)
                b = Gtk.Button(tooltip_text=name, relief=Gtk.ReliefStyle.NONE)
                b.add(icon)
                b.get_style_context().add_class("palette-shape")
                b.connect("clicked", lambda _b, k=kind: self.add_shape(k))
                b.drag_source_set(Gdk.ModifierType.BUTTON1_MASK,
                                  [Gtk.TargetEntry.new("text/plain", Gtk.TargetFlags.SAME_APP, 0)],
                                  Gdk.DragAction.COPY)
                b.connect("drag-data-get", lambda _w, _c, data, _i, _t, k=kind: data.set_text(k, -1))
                flow.add(b)
            exp.add(flow)
            box.pack_start(exp, False, False, 0)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=196)
        sw.get_style_context().add_class("side-panel")
        sw.add(box)
        return sw

    @staticmethod
    def draw_icon(widget, cr, kind):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        bw, bh = dg.SIZES.get(kind, (150, 70))
        scale = min((w - 6) / bw, (h - 6) / bh)
        cr.translate((w - bw * scale) / 2, (h - bh * scale) / 2)
        cr.scale(scale, scale)
        shape = {"kind": kind, "x": 0, "y": 0, "w": bw, "h": bh, "label": "T" if kind == "text" else "",
                 "color": "blue", "line": 1.4 / scale, "font": 40 if kind == "text" else 12}
        dg.draw_shape(cr, shape)

    def build_format(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)

        def title(text):
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("card-title")
            box.pack_start(label, False, False, 2)

        def row(text, widget):
            r = Gtk.Box(spacing=8)
            r.pack_start(Gtk.Label(label=text, xalign=0, width_chars=9), False, False, 0)
            r.pack_start(widget, True, True, 0)
            box.pack_start(r, False, False, 0)
            return widget

        def combo(values, handler):
            cb = Gtk.ComboBoxText()
            for v in values:
                cb.append(v, v)
            cb.connect("changed", lambda w: handler(w.get_active_id()))
            return cb

        self.sel_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=26)
        self.sel_label.get_style_context().add_class("muted")
        box.pack_start(self.sel_label, False, False, 0)

        title("Shape")
        swatches = Gtk.Box(spacing=3)
        for name, (fill, stroke) in dg.COLORS.items():
            b = Gtk.Button(tooltip_text=name.capitalize())
            b.get_style_context().add_class("swatch")
            dot = Gtk.DrawingArea(width_request=16, height_request=16)
            dot.connect("draw", self.draw_swatch, fill, stroke)
            b.add(dot)
            b.connect("clicked", lambda _b, f=fill, s=stroke: self.set_style(fill=f, stroke=s))
            swatches.pack_start(b, False, False, 0)
        box.pack_start(swatches, False, False, 0)
        self.fill_btn = row("Fill", Gtk.ColorButton())
        self.fill_btn.connect("color-set", lambda b: self.set_style(fill=hex_of(b.get_rgba())))
        self.stroke_btn = row("Line", Gtk.ColorButton())
        self.stroke_btn.connect("color-set", lambda b: self.set_style(stroke=hex_of(b.get_rgba())))
        self.dashed = Gtk.CheckButton(label="Dashed line")
        self.dashed.connect("toggled", lambda b: self.set_style(dashed=b.get_active()))
        box.pack_start(self.dashed, False, False, 0)

        title("Text")
        self.font = row("Size", Gtk.SpinButton.new_with_range(6, 72, 1))
        self.font.connect("value-changed", lambda b: self.set_style(font=b.get_value()))
        self.text_btn = row("Color", Gtk.ColorButton())
        self.text_btn.connect("color-set", lambda b: self.set_style(text_color=hex_of(b.get_rgba())))
        self.align_cb = row("Align", combo(["left", "center", "right"], lambda v: self.set_style(align=v)))
        self.bold = Gtk.CheckButton(label="Bold")
        self.bold.connect("toggled", lambda b: self.set_style(bold=b.get_active()))
        box.pack_start(self.bold, False, False, 0)

        title("Arrow")
        self.style_cb = row("Route", combo(dg.EDGE_STYLES, lambda v: self.set_edge(style=v)))
        self.tail_cb = row("Start", combo(dg.HEADS, lambda v: self.set_edge(tail=v)))
        self.head_cb = row("End", combo(dg.HEADS, lambda v: self.set_edge(head=v)))
        self.bus = row("Bus width", Gtk.SpinButton.new_with_range(0, 4096, 1))
        self.bus.set_tooltip_text("Bits of the bus the arrow stands for; 0 for a single wire")
        self.bus.connect("value-changed", lambda b: self.set_edge(bus=int(b.get_value())))
        self.edge_dashed = Gtk.CheckButton(label="Dashed")
        self.edge_dashed.connect("toggled", lambda b: self.set_edge(dashed=b.get_active()))
        box.pack_start(self.edge_dashed, False, False, 0)
        r = Gtk.Box(spacing=4)
        for label, handler, tip in (("Straighten", self.clear_waypoints, "Remove the arrow's waypoints"),
                                    ("Reverse", self.reverse_edges, "Swap the arrow's ends")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _b, h=handler: h())
            r.pack_start(b, True, True, 0)
        box.pack_start(r, False, False, 0)
        self.edge_widgets = [self.style_cb, self.tail_cb, self.head_cb, self.bus, self.edge_dashed, r]

        title("Arrange")
        grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=5, min_children_per_line=5)
        for how, glyph, tip in ALIGN:
            b = Gtk.Button(label=glyph, tooltip_text=tip)
            b.connect("clicked", lambda _b, h=how: self.align(h))
            grid.add(b)
        box.pack_start(grid, False, False, 0)
        r = Gtk.Box(spacing=4)
        for label, handler, tip in (("Front", lambda: self.reorder(True), "Bring to the front"),
                                    ("Back", lambda: self.reorder(False), "Send to the back"),
                                    ("Group", self.group, "Group the selection (Ctrl+G)"),
                                    ("Ungroup", self.ungroup, "Ungroup (Ctrl+Shift+G)")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", lambda _b, h=handler: h())
            r.pack_start(b, True, True, 0)
        box.pack_start(r, False, False, 0)

        title("Diagram")
        self.title_entry = row("Title", Gtk.Entry())
        self.title_entry.connect("changed", self.on_title)
        self.grid_check = Gtk.CheckButton(label="Show grid", active=True)
        self.grid_check.connect("toggled", lambda b: (setattr(self, "grid", b.get_active()), self.area.queue_draw()))
        self.snap_check = Gtk.CheckButton(label="Snap to grid and shapes", active=True)
        self.snap_check.connect("toggled", lambda b: setattr(self, "snap", b.get_active()))
        box.pack_start(self.grid_check, False, False, 0)
        box.pack_start(self.snap_check, False, False, 0)
        self.shape_widgets = [swatches, self.fill_btn, self.stroke_btn, self.dashed, self.font, self.text_btn,
                              self.align_cb, self.bold]
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, width_request=250)
        sw.get_style_context().add_class("side-panel")
        sw.add(box)
        return sw

    @staticmethod
    def draw_swatch(_w, cr, fill, stroke):
        cr.rectangle(1, 1, 14, 14)
        cr.set_source_rgb(*dg._rgb(fill))
        cr.fill_preserve()
        cr.set_source_rgb(*dg._rgb(stroke))
        cr.set_line_width(1.5)
        cr.stroke()

    # ---------- files ----------
    def folder(self):
        p = self.app.project
        return p.dir / "docs" if p else None

    def flow_changed(self):
        pass

    def project_changed(self):
        self.load(None)
        self.fill_files()

    def project_updated(self):
        pass

    def fill_files(self, select=None):
        self.file_cb.remove_all()
        folder = self.folder()
        files = sorted(folder.glob(f"*{SUFFIX}")) if folder and folder.exists() else []
        for f in files:
            self.file_cb.append(str(f), f.name[:-len(SUFFIX)])
        if select:
            self.file_cb.set_active_id(str(select))
        elif files:
            self.file_cb.set_active(0)

    def on_pick_file(self, cb):
        path = cb.get_active_id()
        if path and Path(path) != self.path:
            if self.dirty and not self.app.confirm("Open another diagram without saving?",
                                                   "Changes to this one are lost."):
                cb.set_active_id(str(self.path))
                return
            self.load(Path(path))

    def load(self, path):
        self.path = path
        try:
            self.d = dg.Diagram.from_json(path.read_text()) if path else dg.Diagram()
        except (OSError, ValueError) as e:
            self.app.set_status(f"Could not read {path}: {e}")
            self.d = dg.Diagram()
        self.selected, self.undo_stack, self.redo_stack = set(), [], []
        self.set_dirty(False)
        self.resize_area()
        self.update_format()

    def set_dirty(self, dirty):
        self.dirty = dirty
        name = self.path.name[:-len(SUFFIX)] if self.path else "unsaved diagram"
        self.state.set_markup(markup(f"{name}{' (unsaved)' if dirty else ''}", MUTED))
        self.area.queue_draw()

    def is_dirty(self):
        return self.dirty

    def save(self, path=None):
        path = path or self.path
        if path is None:
            return self.on_new(save_current=True)
        self.commit_edit()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.d.to_json())
        self.path = path
        self.set_dirty(False)
        self.fill_files(select=path)
        self.app.set_status(f"Saved {path}")
        return True

    def ask_name(self, title, default=""):
        folder = self.folder()
        vals = dialogs.ask_fields(self.app, title, [("name", "Name", default, "text")], ok="Create",
                                  check=lambda v: None if v["name"] and "/" not in v["name"] and not (
                                      folder / f"{v['name']}{SUFFIX}").exists() else "Give a new, plain name.")
        return folder / f"{vals['name']}{SUFFIX}" if vals else None

    def on_new(self, *_, save_current=False):
        if not self.app.project:
            self.app.message("Make or open a project first: diagrams are saved in its docs folder.")
            return False
        path = self.ask_name("New diagram", "architecture")
        if not path:
            return False
        if not save_current:
            self.d = dg.Diagram()
            self.selected, self.undo_stack, self.redo_stack = set(), [], []
        return self.save(path)

    def on_from_rtl(self, *_):
        p = self.app.project
        if not p:
            return
        mods = p.modules()
        design = [m for m in mods.values() if not hdl.is_testbench(m)]
        if not design:
            self.app.message("The project has no modules yet.")
            return
        names = [p.top()] + sorted(m.name for m in design if m.name != p.top()) if p.top() in mods else \
            sorted(m.name for m in design)
        vals = dialogs.ask_fields(self.app, "Block diagram from RTL", [("module", "Module", names[0], names)],
                                  intro="Draws the module's ports, its submodule instances and the nets between "
                                        "them. Edit the result freely; it is saved as a new diagram.", ok="Draw")
        if vals:
            self.d = dg.from_rtl(mods, vals["module"])
            self.selected, self.undo_stack, self.redo_stack = set(), [], []
            folder = self.folder()
            path = folder / f"{vals['module']}_blocks{SUFFIX}"
            n = 2
            while path.exists():
                path = folder / f"{vals['module']}_blocks_{n}{SUFFIX}"
                n += 1
            self.save(path)
            self.resize_area()

    def on_export(self, *_):
        if not self.d.shapes:
            return
        dlg = Gtk.FileChooserDialog(title="Export diagram", transient_for=self.app,
                                    action=Gtk.FileChooserAction.SAVE, do_overwrite_confirmation=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Export", Gtk.ResponseType.OK)
        for name, pattern in (("PNG image", "*.png"), ("SVG drawing", "*.svg"), ("PDF", "*.pdf"),
                              ("draw.io diagram", "*.drawio")):
            f = Gtk.FileFilter()
            f.set_name(f"{name} ({pattern})")
            f.add_pattern(pattern)
            dlg.add_filter(f)
        if self.folder():
            self.folder().mkdir(parents=True, exist_ok=True)
            dlg.set_current_folder(str(self.folder()))
        dlg.set_current_name((self.path.name[:-len(SUFFIX)] if self.path else "diagram") + ".png")
        if dlg.run() == Gtk.ResponseType.OK:
            out = dlg.get_filename()
            if not out.lower().endswith((".png", ".svg", ".pdf", ".drawio")):
                out += ".png"
            self.commit_edit()
            dg.export(self.d, out)
            self.app.set_status(f"Exported {out}")
        dlg.destroy()

    # ---------- undo ----------
    def snapshot(self):
        self.undo_stack.append(self.d.to_json())
        del self.undo_stack[:-200]
        self.redo_stack.clear()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.d.to_json())
            self.d = dg.Diagram.from_json(self.undo_stack.pop())
            self.after_history()

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.d.to_json())
            self.d = dg.Diagram.from_json(self.redo_stack.pop())
            self.after_history()

    def after_history(self):
        self.selected = {i for i in self.selected if self.d.item(i)}
        self.set_dirty(True)
        self.update_format()

    def changed(self):
        self.set_dirty(True)
        self.update_format()

    # ---------- view ----------
    def to_diagram(self, x, y):
        return (x - self.ox) / self.zoom, (y - self.oy) / self.zoom

    def set_zoom(self, zoom, about=None):
        """Zoom, keeping the point `about` (in widget pixels; default the middle) where it is."""
        zoom = max(0.2, min(4.0, zoom))
        w, h = self.area.get_allocated_width(), self.area.get_allocated_height()
        ax, ay = about or (w / 2, h / 2)
        dx, dy = self.to_diagram(ax, ay)
        self.zoom = zoom
        self.ox, self.oy = ax - dx * zoom, ay - dy * zoom
        self.zoom_label.set_text(f"{zoom * 100:.0f}%")
        self.area.queue_draw()

    def fit(self):
        """Show the whole diagram, at no more than 100%."""
        w, h = max(self.area.get_allocated_width(), 200), max(self.area.get_allocated_height(), 200)
        x0, y0, x1, y1 = self.d.bounds()
        self.zoom = max(0.2, min(1.0, (w - 20) / max(x1 - x0, 1), (h - 20) / max(y1 - y0, 1)))
        self.ox = (w - (x1 - x0) * self.zoom) / 2 - x0 * self.zoom
        self.oy = (h - (y1 - y0) * self.zoom) / 2 - y0 * self.zoom
        self.zoom_label.set_text(f"{self.zoom * 100:.0f}%")
        self.area.queue_draw()

    def resize_area(self):
        """The diagram was replaced: show all of it."""
        self.fit()
        self._filling = True
        self.title_entry.set_text(self.d.title)
        self._filling = False

    def on_title(self, entry):
        if not self._filling and entry.get_text() != self.d.title:
            self.snapshot()
            self.d.title = entry.get_text()
            self.set_dirty(True)

    # ---------- selection and the format panel ----------
    def select(self, ids, add=False):
        ids = self.d.with_groups(set(ids))
        self.selected = (self.selected | ids) if add else ids
        self.update_format()
        self.area.queue_draw()

    def shapes_selected(self):
        return [s for s in self.d.shapes if s["id"] in self.selected]

    def edges_selected(self):
        return [e for e in self.d.edges if e["id"] in self.selected]

    def update_format(self):
        """Show the style of the selection in the format panel."""
        shapes, edges = self.shapes_selected(), self.edges_selected()
        self._filling = True
        for w in self.shape_widgets:
            w.set_sensitive(bool(shapes))
        for w in self.edge_widgets:
            w.set_sensitive(bool(edges))
        if shapes:
            s = shapes[0]
            fill, stroke = dg.colors_of(s)
            self.fill_btn.set_rgba(rgba(fill))
            self.stroke_btn.set_rgba(rgba(stroke))
            self.text_btn.set_rgba(rgba(s.get("text_color") or "#1c2230"))
            self.dashed.set_active(bool(s.get("dashed")))
            self.font.set_value(float(s.get("font", 12.5)))
            self.align_cb.set_active_id(s.get("align", "center"))
            self.bold.set_active(bool(s.get("bold")))
        if edges:
            e = edges[0]
            self.style_cb.set_active_id(e.get("style", "orthogonal"))
            self.head_cb.set_active_id(e.get("head", "arrow"))
            self.tail_cb.set_active_id(e.get("tail") or ("arrow" if e.get("both") else "none"))
            self.bus.set_value(int(e.get("bus", 0) or 0))
            self.edge_dashed.set_active(bool(e.get("dashed")))
        what = []
        if shapes:
            what.append(dg.NAMES.get(shapes[0]["kind"], shapes[0]["kind"]) if len(shapes) == 1
                        else f"{len(shapes)} shapes")
        if edges:
            what.append("1 arrow" if len(edges) == 1 else f"{len(edges)} arrows")
        self.sel_label.set_text("Selected: " + " and ".join(what) if what else
                                "Nothing selected: the settings below apply to what you select.")
        self._filling = False

    def set_style(self, **style):
        """Change the style of the selected shapes (fill, stroke, dashed, font, bold, align, text_color)."""
        shapes = self.shapes_selected()
        if self._filling or not shapes:
            return
        self.snapshot()
        for s in shapes:
            s.update(style)
        self.changed()

    def set_edge(self, **style):
        """Change the selected arrows (style, head, tail, bus, dashed, color, label)."""
        edges = self.edges_selected()
        if self._filling or not edges:
            return
        self.snapshot()
        for e in edges:
            e.update(style)
            if "tail" in style:
                e["both"] = style["tail"] == "arrow"
        self.changed()

    def clear_waypoints(self):
        if self.edges_selected():
            self.snapshot()
            for e in self.edges_selected():
                e.pop("points", None)
                e.pop("src_side", None)
                e.pop("dst_side", None)
            self.set_dirty(True)

    def reverse_edges(self):
        if self.edges_selected():
            self.snapshot()
            for e in self.edges_selected():
                e["src"], e["dst"] = e["dst"], e["src"]
                e["src_side"], e["dst_side"] = e.get("dst_side"), e.get("src_side")
                e["points"] = list(reversed(e.get("points", [])))
            self.set_dirty(True)

    # ---------- editing commands ----------
    def add_shape(self, kind, x=None, y=None):
        """Add a shape: centered on (x, y), or in the middle of the view (stepped aside if a shape is there)."""
        self.commit_edit()
        self.snapshot()
        w, h = dg.SIZES.get(kind, (150, 70))
        if x is None:
            x, y = self.to_diagram(self.area.get_allocated_width() / 2, self.area.get_allocated_height() / 2)
            while any(abs(s["x"] - dg.snap(x - w / 2)) < 10 and abs(s["y"] - dg.snap(y - h / 2)) < 10
                      for s in self.d.shapes):
                x, y = x + 20, y + 20
        shape = self.d.add_shape(kind, x - w / 2, y - h / 2)
        self.select([shape["id"]])
        self.changed()
        self.area.grab_focus()
        return shape

    def on_drop(self, _w, _context, x, y, data, _info, _time):
        kind = data.get_text()
        if kind in dg.KINDS:
            self.add_shape(kind, *self.to_diagram(x, y))

    def delete_selected(self):
        if self.selected:
            self.snapshot()
            self.d.remove(self.selected)
            self.selected = set()
            self.changed()

    def copy(self):
        if self.selected:
            self.clipboard = self.d.copy_items(self.selected)

    def cut(self):
        self.copy()
        self.delete_selected()

    def paste(self):
        if self.clipboard:
            self.snapshot()
            self.selected = set(self.d.paste(self.clipboard))
            # A second paste lands beside the first.
            self.clipboard = self.d.copy_items(self.selected)
            self.changed()

    def duplicate(self):
        if self.selected:
            self.snapshot()
            self.selected = set(self.d.duplicate(self.selected))
            self.changed()

    def select_all(self):
        self.select([s["id"] for s in self.d.shapes] + [e["id"] for e in self.d.edges])

    def align(self, how):
        if len(self.shapes_selected()) > 1:
            self.snapshot()
            self.d.align(self.selected, how)
            self.changed()

    def reorder(self, front):
        if self.shapes_selected():
            self.snapshot()
            self.d.reorder(self.selected, front)
            self.changed()

    def group(self):
        if len(self.shapes_selected()) > 1:
            self.snapshot()
            self.d.group(self.selected)
            self.changed()

    def ungroup(self):
        if self.shapes_selected():
            self.snapshot()
            self.d.ungroup(self.selected)
            self.changed()

    def move_selected(self, dx, dy):
        shapes = self.shapes_selected()
        if shapes:
            self.snapshot()
            for s in shapes:
                s["x"], s["y"] = s["x"] + dx, s["y"] + dy
            self.changed()

    # ---------- editing a label in place ----------
    def edit_label(self, item):
        """Open the text editor over a shape or arrow."""
        self.commit_edit()
        self.editing = item["id"]
        self.entry.get_buffer().set_text(str(item.get("label", "")))
        self.entry.show()
        self.overlay.queue_resize()
        self.entry.grab_focus()
        buf = self.entry.get_buffer()
        buf.select_range(buf.get_start_iter(), buf.get_end_iter())

    def on_entry_position(self, _overlay, widget, alloc):
        item = self.d.item(self.editing) if self.editing else None
        if widget is not self.entry or not item:
            return False
        if "kind" in item:
            x, y, w, h = item["x"], item["y"], item["w"], item["h"]
        else:
            mx, my = dg.label_point(self.d.edge_points(item))
            x, y, w, h = mx - 70, my - 14, 140, 28
        alloc.x, alloc.y = int(x * self.zoom + self.ox), int(y * self.zoom + self.oy)
        alloc.width, alloc.height = max(int(w * self.zoom), 90), max(int(h * self.zoom), 28)
        return True

    def on_entry_key(self, _w, event):
        key = Gdk.keyval_name(event.keyval)
        if key == "Escape":
            self.editing = None
            self.entry.hide()
            self.area.grab_focus()
            return True
        if key in ("Return", "KP_Enter") and not event.state & Gdk.ModifierType.SHIFT_MASK:
            self.commit_edit()
            self.area.grab_focus()
            return True
        return False

    def commit_edit(self, text=None):
        """Take the edited text as the item's label (text: use this instead of what was typed)."""
        if not self.editing:
            return
        item, self.editing = self.d.item(self.editing), None
        buf = self.entry.get_buffer()
        new = text if text is not None else buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
        self.entry.hide()
        if item is not None and new != item.get("label", ""):
            self.snapshot()
            item["label"] = new
            self.changed()

    # ---------- mouse ----------
    def handle_at(self, x, y):
        """The resize handle of the one selected shape under (x, y), or None."""
        shapes = self.shapes_selected()
        if len(shapes) != 1 or self.edges_selected():
            return None
        s, r = shapes[0], HIT / self.zoom
        for name, (fx, fy) in HANDLES.items():
            if abs(x - (s["x"] + fx * s["w"])) <= r and abs(y - (s["y"] + fy * s["h"])) <= r:
                return name
        return None

    def anchor_at(self, x, y):
        """(shape, side) of the connection point under (x, y) on the hovered shape, or None."""
        s = self.d.shape(self.hover) if self.hover else None
        if not s:
            return None
        r = (HIT + 2) / self.zoom
        for side in dg.SIDES:
            ax, ay = self.d.anchor(s, side)
            if math.hypot(x - ax, y - ay) <= r:
                return s, side
        return None

    def point_at(self, x, y):
        """For the one selected arrow: ("point", index) of a waypoint under (x, y), or ("mid", index of the
        segment) of a segment's middle, or None."""
        edges = self.edges_selected()
        if len(edges) != 1 or self.shapes_selected():
            return None
        e, r = edges[0], (HIT + 1) / self.zoom
        for i, (px, py) in enumerate(e.get("points", [])):
            if math.hypot(x - px, y - py) <= r:
                return "point", i
        pts = self.d.edge_points(e)
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(pts, pts[1:])):
            if math.hypot(x - (x1 + x2) / 2, y - (y1 + y2) / 2) <= r:
                return "mid", i
        return None

    @staticmethod
    def nearest_side(shape, x, y):
        return min(dg.SIDES, key=lambda side: math.hypot(x - dg.Diagram.anchor(shape, side)[0],
                                                          y - dg.Diagram.anchor(shape, side)[1]))

    def on_press(self, widget, event):
        widget.grab_focus()
        x, y = self.to_diagram(event.x, event.y)
        self.commit_edit()
        if event.button == 2 or (event.button == 1 and self.space):
            self.drag = {"kind": "pan", "sx": event.x, "sy": event.y, "ox": self.ox, "oy": self.oy}
            return True
        if event.button == 3:
            hit = self.d.shape_at(x, y) or self.d.edge_at(x, y, 6 / self.zoom)
            if hit and hit["id"] not in self.selected:
                self.select([hit["id"]])
            self.context_menu(event)
            return True
        if event.button != 1:
            return False
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        shape, edge = self.d.shape_at(x, y), self.d.edge_at(x, y, 6 / self.zoom)
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.drag = None
            if shape or edge:
                self.select([(shape or edge)["id"]])
                self.edit_label(shape or edge)
            return True
        handle = self.handle_at(x, y)
        if handle:
            s = self.shapes_selected()[0]
            self.drag = {"kind": "resize", "handle": handle, "id": s["id"], "start": dict(s), "moved": False}
            return True
        anchor = self.anchor_at(x, y)
        if anchor:
            self.drag = {"kind": "connect", "src": anchor[0]["id"], "side": anchor[1]}
            return True
        point = self.point_at(x, y)
        if point:
            e = self.edges_selected()[0]
            self.snapshot()
            if point[0] == "mid":
                pts = self.d.edge_points(e)
                (x1, y1), (x2, y2) = pts[point[1]], pts[point[1] + 1]
                # The new waypoint goes among the existing ones in path order.
                before = sum(1 for p in e.get("points", []) if tuple(p) in pts[:point[1] + 1])
                e.setdefault("points", []).insert(before, [dg.snap((x1 + x2) / 2), dg.snap((y1 + y2) / 2)])
                index = before
            else:
                index = point[1]
            self.drag = {"kind": "point", "id": e["id"], "index": index}
            self.set_dirty(True)
            return True
        if shape:
            if shift:
                if shape["id"] in self.selected:
                    self.selected -= self.d.with_groups({shape["id"]})
                    self.update_format()
                else:
                    self.select([shape["id"]], add=True)
            elif shape["id"] not in self.selected:
                self.select([shape["id"]])
            self.drag = {"kind": "move", "sx": x, "sy": y, "moved": False,
                         "start": {s["id"]: (s["x"], s["y"]) for s in self.shapes_selected()}}
        elif edge:
            self.select([edge["id"]], add=shift)
        else:
            if not shift:
                self.select([])
            self.drag = {"kind": "box", "sx": x, "sy": y, "base": set(self.selected)}
        self.area.queue_draw()
        return True

    def snap_move(self, dx, dy, start):
        """Adjust a move so the selection's edges or centers line up with another shape's, else with the grid.
        Returns (dx, dy) and sets the guides to draw."""
        self.guides = []
        if not self.snap or not start:
            return dx, dy
        moving = set(start)
        xs0 = min(x for x, _y in start.values())
        ys0 = min(y for _x, y in start.values())
        xs1 = max(start[s["id"]][0] + s["w"] for s in self.d.shapes if s["id"] in moving)
        ys1 = max(start[s["id"]][1] + s["h"] for s in self.d.shapes if s["id"] in moving)
        mine_x = [xs0 + dx, (xs0 + xs1) / 2 + dx, xs1 + dx]
        mine_y = [ys0 + dy, (ys0 + ys1) / 2 + dy, ys1 + dy]
        tol = 6 / self.zoom
        best_x = best_y = None
        for s in self.d.shapes:
            if s["id"] in moving:
                continue
            for theirs in (s["x"], s["x"] + s["w"] / 2, s["x"] + s["w"]):
                for m in mine_x:
                    if abs(theirs - m) <= tol and (best_x is None or abs(theirs - m) < abs(best_x[0])):
                        best_x = (theirs - m, theirs)
            for theirs in (s["y"], s["y"] + s["h"] / 2, s["y"] + s["h"]):
                for m in mine_y:
                    if abs(theirs - m) <= tol and (best_y is None or abs(theirs - m) < abs(best_y[0])):
                        best_y = (theirs - m, theirs)
        if best_x:
            dx += best_x[0]
            self.guides.append(("v", best_x[1]))
        else:
            dx = dg.snap(xs0 + dx) - xs0
        if best_y:
            dy += best_y[0]
            self.guides.append(("h", best_y[1]))
        else:
            dy = dg.snap(ys0 + dy) - ys0
        return dx, dy

    def on_motion(self, _widget, event):
        x, y = self.to_diagram(event.x, event.y)
        self.pointer = (x, y)
        drag = self.drag
        if not drag:
            under = self.d.shape_at(x, y)
            # Keep the hovered shape while the pointer is on one of its connection points, just outside it.
            if under or not (self.hover and self.anchor_at(x, y)):
                new = under["id"] if under else None
                if new != self.hover:
                    self.hover = new
                    self.area.queue_draw()
            return True
        snap = dg.snap if self.snap else (lambda v: v)
        if drag["kind"] == "pan":
            self.ox, self.oy = drag["ox"] + event.x - drag["sx"], drag["oy"] + event.y - drag["sy"]
        elif drag["kind"] == "move":
            if not drag["moved"]:
                if math.hypot(x - drag["sx"], y - drag["sy"]) < 3 / self.zoom:
                    return True
                self.snapshot()
                drag["moved"] = True
            dx, dy = self.snap_move(x - drag["sx"], y - drag["sy"], drag["start"])
            for s in self.d.shapes:
                if s["id"] in drag["start"]:
                    s["x"], s["y"] = drag["start"][s["id"]][0] + dx, drag["start"][s["id"]][1] + dy
            self.dirty = True
        elif drag["kind"] == "resize":
            if not drag["moved"]:
                self.snapshot()
                drag["moved"] = True
            s, o, hname = self.d.shape(drag["id"]), drag["start"], drag["handle"]
            x0, y0, x1, y1 = o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]
            if "w" in hname:
                x0 = min(snap(x), x1 - 20)
            if "e" in hname:
                x1 = max(snap(x), x0 + 20)
            if "n" in hname:
                y0 = min(snap(y), y1 - 20)
            if "s" in hname:
                y1 = max(snap(y), y0 + 20)
            s["x"], s["y"], s["w"], s["h"] = x0, y0, x1 - x0, y1 - y0
            self.dirty = True
        elif drag["kind"] == "point":
            e = self.d.edge(drag["id"])
            e["points"][drag["index"]] = [snap(x), snap(y)]
        elif drag["kind"] == "box":
            self.selected = drag["base"] | self.d.with_groups(set(self.d.in_rect(drag["sx"], drag["sy"], x, y)))
        elif drag["kind"] == "connect":
            under = self.d.shape_at(x, y)
            self.hover = under["id"] if under and under["id"] != drag["src"] else None
        self.area.queue_draw()
        return True

    def on_release(self, _widget, event):
        drag, self.drag = self.drag, None
        self.guides = []
        if not drag:
            return True
        x, y = self.to_diagram(event.x, event.y)
        if drag["kind"] == "connect":
            target = self.d.shape_at(x, y)
            if target and target["id"] != drag["src"]:
                self.connect(drag["src"], target["id"], src_side=drag["side"],
                             dst_side=self.nearest_side(target, x, y))
        elif drag["kind"] == "point":
            e = self.d.edge(drag["id"])
            # A waypoint dropped on the line between its neighbours is not needed: remove it.
            pts = [self.d.anchor(self.d.shape(e["src"]), self.d.sides_for(e)[0])] + \
                [tuple(p) for p in e["points"]] + [self.d.anchor(self.d.shape(e["dst"]), self.d.sides_for(e)[1])]
            i = drag["index"] + 1
            if dg._dist_to_segment(*pts[i], *pts[i - 1], *pts[i + 1]) < 4:
                del e["points"][drag["index"]]
            self.changed()
        elif drag["kind"] in ("move", "resize") and drag.get("moved"):
            self.changed()
        elif drag["kind"] == "box":
            self.update_format()
        self.area.queue_draw()
        return True

    def connect(self, src, dst, **style):
        """Draw an arrow between two shapes and select it."""
        self.snapshot()
        edge = self.d.connect(src, dst, **style)
        if edge:
            self.select([edge["id"]])
            self.changed()
        return edge

    def on_scroll(self, _widget, event):
        if event.direction == Gdk.ScrollDirection.SMOOTH:
            _ok, dx, dy = event.get_scroll_deltas()
        else:
            dx = {Gdk.ScrollDirection.LEFT: -1, Gdk.ScrollDirection.RIGHT: 1}.get(event.direction, 0)
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction, 0)
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            self.set_zoom(self.zoom * (1.1 ** -dy), about=(event.x, event.y))
        else:
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                dx, dy = dy, 0
            self.ox -= dx * 40
            self.oy -= dy * 40
            self.area.queue_draw()
        return True

    def context_menu(self, event):
        menu = Gtk.Menu()
        some, shapes = bool(self.selected), self.shapes_selected()
        for label, handler, on in (("Cut", self.cut, some), ("Copy", self.copy, some),
                                   ("Paste", self.paste, bool(self.clipboard)),
                                   ("Duplicate", self.duplicate, some), ("Delete", self.delete_selected, some),
                                   (None, None, True),
                                   ("Bring to front", lambda: self.reorder(True), bool(shapes)),
                                   ("Send to back", lambda: self.reorder(False), bool(shapes)),
                                   ("Group", self.group, len(shapes) > 1), ("Ungroup", self.ungroup, bool(shapes)),
                                   (None, None, True), ("Select all", self.select_all, True)):
            if label is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            item = Gtk.MenuItem(label=label, sensitive=on)
            item.connect("activate", lambda _i, h=handler: h())
            menu.append(item)
        menu.show_all()
        menu.popup_at_pointer(event)

    # ---------- keyboard ----------
    def on_key(self, _widget, event):
        key = Gdk.keyval_name(event.keyval)
        ctrl = bool(event.state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(event.state & Gdk.ModifierType.SHIFT_MASK)
        low = key.lower() if key else ""
        if key == "space":
            self.space = True
        elif key in ("Delete", "BackSpace"):
            self.delete_selected()
        elif key == "Escape":
            self.drag = None
            self.select([])
        elif key == "F2" and len(self.selected) == 1:
            self.edit_label(self.d.item(next(iter(self.selected))))
        elif key in ("Left", "Right", "Up", "Down") and self.shapes_selected():
            step = 1 if shift else dg.GRID
            self.move_selected({"Left": -step, "Right": step}.get(key, 0), {"Up": -step, "Down": step}.get(key, 0))
        elif ctrl and low == "z":
            self.redo() if shift else self.undo()
        elif ctrl and low == "y":
            self.redo()
        elif ctrl and low == "a":
            self.select_all()
        elif ctrl and low == "c":
            self.copy()
        elif ctrl and low == "x":
            self.cut()
        elif ctrl and low == "v":
            self.paste()
        elif ctrl and low == "d":
            self.duplicate()
        elif ctrl and low == "g":
            self.ungroup() if shift else self.group()
        elif ctrl and low == "s":
            self.save()
        else:
            return False
        return True

    def on_key_release(self, _widget, event):
        if Gdk.keyval_name(event.keyval) == "space":
            self.space = False
        return False

    # ---------- drawing ----------
    def on_draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        dark = theme.mode() == "dark"
        cr.set_source_rgb(*((0.1, 0.11, 0.14) if dark else (1, 1, 1)))
        cr.paint()
        if self.grid and self.zoom >= 0.4:
            # Grid dots every other grid step, so they stay a hint and not a texture.
            step = dg.GRID * 2 * self.zoom
            cr.set_source_rgba(*((1, 1, 1, 0.10) if dark else (0.11, 0.13, 0.19, 0.16)))
            x = self.ox % step
            while x < w:
                y = self.oy % step
                while y < h:
                    cr.rectangle(x - 0.6, y - 0.6, 1.2, 1.2)
                    y += step
                x += step
            cr.fill()
        cr.save()
        cr.translate(self.ox, self.oy)
        cr.scale(self.zoom, self.zoom)
        dg.draw(cr, self.d, self.selected, dark)
        z = self.zoom
        cr.set_source_rgb(*ACCENT)
        shapes = self.shapes_selected()
        for s in shapes:
            cr.set_line_width(1.2 / z)
            cr.set_dash([4 / z, 3 / z])
            cr.rectangle(s["x"] - 2 / z, s["y"] - 2 / z, s["w"] + 4 / z, s["h"] + 4 / z)
            cr.stroke()
        cr.set_dash([])
        if len(shapes) == 1 and not self.edges_selected():
            s = shapes[0]
            for fx, fy in HANDLES.values():
                self.square(cr, s["x"] + fx * s["w"], s["y"] + fy * s["h"], 4 / z)
        edges = self.edges_selected()
        if len(edges) == 1 and not shapes:
            e = edges[0]
            pts = self.d.edge_points(e)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                self.dot(cr, (x1 + x2) / 2, (y1 + y2) / 2, 3 / z, hollow=True)
            for px, py in e.get("points", []):
                self.dot(cr, px, py, 4.5 / z)
        hovered = self.d.shape(self.hover) if self.hover else None
        if hovered and (not self.drag or self.drag["kind"] == "connect"):
            for side in dg.SIDES:
                self.dot(cr, *self.d.anchor(hovered, side), 4.5 / z)
        if self.drag and self.drag["kind"] == "connect":
            src = self.d.shape(self.drag["src"])
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(1.6 / z)
            cr.set_dash([5 / z, 4 / z])
            cr.move_to(*self.d.anchor(src, self.drag["side"]))
            cr.line_to(*self.pointer)
            cr.stroke()
            cr.set_dash([])
        if self.drag and self.drag["kind"] == "box":
            x0, y0 = self.drag["sx"], self.drag["sy"]
            cr.rectangle(x0, y0, self.pointer[0] - x0, self.pointer[1] - y0)
            cr.set_source_rgba(*ACCENT, 0.08)
            cr.fill_preserve()
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(1 / z)
            cr.stroke()
        cr.set_source_rgba(0.95, 0.3, 0.5, 0.9)
        cr.set_line_width(1 / z)
        for kind, at in self.guides:
            if kind == "v":
                cr.move_to(at, -self.oy / z)
                cr.line_to(at, (h - self.oy) / z)
            else:
                cr.move_to(-self.ox / z, at)
                cr.line_to((w - self.ox) / z, at)
            cr.stroke()
        cr.restore()
        if not self.d.shapes:
            cr.set_source_rgba(*((1, 1, 1, 0.45) if dark else (0.11, 0.13, 0.19, 0.45)))
            cr.set_font_size(14)
            text = "Click a shape on the left to start, or use From RTL… to draw a module's block diagram."
            ext = cr.text_extents(text)
            cr.move_to((w - ext.width) / 2, h / 2)
            cr.show_text(text)
        return False

    @staticmethod
    def square(cr, x, y, r):
        cr.rectangle(x - r, y - r, 2 * r, 2 * r)
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_source_rgb(*ACCENT)
        cr.set_line_width(r / 3)
        cr.stroke()

    @staticmethod
    def dot(cr, x, y, r, hollow=False):
        cr.new_sub_path()
        cr.arc(x, y, r, 0, 2 * math.pi)
        if hollow:
            cr.set_source_rgb(1, 1, 1)
            cr.fill_preserve()
            cr.set_source_rgb(*ACCENT)
            cr.set_line_width(r / 2.5)
            cr.stroke()
        else:
            cr.set_source_rgb(*ACCENT)
            cr.fill()
