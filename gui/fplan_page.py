"""Floorplan tab of the ASIC section: plan the chip's floorplan before the flow runs.

Left: the area budget (cell area, utilization or a fixed die, aspect ratio, margin) with the core and die it
gives, and the checks of the plan with one-click fixes (see fplan.py): power straps fit, IO pins fit, rows,
utilization, shape, macros. Right: the plan drawn to scale: die, core, cell rows, power straps, macros (drag them),
and the last floorplan run in grey for comparison.

The plan is a what-if until Apply: Apply writes it into the design through the Inputs → Floorplan page (the config
variables and the macro placement file), so the two never disagree. Run floorplan applies and runs the stage.
"""

import math

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import floorplan as fp  # noqa: E402
import fplan  # noqa: E402
import lef  # noqa: E402
import theme  # noqa: E402
from input_page import MUTED, card, hint_label, markup  # noqa: E402

LEVEL_COLOR = {"ok": "success", "warn": "warning", "error": "danger"}
LEVEL_TEXT = {"ok": "The plan passes every check", "warn": "The plan has warnings",
              "error": "The plan will not build as it is"}


def rgba(name, alpha=1.0):
    c = Gdk.RGBA()
    c.parse(theme.color(name))
    return c.red, c.green, c.blue, alpha


class FloorplanPlanner:
    """The Floorplan tab. self.s holds the plan's settings; nothing is written until apply()."""

    def __init__(self, app):
        self.app = app
        self.s = {"mode": "util", "util": 50.0, "aspect": 1.0, "margin": 2.0, "die_w": 100.0, "die_h": 100.0,
                  "area": 0.0}
        self.area_from_synth = False
        self.placements = {}          # {instance: (x, y, orient)}
        self.sizes, self.masters, self.halo = {}, {}, (0.0, 0.0)
        self.straps, self.layers, self.pin_layers, self.site, self.pins = [], [], [], None, 0
        self.last_run = None
        self.plan, self.checks = None, []
        self.cfg = None               # the config the plan was loaded from
        self.dirty = False
        self.drag = None
        self._filling = False
        self.view = (1.0, 0.0, 0.0)   # scale and offsets of the drawing, set when it is drawn
        self.widget = self.build()

    # ---------- layout ----------
    def build(self):
        outer = Gtk.Box()
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=14)

        budget = card("Area budget", "Size the core from the cell area and a target utilization, or try a fixed "
                                     "die. Nothing changes in the design until you press Apply.")
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        self.mode_cb = Gtk.ComboBoxText()
        self.mode_cb.append("util", "Size from utilization")
        self.mode_cb.append("fixed", "Fixed die")
        self.mode_cb.connect("changed", lambda cb: self.set(mode=cb.get_active_id()))
        self.area = Gtk.Entry(width_chars=10, tooltip_text="Standard cells and macros, in square microns. Taken from "
                                                           "synthesis when it has run; type an estimate before.")
        self.area.connect("changed", lambda e: self.on_number("area", e.get_text()))
        self.area_note = Gtk.Label(xalign=0)
        self.area_note.get_style_context().add_class("muted")
        self.util = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 1, 95, 1)
        self.util.set_hexpand(True)
        self.util.set_value_pos(Gtk.PositionType.RIGHT)
        self.util.connect("value-changed", lambda w: self.set(util=w.get_value()))
        self.aspect = Gtk.SpinButton.new_with_range(0.1, 10, 0.05)
        self.aspect.set_digits(2)
        self.aspect.connect("value-changed", lambda w: self.set(aspect=w.get_value()))
        self.margin = Gtk.SpinButton.new_with_range(0, 500, 0.5)
        self.margin.set_digits(1)
        self.margin.connect("value-changed", lambda w: self.set(margin=w.get_value()))
        self.die_w = Gtk.SpinButton.new_with_range(1, 100000, 1)
        self.die_w.set_digits(1)
        self.die_w.connect("value-changed", lambda w: self.set(die_w=w.get_value()))
        self.die_h = Gtk.SpinButton.new_with_range(1, 100000, 1)
        self.die_h.set_digits(1)
        self.die_h.connect("value-changed", lambda w: self.set(die_h=w.get_value()))
        rows = [("Method", self.mode_cb, None), ("Cell area (µm²)", self.area, self.area_note),
                ("Utilization (%)", self.util, None), ("Aspect ratio (h / w)", self.aspect, None),
                ("Core margin (µm)", self.margin, None), ("Die width (µm)", self.die_w, None),
                ("Die height (µm)", self.die_h, None)]
        self.row_widgets = {}
        for r, (text, widget, note) in enumerate(rows):
            label = Gtk.Label(label=text, xalign=0)
            grid.attach(label, 0, r, 1, 1)
            grid.attach(widget, 1, r, 1, 1)
            if note:
                grid.attach(note, 2, r, 1, 1)
            self.row_widgets[text] = (label, widget)
        budget.pack_start(grid, False, False, 0)
        self.result = Gtk.Label(xalign=0, wrap=True)
        budget.pack_start(self.result, False, False, 0)
        left.pack_start(budget, False, False, 0)

        checks = card("Checks")
        self.verdict = Gtk.Label(xalign=0)
        self.verdict.get_style_context().add_class("card-title")
        checks.pack_start(self.verdict, False, False, 0)
        self.check_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER, min_content_height=160)
        sw.add(self.check_box)
        checks.pack_start(sw, True, True, 0)
        left.pack_start(checks, True, True, 0)

        row = Gtk.Box(spacing=6)
        for label, handler, tip, suggested in (
                ("Apply", lambda *_: self.apply(), "Write the plan into the design (config and macro placement)",
                 True),
                ("Run floorplan", lambda *_: self.run(), "Apply the plan and run the flow up to the floorplan stage",
                 False),
                ("Place macros", lambda *_: self.place_macros(), "A first legal macro placement along the core's "
                                                                 "left edge; drag them from there", False),
                ("Reset", lambda *_: self.refresh(force=True), "Load the plan from the design again", False)):
            b = Gtk.Button(label=label, tooltip_text=tip)
            if suggested:
                b.get_style_context().add_class("suggested-action")
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        left.pack_start(row, False, False, 0)
        self.state = hint_label("")
        left.pack_start(self.state, False, False, 0)
        left.set_size_request(430, -1)
        outer.pack_start(left, False, False, 0)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=12, margin=10)
        self.show = {}
        for key, label, on in (("straps", "Power straps", True), ("rows", "Cell rows", True),
                               ("last", "Last run", True)):
            c = Gtk.CheckButton(label=label, active=on)
            c.connect("toggled", lambda *_: self.canvas.queue_draw())
            bar.pack_start(c, False, False, 0)
            self.show[key] = c
        self.legend = Gtk.Label(xalign=1, hexpand=True)
        self.legend.get_style_context().add_class("muted")
        bar.pack_end(self.legend, True, True, 0)
        right.pack_start(bar, False, False, 0)
        self.canvas = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.canvas.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK |
                               Gdk.EventMask.POINTER_MOTION_MASK)
        self.canvas.connect("draw", self.on_draw)
        self.canvas.connect("button-press-event", self.on_press)
        self.canvas.connect("motion-notify-event", self.on_motion)
        self.canvas.connect("button-release-event", self.on_release)
        right.pack_start(self.canvas, True, True, 0)
        outer.pack_start(right, True, True, 0)
        return outer

    # ---------- the design ----------
    def inputs_page(self):
        return next((p for p in self.app.inputs.pages if p.name == "floorplan"), None)

    def refresh(self, force=False):
        """Load the plan from the design, unless it has unapplied changes for the same config."""
        inp, page = self.app.inputs, self.inputs_page()
        cfg = inp.cfg
        if not cfg or page is None or not inp.effective:
            self.cfg, self.plan, self.checks = None, None, []
            self.state.set_text("Pick a design in the side panel.")
            self.canvas.queue_draw()
            return
        if self.dirty and cfg == self.cfg and not force:
            self.load_context(inp, page)
            self.update()
            return
        self.cfg, self.dirty = cfg, False
        self.load_context(inp, page)
        s = self.s
        s["mode"] = page.mode()
        try:
            s["util"] = float(page.value("CORE_UTILIZATION") or 50)
            s["aspect"] = float(page.value("CORE_ASPECT_RATIO") or 1)
            s["margin"] = float((page.value("CORE_MARGIN") or "2").split()[0])
        except ValueError:
            pass
        die = fp.parse_box(page.value("DIE_AREA"))
        core = fp.parse_box(page.value("CORE_AREA"))
        if die:
            s["die_w"], s["die_h"] = die[2] - die[0], die[3] - die[1]
            if core:
                s["margin"] = round(core[0] - die[0], 3)
        elif s["area"]:
            est = fplan.plan("util", s["area"], s["util"], s["aspect"], s["margin"])
            if est:
                s["die_w"], s["die_h"] = round(est["die"][2], 1), round(est["die"][3], 1)
        self.placements = dict(page.placements)
        self.fill()
        self.update()

    def load_context(self, inp, page):
        """What the checks need from the design: cell area, macros, power straps, layers, pins, the last run."""
        eff = inp.effective
        area = page.synth_area()
        self.area_from_synth = bool(area)
        if area:
            self.s["area"] = area
        self.sizes, self.masters = dict(page.sizes), dict(page.macro_master)
        self.halo = page.channel() if hasattr(page, "channel") else (0.0, 0.0)
        self.site = page.site()
        self.last_run = page.last_run
        if page.last_run is None and self.app.stage_done().get("floorplan") and not getattr(self, "_reading", False):
            # A run finished since the Inputs page last looked: read its die and core (OpenROAD, in the background).
            self._reading, tries = True, [0]
            page.start_last_run_read()

            def arrived():
                tries[0] += 1
                if page.last_run is None and tries[0] < 100:
                    return True
                self._reading = False
                self.last_run = page.last_run
                self.canvas.queue_draw()
                return False
            GLib.timeout_add(300, arrived)
        try:
            self.straps = fplan.straps(inp.resolve(eff["PDN_TCL"]).read_text(errors="replace")) \
                if eff.get("PDN_TCL") else []
        except OSError:
            self.straps = []
        tech = (eff.get("TECH_LEF") or "").split()
        try:
            self.layers = lef.routing_layers(inp.resolve(tech[0])) if tech else []
        except OSError:
            self.layers = []
        self.pin_layers = [eff[v] for v in ("IO_PLACER_H", "IO_PLACER_V") if eff.get(v)]
        self.pins = sum((abs(b[0] - b[1]) + 1) if isinstance(b, tuple) else 1
                        for b in (inp.port_bits.get(n, "scalar") for _d, n in inp.ports or []))

    def fill(self):
        """Show the settings in the widgets."""
        self._filling = True
        s = self.s
        self.mode_cb.set_active_id(s["mode"])
        self.area.set_text(fp.fmt(s["area"]) if s["area"] else "")
        self.area.set_sensitive(not self.area_from_synth)
        self.area_note.set_text("from synthesis" if self.area_from_synth else "estimate")
        self.util.set_value(min(max(s["util"], 1), 95))
        self.aspect.set_value(s["aspect"])
        self.margin.set_value(s["margin"])
        self.die_w.set_value(s["die_w"])
        self.die_h.set_value(s["die_h"])
        self._filling = False
        self.show_mode()

    def show_mode(self):
        util = self.s["mode"] == "util"
        for text, (label, widget) in self.row_widgets.items():
            if text in ("Utilization (%)", "Aspect ratio (h / w)"):
                label.set_visible(util)
                widget.set_visible(util)
            elif text.startswith("Die "):
                label.set_visible(not util)
                widget.set_visible(not util)

    # ---------- the plan ----------
    def on_number(self, key, text):
        try:
            self.set(**{key: float(text)})
        except ValueError:
            pass

    def set(self, **values):
        """Change settings of the plan (mode, util, aspect, margin, die_w, die_h, area) and recompute."""
        if self._filling:
            return
        if values.get("mode") == "fixed" and self.s["mode"] == "util" and self.plan:
            # Start the fixed die from the die the utilization gave.
            self.s["die_w"] = round(self.plan["die"][2] - self.plan["die"][0], 1)
            self.s["die_h"] = round(self.plan["die"][3] - self.plan["die"][1], 1)
        self.s.update(values)
        self.dirty = True
        if "mode" in values or "die_w" in values and not self._filling:
            self.fill()
        self.update()

    def macro_area(self):
        return sum(w * h for w, h in (self.sizes.get(self.masters.get(n, ""), (0, 0)) for n in self.masters))

    def update(self):
        """Recompute the plan and its checks, and show them."""
        s = self.s
        self.plan = fplan.plan(s["mode"], s["area"], s["util"], s["aspect"], s["margin"], s["die_w"], s["die_h"],
                               self.site)
        self.checks = fplan.check(self.plan, s["area"], self.macro_area(), self.straps, self.layers, self.pins,
                                  self.pin_layers, self.site, self.placements, self.sizes, self.masters, self.halo,
                                  s["aspect"], s["mode"])
        if self.placements and s["mode"] == "util":
            self.checks.insert(0, {"level": "warn", "title": "Macros need a fixed die",
                                   "detail": "Macros are placed, but a core sized from utilization can end up a "
                                             "different size than drawn.",
                                   "fix": {"label": "Use a fixed die", "set": {"mode": "fixed"}}})
        p = self.plan
        if p:
            die = p["die"]
            util = f"{p['utilization']:.0f}%" if p["utilization"] is not None else "unknown"
            self.result.set_markup(
                f"Core <b>{p['core_w']:.1f} × {p['core_h']:.1f} µm</b>   Die <b>{die[2] - die[0]:.1f} × "
                f"{die[3] - die[1]:.1f} µm</b>   Utilization <b>{util}</b>")
        else:
            self.result.set_markup(markup("No floorplan yet.", MUTED))
        level = fplan.verdict(self.checks)
        self.verdict.set_markup(f'<span foreground="{theme.color(LEVEL_COLOR[level])}">{LEVEL_TEXT[level]}</span>')
        for child in self.check_box.get_children():
            self.check_box.remove(child)
        for c in sorted(self.checks, key=lambda c: ("error", "warn", "ok").index(c["level"])):
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title = Gtk.Label(xalign=0)
            title.set_markup(f'<span foreground="{theme.color(LEVEL_COLOR[c["level"]])}">●</span> '
                             f'<b>{GLib.markup_escape_text(c["title"])}</b>')
            detail = Gtk.Label(label=c["detail"], xalign=0, wrap=True, max_width_chars=52)
            detail.get_style_context().add_class("muted")
            row.pack_start(title, False, False, 0)
            row.pack_start(detail, False, False, 0)
            if c["fix"]:
                b = Gtk.Button(label=c["fix"]["label"], halign=Gtk.Align.START)
                b.connect("clicked", lambda _b, fix=c["fix"]: self.apply_fix(fix))
                row.pack_start(b, False, False, 0)
            self.check_box.pack_start(row, False, False, 0)
        self.check_box.show_all()
        self.state.set_text("The plan has changes that are not in the design yet: press Apply." if self.dirty else
                            "The plan matches the design.")
        self.legend.set_text(f"{self.pins} IO pins · {len(self.masters)} macros · "
                             f"{len(self.straps)} strap layers" if self.cfg else "")
        self.canvas.queue_draw()

    def apply_fix(self, fix):
        self.set(**fix["set"])
        self.fill()

    def place_macros(self):
        macros = {n: self.sizes[m] for n, m in self.masters.items() if m in self.sizes}
        if not macros:
            self.app.set_status("This design has no macros (after synthesis, with their LEFs in ADDITIONAL_LEFS).")
            return
        if not self.plan:
            return
        try:
            self.placements = fplan.pack_macros(macros, self.plan["core"], self.halo, self.site)
        except ValueError as e:
            self.app.message(f"{e}. Make the core larger first.")
            return
        self.dirty = True
        self.update()

    # ---------- writing the plan into the design ----------
    def apply(self):
        """Write the plan through the Inputs → Floorplan page and save it. True when it was saved."""
        page, s, p = self.inputs_page(), self.s, self.plan
        if page is None or p is None:
            self.app.set_status("There is no plan to apply.")
            return False
        if self.app.job and self.app.job.running():
            self.app.message("A run is going: apply the plan when it has finished.")
            return False
        if page.mode() != s["mode"]:
            page.mode_buttons[s["mode"]].set_active(True)
        if s["mode"] == "util":
            page.set_pending("CORE_UTILIZATION", fp.fmt(s["util"]))
            page.set_pending("CORE_ASPECT_RATIO", fp.fmt(s["aspect"]))
            page.set_pending("CORE_MARGIN", fp.fmt(s["margin"]))
        else:
            page.set_pending("DIE_AREA", " ".join(fp.fmt(v) for v in p["die"]))
            page.set_pending("CORE_AREA", " ".join(fp.fmt(v) for v in (s["margin"], s["margin"],
                                                                         s["die_w"] - s["margin"],
                                                                         s["die_h"] - s["margin"])))
        if self.placements != page.placements:
            page.placements = dict(self.placements)
            page.macro_dirty = True
        page.load()
        self.app.inputs.settings_changed(source=None)
        page.save()
        self.dirty = False
        self.update()
        self.app.set_status("Floorplan plan applied to the design.")
        return True

    def run(self):
        """Apply the plan if needed, then run the flow up to the floorplan stage."""
        if self.dirty and not self.apply():
            return
        app = self.app

        def start():
            if app.inputs.is_dirty() or not app.run_btn.get_sensitive():
                return True                      # the save is still being read back
            app.from_cb.set_active_id("continue" if not app.stage_done().get("floorplan") else "floorplan")
            app.to_cb.set_active_id("floorplan")
            app.on_run()
            return False
        GLib.timeout_add(300, start)

    # ---------- drawing ----------
    def on_draw(self, widget, cr):
        self.draw(cr, widget.get_allocated_width(), widget.get_allocated_height())
        return False

    def render_png(self, path, w=1000, h=800):
        """The plan as a picture (for the assistant to look at); the path."""
        import cairo
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        view = self.view
        self.draw(cairo.Context(surface), w, h)
        self.view = view              # the picture's scale must not change how the canvas maps the mouse
        surface.write_to_png(str(path))
        return path

    def draw(self, cr, w, h):
        cr.set_source_rgba(*rgba("surface"))
        cr.paint()
        p = self.plan
        if not p:
            cr.set_source_rgba(*rgba("text_muted"))
            cr.set_font_size(14)
            text = "No floorplan to draw: pick a design and give a cell area."
            cr.move_to((w - cr.text_extents(text).width) / 2, h / 2)
            cr.show_text(text)
            return False
        die, core = p["die"], p["core"]
        boxes = [die] + ([self.last_run[0]] if self.show["last"].get_active() and self.last_run
                         and self.last_run[0] else [])
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        pad = 56
        scale = min((w - 2 * pad) / max(x1, 1e-6), (h - 2 * pad) / max(y1, 1e-6))
        ox, oy = (w - x1 * scale) / 2, (h + y1 * scale) / 2        # y grows upward, as in the layout
        self.view = (scale, ox, oy)

        def pt(x, y):
            return ox + x * scale, oy - y * scale

        def rect(box, fill=None, stroke=None, dash=None, width=1.2):
            (ax, ay), (bx, by) = pt(box[0], box[3]), pt(box[2], box[1])
            cr.rectangle(ax, ay, bx - ax, by - ay)
            if fill:
                cr.set_source_rgba(*fill)
                cr.fill_preserve() if stroke else cr.fill()
            if stroke:
                cr.set_source_rgba(*stroke)
                cr.set_line_width(width)
                cr.set_dash(dash or [])
                cr.stroke()
                cr.set_dash([])

        rect(die, fill=rgba("surface_alt"), stroke=rgba("text"), width=1.6)
        rect(core, fill=rgba("accent", 0.07), stroke=rgba("accent"))
        if self.show["rows"].get_active() and self.site:
            rows = int(p["core_h"] / self.site[1])
            step = max(1, math.ceil(rows / max((p["core_h"] * scale) / 5, 1)))    # at most a line every 5 pixels
            cr.set_source_rgba(*rgba("accent", 0.18))
            cr.set_line_width(0.6)
            for r in range(step, rows, step):
                y = core[1] + r * self.site[1]
                cr.move_to(*pt(core[0], y))
                cr.line_to(*pt(core[2], y))
            cr.stroke()
        if self.show["straps"].get_active():
            direction = {la["name"]: la["direction"] for la in self.layers}
            for s in self.straps:
                vertical = direction.get(s["layer"], "vertical") == "vertical"
                span = p["core_w"] if vertical else p["core_h"]
                fits = span >= fplan.strap_need(s)
                cr.set_source_rgba(*rgba("warning" if vertical else "info", 0.75 if fits else 0.0))
                cr.set_line_width(max(s["width"] * scale, 1.2))
                gap = s["spacing"] if s["spacing"] is not None else s["pitch"] / 2 - s["width"]
                at, n = s["offset"] + s["width"] / 2, 0
                while at + s["width"] / 2 <= span and n < 400 and fits:
                    for extra in (0, s["width"] + gap):       # the power and the ground strap of the pair
                        v = at + extra
                        if v + s["width"] / 2 > span:
                            continue
                        if vertical:
                            cr.move_to(*pt(core[0] + v, core[1]))
                            cr.line_to(*pt(core[0] + v, core[3]))
                        else:
                            cr.move_to(*pt(core[0], core[1] + v))
                            cr.line_to(*pt(core[2], core[1] + v))
                    at += s["pitch"]
                    n += 1
                cr.stroke()
        if self.show["last"].get_active() and self.last_run:
            for box in self.last_run[:2]:
                if box:
                    rect(box, stroke=rgba("text_muted", 0.8), dash=[5, 4])
        cr.set_font_size(11)
        for name, (x, y, orient) in sorted(self.placements.items()):
            size = self.sizes.get(self.masters.get(name, ""))
            if not size:
                continue
            mw, mh = fp.macro_size(size, orient)
            box = (x, y, x + mw, y + mh)
            if any(self.halo):
                rect((x - self.halo[0], y - self.halo[1], x + mw + self.halo[0], y + mh + self.halo[1]),
                     stroke=rgba("text_muted", 0.7), dash=[3, 3], width=0.8)
            rect(box, fill=rgba("success", 0.25), stroke=rgba("success"), width=1.4)
            label = name.split("/")[-1].split(".")[-1]
            ext = cr.text_extents(label)
            (ax, ay), (bx, by) = pt(box[0], box[3]), pt(box[2], box[1])
            if ext.width < bx - ax - 4:
                cr.set_source_rgba(*rgba("text"))
                cr.move_to((ax + bx - ext.width) / 2, (ay + by + ext.height) / 2)
                cr.show_text(label)
        # Dimensions along the die's bottom and left edges.
        cr.set_source_rgba(*rgba("text_muted"))
        cr.set_font_size(12)
        text = f"{die[2] - die[0]:.1f} µm"
        bx, by = pt((die[0] + die[2]) / 2, die[1])
        cr.move_to(bx - cr.text_extents(text).width / 2, by + 20)
        cr.show_text(text)
        text = f"{die[3] - die[1]:.1f} µm"
        lx, ly = pt(die[0], (die[1] + die[3]) / 2)
        cr.save()
        cr.translate(lx - 10, ly + cr.text_extents(text).width / 2)
        cr.rotate(-math.pi / 2)
        cr.move_to(0, 0)
        cr.show_text(text)
        cr.restore()
        cx, cy = pt(core[0], core[3])
        cr.set_source_rgba(*rgba("accent"))
        cr.move_to(cx + 6, cy + 16)
        cr.show_text(f"core {p['core_w']:.1f} × {p['core_h']:.1f} µm")
        return False

    # ---------- dragging macros ----------
    def to_plan(self, px, py):
        scale, ox, oy = self.view
        return (px - ox) / scale, (oy - py) / scale

    def macro_at(self, px, py):
        x, y = self.to_plan(px, py)
        for name, (mx, my, orient) in self.placements.items():
            size = self.sizes.get(self.masters.get(name, ""))
            if size:
                mw, mh = fp.macro_size(size, orient)
                if mx <= x <= mx + mw and my <= y <= my + mh:
                    return name
        return None

    def on_press(self, _w, event):
        name = self.macro_at(event.x, event.y)
        if name and event.button == 1:
            x, y = self.to_plan(event.x, event.y)
            mx, my, _o = self.placements[name]
            self.drag = (name, x - mx, y - my)
        return True

    def on_motion(self, _w, event):
        if not self.drag or not self.plan:
            return True
        name, dx, dy = self.drag
        x, y = self.to_plan(event.x, event.y)
        nx, ny = x - dx, y - dy
        core = self.plan["core"]
        if self.site:                        # macros sit on the placement grid, as OpenROAD snaps them
            nx = core[0] + round((nx - core[0]) / self.site[0]) * self.site[0]
            ny = core[1] + round((ny - core[1]) / self.site[1]) * self.site[1]
        self.placements[name] = (round(nx, 4), round(ny, 4), self.placements[name][2])
        self.canvas.queue_draw()
        return True

    def on_release(self, *_):
        if self.drag:
            self.drag = None
            self.dirty = True
            self.update()
        return True
