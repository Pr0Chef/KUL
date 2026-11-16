"""Diagrams section: draw architecture and block diagrams, like a small draw.io, saved with the project.

Pick a shape from the palette and click the canvas to place it; drag shapes to move them and their corner
handle to resize; use Connect, then drag from one shape to another, to draw an arrow. Double-click a shape or an
arrow to edit its label (and an arrow's bus width and style). Delete removes the selection, Ctrl+Z undoes, and
Ctrl+wheel zooms. From RTL draws the block diagram of a module of the project. Export writes PNG or SVG.
"""

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


class DiagramPage:
    name, title = "diagram", "Diagrams"

    def __init__(self, app):
        self.app = app
        self.d = dg.Diagram()
        self.path = None
        self.dirty = False
        self.tool = "select"
        self.selected = set()
        self.drag = None          # ("move", start x, start y, {id: (x, y)}) / ("resize", id) / ("connect", id)
        self.pointer = (0, 0)
        self.zoom = 1.0
        self.undo_stack, self.redo_stack = [], []
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
        button("New…", self.on_new, "A new, empty diagram")
        button("From RTL…", self.on_from_rtl, "Draw the block diagram of a module of the project")
        self.save_btn = button("Save", lambda *_: self.save(), "Save the diagram (Ctrl+S)")
        self.save_btn.get_style_context().add_class("suggested-action")
        button("Export…", self.on_export, "Write the diagram as PNG or SVG")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        self.tool_buttons = {}
        group = None
        for tool, label in [("select", "Select"), ("connect", "Connect")] + [(k, k.capitalize()) for k in dg.KINDS]:
            b = Gtk.RadioButton.new_with_label_from_widget(group, label)
            b.set_mode(False)
            group = group or b
            b.connect("toggled", self.on_tool, tool)
            bar.pack_start(b, False, False, 0)
            self.tool_buttons[tool] = b
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        self.color_cb = Gtk.ComboBoxText(tooltip_text="Color of new shapes and of the selection")
        for c in dg.COLORS:
            self.color_cb.append(c, c.capitalize())
        self.color_cb.set_active_id("blue")
        self.color_cb.connect("changed", self.on_color)
        bar.pack_start(self.color_cb, False, False, 0)
        button("Delete", lambda *_: self.delete_selected())
        button("Undo", lambda *_: self.undo())
        self.state = Gtk.Label(xalign=1, hexpand=True)
        bar.pack_end(self.state, True, True, 0)
        outer.pack_start(bar, False, False, 0)

        self.area = Gtk.DrawingArea(can_focus=True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                             | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.KEY_PRESS_MASK
                             | Gdk.EventMask.SCROLL_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("motion-notify-event", self.on_motion)
        self.area.connect("button-release-event", self.on_release)
        self.area.connect("key-press-event", self.on_key)
        self.area.connect("scroll-event", self.on_scroll)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(self.area)
        holder = Gtk.Box(margin=12)
        holder.get_style_context().add_class("preview")
        holder.pack_start(sw, True, True, 0)
        outer.pack_start(holder, True, True, 0)
        hint = Gtk.Label(xalign=0, margin_start=14, margin_bottom=8, wrap=True)
        hint.set_markup(markup("Click a shape kind, then the canvas, to add it. Drag to move, drag the corner "
                               "square to resize, Connect then drag between shapes to draw an arrow. Double-click "
                               "to edit a label. Delete removes, Ctrl+Z undoes, Ctrl+wheel zooms.", MUTED))
        outer.pack_start(hint, False, False, 0)
        return outer

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
            self.undo_stack = []
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
            self.undo_stack = []
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
        if self.folder():
            self.folder().mkdir(parents=True, exist_ok=True)
            dlg.set_current_folder(str(self.folder()))
        dlg.set_current_name((self.path.name[:-len(SUFFIX)] if self.path else "diagram") + ".png")
        if dlg.run() == Gtk.ResponseType.OK:
            out = dlg.get_filename()
            if not out.lower().endswith((".png", ".svg")):
                out += ".png"
            dg.export(self.d, out)
            self.app.set_status(f"Exported {out}")
        dlg.destroy()

    # ---------- editing ----------
    def snapshot(self):
        self.undo_stack.append(self.d.to_json())
        del self.undo_stack[:-100]
        self.redo_stack = []

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.d.to_json())
            self.d = dg.Diagram.from_json(self.undo_stack.pop())
            self.selected = set()
            self.set_dirty(True)

    def changed(self):
        self.set_dirty(True)
        self.resize_area()

    def delete_selected(self):
        if self.selected:
            self.snapshot()
            self.d.remove(self.selected)
            self.selected = set()
            self.changed()

    def on_tool(self, button, tool):
        if button.get_active():
            self.tool = tool

    def on_color(self, cb):
        if self.selected:
            self.snapshot()
            for s in self.d.shapes:
                if s["id"] in self.selected:
                    s["color"] = cb.get_active_id()
            self.changed()

    def edit_label(self, item):
        is_edge = "src" in item
        fields = [("label", "Label", item.get("label", ""), "text")]
        if is_edge:
            fields += [("bus", "Bus width (0: a single wire)", str(item.get("bus", 0)), "text"),
                       ("dashed", "Dashed", item.get("dashed", False), "check"),
                       ("both", "Arrows at both ends", item.get("both", False), "check")]
        else:
            fields.append(("kind", "Shape", item["kind"], dg.KINDS))
        vals = dialogs.ask_fields(self.app, "Arrow" if is_edge else "Shape", fields,
                                  intro="" if is_edge else "Use \\n in the label for a new line.")
        if vals:
            self.snapshot()
            item["label"] = vals["label"].replace("\\n", "\n")
            if is_edge:
                item.update(bus=int(vals["bus"]) if vals["bus"].isdigit() else 0, dashed=vals["dashed"],
                            both=vals["both"])
            else:
                item["kind"] = vals["kind"]
            self.changed()

    # ---------- mouse and keys ----------
    def to_diagram(self, x, y):
        return x / self.zoom, y / self.zoom

    def on_press(self, widget, event):
        widget.grab_focus()
        x, y = self.to_diagram(event.x, event.y)
        shape = self.d.shape_at(x, y)
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            item = shape or self.d.edge_at(x, y)
            if item:
                self.edit_label(item)
            return True
        if self.tool in dg.KINDS:
            self.snapshot()
            new = self.d.add_shape(self.tool, x, y, color=self.color_cb.get_active_id())
            self.selected = {new["id"]}
            self.tool_buttons["select"].set_active(True)
            self.changed()
            return True
        if self.tool == "connect":
            if shape:
                self.drag = ("connect", shape["id"])
            return True
        if shape and shape["id"] in self.selected and x > shape["x"] + shape["w"] - 8 and \
                y > shape["y"] + shape["h"] - 8:
            self.snapshot()
            self.drag = ("resize", shape["id"])
            return True
        if shape:
            if event.state & Gdk.ModifierType.SHIFT_MASK:
                self.selected ^= {shape["id"]}
            elif shape["id"] not in self.selected:
                self.selected = {shape["id"]}
            self.snapshot()
            self.drag = ("move", x, y, {s["id"]: (s["x"], s["y"]) for s in self.d.shapes
                                        if s["id"] in self.selected})
        else:
            edge = self.d.edge_at(x, y)
            self.selected = {edge["id"]} if edge else set()
        self.area.queue_draw()
        return True

    def on_motion(self, _widget, event):
        x, y = self.to_diagram(event.x, event.y)
        self.pointer = (x, y)
        if not self.drag:
            return False
        if self.drag[0] == "move":
            _, x0, y0, start = self.drag
            for s in self.d.shapes:
                if s["id"] in start:
                    s["x"], s["y"] = dg.snap(start[s["id"]][0] + x - x0), dg.snap(start[s["id"]][1] + y - y0)
            self.set_dirty(True)
        elif self.drag[0] == "resize":
            s = self.d.shape(self.drag[1])
            s["w"], s["h"] = max(dg.GRID * 3, dg.snap(x - s["x"])), max(dg.GRID * 2, dg.snap(y - s["y"]))
            self.set_dirty(True)
        self.area.queue_draw()
        return True

    def on_release(self, _widget, event):
        if self.drag and self.drag[0] == "connect":
            x, y = self.to_diagram(event.x, event.y)
            target = self.d.shape_at(x, y)
            if target and target["id"] != self.drag[1]:
                self.snapshot()
                edge = self.d.connect(self.drag[1], target["id"])
                self.selected = {edge["id"]}
                self.changed()
        elif self.drag and self.drag[0] == "move":
            # A click without a move leaves nothing to undo.
            if all((s["x"], s["y"]) == self.drag[3].get(s["id"], (s["x"], s["y"])) for s in self.d.shapes):
                self.undo_stack.pop()
            self.resize_area()
        self.drag = None
        self.area.queue_draw()
        return True

    def on_key(self, _widget, event):
        key = Gdk.keyval_name(event.keyval) or ""
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        if key in ("Delete", "BackSpace"):
            self.delete_selected()
        elif ctrl and key.lower() == "z":
            self.undo()
        elif ctrl and key.lower() == "s":
            self.save()
        elif ctrl and key.lower() == "a":
            self.selected = {s["id"] for s in self.d.shapes}
            self.area.queue_draw()
        else:
            return False
        return True

    def on_scroll(self, _widget, event):
        if not event.state & Gdk.ModifierType.CONTROL_MASK:
            return False
        ok, _dx, dy = event.get_scroll_deltas()
        if not ok:
            dy = -1 if event.direction == Gdk.ScrollDirection.UP else 1
        self.zoom = min(3.0, max(0.25, self.zoom * (0.9 if dy > 0 else 1.1)))
        self.resize_area()
        return True

    def resize_area(self):
        _, _, x1, y1 = self.d.bounds(margin=200)
        self.area.set_size_request(int(max(x1, 800) * self.zoom), int(max(y1, 600) * self.zoom))
        self.area.queue_draw()

    # ---------- drawing ----------
    def on_draw(self, widget, cr):
        bg = Gdk.RGBA()
        bg.parse(theme.color("surface"))
        cr.set_source_rgb(bg.red, bg.green, bg.blue)
        cr.paint()
        cr.scale(self.zoom, self.zoom)
        # Grid dots.
        dot = Gdk.RGBA()
        dot.parse(theme.color("border"))
        cr.set_source_rgb(dot.red, dot.green, dot.blue)
        w, h = widget.get_allocated_width() / self.zoom, widget.get_allocated_height() / self.zoom
        for gx in range(0, int(w), dg.GRID * 2):
            for gy in range(0, int(h), dg.GRID * 2):
                cr.rectangle(gx, gy, 1, 1)
        cr.fill()
        dg.draw(cr, self.d, self.selected, dark=theme.mode() == "dark")
        if self.drag and self.drag[0] == "connect":
            s = self.d.shape(self.drag[1])
            cr.set_source_rgb(0.24, 0.35, 1)
            cr.set_dash([4, 3])
            cr.move_to(s["x"] + s["w"] / 2, s["y"] + s["h"] / 2)
            cr.line_to(*self.pointer)
            cr.stroke()
        if not self.d.shapes:
            cr.set_dash([])
            cr.set_source_rgb(0.5, 0.53, 0.6)
            cr.set_font_size(15)
            cr.move_to(40, 60)
            cr.show_text("Empty diagram: pick a shape above and click here, or use From RTL.")
        return False

