"""Floorplan of the Floorplan page: die and core size, and where each macro goes.

Die and core come from config variables, in one of two ways, as in ORFS:
  - utilization: CORE_UTILIZATION (%), CORE_ASPECT_RATIO (height / width) and CORE_MARGIN (core to die, microns);
    the flow sizes the core from the cell area, so the page can only estimate it from the synthesis area;
  - fixed: DIE_AREA and CORE_AREA, each "x1 y1 x2 y2" in microns.
Macro positions go in MACRO_PLACEMENT_TCL as place_macro lines, in one marked block that is rewritten on every save.
Macros left out of the block are placed by the flow's macro placer (RTL-MP).
"""

import math
import re

BEGIN = "# >>> ORFS Flow Runner: macros. Set on the Floorplan page; the lines up to the end marker are rewritten."
END = "# <<< ORFS Flow Runner: macros"
ORIENTS = ["R0", "R180", "MX", "MY", "R90", "R270", "MXR90", "MYR90"]
ROTATED = {"R90", "R270", "MXR90", "MYR90"}
PLACE_RE = re.compile(r"^\s*place_macro\s+-macro_name\s+(\{[^}]*\}|\S+)\s+-location\s+\{\s*(\S+)\s+(\S+)\s*\}"
                      r"(?:\s+-orientation\s+(\S+))?\s*$")
AREA_RE = re.compile(r"Chip area(?: for (?:top )?module '?[^:]*'?)?\s*:\s*([\d.]+)")


def parse_box(text):
    """'x1 y1 x2 y2' as four floats, or None if it is not four numbers with x2 > x1 and y2 > y1."""
    try:
        x1, y1, x2, y2 = (float(v) for v in (text or "").split())
    except ValueError:
        return None
    return (x1, y1, x2, y2) if x2 > x1 and y2 > y1 else None


def fmt(v):
    """A micron value without needless digits: 12.0 -> '12', 3.14159 -> '3.142'."""
    return f"{v:.3f}".rstrip("0").rstrip(".")


def estimate(cell_area, utilization, aspect_ratio, margin):
    """(die box, core box) the flow would make for this cell area, utilization (%) and aspect ratio.

    ORFS gives these to OpenROAD's initialize_floorplan, which makes the core area cell_area / utilization with
    height = aspect_ratio * width, then adds the margin on every side. Row rounding makes the real core a little
    different, so the page calls the result an estimate.
    """
    core_area = cell_area / (utilization / 100.0)
    width = math.sqrt(core_area / aspect_ratio)
    height = width * aspect_ratio
    core = (margin, margin, margin + width, margin + height)
    return (0.0, 0.0, width + 2 * margin, height + 2 * margin), core


def snap_core(core, site):
    """The core OpenROAD makes from a fixed CORE_AREA: rounded inward to whole sites across and rows up.

    For nangate45 (site 0.19 x 1.4), 10 10 60 60 becomes 10.07 11.2 59.85 58.8.
    """
    if not core or not site:
        return core
    sw, sh = site
    x1 = math.ceil(round(core[0] / sw, 6)) * sw
    y1 = math.ceil(round(core[1] / sh, 6)) * sh
    x2 = x1 + math.floor(round((core[2] - x1) / sw, 6)) * sw
    y2 = y1 + math.floor(round((core[3] - y1) / sh, 6)) * sh
    return tuple(round(v, 4) for v in (x1, y1, x2, y2))


def synth_area(stat_text):
    """The design's cell area in square microns from Yosys's synth_stat.txt, or None."""
    found = AREA_RE.findall(stat_text or "")
    return float(found[-1]) if found else None


def macro_size(size, orient):
    """Width and height of a macro once it is turned to orient."""
    w, h = size
    return (h, w) if orient in ROTATED else (w, h)


def tcl_name(name):
    """The instance name as OpenROAD wants it in place_macro: brackets of flattened names escaped, in braces."""
    return "{" + re.sub(r"(?<!\\)([\[\]])", r"\\\1", name) + "}"


def plain_name(word):
    """The instance name back from a place_macro word."""
    if word.startswith("{") and word.endswith("}"):
        word = word[1:-1]
    return word.replace("\\[", "[").replace("\\]", "]")


def render(placements):
    """Block lines for {instance: (x, y, orient)}, sorted by name; [] when nothing is placed."""
    if not placements:
        return []
    lines = [BEGIN]
    for name in sorted(placements):
        x, y, orient = placements[name]
        lines.append(f"place_macro -macro_name {tcl_name(name)} -location {{{fmt(x)} {fmt(y)}}} -orientation {orient}")
    return lines + [END]


def block_span(lines):
    try:
        first = next(i for i, ln in enumerate(lines) if ln.strip() == BEGIN)
        last = next(i for i in range(first, len(lines)) if lines[i].strip() == END)
        return first, last
    except StopIteration:
        return None


def parse(text):
    """(placements in the block, placements outside it, other code lines outside it).

    Placements are {instance: (x, y, orient)}; the ones outside the block were written by hand and are shown as
    fixed. Other lines are any other Tcl outside the block, kept as they are.
    """
    lines = (text or "").split("\n")
    span = block_span(lines)
    mine, theirs, other = {}, {}, []
    for i, line in enumerate(lines):
        inside = bool(span) and span[0] <= i <= span[1]
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = PLACE_RE.match(s)
        if m:
            target = mine if inside else theirs
            target[plain_name(m.group(1))] = (float(m.group(2)), float(m.group(3)), m.group(4) or "R0")
        elif not inside:
            other.append((i + 1, s))
    return mine, theirs, other


def set_block(text, placements, header=""):
    """The file text with the block replaced, added at the end, or removed when nothing is placed."""
    lines = (text or header).split("\n")
    span = block_span(lines)
    new = render(placements)
    if span:
        lines[span[0]:span[1] + 1] = new
        return "\n".join(lines)
    if not new:
        return "\n".join(lines)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + ([""] if lines else []) + new + [""])


def overlaps(a, b):
    """True when boxes (x1, y1, x2, y2) share area; touching edges do not count."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def problems(placements, sizes, masters, core, halo=(0.0, 0.0)):
    """Placed macros that stick out of the core or overlap another one, counting the halo; plain words."""
    out, boxes = [], {}
    hx, hy = halo
    for name, (x, y, orient) in sorted(placements.items()):
        size = sizes.get(masters.get(name, ""))
        if not size:
            out.append(f"{name}: size unknown (its LEF is not in ADDITIONAL_LEFS)")
            continue
        w, h = macro_size(size, orient)
        box = (x, y, x + w, y + h)
        boxes[name] = box
        if core and not (box[0] >= core[0] - 1e-6 and box[1] >= core[1] - 1e-6 and box[2] <= core[2] + 1e-6
                         and box[3] <= core[3] + 1e-6):
            out.append(f"{name} is not inside the core area")
    names = sorted(boxes)
    for i, a in enumerate(names):
        ax1, ay1, ax2, ay2 = boxes[a]
        for b in names[i + 1:]:
            if overlaps((ax1 - hx, ay1 - hy, ax2 + hx, ay2 + hy), boxes[b]):
                out.append(f"{a} and {b} overlap" + (" (with the halo)" if not overlaps(boxes[a], boxes[b]) else ""))
    return out
