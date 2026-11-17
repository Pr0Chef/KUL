"""The diagram editor, off screen, driven through the canvas's mouse and key handlers.

Checks: adding shapes from the palette, selecting (click, Shift+click, a selection box), moving with snapping,
resizing by a handle, drawing an arrow from a connection point, adding a waypoint, editing a label in place,
styles from the format panel, copy / paste / duplicate, align and distribute, group, stacking order, undo and
redo, zoom and fit, saving and reloading, export to PNG, SVG, PDF and draw.io, and a block diagram from RTL.

Set SHOTS=<folder> to save screenshots.
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness as hn  # noqa: E402

import diagram as dg  # noqa: E402
import workspace as ws  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="diagram-gui-"))
area = tmp / "area"
ws.create_work_area(area, hn.KUL_FLOW)
proj = ws.new_project(area, "soc", "soc")
(proj.dir / "soc.v").write_text("""module cpu (input clk, input [7:0] din, output [7:0] addr); endmodule
module ram (input clk, input [7:0] addr, output [7:0] dout); endmodule
module soc (input clk, input rst, output [7:0] dbg);
  wire [7:0] a, d;
  cpu u_cpu (.clk(clk), .din(d), .addr(a));
  ram u_ram (.clk(clk), .addr(a), .dout(d));
  assign dbg = a;
endmodule
""")
h = hn.Harness(area, shots=os.environ.get("SHOTS"))
app = h.app
app.resize(1440, 900)
app.set_project("soc")
app.show_section("diagram")
h.pump(0.8)
page = app.section_pages["diagram"]
Gdk = hn.Gdk


def screen(x, y):
    return x * page.zoom + page.ox, y * page.zoom + page.oy


def mouse(kind, x, y, button=1, state=0, double=False):
    """Send a mouse event at diagram coordinates (x, y) to the canvas's handlers."""
    sx, sy = screen(x, y)
    e = NS(x=sx, y=sy, button=button, state=Gdk.ModifierType(state),
           type=Gdk.EventType._2BUTTON_PRESS if double else Gdk.EventType.BUTTON_PRESS)
    {"press": page.on_press, "release": page.on_release, "move": page.on_motion}[kind](page.area, e)
    h.pump(0.02)


def drag(x0, y0, x1, y1, state=0, steps=6):
    mouse("move", x0, y0)
    mouse("press", x0, y0, state=state)
    for i in range(1, steps + 1):
        mouse("move", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, state=state)
    mouse("release", x1, y1, state=state)


def click(x, y, state=0):
    mouse("move", x, y)
    mouse("press", x, y, state=state)
    mouse("release", x, y, state=state)


def key(name, ctrl=False, shift=False):
    state = Gdk.ModifierType((Gdk.ModifierType.CONTROL_MASK if ctrl else 0) |
                             (Gdk.ModifierType.SHIFT_MASK if shift else 0))
    page.on_key(page.area, NS(keyval=Gdk.keyval_from_name(name), state=state))
    h.pump(0.02)


SHIFT = int(Gdk.ModifierType.SHIFT_MASK)
page.ox, page.oy, page.zoom = 0.0, 0.0, 1.0

# ---------------- palette ----------------
h.check("palette lists every shape kind", len(dg.KINDS) == 34 and len(set(dg.KINDS)) == 34)
a = page.add_shape("module", 200, 150)
b = page.add_shape("register", 500, 150)
c = page.add_shape("mux", 500, 320)
h.check("shapes added and the last one selected", len(page.d.shapes) == 3 and page.selected == {c["id"]})
h.check("added shapes sit on the grid", all(s["x"] % 10 == 0 and s["y"] % 10 == 0 for s in page.d.shapes))

# ---------------- selecting ----------------
click(a["x"] + 20, a["y"] + 40)
h.check("click selects one shape", page.selected == {a["id"]})
click(b["x"] + 20, b["y"] + 20, state=SHIFT)
h.check("Shift+click adds to the selection", page.selected == {a["id"], b["id"]})
click(900, 600)
h.check("click on empty space clears the selection", not page.selected)
drag(100, 80, 620, 260)
h.check("a selection box takes the shapes wholly inside it", page.selected == {a["id"], b["id"]}, page.selected)

# ---------------- moving, snapping, resizing ----------------
click(900, 600)
ax, ay = a["x"], a["y"]
drag(ax + 30, ay + 40, ax + 30 + 43, ay + 40 + 87)
a = page.d.shape(a["id"])
h.check("drag moves a shape, snapped to the grid", (a["x"], a["y"]) == (ax + 40, ay + 90), (a["x"], a["y"]))
by = page.d.shape(b["id"])["y"]
drag(a["x"] + 30, a["y"] + 40, a["x"] + 30, a["y"] + 40 + (by - a["y"]) + 3)
a = page.d.shape(a["id"])
h.check("a move snaps to another shape's edge", a["y"] == by, (a["y"], by))
w0, h0 = a["w"], a["h"]
drag(a["x"] + a["w"], a["y"] + a["h"], a["x"] + a["w"] + 60, a["y"] + a["h"] + 40)
a = page.d.shape(a["id"])
h.check("the corner handle resizes", (a["w"], a["h"]) == (w0 + 60, h0 + 40), (a["w"], a["h"]))

# ---------------- arrows ----------------
click(900, 600)
b = page.d.shape(b["id"])
mouse("move", a["x"] + 20, a["y"] + 40)                       # hover: the connection points appear
h.check("hovering a shape shows its connection points", page.hover == a["id"])
sx, sy = page.d.anchor(a, "right")
drag(sx, sy, b["x"] + 5, b["y"] + b["h"] / 2)
h.check("dragging from a connection point draws an arrow", len(page.d.edges) == 1
        and (page.d.edges[0]["src"], page.d.edges[0]["dst"]) == (a["id"], b["id"])
        and page.d.edges[0]["src_side"] == "right" and page.d.edges[0]["dst_side"] == "left", page.d.edges)
e = page.d.edges[0]
h.check("the new arrow is selected", page.selected == {e["id"]})
page.set_edge(style="straight")
pts = page.d.edge_points(e)
mx, my = (pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2
drag(mx, my, mx, my + 80)
h.check("dragging a segment's middle adds a waypoint", len(page.d.edges[0].get("points", [])) == 1,
        page.d.edges[0].get("points"))
page.clear_waypoints()
page.set_edge(style="orthogonal", bus=8, head="open", tail="dot", dashed=True)
e = page.d.edges[0]
h.check("arrow style set from the format panel", (e["bus"], e["head"], e["tail"], e["dashed"]) == (8, "open", "dot", True))
c = page.d.shape(c["id"])
edge2 = page.connect(b["id"], c["id"])
h.check("arrows route orthogonally", all(p[0] == q[0] or p[1] == q[1] for p, q in
                                          zip(page.d.edge_points(edge2), page.d.edge_points(edge2)[1:])))

# ---------------- labels and styles ----------------
mouse("press", a["x"] + 30, a["y"] + 40, double=True)
h.pump(0.2)
h.check("double-click opens the label editor on the shape", page.editing == a["id"] and page.entry.get_visible())
page.commit_edit("CPU core\nRV32")
h.check("the edited text becomes the label", page.d.shape(a["id"])["label"] == "CPU core\nRV32"
        and not page.entry.get_visible())
page.select([a["id"]])
page.set_style(fill="#e3f6ec", stroke="#1e8e5a", bold=True, font=14, align="left")
s = page.d.shape(a["id"])
h.check("shape style set from the format panel", (s["fill"], s["bold"], s["font"], s["align"])
        == ("#e3f6ec", True, 14, "left"))
h.check("format panel shows the selection", page.bold.get_active() and page.font.get_value() == 14
        and page.sel_label.get_text() == "Selected: Module")

# ---------------- copy, paste, duplicate, align, group, order ----------------
page.select([a["id"], b["id"]])
key("c", ctrl=True)
key("v", ctrl=True)
h.check("copy and paste copy the shapes and the arrow between them", len(page.d.shapes) == 5
        and len(page.d.edges) == 3 and len(page.selected) == 3, (len(page.d.shapes), len(page.d.edges)))
key("d", ctrl=True)
h.check("duplicate", len(page.d.shapes) == 7)
key("z", ctrl=True)
key("z", ctrl=True)
h.check("undo twice removes both copies", len(page.d.shapes) == 3 and len(page.d.edges) == 2)
key("y", ctrl=True)
h.check("redo brings the paste back", len(page.d.shapes) == 5)
key("z", ctrl=True)
page.select([a["id"], b["id"], c["id"]])
page.align("top")
h.check("align top", len({page.d.shape(i)["y"] for i in (a["id"], b["id"], c["id"])}) == 1)
page.align("hspace")
xs = sorted((page.d.shape(i)["x"], page.d.shape(i)["w"]) for i in (a["id"], b["id"], c["id"]))
gaps = [xs[i + 1][0] - (xs[i][0] + xs[i][1]) for i in range(2)]
h.check("space evenly across", abs(gaps[0] - gaps[1]) <= 10, gaps)
page.select([a["id"], b["id"]])
key("g", ctrl=True)
click(900, 600)
click(page.d.shape(a["id"])["x"] + 20, page.d.shape(a["id"])["y"] + 40)
h.check("clicking one shape of a group selects the group", page.selected == {a["id"], b["id"]})
key("g", ctrl=True, shift=True)
page.select([a["id"]])
page.reorder(True)
h.check("bring to front", page.d.shapes[-1]["id"] == a["id"])
key("a", ctrl=True)
h.check("select all", len(page.selected) == 5)
x_before = page.d.shape(a["id"])["x"]
key("Right")
h.check("arrow keys nudge by the grid", page.d.shape(a["id"])["x"] == x_before + 10)
box = page.add_shape("container", 400, 500)
h.check("a container goes behind the other shapes", page.d.shapes[0]["id"] == box["id"])
page.select([box["id"]])
key("Delete")
h.check("Delete removes the selection", not page.d.shape(box["id"]))

# ---------------- view ----------------
page.set_zoom(2.0)
h.check("zoom", page.zoom == 2.0 and page.zoom_label.get_text() == "200%")
page.fit()
h.pump(0.3)
x0, y0, x1, y1 = page.d.bounds()
w, hgt = page.area.get_allocated_width(), page.area.get_allocated_height()
h.check("fit shows the whole diagram", screen(x0, y0)[0] >= 0 and screen(x1, y1)[0] <= w
        and screen(x1, y1)[1] <= hgt and page.zoom <= 1.0, (page.zoom, screen(x1, y1), w, hgt))
page.d.title = "SoC architecture"
page.select([a["id"]])
h.shot("d1_editor")

# ---------------- files ----------------
h.answer_next_dialog(lambda dlg: dlg.widgets["name"].set_text("arch"))
page.save()
path = proj.dir / "docs" / "arch.diagram.json"
h.check("saved in the project's docs folder", path.exists() and not page.is_dirty())
again = dg.Diagram.from_json(path.read_text())
h.check("the saved file holds the styles and arrows", again.shape(a["id"])["bold"] and again.edges[0]["head"] == "open"
        and len(again.edges) == 2)
out = tmp / "out"
out.mkdir()
for ext in ("png", "svg", "pdf", "drawio"):
    dg.export(page.d, out / f"arch.{ext}")
h.check("exports PNG, SVG and PDF", all((out / f"arch.{e}").stat().st_size > 1000 for e in ("png", "svg", "pdf")))
cells = ElementTree.parse(out / "arch.drawio").getroot().iter("mxCell")
cells = list(cells)
h.check("draw.io file has every shape and arrow", sum(c.get("vertex") == "1" for c in cells) == 3
        and sum(c.get("edge") == "1" for c in cells) == 2)

# ---------------- from RTL ----------------
h.answer_next_dialog(lambda dlg: dlg.widgets["module"].set_active_id("soc") and None)
page.on_from_rtl()
h.pump(0.5)
labels = [s["label"] for s in page.d.shapes]
h.check("block diagram from RTL has the ports and instances", {"clk", "rst", "dbg", "u_cpu\ncpu", "u_ram\nram"}
        <= set(labels) and len(page.d.edges) >= 5, labels)
h.shot("d2_from_rtl")
h.done()
