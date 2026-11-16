"""Built-in waveform viewer: a signal tree, the chosen signals drawn over time, a cursor, zoom and radix.

Reads VCD through vcd.py. One-bit signals are drawn as high/low lines (x red, z amber); buses as hexagon cells
with their value written in. Click to place the cursor (its time and every value at it are shown), scroll the
wheel with Ctrl to zoom around the pointer, and without it to pan. For very large dumps, or for FST, use
"Open in GTKWave".
"""

import math

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # noqa: E402

import theme  # noqa: E402
import vcd  # noqa: E402

ROW = 26          # pixels per signal row
NAMES = 210       # width of the name column
RULER = 24        # height of the time ruler


def rgba(name, alpha=1.0):
    c = Gdk.RGBA()
    c.parse(theme.color(name))
    return c.red, c.green, c.blue, alpha


class WaveView(Gtk.Box):
    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.waves = None
        self.shown = []           # signal paths in display order
        self.radix = "hex"
        self.t0, self.t1 = 0, 1   # visible time window
        self.cursor = None

        bar = Gtk.Box(spacing=6)
        self.file_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.START)
        self.file_label.get_style_context().add_class("muted")
        self.radix_cb = Gtk.ComboBoxText()
        for r, label in (("hex", "Hex"), ("dec", "Decimal"), ("bin", "Binary")):
            self.radix_cb.append(r, label)
        self.radix_cb.set_active_id("hex")
        self.radix_cb.connect("changed", lambda c: (setattr(self, "radix", c.get_active_id()), self.area.queue_draw()))
        bar.pack_start(self.file_label, True, True, 0)
        for label, handler in (("Zoom in", lambda *_: self.zoom(0.5)), ("Zoom out", lambda *_: self.zoom(2.0)),
                               ("Fit", lambda *_: self.fit()), ("Remove", lambda *_: self.remove_selected()),
                               ("Clear", lambda *_: self.set_shown([]))):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            bar.pack_start(b, False, False, 0)
        bar.pack_start(self.radix_cb, False, False, 0)
        self.pack_start(bar, False, False, 0)

        paned = Gtk.Paned(position=240)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.search = Gtk.SearchEntry(placeholder_text="Find a signal…")
        self.search.connect("search-changed", lambda *_: self.fill_tree())
        left.pack_start(self.search, False, False, 0)
        self.tstore = Gtk.TreeStore(str, str)   # label, signal path ("" for a scope)
        self.tree = Gtk.TreeView(model=self.tstore, headers_visible=False)
        self.tree.append_column(Gtk.TreeViewColumn("Signal", Gtk.CellRendererText(), text=0))
        self.tree.connect("row-activated", self.on_tree_row)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.tree)
        left.pack_start(sw, True, True, 0)
        add = Gtk.Button(label="Add selected")
        add.connect("clicked", lambda *_: self.add_selected())
        left.pack_start(add, False, False, 0)
        left.pack_start(Gtk.Label(label="Double-click a signal, or a scope for all its signals.", wrap=True,
                                  xalign=0), False, False, 0)
        paned.pack1(left, False, False)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.SCROLL_MASK
                             | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_press)
        self.area.connect("scroll-event", self.on_scroll)
        self.selected_row = None
        self.scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.add(self.area)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        right.get_style_context().add_class("preview")
        right.pack_start(self.scroll, True, True, 0)
        self.cursor_label = Gtk.Label(xalign=0, selectable=True)
        self.cursor_label.get_style_context().add_class("muted")
        right.pack_start(self.cursor_label, False, False, 4)
        paned.pack2(right, True, False)
        self.pack_start(paned, True, True, 0)

    # ---------- data ----------
    def load(self, path, show=None):
        """Open a VCD file; show: signal paths to display first (default: the top scope's ports, up to 16).
        Raises ValueError from vcd.parse."""
        self.waves = vcd.parse(path)
        self.file_label.set_text(f"{path}  ·  {len(self.waves.signals)} signals, {self.waves.end} × "
                                 f"{self.waves.timescale}")
        self.fill_tree()
        if show is None:
            scopes = self.waves.scopes()
            first = min(scopes, key=len) if scopes else ()
            show = [p for p in scopes.get(first, []) if self.waves.signals[p].kind != "parameter"][:16]
        self.set_shown([p for p in show if p in self.waves.signals])
        self.fit()
        # A long run opens on its first 40 clock cycles, where the edges can still be told apart.
        clocks = [sig for sig in self.waves.signals.values() if sig.width == 1 and len(sig.times) > 200]
        if clocks:
            busiest = max(clocks, key=lambda sig: len(sig.times))
            self.t1 = max(busiest.times[80], 1)
            self.area.queue_draw()

    def set_shown(self, paths):
        self.shown = list(dict.fromkeys(paths))
        self.selected_row = None
        self.area.set_size_request(-1, RULER + ROW * max(1, len(self.shown)) + 8)
        self.area.queue_draw()
        self.update_cursor_label()

    def fill_tree(self):
        self.tstore.clear()
        if not self.waves:
            return
        q = self.search.get_text().strip().lower()
        nodes = {}
        for scope, paths in sorted(self.waves.scopes().items()):
            parent = None
            for depth in range(1, len(scope) + 1):
                key = scope[:depth]
                if key not in nodes:
                    nodes[key] = self.tstore.append(nodes.get(key[:-1]), [key[-1], ""])
                parent = nodes[key]
            for p in paths:
                sig = self.waves.signals[p]
                if q and q not in p.lower():
                    continue
                width = f" [{sig.width - 1}:0]" if sig.width > 1 else ""
                self.tstore.append(parent, [sig.name + width, p])
        if q or len(self.waves.signals) < 200:
            self.tree.expand_all()
        else:
            self.tree.expand_row(Gtk.TreePath.new_first(), False)

    def on_tree_row(self, _tv, tpath, _col):
        it = self.tstore.get_iter(tpath)
        path = self.tstore[it][1]
        if path:
            self.set_shown(self.shown + [path])
        else:
            child = self.tstore.iter_children(it)
            add = []
            while child:
                if self.tstore[child][1]:
                    add.append(self.tstore[child][1])
                child = self.tstore.iter_next(child)
            self.set_shown(self.shown + add)

    def add_selected(self):
        model, rows = self.tree.get_selection().get_selected_rows()
        self.set_shown(self.shown + [model[r][1] for r in rows if model[r][1]])

    def remove_selected(self):
        if self.selected_row is not None and self.selected_row < len(self.shown):
            self.set_shown(self.shown[:self.selected_row] + self.shown[self.selected_row + 1:])

    # ---------- view ----------
    def fit(self):
        end = self.waves.end if self.waves else 1
        self.t0, self.t1 = 0, max(end, 1)
        self.area.queue_draw()

    def zoom(self, factor, around=None):
        mid = around if around is not None else (self.t0 + self.t1) / 2
        span = max((self.t1 - self.t0) * factor, 2)
        self.t0 = max(0, mid - (mid - self.t0) * factor)
        self.t1 = self.t0 + span
        self.area.queue_draw()

    def x_of(self, t, width):
        return NAMES + (t - self.t0) / (self.t1 - self.t0) * (width - NAMES - 8)

    def t_of(self, x, width):
        return self.t0 + (x - NAMES) / (width - NAMES - 8) * (self.t1 - self.t0)

    def on_scroll(self, widget, event):
        width = widget.get_allocated_width()
        ok, dx, dy = event.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(event.direction, 0)
        if event.state & Gdk.ModifierType.CONTROL_MASK:
            self.zoom(1.25 if dy > 0 else 0.8, around=self.t_of(event.x, width))
            return True
        if event.state & Gdk.ModifierType.SHIFT_MASK:
            shift = (self.t1 - self.t0) * 0.1 * (1 if dy > 0 else -1)
            self.t0, self.t1 = max(0, self.t0 + shift), max(0, self.t0 + shift) + (self.t1 - self.t0)
            self.area.queue_draw()
            return True
        return False

    def on_press(self, widget, event):
        width = widget.get_allocated_width()
        row = int((event.y - RULER) // ROW)
        self.selected_row = row if 0 <= row < len(self.shown) else None
        if event.x > NAMES:
            self.cursor = max(0, round(self.t_of(event.x, width)))
        self.update_cursor_label()
        self.area.queue_draw()
        return True

    def update_cursor_label(self):
        if self.cursor is None or not self.waves:
            self.cursor_label.set_text("Click the waves to place the cursor. Ctrl+wheel zooms, Shift+wheel pans.")
            return
        unit = self.waves.unit()
        self.cursor_label.set_text(f"Cursor at {self.cursor * unit[0]} {unit[1]}")

    # ---------- drawing ----------
    def on_draw(self, widget, cr):
        width = widget.get_allocated_width()
        cr.set_source_rgba(*rgba("surface"))
        cr.paint()
        if not self.waves:
            return False
        layout = widget.create_pango_layout("")
        layout.set_font_description(Pango.FontDescription("monospace 8.5"))
        self.draw_ruler(cr, layout, width)
        for row, path in enumerate(self.shown):
            y = RULER + row * ROW
            if row == self.selected_row:
                cr.set_source_rgba(*rgba("accent_soft"))
                cr.rectangle(0, y, width, ROW)
                cr.fill()
            sig = self.waves.signals.get(path)
            if sig is None:
                continue
            value = sig.value_at(self.cursor) if self.cursor is not None else None
            name = sig.name + (f" = {vcd.format_value(value, sig.width, self.radix)}" if value is not None else "")
            cr.set_source_rgba(*rgba("text"))
            layout.set_text(name, -1)
            layout.set_width((NAMES - 10) * Pango.SCALE)
            layout.set_ellipsize(Pango.EllipsizeMode.START)
            cr.move_to(6, y + 6)
            from gi.repository import PangoCairo
            PangoCairo.show_layout(cr, layout)
            layout.set_width(-1)
            self.draw_signal(cr, layout, sig, y, width)
            cr.set_source_rgba(*rgba("border"))
            cr.set_line_width(0.5)
            cr.move_to(0, y + ROW)
            cr.line_to(width, y + ROW)
            cr.stroke()
        if self.cursor is not None and self.t0 <= self.cursor <= self.t1:
            x = self.x_of(self.cursor, width)
            cr.set_source_rgba(*rgba("warning"))
            cr.set_line_width(1.2)
            cr.move_to(x, 0)
            cr.line_to(x, RULER + ROW * len(self.shown))
            cr.stroke()
        return False

    def draw_ruler(self, cr, layout, width):
        from gi.repository import PangoCairo
        span = self.t1 - self.t0
        step = 10 ** math.floor(math.log10(max(span / 8, 1)))
        for m in (1, 2, 5, 10):
            if span / (step * m) <= 10:
                step *= m
                break
        unit = self.waves.unit()
        t = math.ceil(self.t0 / step) * step
        cr.set_line_width(0.5)
        while t <= self.t1:
            x = self.x_of(t, width)
            cr.set_source_rgba(*rgba("border"))
            cr.move_to(x, RULER - 6)
            cr.line_to(x, RULER + ROW * len(self.shown))
            cr.stroke()
            cr.set_source_rgba(*rgba("text_muted"))
            layout.set_text(f"{t * unit[0]:g}{unit[1]}", -1)
            cr.move_to(x + 2, 3)
            PangoCairo.show_layout(cr, layout)
            t += step

    def draw_signal(self, cr, layout, sig, y, width):
        from gi.repository import PangoCairo
        top, bottom, mid = y + 5, y + ROW - 5, y + ROW / 2
        i = max(sig.index_at(self.t0), 0)
        cr.set_line_width(1.3)
        while i < len(sig.times):
            t = sig.times[i]
            if t > self.t1:
                break
            nxt = sig.times[i + 1] if i + 1 < len(sig.times) else self.waves.end
            x0, x1 = max(self.x_of(t, width), NAMES), min(self.x_of(max(nxt, t), width), width - 8)
            value = sig.values[i]
            if x1 >= x0:
                unknown = "x" in value
                hiz = "z" in value and not unknown
                color = rgba("danger") if unknown else rgba("warning") if hiz else rgba("success")
                cr.set_source_rgba(*color)
                if sig.width == 1 and sig.kind != "real":
                    level = top if value == "1" else bottom if value == "0" else mid
                    cr.move_to(x0, level)
                    cr.line_to(x1, level)
                    if i + 1 < len(sig.times) and sig.times[i + 1] <= self.t1:
                        nv = sig.values[i + 1]
                        cr.line_to(x1, top if nv == "1" else bottom if nv == "0" else mid)
                    cr.stroke()
                else:
                    slant = min(3, (x1 - x0) / 2)
                    cr.move_to(x0, mid)
                    cr.line_to(x0 + slant, top)
                    cr.line_to(x1 - slant, top)
                    cr.line_to(x1, mid)
                    cr.line_to(x1 - slant, bottom)
                    cr.line_to(x0 + slant, bottom)
                    cr.close_path()
                    cr.stroke()
                    text = vcd.format_value(value, sig.width, self.radix) if sig.kind != "real" else value
                    layout.set_text(text, -1)
                    tw, th = layout.get_pixel_size()
                    if tw + 8 < x1 - x0:
                        cr.set_source_rgba(*rgba("text"))
                        cr.move_to(x0 + (x1 - x0 - tw) / 2, mid - th / 2)
                        PangoCairo.show_layout(cr, layout)
            i += 1
