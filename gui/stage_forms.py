"""Settings pages for the flow stages (Synthesis, Placement, Clock tree, Routing), built from field lists.

Each field is one ORFS config variable shown as a proper input: a check box, a number, a layer list or text.
Changes go into the Inputs tab's pending settings, the same ones the Flow settings table shows, and Save writes all
pending settings into the config file. A field's help text comes from ORFS's own variables.json.
"""

import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import fastroute  # noqa: E402
import lef  # noqa: E402
from input_page import ERROR, MUTED, InputPage, hint_label, markup, scrolled, section  # noqa: E402

NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")

# Field: (variable, label, kind, limits). Kinds: bool, int, float, layer, text. Limits: (min, max) for numbers.
SYNTHESIS = [
    ("Structure", [
        ("SYNTH_HIERARCHICAL", "Keep the module hierarchy", "bool", None),
        ("SYNTH_MINIMUM_KEEP_SIZE", "Smallest module kept as its own block", "int", (0, None)),
        ("SYNTH_KEEP_MODULES", "Modules always kept", "text", None),
        ("SYNTH_MEMORY_MAX_BITS", "Largest memory turned into flip-flops (bits)", "int", (0, None)),
    ]),
    ("Optimization", [
        ("ABC_AREA", "Optimize for area rather than speed", "bool", None),
        ("SYNTH_REPEATABLE_BUILD", "Same netlist on every run", "bool", None),
    ]),
    ("Sources", [
        ("VERILOG_DEFINES", "Verilog defines (for example -DSYNTHESIS)", "text", None),
        ("VERILOG_INCLUDE_DIRS", "Include folders", "text", None),
        ("SYNTH_HDL_FRONTEND", "HDL frontend (empty: Yosys's own; slang: SystemVerilog)", "text", None),
    ]),
]
PLACEMENT = [
    ("Global placement", [
        ("PLACE_DENSITY", "Target cell density (0 to 1)", "float", (0.0, 1.0)),
        ("PLACE_DENSITY_LB_ADDON", "Added to the lowest density that fits", "float", (0.0, 1.0)),
        ("GPL_TIMING_DRIVEN", "Timing-driven placement", "bool", None),
        ("GPL_ROUTABILITY_DRIVEN", "Routability-driven placement", "bool", None),
        ("CELL_PAD_IN_SITES_GLOBAL_PLACEMENT", "Cell padding in global placement (sites)", "int", (0, None)),
    ]),
    ("Detailed placement", [
        ("CELL_PAD_IN_SITES_DETAIL_PLACEMENT", "Cell padding in detailed placement (sites)", "int", (0, None)),
        ("ENABLE_DPO", "Run detailed placement optimization", "bool", None),
    ]),
    ("Pins and repair", [
        ("IO_PLACER_H", "Layer for pins on the left and right edges", "layer", None),
        ("IO_PLACER_V", "Layer for pins on the top and bottom edges", "layer", None),
        ("TNS_END_PERCENT", "Share of failing endpoints timing repair works on (%)", "int", (0, 100)),
    ]),
]
CLOCK_TREE = [
    ("Clock tree", [
        ("CTS_BUF_LIST", "Buffers the clock tree may use", "text", None),
        ("CTS_BUF_DISTANCE", "Distance between clock buffers (microns)", "float", (0.0, None)),
        ("CTS_CLUSTER_SIZE", "Most clock sinks per cluster", "int", (1, None)),
        ("CTS_CLUSTER_DIAMETER", "Largest cluster diameter (microns)", "float", (0.0, None)),
    ]),
    ("Timing repair after CTS", [
        ("SKIP_CTS_REPAIR_TIMING", "Skip timing repair after CTS", "bool", None),
        ("SETUP_SLACK_MARGIN", "Setup slack margin", "float", None),
        ("HOLD_SLACK_MARGIN", "Hold slack margin", "float", None),
    ]),
]
ROUTING = [
    ("Layers", [
        ("MIN_ROUTING_LAYER", "Lowest signal routing layer", "layer", None),
        ("MAX_ROUTING_LAYER", "Highest routing layer", "layer", None),
        ("MIN_CLK_ROUTING_LAYER", "Lowest clock routing layer", "layer", None),
        ("ROUTING_LAYER_ADJUSTMENT", "Capacity held back on every layer (0 to 1)", "float", (0.0, 1.0)),
    ]),
    ("Global routing", [
        ("SKIP_INCREMENTAL_REPAIR", "Skip timing repair during global routing", "bool", None),
        ("GLOBAL_ROUTE_ARGS", "Extra global_route arguments", "text", None),
    ]),
    ("Detailed routing", [
        ("DETAILED_ROUTE_END_ITERATION", "Most detailed routing iterations", "int", (1, None)),
        ("SKIP_ANTENNA_REPAIR_POST_DRT", "Skip antenna repair after detailed routing", "bool", None),
        ("MAX_REPAIR_ANTENNAS_ITER_DRT", "Most antenna repair rounds", "int", (0, None)),
        ("FILL_CELLS", "Filler cells", "text", None),
    ]),
]


def check(kind, value, limits=None, layers=()):
    """Why a value can't be used for this kind of field, or None if it can. An empty value is always allowed."""
    if value == "":
        return None
    if kind in ("int", "float"):
        if kind == "int" and not re.fullmatch(r"-?\d+", value):
            return "must be a whole number"
        if kind == "float" and not NUMBER_RE.match(value):
            return "must be a number"
        lo, hi = limits or (None, None)
        if lo is not None and float(value) < lo:
            return f"must be at least {lo:g}"
        if hi is not None and float(value) > hi:
            return f"must be at most {hi:g}"
    if kind == "bool" and value not in ("0", "1"):
        return "must be 0 or 1"
    if kind == "layer" and layers and value not in layers and "$" not in value:
        return "is not a routing layer of this platform"
    return None


def first_sentence(text):
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return m.group(1) if m else text


class FormPage(InputPage):
    """A page of fields; subclasses set name, title, intro and groups."""

    intro = ""
    groups = []

    def build(self):
        self.fields = {}      # variable: (kind, limits, widget, origin label, error label, reset button)
        self._filling = False
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        outer.pack_start(hint_label(self.intro), False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_end=12)
        # One grid for all groups, so the columns line up down the page.
        grid = Gtk.Grid(column_spacing=12, row_spacing=2)
        row = 0
        for title, fields in self.groups:
            title_label = section(title)
            title_label.set_margin_top(8 if row else 0)
            grid.attach(title_label, 0, row, 4, 1)
            row += 1
            for var, label, kind, limits in fields:
                row = self.add_field(grid, row, var, label, kind, limits)
        if self.groups:
            body.pack_start(grid, False, False, 0)
        self.extra(body)
        outer.pack_start(self.wrap_body(scrolled(body)), True, True, 0)
        outer.pack_start(self.footer("Settings changed on any page are saved together into this config."),
                         False, False, 0)
        return outer

    def extra(self, body):
        """Room for a subclass to add more below the fields."""

    def wrap_body(self, body):
        """The widget that holds the scrolled fields; a subclass can put it beside something else."""
        return body

    def add_field(self, grid, row, var, label, kind, limits):
        name = Gtk.Label(label=label, xalign=0, tooltip_text=var)
        if kind == "bool":
            widget = Gtk.CheckButton()
            widget.connect("toggled", lambda w, v=var: self.on_change(v, "1" if w.get_active() else "0"))
        elif kind == "layer":
            widget = Gtk.ComboBoxText(has_entry=True)
            widget.get_child().connect("changed", lambda e, v=var: self.on_change(v, e.get_text().strip()))
        else:
            widget = Gtk.Entry(width_chars=12 if kind in ("int", "float") else 24)
            widget.connect("changed", lambda e, v=var: self.on_change(v, e.get_text().strip()))
        origin = Gtk.Label(xalign=0)
        reset = Gtk.Button(label="Use default", tooltip_text="Remove this setting from this config, so the "
                                                             "platform's or ORFS's default applies")
        reset.connect("clicked", lambda *_: self.on_reset(var))
        error = Gtk.Label(xalign=0)
        desc = hint_label("")
        grid.attach(name, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)
        grid.attach(origin, 2, row, 1, 1)
        grid.attach(reset, 3, row, 1, 1)
        grid.attach(error, 1, row + 1, 3, 1)
        grid.attach(desc, 1, row + 2, 3, 1)
        self.fields[var] = (kind, limits, widget, origin, error, reset, desc)
        return row + 3

    # ---- values ----
    def layers(self):
        tech = self.tab.effective.get("TECH_LEF", "")
        key = tech
        if getattr(self, "_layers_key", None) != key:
            self._layers_key = key
            self._layers = [layer["name"] for layer in lef.routing_layers(self.tab.resolve(tech))] if tech else []
        return self._layers

    def here(self):
        """{variable: value} set in this config's own file."""
        cfg = self.tab.cfg_path()
        return {v: info["value"] for v, p, info in self.tab.chain if p == cfg}

    def origin(self, var):
        """Where the value comes from, in a few words."""
        if var in self.tab.pending:
            return "changed, not saved" if self.tab.pending[var] is not None else "back to the default, not saved"
        src = None
        for v, p, _ in self.tab.chain:
            if v == var:
                src = p
        if src == self.tab.cfg_path():
            return "set in this config"
        if src:
            return f"from {src.name}"
        if var in self.tab.platform_vars():
            return "platform default"
        return "ORFS default" if self.tab.effective.get(var) else "not set"

    def value(self, var):
        if var in self.tab.pending:
            pending = self.tab.pending[var]
            return pending if pending is not None else self.default_value(var)
        return self.tab.effective.get(var, "")

    def default_value(self, var):
        """What applies once this config no longer sets var (the value before this config, if known)."""
        meta = self.tab.variables.get(var, {})
        return str(meta.get("default", "") or "") if var in self.here() else self.tab.effective.get(var, "")

    def load(self):
        self._filling = True
        layers = self.layers()
        here = self.here()
        for var, (kind, limits, widget, origin, error, reset, desc) in self.fields.items():
            val = self.value(var)
            if kind == "bool":
                widget.set_active(val not in ("", "0"))
            elif kind == "layer":
                widget.remove_all()
                for name in layers:
                    widget.append_text(name)
                widget.get_child().set_text(val)
            else:
                widget.set_text(val)
            origin.set_markup(markup(self.origin(var), MUTED))
            reset.set_sensitive(var in here or var in self.tab.pending)
            error.set_text("")
            error.set_visible(False)
            meta = self.tab.variables.get(var, {})
            desc.set_markup(markup(first_sentence(meta.get("description", "")), MUTED))
            widget.set_tooltip_text((meta.get("description") or var).strip())
        self._filling = False

    def settings_changed(self):
        self.load()

    def on_change(self, var, value):
        if self._filling:
            return
        kind, limits, widget, origin, error, reset, desc = self.fields[var]
        why = check(kind, value, limits, self.layers())
        error.set_markup(markup(f"Not used: {value} {why}", ERROR) if why else "")
        error.set_visible(bool(why))
        if why:
            return
        here = self.here()
        saved = here[var] if var in here else self.tab.effective.get(var, "")
        if value == saved:
            self.tab.pending.pop(var, None)
        else:
            self.tab.pending[var] = value
        origin.set_markup(markup(self.origin(var), MUTED))
        reset.set_sensitive(var in here or var in self.tab.pending)
        self.tab.settings_changed(source=self)

    def on_reset(self, var):
        if var in self.here():
            self.tab.pending[var] = None
        else:
            self.tab.pending.pop(var, None)
        self.load()
        self.tab.settings_changed(source=self)

    def is_dirty(self):
        return any(v in self.tab.pending for v in self.fields)

    def save(self):
        self.tab.on_settings_save()

    def revert(self):
        for var in self.fields:
            self.tab.pending.pop(var, None)
        self.load()
        self.tab.settings_changed(source=self)


class SynthesisPage(FormPage):
    name, title = "synth", "Synthesis"
    intro = "How Yosys turns the RTL into a netlist of the platform's cells."
    groups = SYNTHESIS


class PlacementPage(FormPage):
    name, title = "place", "Placement"
    intro = ("Where the standard cells go: global placement spreads them to the target density, detailed placement "
             "puts each on a legal site, and timing repair resizes and buffers along the way.")
    groups = PLACEMENT


class ClockTreePage(FormPage):
    name, title = "cts", "Clock tree"
    intro = ("Clock tree synthesis (CTS) builds buffer trees from each clock pin to its flip-flops so the clock "
             "arrives at about the same time everywhere, then repairs setup and hold timing.")
    groups = CLOCK_TREE


class RoutingPage(FormPage):
    """Routing fields, plus the per-layer capacity adjustments of FASTROUTE_TCL."""

    name, title = "route", "Routing"
    intro = ("Global routing plans each net's path over a coarse grid; detailed routing draws the real wires on "
             "the layers between the lowest and highest routing layer.")
    groups = ROUTING

    def extra(self, body):
        body.pack_start(section("Capacity per layer"), False, False, 0)
        self.adj_note = hint_label("")
        body.pack_start(self.adj_note, False, False, 0)
        # layers ("metal2-metal3"), value ("0.5")
        self.adj_store = Gtk.ListStore(str, str)
        tv = Gtk.TreeView(model=self.adj_store, height_request=130)
        for col, title in ((0, "Layers (one, or a range like metal2-metal3)"), (1, "Held back (0 to 1)")):
            cell = Gtk.CellRendererText(editable=True)
            cell.connect("edited", self.on_adj_edit, col)
            tv.append_column(Gtk.TreeViewColumn(title, cell, text=col))
        self.adj_tv = tv
        body.pack_start(tv, False, False, 0)
        row = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add layer")
        add.connect("clicked", self.on_adj_add)
        remove = Gtk.Button(label="Remove")
        remove.connect("clicked", self.on_adj_remove)
        self.adj_problems = Gtk.Label(xalign=0, wrap=True)
        row.pack_start(add, False, False, 0)
        row.pack_start(remove, False, False, 0)
        row.pack_start(self.adj_problems, True, True, 0)
        body.pack_start(row, False, False, 0)

    def adj_file(self):
        value = self.tab.effective.get("FASTROUTE_TCL", "")
        return self.tab.resolve(value) if value else None

    def load(self):
        super().load()
        self.adj_dirty = False
        path = self.adj_file()
        try:
            self.adj_text = path.read_text(errors="replace") if path else ""
            self.adj_mtime = path.stat().st_mtime if path else None
        except OSError:
            self.adj_text, self.adj_mtime = "", None
        self.adj_store.clear()
        for r in fastroute.parse(self.adj_text):
            self.adj_store.append([r["layers"], r["value"]])
        if path:
            # The flow only uses ROUTING_LAYER_ADJUSTMENT without FASTROUTE_TCL; say so at the field.
            self.fields["ROUTING_LAYER_ADJUSTMENT"][6].set_markup(markup(
                "Not used by this config: FASTROUTE_TCL is set, so the per-layer capacities below apply.", ERROR))
            shared = self.tab.design_dir() not in path.parents
            self.adj_note.set_markup(markup(
                f"From {self.tab.short(path)} (FASTROUTE_TCL), which the flow runs before global routing. With it "
                "set, ROUTING_LAYER_ADJUSTMENT above is not used." + (
                    " It is the platform's file: saving puts an edited copy in the design folder." if shared else ""),
                MUTED))
        else:
            self.adj_note.set_markup(markup(
                "No FASTROUTE_TCL: ROUTING_LAYER_ADJUSTMENT above applies to every layer. Adding a layer here makes "
                "a fastroute.tcl in the design folder, and then that file sets the capacity.", MUTED))
        self.show_adj_problems()

    def forget(self):
        self.adj_dirty = False

    def rows(self):
        return [{"layers": r[0], "value": r[1]} for r in self.adj_store]

    def show_adj_problems(self):
        probs = fastroute.problems(self.rows(), self.layers())
        self.adj_problems.set_markup(markup("; ".join(probs), ERROR) if probs else "")

    def on_adj_edit(self, _cell, path, text, col):
        self.adj_store[path][col] = text.strip()
        self.adj_changed()

    def on_adj_add(self, *_):
        layers = self.layers()
        self.adj_store.append([layers[1] if len(layers) > 1 else "", "0.5"])
        self.adj_changed()

    def on_adj_remove(self, *_):
        model, it = self.adj_tv.get_selection().get_selected()
        if it:
            model.remove(it)
            self.adj_changed()

    def adj_changed(self):
        self.adj_dirty = True
        self.show_adj_problems()
        self.tab.dirty()

    def is_dirty(self):
        return super().is_dirty() or self.adj_dirty

    def save(self):
        if not self.adj_dirty:
            self.tab.on_settings_save()
            return
        probs = fastroute.problems(self.rows(), self.layers())
        if probs:
            self.tab.app.message("Fix the layer capacities first:\n" + "\n".join(probs))
            return
        start = self.adj_text
        if not start:
            start = ("# Global routing layers and capacity, run by the flow before global routing (FASTROUTE_TCL).\n"
                     "set_routing_layers -signal $::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)\n")
        text = fastroute.rebuild(start, self.rows())
        path, var_value = self.tab.own_file("FASTROUTE_TCL", "fastroute.tcl")
        if self.tab.save_with_settings([(path, text, self.adj_mtime if path == self.adj_file() else None)],
                                       {"FASTROUTE_TCL": var_value} if var_value else {}):
            self.adj_dirty = False
