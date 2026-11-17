"""Diagrams: the model, drawing (screen, PNG, SVG and PDF with the same code), draw.io export, and a diagram
from RTL.

A diagram is shapes and arrows. A shape has a kind (see PALETTE: general shapes, flowchart shapes, digital design
blocks and logic gates), a box, a label and a style: fill and line colors, dashed line, font size, bold, text
alignment. A shape may belong to a group (shapes of a group select and move together). An arrow joins two shapes:
it leaves and enters by a chosen side or the nearest one, runs orthogonal, straight or curved, can pass through
waypoints, and has head styles, a label and an optional bus width. The order of the shapes is their stacking order.
Diagrams are saved as JSON (*.diagram.json) in the project, next to the RTL.
"""

import json
import math
from xml.sax.saxutils import quoteattr

import cairo

# The palette, as the editor shows it: (category, [(kind, name)]).
PALETTE = [
    ("General", [("block", "Rectangle"), ("rounded", "Rounded"), ("ellipse", "Ellipse"), ("diamond", "Diamond"),
                 ("text", "Text"), ("note", "Note"), ("hexagon", "Hexagon"), ("parallelogram", "Parallelogram"),
                 ("triangle", "Triangle"), ("cylinder", "Cylinder"), ("cloud", "Cloud"),
                 ("container", "Container")]),
    ("Flowchart", [("terminator", "Start / end"), ("process", "Process"), ("decision", "Decision"),
                   ("data", "Data"), ("document", "Document")]),
    ("Digital design", [("module", "Module"), ("port", "Port"), ("register", "Register"), ("mux", "Mux"),
                        ("adder", "Adder"), ("fifo", "FIFO"), ("memory", "Memory"), ("state", "FSM state"),
                        ("clock", "Clock"), ("bus", "Bus")]),
    ("Logic gates", [("and", "AND"), ("or", "OR"), ("not", "NOT"), ("xor", "XOR"), ("nand", "NAND"),
                     ("nor", "NOR"), ("buffer", "Buffer")]),
]
KINDS = [kind for _cat, items in PALETTE for kind, _name in items]
NAMES = {kind: name for _cat, items in PALETTE for kind, name in items}
# Named color pairs (fill, line); a shape may also carry its own "fill" and "stroke" hex colors.
COLORS = {"blue": ("#e8ecff", "#3d5afe"), "green": ("#e3f6ec", "#1e8e5a"), "amber": ("#fff4dd", "#b7791f"),
          "red": ("#fde8e8", "#d64545"), "grey": ("#f1f3f6", "#6b7385"), "purple": ("#f0e8ff", "#7048e8"),
          "white": ("#ffffff", "#1c2230")}
SIZES = {"text": (120, 30), "port": (90, 30), "ellipse": (120, 70), "diamond": (120, 80), "decision": (120, 80),
         "note": (160, 80), "container": (320, 220), "terminator": (130, 50), "register": (110, 70),
         "mux": (60, 110), "adder": (60, 60), "state": (80, 80), "fifo": (150, 60), "memory": (110, 90),
         "clock": (110, 40), "bus": (200, 20), "cylinder": (100, 90), "cloud": (150, 90), "triangle": (100, 90),
         "and": (80, 60), "or": (80, 60), "xor": (80, 60), "nand": (90, 60), "nor": (90, 60), "not": (80, 50),
         "buffer": (80, 50)}
DEFAULT_LABEL = {"adder": "+", "and": "", "or": "", "not": "", "xor": "", "nand": "", "nor": "", "buffer": "",
                 "bus": "", "register": "REG", "mux": "MUX", "clock": "clk", "state": "S0", "fifo": "FIFO",
                 "memory": "RAM", "terminator": "Start", "decision": "?", "container": "Container"}
SIDES = ("left", "right", "top", "bottom")
EDGE_STYLES = ("orthogonal", "straight", "curved")
HEADS = ("arrow", "open", "diamond", "dot", "none")
GRID = 10


def _rgb(hex_color):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def snap(v):
    return round(v / GRID) * GRID


def colors_of(shape):
    """(fill, line) hex colors of a shape: its own, else its named pair."""
    fill, stroke = COLORS.get(shape.get("color", "blue"), COLORS["blue"])
    return shape.get("fill") or fill, shape.get("stroke") or stroke


class Diagram:
    def __init__(self):
        self.shapes = []   # {"id", "kind", "x", "y", "w", "h", "label", "color"} + optional style and "group"
        self.edges = []    # {"id", "src", "dst", "label", "dashed", "both", "bus"} + optional style, sides, points
        self.title = ""
        self._next = 1

    # ---------- editing ----------
    def new_id(self):
        used = {s["id"] for s in self.shapes} | {e["id"] for e in self.edges}
        while f"n{self._next}" in used:
            self._next += 1
        self._next += 1
        return f"n{self._next - 1}"

    def add_shape(self, kind, x, y, label="", w=None, h=None, color="blue", **style):
        size = SIZES.get(kind, (150, 70))
        if not label:
            label = DEFAULT_LABEL.get(kind, NAMES.get(kind, kind.capitalize()))
        shape = {"id": self.new_id(), "kind": kind, "x": snap(x), "y": snap(y), "w": w or size[0],
                 "h": h or size[1], "label": label, "color": color}
        shape.update({k: v for k, v in style.items() if v not in (None, "")})
        # A container sits behind what is put into it.
        if kind == "container":
            self.shapes.insert(0, shape)
        else:
            self.shapes.append(shape)
        return shape

    def connect(self, src, dst, label="", dashed=False, both=False, bus=0, **style):
        if src == dst or not self.shape(src) or not self.shape(dst):
            return None
        edge = {"id": self.new_id(), "src": src, "dst": dst, "label": label, "dashed": dashed, "both": both,
                "bus": bus}
        edge.update({k: v for k, v in style.items() if v not in (None, "")})
        self.edges.append(edge)
        return edge

    def shape(self, sid):
        return next((s for s in self.shapes if s["id"] == sid), None)

    def edge(self, eid):
        return next((e for e in self.edges if e["id"] == eid), None)

    def item(self, iid):
        return self.shape(iid) or self.edge(iid)

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

    def in_rect(self, x0, y0, x1, y1):
        """Ids of the shapes wholly inside a rectangle (for rubber-band selection)."""
        x0, x1, y0, y1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
        return [s["id"] for s in self.shapes
                if s["x"] >= x0 and s["y"] >= y0 and s["x"] + s["w"] <= x1 and s["y"] + s["h"] <= y1]

    def with_groups(self, ids):
        """ids plus every shape that shares a group with one of them."""
        groups = {s.get("group") for s in self.shapes if s["id"] in ids and s.get("group")}
        return set(ids) | {s["id"] for s in self.shapes if s.get("group") in groups}

    def group(self, ids):
        gid = self.new_id()
        for s in self.shapes:
            if s["id"] in ids:
                s["group"] = gid
        return gid

    def ungroup(self, ids):
        for s in self.shapes:
            if s["id"] in self.with_groups(ids):
                s.pop("group", None)

    def duplicate(self, ids, dx=20, dy=20):
        """Copy shapes (and the arrows among them), moved by dx, dy; the new ids."""
        return self.paste(self.copy_items(ids), dx, dy)

    def copy_items(self, ids):
        ids = set(ids)
        return {"shapes": [dict(s) for s in self.shapes if s["id"] in ids],
                "edges": [json.loads(json.dumps(e)) for e in self.edges
                          if e["id"] in ids or (e["src"] in ids and e["dst"] in ids)]}

    def paste(self, clip, dx=20, dy=20):
        new_ids, groups, made = {}, {}, []
        for s in clip["shapes"]:
            c = dict(s, id=self.new_id(), x=s["x"] + dx, y=s["y"] + dy)
            if c.get("group"):
                c["group"] = groups.setdefault(c["group"], self.new_id())
            new_ids[s["id"]] = c["id"]
            self.shapes.append(c)
            made.append(c["id"])
        for e in clip["edges"]:
            if e["src"] in new_ids and e["dst"] in new_ids:
                c = dict(e, id=self.new_id(), src=new_ids[e["src"]], dst=new_ids[e["dst"]])
                c["points"] = [[px + dx, py + dy] for px, py in e.get("points", [])]
                self.edges.append(c)
                made.append(c["id"])
        return made

    def reorder(self, ids, front):
        """Bring shapes to the front or send them to the back, keeping their order among themselves."""
        picked = [s for s in self.shapes if s["id"] in ids]
        rest = [s for s in self.shapes if s["id"] not in ids]
        self.shapes = rest + picked if front else picked + rest

    def align(self, ids, how):
        """Align shapes: left, center, right, top, middle, bottom; or spread them evenly: hspace, vspace; or give
        them the first one's width or height: width, height."""
        shapes = [s for s in self.shapes if s["id"] in ids]
        if len(shapes) < 2:
            return
        x0, x1 = min(s["x"] for s in shapes), max(s["x"] + s["w"] for s in shapes)
        y0, y1 = min(s["y"] for s in shapes), max(s["y"] + s["h"] for s in shapes)
        for s in shapes:
            if how == "left":
                s["x"] = x0
            elif how == "right":
                s["x"] = x1 - s["w"]
            elif how == "center":
                s["x"] = snap((x0 + x1) / 2 - s["w"] / 2)
            elif how == "top":
                s["y"] = y0
            elif how == "bottom":
                s["y"] = y1 - s["h"]
            elif how == "middle":
                s["y"] = snap((y0 + y1) / 2 - s["h"] / 2)
            elif how == "width":
                s["w"] = shapes[0]["w"]
            elif how == "height":
                s["h"] = shapes[0]["h"]
        if how in ("hspace", "vspace") and len(shapes) > 2:
            axis, size = ("x", "w") if how == "hspace" else ("y", "h")
            order = sorted(shapes, key=lambda s: s[axis])
            free = (order[-1][axis] + order[-1][size] - order[0][axis]) - sum(s[size] for s in order)
            gap, at = free / (len(order) - 1), order[0][axis]
            for s in order:
                s[axis] = snap(at)
                at += s[size] + gap

    def bounds(self, margin=30):
        if not self.shapes:
            return 0, 0, 400, 300
        xs = [s["x"] for s in self.shapes] + [p[0] for e in self.edges for p in e.get("points", [])]
        ys = [s["y"] for s in self.shapes] + [p[1] for e in self.edges for p in e.get("points", [])]
        x0, y0 = min(xs) - margin, min(ys) - margin - (30 if self.title else 0)
        x1 = max([s["x"] + s["w"] for s in self.shapes] + xs) + margin
        y1 = max([s["y"] + s["h"] for s in self.shapes] + ys) + margin
        return x0, y0, x1, y1

    # ---------- geometry ----------
    @staticmethod
    def anchor(shape, side):
        """The point in the middle of a shape's side."""
        x, y, w, h = shape["x"], shape["y"], shape["w"], shape["h"]
        return {"left": (x, y + h / 2), "right": (x + w, y + h / 2), "top": (x + w / 2, y),
                "bottom": (x + w / 2, y + h)}[side]

    def sides_for(self, e):
        """(source side, target side) of an arrow: the ones it names, else the sides that face each other."""
        a, b = self.shape(e["src"]), self.shape(e["dst"])
        ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"] / 2
        bx, by = b["x"] + b["w"] / 2, b["y"] + b["h"] / 2
        via = e.get("points") or []
        tx, ty = via[0] if via else (bx, by)        # where the source side should face
        fx, fy = via[-1] if via else (ax, ay)       # where the target side should face
        apart = a["x"] + a["w"] < b["x"] or b["x"] + b["w"] < a["x"]

        def facing(cx, cy, px, py, horizontal):
            if horizontal:
                return "right" if px > cx else "left"
            return "bottom" if py > cy else "top"
        horizontal = apart if not via else abs(tx - ax) >= abs(ty - ay)
        s_side = e.get("src_side") or facing(ax, ay, tx, ty, horizontal)
        horizontal = apart if not via else abs(fx - bx) >= abs(fy - by)
        t_side = e.get("dst_side") or facing(bx, by, fx, fy, horizontal)
        return s_side, t_side

    def edge_points(self, e):
        """Points of an arrow from its source's side to its target's, through its waypoints. Orthogonal arrows
        get the bends that keep every segment horizontal or vertical."""
        a, b = self.shape(e["src"]), self.shape(e["dst"])
        if not a or not b:
            return []
        s_side, t_side = self.sides_for(e)
        start, end = self.anchor(a, s_side), self.anchor(b, t_side)
        via = [tuple(p) for p in e.get("points", [])]
        if e.get("style", "orthogonal") != "orthogonal":
            return [start] + via + [end]
        pts = [start]
        stops = via + [end]
        horizontal = s_side in ("left", "right")       # the direction the next segment leaves in
        for i, (px, py) in enumerate(stops):
            x, y = pts[-1]
            last = i == len(stops) - 1
            if last and not via:
                enters_h = t_side in ("left", "right")
                if horizontal and enters_h:
                    mx = (x + px) / 2
                    pts += [(mx, y), (mx, py)]
                elif not horizontal and not enters_h:
                    my = (y + py) / 2
                    pts += [(x, my), (px, my)]
                elif horizontal:
                    pts.append((px, y))
                else:
                    pts.append((x, py))
            elif x != px and y != py:
                if last:
                    horizontal = t_side in ("top", "bottom")   # arrive along the target side's direction
                pts.append((px, y) if horizontal else (x, py))
            pts.append((px, py))
            horizontal = not horizontal
        return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]

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


def _dist_to_segment(px, py, x1, y1, x2, y2):
    dx, dy = x2 - x1, y2 - y1
    if dx == dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


# ---------------------------------------------------------------- drawing
def draw(cr, d, selected=(), dark=False):
    """Draw a diagram with Cairo, in diagram coordinates (the caller sets the transform). Selection handles are
    drawn by the editor, not here, so exports never show them."""
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    text_color = (0.9, 0.92, 0.95) if dark else (0.11, 0.13, 0.19)
    if d.title:
        x0, y0, _, _ = d.bounds()
        cr.set_source_rgb(*text_color)
        cr.set_font_size(18)
        cr.move_to(x0 + 30, y0 + 26)
        cr.show_text(d.title)
    for s in d.shapes:
        if s["kind"] == "container":
            draw_shape(cr, s, s["id"] in selected, text_color)
    for e in d.edges:
        _draw_edge(cr, d, e, e["id"] in selected, text_color)
    for s in d.shapes:
        if s["kind"] != "container":
            draw_shape(cr, s, s["id"] in selected, text_color)


def _rounded(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _poly(cr, pts):
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.close_path()


def _path(cr, s):
    """The outline of a shape, as the current Cairo path."""
    x, y, w, h = s["x"], s["y"], s["w"], s["h"]
    kind = s["kind"]
    if kind in ("ellipse", "state", "adder"):
        cr.save()
        cr.translate(x + w / 2, y + h / 2)
        cr.scale(w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(0, 0, 1, 0, 2 * math.pi)
        cr.restore()
    elif kind in ("diamond", "decision"):
        _poly(cr, [(x + w / 2, y), (x + w, y + h / 2), (x + w / 2, y + h), (x, y + h / 2)])
    elif kind == "port":
        _poly(cr, [(x, y), (x + w - h / 2, y), (x + w, y + h / 2), (x + w - h / 2, y + h), (x, y + h)])
    elif kind == "note":
        _poly(cr, [(x, y), (x + w - 14, y), (x + w, y + 14), (x + w, y + h), (x, y + h)])
    elif kind == "hexagon":
        k = min(w * 0.22, h / 2)
        _poly(cr, [(x + k, y), (x + w - k, y), (x + w, y + h / 2), (x + w - k, y + h), (x + k, y + h),
                   (x, y + h / 2)])
    elif kind in ("parallelogram", "data"):
        k = min(w * 0.2, h)
        _poly(cr, [(x + k, y), (x + w, y), (x + w - k, y + h), (x, y + h)])
    elif kind == "triangle":
        _poly(cr, [(x + w / 2, y), (x + w, y + h), (x, y + h)])
    elif kind == "mux":
        k = h * 0.2
        _poly(cr, [(x, y), (x + w, y + k), (x + w, y + h - k), (x, y + h)])
    elif kind in ("not", "buffer"):
        tip = x + w - (10 if kind == "not" else 0)
        _poly(cr, [(x, y), (tip, y + h / 2), (x, y + h)])
    elif kind in ("and", "nand"):
        end = x + w - (10 if kind == "nand" else 0)
        r = h / 2
        cr.new_sub_path()
        cr.move_to(x, y)
        cr.line_to(end - r, y)
        cr.arc(end - r, y + r, r, -math.pi / 2, math.pi / 2)
        cr.line_to(x, y + h)
        cr.close_path()
    elif kind in ("or", "nor", "xor"):
        end = x + w - (10 if kind == "nor" else 0)
        left = x + (8 if kind == "xor" else 0)
        cr.new_sub_path()
        cr.move_to(left, y)
        cr.curve_to(left + (end - left) * 0.55, y, end - (end - left) * 0.15, y + h * 0.2, end, y + h / 2)
        cr.curve_to(end - (end - left) * 0.15, y + h * 0.8, left + (end - left) * 0.55, y + h, left, y + h)
        cr.curve_to(left + w * 0.18, y + h * 0.7, left + w * 0.18, y + h * 0.3, left, y)
        cr.close_path()
    elif kind == "terminator":
        _rounded(cr, x, y, w, h, h / 2)
    elif kind == "cylinder":
        ry = min(h * 0.14, 14)
        cr.new_sub_path()
        cr.move_to(x, y + ry)
        cr.curve_to(x, y - ry / 3, x + w, y - ry / 3, x + w, y + ry)
        cr.line_to(x + w, y + h - ry)
        cr.curve_to(x + w, y + h + ry / 3, x, y + h + ry / 3, x, y + h - ry)
        cr.close_path()
    elif kind == "cloud":
        cr.new_sub_path()
        cr.move_to(x + w * 0.25, y + h * 0.85)
        cr.curve_to(x - w * 0.05, y + h * 0.85, x - w * 0.05, y + h * 0.35, x + w * 0.22, y + h * 0.38)
        cr.curve_to(x + w * 0.2, y + h * 0.02, x + w * 0.55, y - h * 0.02, x + w * 0.62, y + h * 0.25)
        cr.curve_to(x + w * 0.95, y + h * 0.08, x + w * 1.08, y + h * 0.6, x + w * 0.82, y + h * 0.62)
        cr.curve_to(x + w * 0.95, y + h * 0.9, x + w * 0.6, y + h * 0.98, x + w * 0.25, y + h * 0.85)
        cr.close_path()
    elif kind == "document":
        cr.new_sub_path()
        cr.move_to(x, y)
        cr.line_to(x + w, y)
        cr.line_to(x + w, y + h * 0.85)
        cr.curve_to(x + w * 0.7, y + h * 0.7, x + w * 0.3, y + h * 1.05, x, y + h * 0.88)
        cr.close_path()
    elif kind == "bus":
        _rounded(cr, x, y, w, h, min(w, h) / 2)
    else:
        r = 10 if kind in ("rounded", "process") else 4 if kind in ("block", "module", "register", "fifo",
                                                                    "memory", "clock", "container") else 0
        _rounded(cr, x, y, w, h, r)


def _decor(cr, s):
    """The marks inside a shape that make it what it is: a register's clock triangle, a FIFO's cells, a bubble."""
    x, y, w, h = s["x"], s["y"], s["w"], s["h"]
    kind = s["kind"]
    cr.set_line_width(1)
    if kind in ("module", "container"):
        cr.move_to(x, y + 22)
        cr.line_to(x + w, y + 22)
        cr.stroke()
    elif kind == "register":
        cr.move_to(x, y + h - 22)
        cr.line_to(x + 10, y + h - 14)
        cr.line_to(x, y + h - 6)
        cr.stroke()
    elif kind == "fifo":
        for i in range(1, 4):
            cr.move_to(x + w * (0.55 + i * 0.11), y)
            cr.line_to(x + w * (0.55 + i * 0.11), y + h)
        cr.stroke()
    elif kind == "memory":
        for i in range(1, 4):
            cr.move_to(x, y + h * i / 4)
            cr.line_to(x + w * 0.22, y + h * i / 4)
        cr.move_to(x + w * 0.22, y)
        cr.line_to(x + w * 0.22, y + h)
        cr.stroke()
    elif kind == "clock":
        px, py, step = x + 8, y + h * 0.7, min((w - 16) / 6, 10)
        cr.move_to(px, py)
        for i in range(3):
            cr.line_to(px + step * (2 * i), y + h * 0.3)
            cr.line_to(px + step * (2 * i + 1), y + h * 0.3)
            cr.line_to(px + step * (2 * i + 1), py)
            cr.line_to(px + step * (2 * i + 2), py)
        cr.stroke()
    elif kind == "cylinder":
        ry = min(h * 0.14, 14)
        cr.move_to(x, y + ry)
        cr.curve_to(x, y + ry * 2.3, x + w, y + ry * 2.3, x + w, y + ry)
        cr.stroke()
    elif kind in ("not", "nand", "nor"):
        cr.new_sub_path()
        cr.arc(x + w - 5, y + h / 2, 5, 0, 2 * math.pi)
        cr.stroke()
    elif kind == "xor":
        cr.move_to(x, y)
        cr.curve_to(x + w * 0.18, y + h * 0.3, x + w * 0.18, y + h * 0.7, x, y + h)
        cr.stroke()


def draw_shape(cr, s, selected=False, text_color=(0.11, 0.13, 0.19)):
    fill, stroke = colors_of(s)
    kind = s["kind"]
    if kind != "text":
        _path(cr, s)
        if kind == "container":
            cr.set_source_rgba(*_rgb(fill), 0.35)
        else:
            cr.set_source_rgb(*_rgb(fill))
        cr.fill_preserve()
        cr.set_source_rgb(*_rgb(stroke))
        cr.set_line_width(s.get("line", 1.4))
        cr.set_dash([6, 4] if s.get("dashed") else [])
        cr.stroke()
        cr.set_dash([])
        _decor(cr, s)
    lines = str(s.get("label", "")).split("\n")
    size = float(s.get("font", 12.5))
    cr.set_font_size(size)
    weight = cairo.FONT_WEIGHT_BOLD if s.get("bold") else cairo.FONT_WEIGHT_NORMAL
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, weight)
    cr.set_source_rgb(*(text_color if kind == "text" and not s.get("text_color") else
                        _rgb(s.get("text_color") or "#1c2230")))
    x, top, w, height = s["x"], s["y"], s["w"], s["h"]
    if kind in ("module", "container"):
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ext = cr.text_extents(lines[0])
        cr.move_to(x + (w - ext.width) / 2 - ext.x_bearing, top + 15)
        cr.show_text(lines[0])
        cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, weight)
        lines, top, height = lines[1:], top + 22, height - 22
    elif kind == "clock":
        top, height = top - h_shift(s), height
    align = s.get("align", "center")
    y = top + (height - len(lines) * (size + 4)) / 2 + size
    if kind == "clock":
        y = s["y"] + s["h"] + size + 2
    for line in lines:
        ext = cr.text_extents(line)
        if align == "left":
            tx = x + 8
        elif align == "right":
            tx = x + w - ext.width - 8
        else:
            tx = x + (w - ext.width) / 2
        cr.move_to(tx - ext.x_bearing, y)
        cr.show_text(line)
        y += size + 4
    cr.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)


def h_shift(_s):
    return 0


def _head(cr, kind, x1, y1, x2, y2, size=9):
    """An arrow head at (x2, y2) for a line coming from (x1, y1)."""
    if kind == "none":
        return
    angle = math.atan2(y2 - y1, x2 - x1)
    if kind == "dot":
        cr.new_sub_path()
        cr.arc(x2 - 4 * math.cos(angle), y2 - 4 * math.sin(angle), 4, 0, 2 * math.pi)
        cr.fill()
        return
    left = (x2 - size * math.cos(angle - 0.4), y2 - size * math.sin(angle - 0.4))
    right = (x2 - size * math.cos(angle + 0.4), y2 - size * math.sin(angle + 0.4))
    if kind == "open":
        cr.move_to(*left)
        cr.line_to(x2, y2)
        cr.line_to(*right)
        cr.stroke()
    elif kind == "diamond":
        back = (x2 - 1.8 * size * math.cos(angle), y2 - 1.8 * size * math.sin(angle))
        _poly(cr, [(x2, y2), left, back, right])
        cr.fill()
    else:
        _poly(cr, [(x2, y2), left, right])
        cr.fill()


def label_point(pts):
    """Where an arrow's label sits: the middle of its longest segment."""
    best = max(zip(pts, pts[1:]), key=lambda seg: math.hypot(seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]))
    return (best[0][0] + best[1][0]) / 2, (best[0][1] + best[1][1]) / 2


def _draw_edge(cr, d, e, selected, text_color):
    pts = d.edge_points(e)
    if len(pts) < 2:
        return
    color = _rgb(e["color"]) if e.get("color") else (0.35, 0.39, 0.47)
    cr.set_source_rgb(*((0.24, 0.35, 1) if selected else color))
    cr.set_line_width((3.2 if e.get("bus") else float(e.get("width", 1.6))) + (0.8 if selected else 0))
    cr.set_dash([6, 4] if e.get("dashed") else [])
    cr.move_to(*pts[0])
    if e.get("style") == "curved" and len(pts) == 2:
        (x1, y1), (x2, y2) = pts
        s_side, t_side = d.sides_for(e)
        k = max(abs(x2 - x1), abs(y2 - y1)) / 2
        out = {"left": (-k, 0), "right": (k, 0), "top": (0, -k), "bottom": (0, k)}
        c1 = (x1 + out[s_side][0], y1 + out[s_side][1])
        c2 = (x2 + out[t_side][0], y2 + out[t_side][1])
        cr.curve_to(*c1, *c2, x2, y2)
        tail, tip = (c2, (x2, y2)), (c1, (x1, y1))
    else:
        for p in pts[1:]:
            cr.line_to(*p)
        tail, tip = (pts[-2], pts[-1]), (pts[1], pts[0])
    cr.stroke()
    cr.set_dash([])
    cr.set_line_width(1.6)
    _head(cr, e.get("head", "arrow"), *tail[0], *tail[1])
    start = e.get("tail") or ("arrow" if e.get("both") else "none")
    _head(cr, start, *tip[0], *tip[1])
    label = e.get("label", "") + (f" [{e['bus']}]" if e.get("bus") else "")
    if label:
        mx, my = label_point(pts)
        cr.set_font_size(11)
        ext = cr.text_extents(label)
        cr.set_source_rgb(1, 1, 1)
        cr.rectangle(mx - ext.width / 2 - 3, my - ext.height / 2 - 4, ext.width + 6, ext.height + 6)
        cr.fill()
        cr.set_source_rgb(*text_color if text_color[0] < 0.5 else (0.11, 0.13, 0.19))
        cr.move_to(mx - ext.width / 2 - ext.x_bearing, my + ext.height / 2 - 1)
        cr.show_text(label)


def export(d, path, scale=2.0):
    """Write the diagram to a PNG, SVG, PDF or draw.io file (by its suffix)."""
    x0, y0, x1, y1 = d.bounds()
    w, h = int((x1 - x0) * scale), int((y1 - y0) * scale)
    path = str(path)
    low = path.lower()
    if low.endswith(".drawio"):
        with open(path, "w") as f:
            f.write(to_drawio(d))
        return
    if low.endswith((".svg", ".pdf")):
        surface = (cairo.SVGSurface if low.endswith(".svg") else cairo.PDFSurface)(path, x1 - x0, y1 - y0)
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
    if low.endswith((".svg", ".pdf")):
        surface.finish()
    else:
        surface.write_to_png(path)


def render_png(d, path, max_side=1400):
    """A PNG no larger than max_side pixels on its long side (for the assistant to look at); the path."""
    x0, y0, x1, y1 = d.bounds()
    export(d, path, scale=min(2.0, max_side / max(x1 - x0, y1 - y0, 1)))
    return path


# draw.io (diagrams.net) styles for each kind; shapes it has no match for become plain boxes with their label.
DRAWIO = {"block": "rounded=0", "rounded": "rounded=1", "process": "rounded=1", "ellipse": "ellipse",
          "state": "ellipse", "adder": "ellipse", "diamond": "rhombus", "decision": "rhombus",
          "text": "text;strokeColor=none;fillColor=none", "note": "shape=note", "hexagon": "shape=hexagon",
          "parallelogram": "shape=parallelogram", "data": "shape=parallelogram", "triangle": "triangle;direction=north",
          "cylinder": "shape=cylinder3", "cloud": "ellipse;shape=cloud", "container": "swimlane",
          "module": "swimlane", "terminator": "rounded=1;arcSize=50", "document": "shape=document",
          "port": "shape=offPageConnector;direction=north", "mux": "shape=trapezoid;direction=south",
          "and": "shape=mxgraph.electrical.logic_gates.logic_gate;operation=and",
          "nand": "shape=mxgraph.electrical.logic_gates.logic_gate;operation=and;negating=1",
          "or": "shape=mxgraph.electrical.logic_gates.logic_gate;operation=or",
          "nor": "shape=mxgraph.electrical.logic_gates.logic_gate;operation=or;negating=1",
          "xor": "shape=mxgraph.electrical.logic_gates.logic_gate;operation=xor",
          "not": "shape=mxgraph.electrical.logic_gates.inverter_2", "buffer": "shape=mxgraph.electrical.logic_gates.buffer2",
          "bus": "rounded=1;arcSize=50"}
DRAWIO_SIDE = {"left": (0, 0.5), "right": (1, 0.5), "top": (0.5, 0), "bottom": (0.5, 1)}
DRAWIO_HEAD = {"arrow": "block", "open": "open", "diamond": "diamond", "dot": "oval", "none": "none"}


def to_drawio(d):
    """The diagram as a draw.io (diagrams.net) file, to open and keep editing there."""
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for s in d.shapes:
        fill, stroke = colors_of(s)
        style = [DRAWIO.get(s["kind"], "rounded=0"), "whiteSpace=wrap", "html=1", f"fontSize={int(s.get('font', 12))}",
                 f"align={s.get('align', 'center')}"]
        if s["kind"] != "text":
            style += [f"fillColor={fill}", f"strokeColor={stroke}"]
        if s.get("bold"):
            style.append("fontStyle=1")
        if s.get("dashed"):
            style.append("dashed=1")
        label = str(s.get("label", "")).replace("\n", "<br>")
        cells.append(f'<mxCell id={quoteattr(s["id"])} value={quoteattr(label)} style={quoteattr(";".join(style) + ";")} '
                     f'vertex="1" parent="1"><mxGeometry x="{s["x"]}" y="{s["y"]}" width="{s["w"]}" '
                     f'height="{s["h"]}" as="geometry"/></mxCell>')
    for e in d.edges:
        if not d.shape(e["src"]) or not d.shape(e["dst"]):
            continue
        s_side, t_side = d.sides_for(e)
        kind = e.get("style", "orthogonal")
        style = ["edgeStyle=orthogonalEdgeStyle" if kind == "orthogonal" else "edgeStyle=none",
                 "rounded=0", "html=1", f"endArrow={DRAWIO_HEAD[e.get('head', 'arrow')]}",
                 f"startArrow={DRAWIO_HEAD[e.get('tail') or ('arrow' if e.get('both') else 'none')]}",
                 f"strokeWidth={3 if e.get('bus') else 1}",
                 f"exitX={DRAWIO_SIDE[s_side][0]}", f"exitY={DRAWIO_SIDE[s_side][1]}",
                 f"entryX={DRAWIO_SIDE[t_side][0]}", f"entryY={DRAWIO_SIDE[t_side][1]}"]
        if kind == "curved":
            style.append("curved=1")
        if e.get("dashed"):
            style.append("dashed=1")
        if e.get("color"):
            style.append(f"strokeColor={e['color']}")
        label = e.get("label", "") + (f" [{e['bus']}]" if e.get("bus") else "")
        points = "".join(f'<mxPoint x="{px}" y="{py}"/>' for px, py in e.get("points", []))
        geometry = f'<mxGeometry relative="1" as="geometry">' + \
            (f'<Array as="points">{points}</Array>' if points else "") + "</mxGeometry>"
        cells.append(f'<mxCell id={quoteattr(e["id"])} value={quoteattr(label)} style={quoteattr(";".join(style) + ";")} '
                     f'edge="1" parent="1" source={quoteattr(e["src"])} target={quoteattr(e["dst"])}>{geometry}'
                     "</mxCell>")
    x0, y0, x1, y1 = d.bounds()
    return ('<mxfile host="ORFS Flow Runner"><diagram name=' + quoteattr(d.title or "Diagram") + ">"
            f'<mxGraphModel dx="{int(x1 - x0)}" dy="{int(y1 - y0)}" grid="1" gridSize="{GRID}" guides="1" '
            'tooltips="1" connect="1" arrows="1" page="0" math="0" shadow="0"><root>'
            + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>\n")


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
