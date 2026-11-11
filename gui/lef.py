"""Read what the Inputs pages need from LEF files: routing layers, macro sizes and the placement site.

Only the few statements used here are parsed; everything else in the LEF is skipped.
"""

import re

LAYER_RE = re.compile(r"^\s*LAYER\s+(\S+)\s*$")
TYPE_RE = re.compile(r"^\s*TYPE\s+(\w+)\s*;")
DIRECTION_RE = re.compile(r"^\s*DIRECTION\s+(\w+)\s*;")
PITCH_RE = re.compile(r"^\s*PITCH\s+([\d.]+)")
MACRO_RE = re.compile(r"^\s*MACRO\s+(\S+)\s*$")
SITE_RE = re.compile(r"^\s*SITE\s+(\S+)\s*$")
SIZE_RE = re.compile(r"^\s*SIZE\s+([\d.]+)\s+BY\s+([\d.]+)\s*;")
CLASS_RE = re.compile(r"^\s*CLASS\s+(\w+)")


def _lines(path):
    try:
        with open(path, errors="replace") as f:
            yield from f
    except OSError:
        return


def routing_layers(tech_lef):
    """Routing layers from bottom to top: [{"name", "direction", "pitch"}]."""
    layers, cur = [], None
    for line in _lines(tech_lef):
        m = LAYER_RE.match(line)
        if m:
            cur = {"name": m.group(1), "type": None, "direction": None, "pitch": None}
            continue
        if cur is None:
            continue
        if line.strip() == f"END {cur['name']}":
            if cur["type"] == "ROUTING":
                layers.append({k: cur[k] for k in ("name", "direction", "pitch")})
            cur = None
        elif (m := TYPE_RE.match(line)):
            cur["type"] = m.group(1)
        elif (m := DIRECTION_RE.match(line)):
            cur["direction"] = m.group(1).lower()
        elif (m := PITCH_RE.match(line)) and cur["pitch"] is None:
            cur["pitch"] = float(m.group(1))
    return layers


def macro_sizes(lef_paths):
    """Size in microns of every MACRO in the files: {name: (width, height)}; block macros only when CLASS is known."""
    sizes = {}
    for path in lef_paths:
        cur, cls, size = None, None, None
        for line in _lines(path):
            m = MACRO_RE.match(line)
            if m:
                cur, cls, size = m.group(1), None, None
                continue
            if cur is None:
                continue
            if line.strip() == f"END {cur}":
                if size and cls in (None, "BLOCK", "RING"):
                    sizes[cur] = size
                cur = None
            elif (m := SIZE_RE.match(line)):
                size = (float(m.group(1)), float(m.group(2)))
            elif (m := CLASS_RE.match(line)) and cls is None:
                cls = m.group(1).upper()
    return sizes


def site_size(lef_paths, site):
    """(width, height) of a placement SITE, or None if it is not found."""
    for path in lef_paths:
        cur = None
        for line in _lines(path):
            m = SITE_RE.match(line)
            if m:
                cur = m.group(1)
            elif cur == site and (m := SIZE_RE.match(line)):
                return float(m.group(1)), float(m.group(2))
            elif cur and line.strip() == f"END {cur}":
                cur = None
    return None
