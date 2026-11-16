"""A code editor widget for Verilog, SystemVerilog, Tcl, SDC and makefiles, built on Gtk.TextView.

It adds what a plain text view lacks: line numbers, syntax colors, undo and redo, auto-indent, find, go to line,
comment toggling, and marks for lint problems. (GtkSourceView would give the same, but it is one more system
package; this keeps the app's dependencies to GTK itself.)
"""

import re

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

import theme  # noqa: E402

VERILOG_KEYWORDS = (
    "module endmodule input output inout wire reg logic assign always always_ff always_comb always_latch if else "
    "case casez casex endcase begin end for generate endgenerate genvar parameter localparam function endfunction "
    "task endtask initial typedef struct enum import package endpackage interface endinterface assert assume "
    "cover property endproperty return while repeat forever integer real bit signed unsigned posedge negedge or "
    "and not default unique priority modport packed union automatic static const int byte shortint longint "
    "string void time event wait fork join disable final bind sequence endsequence class endclass program "
    "endprogram covergroup endgroup tri supply0 supply1 defparam specify endspecify primitive endprimitive")
TCL_KEYWORDS = ("set proc if else elseif foreach for while return puts expr source global variable lappend list "
                "lindex llength string regexp regsub catch error switch break continue incr unset array dict "
                "create_clock create_generated_clock set_input_delay set_output_delay set_false_path "
                "set_multicycle_path set_max_delay set_min_delay set_clock_groups set_clock_uncertainty "
                "set_clock_latency set_clock_transition set_driving_cell set_load set_max_fanout "
                "set_max_transition set_max_capacitance get_ports get_clocks get_pins get_cells get_nets "
                "all_inputs all_outputs all_clocks current_design")
MAKE_KEYWORDS = "export include ifeq ifneq ifdef ifndef else endif define endef override"

LANGUAGES = {
    "verilog": {"keywords": VERILOG_KEYWORDS, "comment": "//", "block": (r"/\*", r"\*/"),
                "extra": [("pre", r"`\w+"), ("sys", r"\$\w+")]},
    "tcl": {"keywords": TCL_KEYWORDS, "comment": "#", "block": None, "extra": [("sys", r"\$\{?[\w:]+\}?")]},
    "make": {"keywords": MAKE_KEYWORDS, "comment": "#", "block": None, "extra": [("sys", r"\$\([^)]*\)")]},
    "text": {"keywords": "", "comment": "#", "block": None, "extra": []},
}
SUFFIX_LANGUAGE = {".v": "verilog", ".sv": "verilog", ".vh": "verilog", ".svh": "verilog", ".tcl": "tcl",
                   ".sdc": "tcl", ".xdc": "tcl", ".mk": "make", ".sby": "text", ".pcf": "text", ".lpf": "text",
                   ".cst": "text"}
MAX_HIGHLIGHT = 600_000   # characters; larger files are shown without colors


def language_of(path):
    name = str(path)
    if name.endswith("Makefile"):
        return "make"
    return SUFFIX_LANGUAGE.get("." + name.rsplit(".", 1)[-1].lower() if "." in name else "", "text")


class CodeView(Gtk.Box):
    """The editor: self.view is the text view, self.buffer its buffer. Emits nothing itself; connect to the
    buffer's "modified-changed" to follow unsaved state."""

    def __init__(self, language="verilog"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.language = language
        self.view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.NONE, left_margin=8, top_margin=4)
        self.view.get_style_context().add_class("code-view")
        self.buffer = self.view.get_buffer()
        self.problems = {}            # line (0-based): message
        self._undo, self._redo, self._in_undo, self._timer = [], [], False, None
        self.view.set_border_window_size(Gtk.TextWindowType.LEFT, 52)
        self.view.connect_after("draw", self.on_draw)
        self.view.connect("key-press-event", self.on_key)
        self.view.connect("query-tooltip", self.on_tooltip)
        self.view.set_has_tooltip(True)
        self.buffer.connect("insert-text", self.on_insert)
        self.buffer.connect("delete-range", self.on_delete)
        self.buffer.connect("changed", self.on_changed)
        tags = (("kw", "accent", 600), ("comment", "text_muted", None), ("str", "success", None),
                ("num", "warning", None), ("pre", "info", None), ("sys", "info", None))
        for name, hue, weight in tags:
            tag = self.buffer.create_tag(name, foreground=theme.color(hue))
            if weight:
                tag.set_property("weight", weight)
            if name == "comment":
                tag.set_property("style", Pango.Style.ITALIC)
        self.buffer.create_tag("problem", underline=Pango.Underline.ERROR)
        self.buffer.create_tag("found", background=theme.color("accent_soft"))
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(self.view)
        self.pack_start(sw, True, True, 0)

        # Find bar, hidden until Ctrl+F.
        self.find_bar = Gtk.Box(spacing=6, margin=4)
        self.find_entry = Gtk.SearchEntry(placeholder_text="Find…", hexpand=True)
        self.find_entry.connect("search-changed", lambda *_: self.find(self.find_entry.get_text(), restart=True))
        self.find_entry.connect("activate", lambda *_: self.find(self.find_entry.get_text()))
        self.find_entry.connect("stop-search", lambda *_: self.hide_find())
        self.find_count = Gtk.Label()
        nxt = Gtk.Button(label="Next")
        nxt.connect("clicked", lambda *_: self.find(self.find_entry.get_text()))
        close = Gtk.Button(label="Close")
        close.connect("clicked", lambda *_: self.hide_find())
        for w, expand in ((self.find_entry, True), (self.find_count, False), (nxt, False), (close, False)):
            self.find_bar.pack_start(w, expand, expand, 0)
        self.find_bar.set_no_show_all(True)
        self.pack_start(self.find_bar, False, False, 0)

    # ---------- text ----------
    def set_text(self, text):
        """Load text: clears undo and the unsaved flag."""
        self._in_undo = True
        self.buffer.set_text(text)
        self._in_undo = False
        self._undo, self._redo = [], []
        self.buffer.set_modified(False)
        self.buffer.place_cursor(self.buffer.get_start_iter())
        self.highlight()

    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def goto(self, line, column=0):
        """Put the cursor on a 1-based line and scroll to it."""
        it = self.buffer.get_iter_at_line(max(0, min(line - 1, self.buffer.get_line_count() - 1)))
        it.forward_chars(max(0, min(column, it.get_chars_in_line() - 1)))
        self.buffer.place_cursor(it)
        # Scrolled from the cursor mark once the view is laid out: an iterator would not survive an edit.
        GLib.idle_add(lambda: self.view.scroll_to_mark(self.buffer.get_insert(), 0.1, True, 0, 0.4) and False)
        self.view.grab_focus()

    def cursor(self):
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        return it.get_line() + 1, it.get_line_offset() + 1

    # ---------- undo ----------
    def on_insert(self, buf, it, text, _length):
        if not self._in_undo:
            self._record(("ins", it.get_offset(), text))

    def on_delete(self, buf, start, end):
        if not self._in_undo:
            self._record(("del", start.get_offset(), buf.get_text(start, end, False)))

    def _record(self, action):
        last = self._undo[-1] if self._undo else None
        # Typing one character after another is one undo step until a space or a new line.
        if last and last[0] == "ins" and action[0] == "ins" and len(action[2]) == 1 \
                and action[1] == last[1] + len(last[2]) and not action[2].isspace() and "\n" not in last[2]:
            self._undo[-1] = ("ins", last[1], last[2] + action[2])
        else:
            self._undo.append(action)
        self._redo = []
        del self._undo[:-2000]

    def _apply(self, action, invert):
        kind, offset, text = action
        self._in_undo = True
        it = self.buffer.get_iter_at_offset(offset)
        if (kind == "ins") != invert:
            self.buffer.insert(it, text)
            self.buffer.place_cursor(self.buffer.get_iter_at_offset(offset + len(text)))
        else:
            self.buffer.delete(it, self.buffer.get_iter_at_offset(offset + len(text)))
            self.buffer.place_cursor(self.buffer.get_iter_at_offset(offset))
        self._in_undo = False
        self.view.scroll_mark_onscreen(self.buffer.get_insert())

    def undo(self):
        if self._undo:
            action = self._undo.pop()
            self._apply(action, invert=True)
            self._redo.append(action)

    def redo(self):
        if self._redo:
            action = self._redo.pop()
            self._apply(action, invert=False)
            self._undo.append(action)

    # ---------- keys ----------
    def on_key(self, _view, event):
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        key = Gdk.keyval_name(event.keyval) or ""
        if ctrl and key.lower() == "z":
            self.redo() if shift else self.undo()
        elif ctrl and key.lower() == "y":
            self.redo()
        elif ctrl and key.lower() == "f":
            self.show_find()
        elif ctrl and key == "slash":
            self.toggle_comment()
        elif key in ("Return", "KP_Enter") and not ctrl:
            self.new_line()
        elif key == "Tab" and not ctrl:
            self.buffer.insert_at_cursor("  ")
        else:
            return False
        return True

    def new_line(self):
        """Enter keeps the indent of the current line, and adds one level after begin, ( or {."""
        it = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        start = self.buffer.get_iter_at_line(it.get_line())
        line = self.buffer.get_text(start, it, False)
        indent = re.match(r"[ \t]*", line).group(0)
        if re.search(r"(\bbegin|\(|\{)\s*$", line):
            indent += "  "
        self.buffer.insert_at_cursor("\n" + indent)
        self.view.scroll_mark_onscreen(self.buffer.get_insert())

    def toggle_comment(self):
        mark = LANGUAGES[self.language]["comment"]
        bounds = self.buffer.get_selection_bounds()
        first = (bounds[0] if bounds else self.buffer.get_iter_at_mark(self.buffer.get_insert())).get_line()
        last = (bounds[1] if bounds else self.buffer.get_iter_at_mark(self.buffer.get_insert())).get_line()
        lines = []
        for n in range(first, last + 1):
            a = self.buffer.get_iter_at_line(n)
            b = a.copy()
            if not b.ends_line():
                b.forward_to_line_end()
            lines.append(self.buffer.get_text(a, b, False))
        commented = all(ln.lstrip().startswith(mark) or not ln.strip() for ln in lines)
        self.buffer.begin_user_action()
        for n, ln in zip(range(first, last + 1), lines):
            a = self.buffer.get_iter_at_line(n)
            b = a.copy()
            if not b.ends_line():
                b.forward_to_line_end()
            new = re.sub(r"^(\s*)" + re.escape(mark) + r" ?", r"\1", ln, count=1) if commented else \
                (re.sub(r"^(\s*)", r"\1" + mark + " ", ln, count=1) if ln.strip() else ln)
            if new != ln:
                self.buffer.delete(a, b)
                self.buffer.insert(self.buffer.get_iter_at_line(n), new)
        self.buffer.end_user_action()

    # ---------- colors ----------
    def on_changed(self, *_):
        if self._timer:
            GLib.source_remove(self._timer)
        self._timer = GLib.timeout_add(200, self.highlight)
        self.view.queue_draw()

    def highlight(self):
        self._timer = None
        buf, text = self.buffer, self.text()
        start, end = buf.get_start_iter(), buf.get_end_iter()
        for tag in ("kw", "comment", "str", "num", "pre", "sys"):
            buf.remove_tag_by_name(tag, start, end)
        if len(text) > MAX_HIGHLIGHT:
            return False
        lang = LANGUAGES[self.language]
        parts = [("comment", re.escape(lang["comment"]) + r"[^\n]*"), ("str", r'"(?:\\.|[^"\\\n])*"')]
        if lang["block"]:
            parts.insert(0, ("comment2", lang["block"][0] + r".*?" + lang["block"][1]))
        parts += lang["extra"]
        parts.append(("num", r"\b\d+'[sS]?[bBoOdDhH][0-9a-fA-FxXzZ_?]+|\b\d[\d_]*(?:\.\d+)?\b|'[01xXzZ]\b"))
        if lang["keywords"]:
            parts.append(("kw", r"\b(?:" + "|".join(lang["keywords"].split()) + r")\b"))
        rx = re.compile("|".join(f"(?P<{name}>{pat})" for name, pat in parts), re.S)
        for m in rx.finditer(text):
            tag = m.lastgroup.rstrip("2")
            buf.apply_tag_by_name(tag, buf.get_iter_at_offset(m.start()), buf.get_iter_at_offset(m.end()))
        return False

    def set_problems(self, problems):
        """Mark lines with lint problems: {1-based line: message}."""
        buf = self.buffer
        buf.remove_tag_by_name("problem", buf.get_start_iter(), buf.get_end_iter())
        self.problems = {}
        for line, message in problems.items():
            if 1 <= line <= buf.get_line_count():
                self.problems[line - 1] = message
                a = buf.get_iter_at_line(line - 1)
                b = a.copy()
                if not b.ends_line():
                    b.forward_to_line_end()
                buf.apply_tag_by_name("problem", a, b)
        self.view.queue_draw()

    def on_tooltip(self, view, x, y, _keyboard, tooltip):
        bx, by = view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, x, y)
        it, _ = view.get_line_at_y(by)
        message = self.problems.get(it.get_line())
        if message:
            tooltip.set_text(message)
            return True
        return False

    # ---------- line numbers ----------
    def on_draw(self, view, cr):
        window = view.get_window(Gtk.TextWindowType.LEFT)
        if window is None or not Gtk.cairo_should_draw_window(cr, window):
            return False
        cr.save()
        Gtk.cairo_transform_to_window(cr, view, window)
        width = view.get_border_window_size(Gtk.TextWindowType.LEFT)
        bg, fg, bad = Gdk.RGBA(), Gdk.RGBA(), Gdk.RGBA()
        bg.parse(theme.color("surface_alt"))
        fg.parse(theme.color("text_muted"))
        bad.parse(theme.color("danger"))
        # Only the gutter: an unclipped paint would cover the text drawn in the same pass.
        cr.rectangle(0, 0, width, window.get_height())
        cr.clip()
        cr.set_source_rgba(bg.red, bg.green, bg.blue, 1)
        cr.paint()
        visible = view.get_visible_rect()
        it, _ = view.get_line_at_y(visible.y)
        layout = view.create_pango_layout("")
        current = self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_line()
        while True:
            y, height = view.get_line_yrange(it)
            _, wy = view.buffer_to_window_coords(Gtk.TextWindowType.LEFT, 0, y)
            line = it.get_line()
            layout.set_text(str(line + 1), -1)
            w, _h = layout.get_pixel_size()
            color = bad if line in self.problems else fg
            cr.set_source_rgba(color.red, color.green, color.blue, 1.0 if line in (current,) or line in self.problems
                               else 0.75)
            cr.move_to(width - w - 10, wy)
            PangoCairo.show_layout(cr, layout)
            if y + height > visible.y + visible.height or not it.forward_line():
                break
        cr.restore()
        return False

    # ---------- find ----------
    def show_find(self):
        self.find_bar.show()
        for child in self.find_bar.get_children():
            child.show()
        bounds = self.buffer.get_selection_bounds()
        if bounds and bounds[0].get_line() == bounds[1].get_line():
            self.find_entry.set_text(self.buffer.get_text(bounds[0], bounds[1], False))
        self.find_entry.grab_focus()

    def hide_find(self):
        self.find_bar.hide()
        self.buffer.remove_tag_by_name("found", self.buffer.get_start_iter(), self.buffer.get_end_iter())
        self.view.grab_focus()

    def find(self, needle, restart=False):
        """Select the next match after the cursor (wrapping), and tint every match; returns the match count."""
        buf = self.buffer
        buf.remove_tag_by_name("found", buf.get_start_iter(), buf.get_end_iter())
        if not needle:
            self.find_count.set_text("")
            return 0
        text = self.text()
        hits = [m.start() for m in re.finditer(re.escape(needle), text, re.I)]
        for h in hits:
            buf.apply_tag_by_name("found", buf.get_iter_at_offset(h), buf.get_iter_at_offset(h + len(needle)))
        self.find_count.set_text(f"{len(hits)} match{'es' if len(hits) != 1 else ''}")
        if not hits:
            return 0
        bounds = buf.get_selection_bounds()
        here = (bounds[0].get_offset() if restart and bounds else
                bounds[1].get_offset() if bounds else buf.get_iter_at_mark(buf.get_insert()).get_offset())
        nxt = next((h for h in hits if h >= here), hits[0])
        a, b = buf.get_iter_at_offset(nxt), buf.get_iter_at_offset(nxt + len(needle))
        buf.select_range(a, b)
        self.view.scroll_to_iter(a, 0.1, True, 0, 0.4)
        return len(hits)
