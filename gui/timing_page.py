"""Timing page of the Inputs tab: clock uncertainty and latency, design rules, and timing exceptions.

Everything set here is written into one marked block of the design's SDC file (see sdc_timing.py). The page edits
the same SDC text as the Clocks and Constraints pages, so all three show the same unsaved state, and Save on any
of them saves the SDC file.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import sdc_timing as st  # noqa: E402
from input_page import ERROR, MUTED, InputPage, card, hint_label, markup, scrolled  # noqa: E402

CHECKS = [("", "setup and hold"), ("setup", "setup only"), ("hold", "hold only")]


def combo_model(items):
    store = Gtk.ListStore(str, str)  # id, label
    for item_id, label in items:
        store.append([item_id, label])
    return store


class TimingPage(InputPage):
    name, title = "timing", "Timing"
    intro = ("What makes timing realistic, and which paths are timed differently. Written into the SDC file in a "
             "block marked 'ORFS Flow Runner: timing'.")

    def build(self):
        self._filling = False
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=20, margin_end=20,
                       margin_top=12, margin_bottom=16)

        # Per-clock settings: name, then one column per CLOCK_FIELDS entry.
        c = card("Clocks")
        body.pack_start(c, False, False, 0)
        self.unit_note = hint_label("")
        c.pack_start(self.unit_note, False, False, 0)
        self.clk_store = Gtk.ListStore(*([str] * (1 + len(st.CLOCK_FIELDS))))
        tv = Gtk.TreeView(model=self.clk_store, height_request=110)
        tv.append_column(Gtk.TreeViewColumn("Clock", Gtk.CellRendererText(), text=0))
        for i, (key, label, _) in enumerate(st.CLOCK_FIELDS, 1):
            cell = Gtk.CellRendererText(editable=True, xalign=1)
            cell.connect("edited", self.on_clock_edit, i)
            col = Gtk.TreeViewColumn(label, cell, text=i)
            col.set_min_width(110)
            tv.append_column(col)
        c.pack_start(tv, False, False, 0)
        c.pack_start(hint_label(
            "Uncertainty is margin taken off every path for jitter and skew; before CTS it stands in for the skew "
            "the clock tree will add. Transition is the clock's slew. Latency is the delay from the clock source "
            "(source) and through the clock tree (network); once the clock tree is built, the real network "
            "latency is used."), False, False, 0)

        # Design rules.
        c = card("Inputs, outputs and design rules")
        body.pack_start(c, False, False, 0)
        self.rule_units = hint_label("")
        c.pack_start(self.rule_units, False, False, 0)
        grid = Gtk.Grid(column_spacing=12, row_spacing=4)
        self.rule_entries = {}
        rows = [("driving_cell", "Cell driving the inputs", "a buffer of the platform, such as BUF_X2"),
                ("driving_pin", "Its output pin", "such as Z; empty: the cell's only output")]
        rows += [(key, label, "") for key, label, _, _ in st.RULE_FIELDS]
        for r, (key, label, tip) in enumerate(rows):
            entry = Gtk.Entry(width_chars=14)
            entry.connect("changed", lambda *_: self.on_edit())
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(entry, 1, r, 1, 1)
            grid.attach(hint_label(tip), 2, r, 1, 1)
            self.rule_entries[key] = entry
        c.pack_start(grid, False, False, 0)

        # Exceptions: type, value, check, then kind and names for from, through and to.
        c = card("Timing exceptions")
        body.pack_start(c, False, False, 0)
        c.pack_start(hint_label(
            "False path: not timed at all (for example a reset, or between unrelated clocks). Multicycle: the path "
            "may take N clock cycles. Max and min delay: a fixed limit instead of the clock period. Names may use "
            "* and ?; several names are separated by spaces."), False, False, 0)
        self.ex_store = Gtk.ListStore(*([str] * 9))
        self.ex_tv = Gtk.TreeView(model=self.ex_store, height_request=150)
        types = combo_model([(k, v[0]) for k, v in st.EXCEPTIONS.items()])
        kinds = combo_model([("", "-")] + [(k, k) for k in st.OBJECT_KINDS])
        checks = combo_model(CHECKS)
        self.add_combo_col("Type", 0, types)
        self.add_text_col("Value", 1)
        self.add_combo_col("Check", 2, checks)
        for n, end in enumerate(("From", "Through", "To")):
            self.add_combo_col(f"{end}", 3 + 2 * n, kinds)
            self.add_text_col(f"{end} names", 4 + 2 * n, expand=True)
        c.pack_start(self.ex_tv, False, False, 0)
        row = Gtk.Box(spacing=6)
        for label, handler in (("Add exception", self.on_add), ("Remove", self.on_remove)):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        c.pack_start(row, False, False, 0)

        self.problem_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.outside_label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        body.pack_start(self.problem_label, False, False, 0)
        body.pack_start(self.outside_label, False, False, 0)
        outer.pack_start(scrolled(body), True, True, 0)
        outer.pack_start(self.footer("Saved in the SDC file, together with the Clocks and Constraints pages."),
                         False, False, 0)
        outer.connect("map", lambda *_: self.load())
        return outer

    def add_text_col(self, title, col, expand=False):
        cell = Gtk.CellRendererText(editable=True)
        cell.connect("edited", self.on_cell, col)
        column = Gtk.TreeViewColumn(title, cell, text=col)
        column.set_expand(expand)
        column.set_resizable(True)
        self.ex_tv.append_column(column)

    def add_combo_col(self, title, col, model):
        cell = Gtk.CellRendererCombo(model=model, text_column=1, has_entry=False, editable=True)
        cell.connect("changed", lambda c, path, it, col=col, m=model: self.on_cell(c, path, m[it][0], col))
        column = Gtk.TreeViewColumn(title, cell)
        labels = {row[0]: row[1] for row in model}

        def show(_column, renderer, store, it, col):
            renderer.set_property("text", labels.get(store[it][col], store[it][col]))
        column.set_cell_data_func(cell, show, col)
        self.ex_tv.append_column(column)

    # ---- model <-> widgets ----
    def load(self):
        """Show what the SDC text (saved or not) holds."""
        if not self.tab.cfg:
            return
        text = self.tab.sdc_text()
        model, unknown = st.parse(text)
        self._filling = True
        clocks = self.tab.clock_names()
        self.clk_store.clear()
        for name in clocks + sorted(set(model["clocks"]) - set(clocks)):
            values = model["clocks"].get(name, {})
            self.clk_store.append([name] + [values.get(k, "") for k, _, _ in st.CLOCK_FIELDS])
        for key, entry in self.rule_entries.items():
            entry.set_text(model["rules"].get(key, ""))
        self.ex_store.clear()
        for ex in model["exceptions"]:
            row = [ex["type"], ex["value"], ex["check"]]
            for end in ("from", "through", "to"):
                row += list(ex[end]) if ex[end] else ["", ""]
            self.ex_store.append(row)
        self._filling = False
        unit = self.tab.unit()
        self.unit_note.set_markup(markup(f"Times are in {unit}, as in the rest of this platform's SDC.", MUTED))
        self.rule_units.set_markup(markup(
            f"Times in {unit}; load and capacitance in the library's unit "
            f"({'fF' if unit == 'ps' else 'pF'} on this platform).", MUTED))
        self.show_state(model, unknown)

    def model(self):
        model = st.empty()
        for row in self.clk_store:
            values = {k: row[i].strip() for i, (k, _, _) in enumerate(st.CLOCK_FIELDS, 1) if row[i].strip()}
            if values:
                model["clocks"][row[0]] = values
        model["rules"] = {k: e.get_text().strip() for k, e in self.rule_entries.items() if e.get_text().strip()}
        for row in self.ex_store:
            ex = {"type": row[0], "value": row[1].strip(), "check": row[2], "from": None, "through": None,
                  "to": None}
            for n, end in enumerate(("from", "through", "to")):
                kind, names = row[3 + 2 * n], " ".join(row[4 + 2 * n].split())
                if kind:
                    ex[end] = (kind, names)
            model["exceptions"].append(ex)
        return model

    def on_edit(self):
        """Write the page's settings into the SDC text (unsaved) after every change."""
        if self._filling:
            return
        model = self.model()
        text = self.tab.sdc_text()
        new = st.set_block(text, model)
        if new != text:
            self.tab.replace_sdc(new)
        self.show_state(model, st.parse(new)[1])

    def show_state(self, model, unknown):
        probs = st.problems(model, self.tab.clock_names())
        probs += [f"Line in the block the page can't read (kept out on the next change): {u}" for u in unknown]
        self.problem_label.set_markup(markup("\n".join(probs), ERROR) if probs else "")
        outside = st.outside(self.tab.sdc_text())
        self.outside_label.set_markup(markup(
            "Also set in the SDC outside this page's block (change them on the Constraints page):\n" +
            "\n".join(f"  line {n}: {t}" for n, t in outside), MUTED) if outside else "")
        self.tab.dirty()

    # ---- edits ----
    def on_clock_edit(self, _cell, path, text, col):
        self.clk_store[path][col] = text.strip()
        self.on_edit()

    def on_cell(self, _cell, path, value, col):
        self.ex_store[path][col] = value
        # A false path has no value; the others need one.
        if col == 0 and value == "false_path":
            self.ex_store[path][1] = ""
        self.on_edit()

    def on_add(self, *_):
        # Starts with no names: a false path from a whole clock would switch off its timing, so nothing is guessed.
        self.ex_store.append(["false_path", "", "", "ports", "", "", "", "", ""])
        self.on_edit()

    def on_remove(self, *_):
        model, it = self.ex_tv.get_selection().get_selected()
        if it:
            model.remove(it)
            self.on_edit()

    # ---- state ----
    def is_dirty(self):
        return self.tab.sdc_dirty()

    def save(self):
        self.tab.on_sdc_save()  # refuses while the timing block has problems

    def revert(self):
        self.tab.load_sdc(force=True)
        self.load()
