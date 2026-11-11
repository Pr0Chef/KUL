"""Power grid (PDN) script of the Power grid page: read and edit the pdngen commands of PDN_TCL.

A PDN script is a list of OpenROAD pdngen commands. The page edits four of them:
  define_pdn_grid  a grid: the standard cell grid, or one around macros (-macro);
  add_pdn_stripe   straps on one layer of a grid: width, pitch and offset in microns, or -followpins on the rails;
  add_pdn_ring     a ring on two layers around the core (or a macro): widths, spacings and offsets;
  add_pdn_connect  vias between two layers of a grid.
Every other line (global connections, voltage domains, comments) is kept exactly as it is. An edited command is
written back as one line; untouched commands keep their original text and layout.
"""

import re

GRID_CMDS = ("define_pdn_grid", "add_pdn_stripe", "add_pdn_ring", "add_pdn_connect")
# Options that are flags (they take no value).
FLAGS = {"followpins", "extend_to_core_ring", "extend_to_boundary", "snap_to_grid", "macro", "default", "power",
         "ground", "add_connect", "connect_to_pads", "allow_out_of_die", "grid_over_pg_pins", "grid_over_boundary",
         "obstructions_off", "connect_to_pad_layers"}
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")


def words(line):
    """Tcl words of one command: {braced} and [bracketed] groups stay one word, braces removed."""
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
            continue
        if line[i] in "{[":
            open_, close = line[i], "}" if line[i] == "{" else "]"
            depth, j = 0, i
            while j < n:
                depth += line[j] == open_
                depth -= line[j] == close
                if depth == 0:
                    break
                j += 1
            out.append(line[i + 1:j] if open_ == "{" else line[i:j + 1])
            i = j + 1
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append(line[i:j])
            i = j
    return out


def logical(text):
    """[(first line index, last line index, joined text)] with backslash continuations joined."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        first, cur = i, lines[i]
        while cur.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            cur = cur.rstrip()[:-1] + " " + lines[i].strip()
        out.append((first, i, cur))
        i += 1
    return out


def parse(text):
    """The pdngen commands the page edits: [{"cmd", "first", "last", "opts": [[option, value or None]]}]."""
    out = []
    for first, last, line in logical(text):
        if line.lstrip().startswith("#"):
            continue
        w = words(line)
        if not w or w[0] not in GRID_CMDS:
            continue
        opts, i = [], 1
        while i < len(w):
            opt = w[i].lstrip("-")
            if w[i].startswith("-") and opt not in FLAGS and i + 1 < len(w):
                opts.append([opt, w[i + 1]])
                i += 2
            else:
                opts.append([opt, None])
                i += 1
        out.append({"cmd": w[0], "first": first, "last": last, "opts": opts})
    return out


def get(command, opt, default=""):
    for o, v in command["opts"]:
        if o == opt:
            return True if v is None else v
    return default


def set_opt(command, opt, value):
    """Set an option (None removes it, True sets a flag); marks the command as edited."""
    for i, (o, _) in enumerate(command["opts"]):
        if o == opt:
            if value is None or value is False:
                del command["opts"][i]
            else:
                command["opts"][i][1] = None if value is True else value
            break
    else:
        if value is not None and value is not False:
            command["opts"].append([opt, None if value is True else value])
    command["edited"] = True


def render(command):
    """One line of Tcl for a command."""
    parts = [command["cmd"]]
    for opt, value in command["opts"]:
        parts.append(f"-{opt}" if value is None else f"-{opt} {{{value}}}")
    return " ".join(parts)


def new_command(cmd, grid, **opts):
    """A command to add, for example new_command("add_pdn_ring", "grid", layers="metal5 metal6", ...)."""
    return {"cmd": cmd, "first": None, "last": None, "edited": True,
            "opts": [["grid", grid]] + [[k, None if v is True else v] for k, v in opts.items()]}


def connects(commands, grid):
    """Layer pairs with vias in a grid: {frozenset({a, b})}."""
    return {frozenset(str(get(c, "layers")).split()) for c in commands
            if c["cmd"] == "add_pdn_connect" and get(c, "grid") == grid and not c.get("deleted")}


def ring_layers_for(commands, grid, layers):
    """Two free adjacent layers (one horizontal, one vertical) for a new ring: the highest pair below the grid's
    top strap layer that no strap uses. layers: [{"name", "direction"}] bottom to top."""
    names = [x["name"] for x in layers]
    used = {get(c, "layer") for c in commands if c["cmd"] == "add_pdn_stripe" and get(c, "grid") == grid
            and not c.get("deleted")}
    top = max((names.index(u) for u in used if u in names), default=len(names))
    for i in range(min(top, len(names)) - 2, -1, -1):
        a, b = layers[i], layers[i + 1]
        if a["name"] not in used and b["name"] not in used and a["direction"] != b["direction"]:
            return f"{a['name']} {b['name']}"
    return " ".join(names[-2:])


def connect_ring(commands, ring, layers):
    """Make a ring part of the grid, as OpenROAD needs it: the grid's straps run out to the ring
    (-extend_to_core_ring), and each strap layer gets vias to the ring layer that runs across it. Without both,
    pdngen drops the ring or leaves it floating, and the final IR drop check fails. Adds to commands in place."""
    grid = get(ring, "grid")
    direction = {x["name"]: x["direction"] for x in layers}
    ring_layers = str(get(ring, "layers")).split()
    have = connects(commands, grid)
    for s in commands:
        if s["cmd"] != "add_pdn_stripe" or get(s, "grid") != grid or s.get("deleted") \
                or get(s, "followpins", False) is True:
            continue
        if get(s, "extend_to_core_ring", False) is not True:
            set_opt(s, "extend_to_core_ring", True)
        layer = get(s, "layer")
        across = next((r for r in ring_layers if direction.get(r) and direction.get(r) != direction.get(layer)),
                      None)
        if across and frozenset((layer, across)) not in have:
            commands.append(new_command("add_pdn_connect", grid, layers=f"{layer} {across}"))
            have.add(frozenset((layer, across)))
    return commands


def grids(commands):
    """Grid names in file order, with whether each is a macro grid: [(name, is_macro)]."""
    return [(get(c, "name"), get(c, "macro", False) is True) for c in commands
            if c["cmd"] == "define_pdn_grid" and not c.get("deleted")]


def rebuild(text, commands):
    """The script text after the page's edits: edited commands rewritten, deleted ones dropped, new ones added
    after the last command of their grid (or at the end)."""
    lines = text.split("\n")
    by_first = {c["first"]: c for c in commands if c["first"] is not None}
    added = [c for c in commands if c["first"] is None and not c.get("deleted")]
    last_of_grid = {}
    for c in commands:
        if c["first"] is not None and not c.get("deleted"):
            g = get(c, "name") if c["cmd"] == "define_pdn_grid" else get(c, "grid")
            last_of_grid[g] = c["last"]
    out, i = [], 0
    inserts = {}
    for c in added:
        inserts.setdefault(last_of_grid.get(get(c, "grid"), len(lines) - 1), []).append(render(c))
    while i < len(lines):
        c = by_first.get(i)
        if c:
            if not c.get("deleted"):
                out += [render(c)] if c.get("edited") else lines[c["first"]:c["last"] + 1]
            end = c["last"]
        else:
            out.append(lines[i])
            end = i
        for j in range(i, end + 1):
            out += inserts.get(j, [])
        i = end + 1
    return "\n".join(out)


def problems(commands, layers):
    """What OpenROAD would reject, in plain words. layers: routing layer names, bottom to top."""
    out = []

    def positive(value, what):
        for v in str(value).split():
            if not NUMBER_RE.match(v) or float(v) <= 0:
                out.append(f"{what}: '{value}' must be a positive number")
                return

    names = [g for g, _ in grids(commands)]
    for c in commands:
        if c.get("deleted") or c["cmd"] == "define_pdn_grid":
            continue
        grid = get(c, "grid")
        where = f"{c['cmd'].replace('add_pdn_', '')} on grid {grid}"
        if grid not in names:
            out.append(f"{where}: the grid is not defined")
        if c["cmd"] == "add_pdn_stripe":
            layer = get(c, "layer")
            if layer not in layers:
                out.append(f"{where}: layer '{layer}' is not a routing layer")
            positive(get(c, "width"), f"{where} {layer} width")
            if get(c, "followpins", False) is not True:
                positive(get(c, "pitch"), f"{where} {layer} pitch")
                try:
                    if float(get(c, "pitch")) <= float(get(c, "width")):
                        out.append(f"{where} {layer}: the pitch must be larger than the width")
                except ValueError:
                    pass
        elif c["cmd"] == "add_pdn_ring":
            # A ring is only powered when straps run out to it and vias join a strap layer to a ring layer;
            # otherwise pdngen drops it or the final IR drop check fails on it (PSM-0069).
            ring = str(get(c, "layers")).split()
            extended = {get(s, "layer") for s in commands if s["cmd"] == "add_pdn_stripe" and get(s, "grid") == grid
                        and not s.get("deleted") and get(s, "extend_to_core_ring", False) is True}
            joined = any(pair & set(ring) and pair & extended for pair in connects(commands, grid))
            if not joined:
                out.append(f"{where}: no strap reaches it, so it would not be powered; Add ring connects it "
                           "(straps extended to the ring, vias between a strap layer and a ring layer)")
            if len(ring) != 2 or any(x not in layers for x in ring):
                out.append(f"{where}: needs two routing layers, has '{get(c, 'layers')}'")
            # Tcl accepts a unique prefix of an option, and some scripts write -core_offset for -core_offsets.
            for opt in ("widths", "spacings", "core_offsets"):
                value = get(c, opt) or get(c, opt[:-1])
                if value:
                    positive(value, f"{where} {opt.replace('_', ' ')}")
                elif opt != "core_offsets" or not (get(c, "pad_offsets") or get(c, "pad_offset")):
                    out.append(f"{where}: {opt.replace('_', ' ')} is not set")
        elif c["cmd"] == "add_pdn_connect":
            pair = str(get(c, "layers")).split()
            if len(pair) != 2 or any(x not in layers for x in pair):
                out.append(f"{where}: needs two routing layers, has '{get(c, 'layers')}'")
    return out
