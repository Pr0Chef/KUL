"""Architecture diagrams: the model, drawing (screen, PNG, SVG with the same code), and a diagram from RTL.

A diagram is shapes and arrows. A shape has a kind (block, module, rounded, ellipse, diamond, note, text, port),
a box, a label, and colors. An arrow joins two shapes, has an optional label and a style (solid or dashed, one or
two heads, a bus width). Diagrams are saved as JSON (*.diagram.json) in the project, next to the RTL.
"""

import json
import math

import cairo

KINDS = ["block", "module", "rounded", "ellipse", "diamond", "note", "text", "port"]
COLORS = {"blue": ("#e8ecff", "#3d5afe"), "green": ("#e3f6ec", "#1e8e5a"), "amber": ("#fff4dd", "#b7791f"),
          "red": ("#fde8e8", "#d64545"), "grey": ("#f1f3f6", "#6b7385"), "purple": ("#f0e8ff", "#7048e8"),
          "white": ("#ffffff", "#1c2230")}
GRID = 10


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


class Diagram:
    def __init__(self):
        self.shapes = []   # {"id", "kind", "x", "y", "w", "h", "label", "color"}
        self.edges = []    # {"id", "src", "dst", "label", "dashed", "both", "bus"}
        self.title = ""
        self._next = 1

    # ---------- editing ----------
    def new_id(self):
        while any(s["id"] == f"n{self._next}" for s in self.shapes + self.edges):
            self._next += 1
        self._next += 1
        return f"n{self._next - 1}"

    def add_shape(self, kind, x, y, label="", w=None, h=None, color="blue"):
        size = {"text": (120, 30), "port": (90, 30), "ellipse": (120, 70), "diamond": (120, 80),
                "note": (160, 80)}.get(kind, (150, 70))
        shape = {"id": self.new_id(), "kind": kind, "x": snap(x), "y": snap(y), "w": w or size[0],
                 "h": h or size[1], "label": label or kind.capitalize(), "color": color}
        self.shapes.append(shape)
        return shape

    def connect(self, src, dst, label="", dashed=False, both=False, bus=0):
        if src == dst:
            return None
        edge = {"id": self.new_id(), "src": src, "dst": dst, "label": label, "dashed": dashed, "both": both,
                "bus": bus}
        self.edges.append(edge)
        return edge

    def shape(self, sid):
        return next((s for s in self.shapes if s["id"] == sid), None)

    def remove(self, ids):
        ids = set(ids)
        self.shapes = [s for s in self.shapes if s["id"] not in ids]
        alive = {s["id"] for s in self.shapes}
        self.edges = [e for e in self.edges if e["id"] not in ids and e["src"] in alive and e["dst"] in alive]

    def shape_at(self, x, y):
        for s in reversed(self.shapes):
            if s["x"] <= x <= s["x"] + s["w"] and s["y"] <= y <= s["y"] + s["h"]:
                return s
        return None

    def edge_at(self, x, y, tolerance=6):
        for e in reversed(self.edges):
            pts = self.edge_points(e)
            for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
                if _dist_to_segment(x, y, x1, y1, x2, y2) <= tolerance:
                    return e
        return None

    def bounds(self, margin=30):
        if not self.shapes:
            return 0, 0, 400, 300
        x0 = min(s["x"] for s in self.shapes) - margin
        y0 = min(s["y"] for s in self.shapes) - margin - (30 if self.title else 0)
        x1 = max(s["x"] + s["w"] for s in self.shapes) + margin
        y1 = max(s["y"] + s["h"] for s in self.shapes) + margin
        return x0, y0, x1, y1

    # ---------- geometry ----------
    def edge_points(self, e):
        """Points of an arrow: out of the source's side facing the target, one bend, into the target."""
        a, b = self.shape(e["src"]), self.shape(e["dst"])
        if not a or not b:
            return []
        ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
        bx, by = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        apart = a["x"] + a["w"] < b["x"] or b["x"] + b["w"] < a["x"]
        if apart:
            # Mostly side by side: leave and enter horizontally.
            sx = a["x"] + a["w"] if bx > ax else a["x"]
            tx = b["x"] if bx > ax else b["x"] + b["w"]
            mx = (sx + tx) / 2
            return [(sx, ay), (mx, ay), (mx, by), (tx, by)]
        sy = a["y"] + a["h"] if by > ay else a["y"]
        ty = b["y"] if by > ay else b["y"] + b["h"]
        my = (sy + ty) / 2
        return [(ax, sy), (ax, my), (bx, my), (bx, ty)]

    # ---------- files ----------
    def to_json(self):
        return json.dumps({"title": self.title, "shapes": self.shapes, "edges": self.edges}, indent=1)

    @classmethod
    def from_json(cls, text):
        data = json.loads(text)
        d = cls()
        d.title = data.get("title", "")
        d.shapes = [dict(s) for s in data.get("shapes", [])]
        d.edges = [dict(e) for e in data.get("edges", [])]
        return d

    def copy(self):
        return Diagram.from_json(self.to_json())


def snap(v):
    return round(v / GRID) * GRID


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


# ---------------------------------------------------------------- drawing
def draw(cr, d, selected=(), dark=False):
    """Draw a diagram with Cairo, in diagram coordinates (the caller sets the transform)."""
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    text_color = (0.9, 0.92, 0.95) if dark else (0.11, 0.13, 0.19)
    if d.title:
        x0, y0, _, _ = d.bounds()
        cr.set_source_rgb(*text_color)
        cr.set_font_size(18)
        cr.move_to(x0 + 30, y0 + 26)
        cr.show_text(d.title)
    for e in d.edges:
        _draw_edge(cr, d, e, e["id"] in selected, text_color)
    for s in d.shapes:
        _draw_shape(cr, s, s["id"] in selected, text_color)


def _path(cr, s):
    x, y, w, h = s["x"], s["y"], s["w"], s["h"]
    kind = s["kind"]
    if kind == "ellipse":
        cr.save()
        cr.translate(x + w / 2, y + h / 2)
        cr.scale(w / 2, h / 2)
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
    elif kind == "diamond":
        cr.move_to(x + w / 2, y)
        cr.line_to(x + w, y + h / 2)
        cr.line_to(x + w / 2, y + h)
        cr.line_to(x, y + h / 2)
        cr.close_path()
    elif kind == "port":
        cr.move_to(x, y)
        cr.line_to(x + w - h / 2, y)
        cr.line_to(x + w, y + h / 2)
        cr.line_to(x + w - h / 2, y + h)
        cr.line_to(x, y + h)
        cr.close_path()
    elif kind == "note":
        fold = 14
        cr.move_to(x, y)
        cr.line_to(x + w - fold, y)
        cr.line_to(x + w, y + fold)
        cr.line_to(x + w, y + h)
        cr.line_to(x, y + h)
        cr.close_path()
    else:
        r = 10 if kind == "rounded" else 4 if kind in ("block", "module") else 0
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


def _draw_shape(cr, s, selected, text_color):
    fill, stroke = COLORS.get(s.get("color", "blue"), COLORS["blue"])
    if s["kind"] != "text":
        _path(cr, s)
        cr.set_source_rgb(*_rgb(fill))
        cr.fill_preserve()
        cr.set_source_rgb(*_rgb(stroke))
        cr.set_line_width(2.4 if selected else 1.4)
        cr.set_dash([])
        cr.stroke()
        if s["kind"] == "module":
            # A module: a title bar over the body.
            cr.move_to(s["x"], s["y"] + 22)
            cr.line_to(s["x"] + s["w"], s["y"] + 22)
            cr.set_line_width(1)
            cr.stroke()
    lines = str(s.get("label", "")).split("\n")
    cr.set_source_rgb(*(_rgb(stroke) if s["kind"] == "text" and s.get("color") not in ("white", None) else
                        (0.11, 0.13, 0.19)))
    if s["kind"] == "text":
        cr.set_source_rgb(*text_color)
    size = 12.5
    cr.set_font_size(size)
    if s["kind"] == "module":
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ext = cr.text_extents(lines[0])
        cr.move_to(s["x"] + (s["w"] - ext.width) / 2 - ext.x_bearing, s["y"] + 15)
        cr.show_text(lines[0])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        lines, top = lines[1:], s["y"] + 22
        height = s["h"] - 22
    else:
        top, height = s["y"], s["h"]
    y = top + (height - len(lines) * (size + 4)) / 2 + size
    for line in lines:
        ext = cr.text_extents(line)
        cr.move_to(s["x"] + (s["w"] - ext.width) / 2 - ext.x_bearing, y)
        cr.show_text(line)
        y += size + 4
    if selected:
        cr.set_source_rgb(0.24, 0.35, 1)
        cr.rectangle(s["x"] + s["w"] - 5, s["y"] + s["h"] - 5, 10, 10)
        cr.fill()


def _arrow_head(cr, x1, y1, x2, y2, size=9):
    angle = math.atan2(y2 - y1, x2 - x1)
    cr.move_to(x2, y2)
    cr.line_to(x2 - size * math.cos(angle - 0.4), y2 - size * math.sin(angle - 0.4))
    cr.line_to(x2 - size * math.cos(angle + 0.4), y2 - size * math.sin(angle + 0.4))
    cr.close_path()
    cr.fill()


def _draw_edge(cr, d, e, selected, text_color):
    pts = d.edge_points(e)
    if len(pts) < 2:
        return
    color = (0.24, 0.35, 1) if selected else (0.35, 0.39, 0.47)
    cr.set_source_rgb(*color)
    cr.set_line_width((3.2 if e.get("bus") else 1.6) + (0.8 if selected else 0))
    cr.set_dash([6, 4] if e.get("dashed") else [])
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.stroke()
    cr.set_dash([])
    _arrow_head(cr, *pts[-2], *pts[-1])
    if e.get("both"):
        _arrow_head(cr, *pts[1], *pts[0])
    label = e.get("label", "") + (f" [{e['bus']}]" if e.get("bus") else "")
    if label:
        (x1, y1), (x2, y2) = pts[1], pts[2]
        cr.set_font_size(11)
        ext = cr.text_extents(label)
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(mx - ext.width / 2 - 3, my - ext.height - 2, ext.width + 6, ext.height + 6)
        cr.fill()
        cr.set_source_rgb(*text_color if text_color[0] < 0.5 else (0.11, 0.13, 0.19))
        cr.move_to(mx - ext.width / 2 - ext.x_bearing, my)
        cr.show_text(label)


def export(d, path, scale=2.0):
    """Write the diagram to a PNG or SVG file (by its suffix)."""
    x0, y0, x1, y1 = d.bounds()
    w, h = int((x1 - x0) * scale), int((y1 - y0) * scale)
    path = str(path)
    if path.lower().endswith(".svg"):
        surface = cairo.SVGSurface(path, x1 - x0, y1 - y0)
        cr = cairo.Context(surface)
        cr.translate(-x0, -y0)
    else:
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(w, 1), max(h, 1))
        cr = cairo.Context(surface)
        cr.scale(scale, scale)
        cr.translate(-x0, -y0)
    cr.set_source_rgb(1, 1, 1)
    cr.paint()
    draw(cr, d)
    if path.lower().endswith(".svg"):
        surface.finish()
    else:
        surface.write_to_png(path)


# ---------------------------------------------------------------- from RTL
def from_rtl(mods, top):
    """A block diagram of a module: its input ports on the left, its submodule instances in columns by how far
    they are from the inputs, its outputs on the right, and arrows for the nets that join them (with the bus
    width when the net is a port bus)."""
    mod = mods.get(top)
    d = Diagram()
    d.title = f"{top}: block diagram"
    if mod is None:
        return d
    inputs = [(n, b) for dirn, n, b in mod.ports if dirn == "input"]
    outputs = [(n, b) for dirn, n, b in mod.ports if dirn != "input"]
    width = {n: (abs(b[0] - b[1]) + 1 if isinstance(b, tuple) else 0) for _, n, b in mod.ports}

    # Continuous assignments join nets: reading x where "assign x = y" reads y, and driving y drives x.
    sources = {}
    for lhs, rhs in mod.assigns:
        for name in lhs:
            sources.setdefault(name, set()).update(rhs)

    def upstream(names):
        out, todo = set(names), list(names)
        while todo:
            for src in sources.get(todo.pop(), ()):
                if src not in out:
                    out.add(src)
                    todo.append(src)
        return out

    def downstream(names):
        out, changed = set(names), True
        while changed:
            changed = False
            for lhs, rhs in mod.assigns:
                if rhs & out and not lhs <= out:
                    out |= lhs
                    changed = True
        return out
    # Which nets each instance drives and reads: an instance's output ports drive, the others read.
    import hdl
    inst_nets = []
    for kind, name, conns in mod.instances:
        sub = mods.get(kind)
        dirs = {n: dirn for dirn, n, _ in sub.ports} if sub else {}
        drives, reads = set(), set()
        for port, expr in conns.items():
            (drives if dirs.get(port) == "output" else reads).update(hdl.names_in(expr))
        inst_nets.append((kind, name, downstream(drives), upstream(reads)))
    # Columns: an instance sits one column right of the instances that drive what it reads.
    col = {name: 0 for _, name, _, _ in inst_nets}
    for _ in range(len(inst_nets)):
        for _, name, _, reads in inst_nets:
            for _, other, drives, _ in inst_nets:
                if other != name and drives & reads and col[name] <= col[other] and col[other] < len(inst_nets):
                    col[name] = col[other] + 1
    ids = {}
    port_w = snap(max([90] + [7.5 * len(n) + 30 for _, n, _ in mod.ports]))
    box_w = max([170] + [7.5 * max(len(kind), len(name)) + 24 for kind, name, _, _ in inst_nets])
    gap_x, gap_y = box_w + 90, 110
    for i, (n, _) in enumerate(inputs):
        ids[("in", n)] = d.add_shape("port", 40, 60 + i * 50, n, w=port_w, color="green")["id"]
    columns = {}
    for kind, name, _, _ in inst_nets:
        columns.setdefault(col[name], []).append((kind, name))
    left = 40 + port_w + 90
    for c, items in sorted(columns.items()):
        for r, (kind, name) in enumerate(items):
            ids[("inst", name)] = d.add_shape("module", left + c * gap_x, 50 + r * gap_y, f"{name}\n{kind}",
                                              w=snap(box_w), h=80, color="blue")["id"]
    out_x = left + (max(columns) + 1 if columns else 0) * gap_x + 20
    for i, (n, _) in enumerate(outputs):
        ids[("out", n)] = d.add_shape("port", out_x, 60 + i * 50, n, w=port_w, color="amber")["id"]
    if not inst_nets:
        core = d.add_shape("module", left, 60, f"{top}\n(no submodules)", w=170, h=max(80, 50 * len(inputs)))
        for n, _ in inputs:
            d.connect(ids[("in", n)], core["id"], bus=width[n])
        for n, _ in outputs:
            d.connect(core["id"], ids[("out", n)], bus=width[n])
        return d
    seen = set()
    for _, name, drives, reads in inst_nets:
        me = ids[("inst", name)]
        for n, _ in inputs:
            if n in reads and (n, name) not in seen:
                seen.add((n, name))
                d.connect(ids[("in", n)], me, bus=width[n])
        for n, _ in outputs:
            if n in drives:
                d.connect(me, ids[("out", n)], bus=width[n])
        for _, other, other_drives, _ in inst_nets:
            shared = sorted(other_drives & reads)
            if other != name and shared:
                d.connect(ids[("inst", other)], me, label=", ".join(shared[:2]) + ("…" if len(shared) > 2 else ""))
    return d
