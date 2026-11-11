"""Per-layer routing capacity of the Routing page: the set_global_routing_layer_adjustment lines of FASTROUTE_TCL.

An adjustment reserves part of a layer's tracks, so global routing plans less on it and detailed routing has room:
0.5 on metal2-metal3 means half their capacity is held back. When FASTROUTE_TCL is set, the flow runs that script
and ROUTING_LAYER_ADJUSTMENT is not used; without it, ROUTING_LAYER_ADJUSTMENT applies to every routing layer.
Only the adjustment lines are edited; every other line of the script is kept as it is.
"""

import re

ADJ_RE = re.compile(r"^(\s*)set_global_routing_layer_adjustment\s+(\S+)\s+(\S+)\s*$")
NUMBER_RE = re.compile(r"^\d+(\.\d+)?$")


def parse(text):
    """Adjustments in file order: [{"line": index, "layers": "metal2-metal3", "value": "0.5"}]."""
    out = []
    for i, line in enumerate(text.split("\n")):
        m = ADJ_RE.match(line)
        if m:
            out.append({"line": i, "layers": m.group(2), "value": m.group(3)})
    return out


def rebuild(text, rows):
    """The script with the adjustment lines replaced by rows, in their place; new rows go after the last one."""
    lines = text.split("\n")
    old = [r["line"] for r in parse(text)]
    new = [f"set_global_routing_layer_adjustment {r['layers']} {r['value']}" for r in rows]
    at = old[0] if old else 0
    for i in reversed(old):
        del lines[i]
    lines[at:at] = new
    return "\n".join(lines)


def problems(rows, layers):
    """Values outside 0..1 and layer names that are not routing layers; plain words."""
    out = []
    for r in rows:
        v = r["value"]
        if not NUMBER_RE.match(v) or not 0 <= float(v) <= 1:
            out.append(f"{r['layers']}: the adjustment must be a number from 0 to 1, not '{v}'")
        for name in r["layers"].split("-"):
            if "$" not in name and name not in layers:
                out.append(f"{r['layers']}: '{name}' is not a routing layer")
    return out
