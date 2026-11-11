"""Power grid page of the Inputs tab: the straps, rings and via connections of the PDN script (PDN_TCL).

The power grid carries VDD and VSS from the top layers down to the standard cell rails. It is built by OpenROAD's
pdngen from the script in PDN_TCL (see pdn.py). The platform's script is shared by every design of the platform,
so the first save from this page writes an edited copy into the design folder and points PDN_TCL at it.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import lef  # noqa: E402
import pdn  # noqa: E402
from input_page import ERROR, MUTED, InputPage, hint_label, markup, scrolled, section  # noqa: E402

# Table columns: command index, kind, layers, width, pitch or spacing, offset, follow pins, follow pins shown.
IDX, KIND, LAYERS, WIDTH, PITCH, OFFSET, FOLLOW, FOLLOW_SHOWN = range(8)
KIND_OF = {"add_pdn_stripe": "strap", "add_pdn_ring": "ring", "add_pdn_connect": "vias"}
# Which option each column edits, per command.
COLUMNS = {"add_pdn_stripe": {LAYERS: "layer", WIDTH: "width", PITCH: "pitch", OFFSET: "offset"},
           "add_pdn_ring": {LAYERS: "layers", WIDTH: "widths", PITCH: "spacings", OFFSET: "core_offsets"},
           "add_pdn_connect": {LAYERS: "layers"}}


class PdnPage(InputPage):
    name, title = "pdn", "Power grid"

    def build(self):
        self.commands, self.text, self.mtime, self.dirty_ = [], "", None, False
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        outer.pack_start(hint_label(
            "Straps are parallel power and ground wires on one layer: width, pitch (distance between two straps of "
            "the same net) and offset from the core edge, in microns. 'Follow pins' straps sit on the standard "
            "cell rails. A ring runs around the core on two layers. Vias connect straps of two layers."),
            False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_end=12)
        self.file_note = hint_label("")
        body.pack_start(self.file_note, False, False, 0)
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="Grid"), False, False, 0)
        self.grid_cb = Gtk.ComboBoxText()
        self.grid_cb.connect("changed", lambda *_: self.fill())
        row.pack_start(self.grid_cb, False, False, 0)
        self.grid_note = hint_label("")
        row.pack_start(self.grid_note, True, True, 0)
        body.pack_start(row, False, False, 0)

        self.store = Gtk.ListStore(int, str, str, str, str, str, bool, bool)
        self.tv = Gtk.TreeView(model=self.store, height_request=220)
        self.tv.append_column(Gtk.TreeViewColumn("Kind", Gtk.CellRendererText(), text=KIND))
        for col, title in ((LAYERS, "Layer (two for rings and vias)"), (WIDTH, "Width"),
                           (PITCH, "Pitch / ring spacing"), (OFFSET, "Offset")):
            cell = Gtk.CellRendererText(editable=True)
            cell.connect("edited", self.on_edit, col)
            column = Gtk.TreeViewColumn(title, cell, text=col)
            column.set_resizable(True)
            column.set_expand(col == LAYERS)
            self.tv.append_column(column)
        follow = Gtk.CellRendererToggle()
        follow.connect("toggled", self.on_follow)
        self.tv.append_column(Gtk.TreeViewColumn("Follow pins", follow, active=FOLLOW, visible=FOLLOW_SHOWN))
        body.pack_start(self.tv, False, False, 0)
        btns = Gtk.Box(spacing=6)
        for label, handler in (("Add strap", self.on_add_strap), ("Add ring", self.on_add_ring),
                               ("Add vias", self.on_add_vias), ("Remove", self.on_remove)):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            btns.pack_start(b, False, False, 0)
        body.pack_start(btns, False, False, 0)
        self.layer_note = hint_label("")
        body.pack_start(self.layer_note, False, False, 0)
        body.pack_start(section("Problems"), False, False, 0)
        self.problem_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.pack_start(self.problem_label, False, False, 0)
        outer.pack_start(scrolled(body), True, True, 0)
        outer.pack_start(self.footer("Run the flow from floorplan after saving: the grid is built there."),
                         False, False, 0)
        return outer

    # ---- loading ----
    def path(self):
        value = self.tab.effective.get("PDN_TCL", "")
        return self.tab.resolve(value) if value else None

    def layer_info(self):
        """Routing layers bottom to top: [{"name", "direction", "pitch"}]."""
        tech = self.tab.effective.get("TECH_LEF", "")
        return lef.routing_layers(self.tab.resolve(tech)) if tech else []

    def load(self):
        if self.dirty_:
            return
        path = self.path()
        try:
            self.text = path.read_text(errors="replace") if path else ""
            self.mtime = path.stat().st_mtime if path else None
        except OSError:
            self.text, self.mtime = "", None
        self.commands = pdn.parse(self.text)
        shared = path and self.tab.design_dir() not in path.parents
        self.file_note.set_markup(markup(
            (f"From {self.tab.short(path)} (PDN_TCL)." + (
                " This is the platform's file: saving writes an edited copy into the design folder and uses it for "
                "this config." if shared else "")) if path else
            "This config has no PDN_TCL, so the flow has no power grid script to show.", MUTED))
        self._layer_info = self.layer_info()
        self._layers = [x["name"] for x in self._layer_info]
        self.layer_note.set_markup(markup("Routing layers, bottom to top: " + " ".join(self._layers), MUTED))
        current = self.grid_cb.get_active_id()
        self.grid_cb.remove_all()
        for name, is_macro in pdn.grids(self.commands):
            self.grid_cb.append(name, f"{name} ({'around macros' if is_macro else 'standard cells'})")
        names = [g for g, _ in pdn.grids(self.commands)]
        self.grid_cb.set_active_id(current if current in names else (names[0] if names else None))
        self.fill()

    def forget(self):
        self.dirty_ = False

    def fill(self):
        grid = self.grid_cb.get_active_id()
        self.store.clear()
        for i, c in enumerate(self.commands):
            if c.get("deleted") or c["cmd"] not in KIND_OF or pdn.get(c, "grid") != grid:
                continue
            cols = COLUMNS[c["cmd"]]
            values = {col: str(pdn.get(c, opt) or pdn.get(c, opt[:-1]) or "") for col, opt in cols.items()}
            stripe = c["cmd"] == "add_pdn_stripe"
            self.store.append([i, KIND_OF[c["cmd"]], values.get(LAYERS, ""), values.get(WIDTH, ""),
                               values.get(PITCH, ""), values.get(OFFSET, ""),
                               stripe and pdn.get(c, "followpins", False) is True, stripe])
        macro = dict(pdn.grids(self.commands)).get(grid)
        self.grid_note.set_markup(markup(
            "Straps over and around macros of this kind (by orientation)." if macro else
            "The grid over the whole core.", MUTED) if grid else "")
        self.show_problems()

    def show_problems(self):
        probs = pdn.problems(self.commands, self._layers if hasattr(self, "_layers") else [])
        self.problem_label.set_markup(markup("\n".join(probs), ERROR) if probs else markup("None.", MUTED))

    # ---- edits ----
    def changed(self):
        self.dirty_ = True
        self.fill()
        self.tab.dirty()

    def on_edit(self, _cell, path, text, col):
        c = self.commands[self.store[path][IDX]]
        opt = COLUMNS[c["cmd"]].get(col)
        if not opt:
            return
        # Keep the spelling the script uses (some write -core_offset for -core_offsets).
        if pdn.get(c, opt) == "" and pdn.get(c, opt[:-1]) != "":
            opt = opt[:-1]
        pdn.set_opt(c, opt, " ".join(text.split()) or None)
        if c["cmd"] == "add_pdn_ring" and col == LAYERS:
            pdn.connect_ring(self.commands, c, self._layer_info)
        self.changed()

    def on_follow(self, _cell, path):
        c = self.commands[self.store[path][IDX]]
        on = not self.store[path][FOLLOW]
        pdn.set_opt(c, "followpins", True if on else None)
        if on:
            pdn.set_opt(c, "pitch", None)
        self.changed()

    def layer_pick(self, n):
        """The n-th layer from the top that exists, for new rows."""
        layers = self._layers or ["metal1"]
        return layers[max(len(layers) - n, 0)]

    def on_add_strap(self, *_):
        grid = self.grid_cb.get_active_id()
        if grid:
            self.commands.append(pdn.new_command("add_pdn_stripe", grid, layer=self.layer_pick(3), width="1.0",
                                                 pitch="20.0", offset="2"))
            self.changed()

    def on_add_ring(self, *_):
        grid = self.grid_cb.get_active_id()
        if grid:
            ring = pdn.new_command("add_pdn_ring", grid,
                                   layers=pdn.ring_layers_for(self.commands, grid, self._layer_info),
                                   widths="2.0", spacings="1.0", core_offsets="2.0")
            self.commands.append(ring)
            pdn.connect_ring(self.commands, ring, self._layer_info)
            self.changed()

    def on_add_vias(self, *_):
        grid = self.grid_cb.get_active_id()
        if grid:
            self.commands.append(pdn.new_command("add_pdn_connect", grid,
                                                 layers=f"{self.layer_pick(4)} {self.layer_pick(3)}"))
            self.changed()

    def on_remove(self, *_):
        model, it = self.tv.get_selection().get_selected()
        if it:
            self.commands[model[it][IDX]]["deleted"] = True
            self.changed()

    # ---- state ----
    def is_dirty(self):
        return self.dirty_

    def save(self):
        probs = pdn.problems(self.commands, self._layers)
        if probs:
            self.tab.app.message("Fix these first; pdngen would stop on them:\n" + "\n".join(probs))
            return
        path, var_value = self.tab.own_file("PDN_TCL", "pdn.tcl")
        text = pdn.rebuild(self.text, self.commands)
        if self.tab.save_with_settings([(path, text, self.mtime if path == self.path() else None)],
                                       {"PDN_TCL": var_value} if var_value else {}):
            self.dirty_ = False

    def revert(self):
        self.dirty_ = False
        self.load()
        self.tab.dirty()
