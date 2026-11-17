"""Floorplan planning: size the die and core from an area budget and check the plan before the flow runs.

The planner answers the questions a floorplan review asks, from numbers that are known before placement:
  - how big are the core and die for this cell area, utilization, aspect ratio and margin (or what is the
    utilization of a fixed die),
  - do the power grid's straps fit in the core (the cause of OpenROAD's PDN-0185),
  - is there room on the die edge for every IO pin,
  - are there enough rows, is the utilization in a range that places and routes, is the shape sensible,
  - do the macros fit inside the core without overlapping, and how much of the core do they take.
Each check has a plain result and, where a number fixes it, the fix (for example the highest utilization at which
the straps fit). pack_macros gives a first legal macro placement along the core's edges.
"""

import math

import floorplan as fp
import pdn


def straps(pdn_text):
    """Strap definitions of a pdngen script: [{"layer", "width", "pitch", "offset", "spacing", "grid"}], microns.
    Rails that follow the cell rows (-followpins) are left out: they fit any core."""
    out = []
    commands = pdn.parse(pdn_text or "")
    # Grids defined for macros (-macro) sit on the macros, so their straps do not limit the core.
    macro_grids = {str(pdn.get(c, "name")) for c in commands if c["cmd"] == "define_pdn_grid" and pdn.get(c, "macro")}
    for c in commands:
        if c["cmd"] != "add_pdn_stripe" or pdn.get(c, "followpins") or str(pdn.get(c, "grid", "")) in macro_grids:
            continue
        try:
            item = {"layer": str(pdn.get(c, "layer")), "grid": str(pdn.get(c, "grid", "")),
                    "width": float(pdn.get(c, "width")), "pitch": float(pdn.get(c, "pitch")),
                    "offset": float(pdn.get(c, "offset", 0) or 0)}
        except (TypeError, ValueError):
            continue      # the value is a Tcl variable or expression: nothing to check here
        try:
            item["spacing"] = float(pdn.get(c, "spacing"))
        except (TypeError, ValueError):
            item["spacing"] = None
        out.append(item)
    return out


def strap_need(strap):
    """The core size across a strap layer that one power/ground pair of its straps needs: the offset plus both
    straps and the gap between them (half the pitch unless a spacing is given). Below this pdngen stops with
    PDN-0185."""
    gap = strap["spacing"] if strap["spacing"] is not None else strap["pitch"] / 2 - strap["width"]
    return strap["offset"] + 2 * strap["width"] + gap


def plan(mode, cell_area, util=50.0, aspect=1.0, margin=2.0, die_w=0.0, die_h=0.0, site=None):
    """{"die", "core", "utilization", "core_w", "core_h"} for the settings, or None when they give no floorplan.

    mode "util": the core is sized from cell_area and the utilization (as OpenROAD does); "fixed": the die is
    die_w x die_h with the core one margin inside, snapped to the placement site, and the utilization follows.
    """
    if mode == "util":
        if not cell_area or util <= 0 or aspect <= 0:
            return None
        die, core = fp.estimate(cell_area, util, aspect, margin)
    else:
        if die_w <= 2 * margin or die_h <= 2 * margin:
            return None
        die = (0.0, 0.0, die_w, die_h)
        core = fp.snap_core((margin, margin, die_w - margin, die_h - margin), site)
    cw, ch = core[2] - core[0], core[3] - core[1]
    return {"die": die, "core": core, "core_w": cw, "core_h": ch,
            "utilization": 100.0 * cell_area / (cw * ch) if cell_area and cw * ch else None}


def max_utilization(cell_area, aspect, need_w, need_h):
    """The highest utilization (%) at which a core of this aspect ratio is at least need_w wide and need_h tall."""
    if not cell_area:
        return None
    area = max(need_w * need_w * aspect, need_h * need_h / aspect, 1e-9)
    return min(100.0, 100.0 * cell_area / area)


def check(p, cell_area=None, macro_area=0.0, strap_list=(), layers=(), pins=0, pin_layers=(), site=None,
          placements=None, sizes=None, masters=None, halo=(0.0, 0.0), aspect=1.0, mode="util"):
    """Checks of a plan (from plan()): [{"level": "ok" | "warn" | "error", "title", "detail", "fix"}].

    fix is None, or {"label", "set": {setting: value}} with planner settings (util, die_w, die_h) that resolve it.
    layers: routing layers from lef.routing_layers; pin_layers: names of the layers IO pins go on.
    """
    out = []

    def add(level, title, detail, fix=None):
        out.append({"level": level, "title": title, "detail": detail, "fix": fix})

    if p is None:
        add("error", "No floorplan", "Give a cell area (run synthesis, or type an estimate) and a utilization, or "
                                     "a die larger than twice the margin.")
        return out
    cw, ch = p["core_w"], p["core_h"]
    direction = {la["name"]: la["direction"] for la in layers}
    pitch = {la["name"]: la["pitch"] for la in layers}

    # Utilization.
    u = p["utilization"]
    if u is None:
        add("warn", "Utilization unknown", "The cell area is not known yet: run synthesis for a real number.")
    elif u > 100:
        fix = {"label": "Use 60% utilization", "set": {"util": 60.0}} if mode == "util" else \
            {"label": "Grow the die for 60%", "set": grow(p, cell_area, 60.0, aspect)}
        add("error", f"Utilization {u:.0f}%", "The cells do not fit in the core.", fix)
    elif u > 80:
        add("warn", f"Utilization {u:.0f}%", "Above 80% leaves little room for buffers and routing; placement or "
                                             "routing often fails. 40 to 70% is usual.",
            {"label": "Use 65% utilization", "set": {"util": 65.0}} if mode == "util" else None)
    elif u < 10:
        add("warn", f"Utilization {u:.0f}%", "Most of the core is empty: fine for a small block that needs the "
                                             "size for its power grid or pins, wasteful otherwise.")
    else:
        add("ok", f"Utilization {u:.0f}%", f"{cell_area:,.0f} µm² of cells in a {cw * ch:,.0f} µm² core.")

    # Power straps: each strap layer needs room across its direction.
    need_w = need_h = 0.0
    worst = []
    for s in strap_list:
        need = strap_need(s)
        vertical = direction.get(s["layer"], "vertical") == "vertical"
        if vertical:
            need_w = max(need_w, need)
        else:
            need_h = max(need_h, need)
        have = cw if vertical else ch
        if have < need:
            worst.append(f"{s['layer']} needs a core {'width' if vertical else 'height'} of {need:.1f} µm "
                         f"(pitch {s['pitch']:g}, width {s['width']:g}, offset {s['offset']:g}); it is {have:.1f}")
    if worst:
        if mode == "util":
            best = max_utilization(cell_area, aspect, need_w, need_h)
            fix = {"label": f"Use {math.floor(best)}% utilization", "set": {"util": float(math.floor(best))}} \
                if best and best >= 1 else None
        else:
            fix = {"label": "Grow the die to fit", "set": {
                "die_w": round(max(p["die"][2], need_w + (p["die"][2] - cw) + 0.5), 1),
                "die_h": round(max(p["die"][3], need_h + (p["die"][3] - ch) + 0.5), 1)}}
        add("error", "Power straps do not fit", "; ".join(worst) + ". The floorplan stage would stop with PDN-0185. "
            "Make the core larger, or reduce the strap pitch on Inputs → Power grid.", fix)
    elif strap_list:
        add("ok", "Power straps fit", f"The widest strap pair needs {max(need_w, need_h):.1f} µm; the core is "
                                      f"{cw:.1f} × {ch:.1f} µm.")

    # IO pins on the die edge: every other routing track of the pin layers can hold a pin.
    if pins:
        die_w, die_h = p["die"][2] - p["die"][0], p["die"][3] - p["die"][1]
        capacity = 0
        for name in pin_layers:
            if pitch.get(name):
                side = die_w if direction.get(name) == "vertical" else die_h      # vertical layers: top and bottom
                capacity += 2 * int(side / (2 * pitch[name]))
        if capacity:
            if pins > capacity:
                add("error", "Not enough room for the IO pins", f"{pins} pins, but the die edge holds about "
                    f"{capacity} on {', '.join(pin_layers)}. Make the die larger.")
            elif pins > 0.6 * capacity:
                add("warn", "IO pins are tight", f"{pins} pins use {100 * pins / capacity:.0f}% of the {capacity} "
                                                 "places on the die edge.")
            else:
                add("ok", "IO pins fit", f"{pins} pins, about {capacity} places on the die edge.")

    # Rows.
    if site:
        rows = int(ch / site[1])
        if rows < 4:
            add("error" if rows < 1 else "warn", f"{rows} cell row{'s' if rows != 1 else ''}",
                f"The core is {ch:.1f} µm tall and a row is {site[1]:g} µm. Timing repair and clock buffers need "
                "more rows.")
        else:
            add("ok", f"{rows} cell rows", f"Row height {site[1]:g} µm, {int(cw / site[0]):,} sites per row.")

    # Shape.
    ratio = ch / cw if cw else 1
    if ratio > 3 or ratio < 1 / 3:
        add("warn", f"Aspect ratio {ratio:.2f}", "A long, thin core makes routing across it congested; keep the "
                                                  "ratio between about 0.5 and 2 unless the pins need it.")

    # Macros.
    if placements:
        for line in fp.problems(placements, sizes or {}, masters or {}, p["core"], halo):
            add("error", "Macro placement", line)
    if macro_area:
        share = 100 * macro_area / (cw * ch)
        if share > 70:
            add("warn", f"Macros take {share:.0f}% of the core", "Little is left for standard cells and the "
                                                                 "channels between macros.")
        elif not any(c["title"] == "Macro placement" for c in out):
            add("ok", f"Macros take {share:.0f}% of the core",
                "Placed macros are inside the core and clear of each other." if placements else
                "Macros not placed here are placed by the flow.")
    return out


def grow(p, cell_area, util, aspect):
    """Die size for a fixed-die plan that brings the utilization down to util, keeping the margin."""
    area = cell_area / (util / 100.0)
    w = math.sqrt(area / aspect)
    margin = (p["die"][2] - p["core_w"]) / 2
    return {"die_w": round(w + 2 * margin, 1), "die_h": round(w * aspect + 2 * margin, 1)}


def verdict(checks):
    levels = [c["level"] for c in checks]
    return "error" if "error" in levels else "warn" if "warn" in levels else "ok"


def pack_macros(macros, core, halo=(0.0, 0.0), site=None):
    """A first legal placement: macros in columns from the core's bottom-left corner, tallest first, with the
    channel (halo) between neighbours. macros: {instance: (width, height)}. Returns {instance: (x, y, "R0")};
    raises ValueError naming the macros that do not fit."""
    hx, hy = max(halo[0], 0.0), max(halo[1], 0.0)
    x0, y0, x1, y1 = core
    out, left = {}, []
    col_x, y, col_w = x0, y0, 0.0
    for name, (w, h) in sorted(macros.items(), key=lambda kv: (-kv[1][1], -kv[1][0], kv[0])):
        if y + h > y1 + 1e-6 and y > y0:
            col_x, y, col_w = col_x + col_w + hx, y0, 0.0      # next column, one channel to the right
        px, py = col_x, y
        if site:
            px = x0 + math.ceil((px - x0) / site[0] - 1e-6) * site[0]
            py = y0 + math.ceil((py - y0) / site[1] - 1e-6) * site[1]
        if px + w > x1 + 1e-6 or py + h > y1 + 1e-6:
            left.append(name)
            continue
        out[name] = (round(px, 4), round(py, 4), "R0")
        y = py + h + hy
        col_w = max(col_w, w)
    if left:
        raise ValueError(f"{len(left)} macro{'s' if len(left) != 1 else ''} do not fit in the core: "
                         + ", ".join(left[:6]))
    return out
