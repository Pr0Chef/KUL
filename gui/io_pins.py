"""IO pin constraints for ORFS Flow Runner: read and write the set_io_pin_constraint lines of IO_CONSTRAINTS.

Rows set on the IO pins page are written to one marked block of the file; lines outside it are read, shown and
left alone. A row is a port, a whole bus (all its bits), or a bit range of a bus such as boot_addr_i[31:16].
"""

import re

BEGIN = "# >>> ORFS Flow Runner: IO pins. Set on the IO pins page; the lines up to the end marker are rewritten."
END = "# <<< ORFS Flow Runner: IO pins"
EDGES = ["left", "right", "top", "bottom"]
HEADER = ("# IO pin constraints, read by the floorplan stage (IO_CONSTRAINTS).\n"
          "# Regions are left, right, top or bottom, with * for the whole edge or a range in microns (bottom:0-50).\n")
SPEC_RE = re.compile(r"^([A-Za-z_\\][\w$.\\]*?)(?:\[(\d+)(?::(\d+))?\])?$")
INTERVAL_RE = re.compile(r"\*|\d+(?:\.\d+)?-\d+(?:\.\d+)?")


def split_spec(spec):
    """("boot_addr_i", 31, 16) for 'boot_addr_i[31:16]', (name, 3, 3) for a bit, (name, None, None) for all."""
    m = SPEC_RE.match(spec)
    if not m:
        return spec, None, None
    if m.group(2) is None:
        return m.group(1), None, None
    hi = int(m.group(2))
    return m.group(1), hi, int(m.group(3)) if m.group(3) is not None else hi


def spec_width(spec, bits):
    """Pins a spec covers, given the port's bits ((msb, lsb), "scalar" or "?"); None if unknown."""
    name, hi, lo = split_spec(spec)
    if hi is not None:
        return abs(hi - lo) + 1
    if bits == "scalar":
        return 1
    return abs(bits[0] - bits[1]) + 1 if isinstance(bits, tuple) else None


def check_spec(spec, bits):
    """None if the spec fits the port, else why not."""
    name, hi, lo = split_spec(spec)
    if hi is None:
        return None
    if bits == "scalar":
        return f"{name} is a single pin, it has no bits to select"
    if isinstance(bits, tuple):
        top, bottom = max(bits), min(bits)
        if not (bottom <= lo <= top and bottom <= hi <= top):
            return f"{name} has bits {bits[0]}:{bits[1]}"
    return None


def logical(text):
    """[(first line number (1-based), joined text)] with backslash continuations joined."""
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        start, cur = i, lines[i]
        while cur.rstrip().endswith("\\") and i + 1 < len(lines):
            i += 1
            cur = cur.rstrip()[:-1] + " " + lines[i].strip()
        out.append((start + 1, cur))
        i += 1
    return out


def _braced(cmd, opt):
    """The value of option opt: a {braced list}, a [bracketed command] (nested brackets allowed) or one word."""
    m = re.search(re.escape(opt) + r"\s+", cmd)
    if not m:
        return None
    i = m.end()
    if i < len(cmd) and cmd[i] in "{[":
        close, depth = "}" if cmd[i] == "{" else "]", 0
        for j in range(i, len(cmd)):
            depth += cmd[j] == cmd[i]
            depth -= cmd[j] == close
            if depth == 0:
                v = cmd[i:j + 1]
                return v[1:-1] if v.startswith("{") else v
        return cmd[i:]
    return cmd[i:].split()[0] if cmd[i:].split() else None


def parse(text):
    """Constraints in an IO file: [{line, edge, interval, group, order, pins, direction, managed, place}]."""
    lines = text.split("\n")
    span = block_span(lines)
    out = []
    for ln, cmd in logical(text):
        s = cmd.strip()
        if s.startswith("#"):
            continue
        managed = bool(span) and span[0] < ln - 1 < span[1]
        if s.startswith("set_io_pin_constraint"):
            region = _braced(s, "-region") or ""
            edge, _, interval = region.partition(":")
            pins = _braced(s, "-pin_names")
            out.append({"line": ln, "edge": edge, "interval": interval or "*", "group": " -group" in f" {s}",
                        "order": " -order" in f" {s}", "pins": pins.split() if pins and not pins.startswith("[")
                        else [], "names_expr": pins if pins and pins.startswith("[") else None,
                        "direction": _braced(s, "-direction"), "managed": managed, "place": False})
        elif s.startswith("place_pin"):
            out.append({"line": ln, "edge": "", "interval": "", "group": False, "order": False,
                        "pins": [_braced(s, "-pin_name") or ""], "names_expr": None, "direction": None,
                        "managed": managed, "place": True, "location": _braced(s, "-location")})
    return out


def block_span(lines):
    try:
        first = next(i for i, ln in enumerate(lines) if ln.strip() == BEGIN)
        last = next(i for i in range(first, len(lines)) if lines[i].strip() == END)
        return first, last
    except StopIteration:
        return None


def render(rows):
    """Block lines for [{spec, edge, interval, group, order}]: ungrouped rows on the same region share a line."""
    out, shared = [BEGIN], {}
    for r in rows:
        if not r.get("edge"):
            continue
        region = f"{r['edge']}:{r.get('interval') or '*'}"
        if r.get("group"):
            flags = " -group" + (" -order" if r.get("order") else "")
            out.append(f"set_io_pin_constraint -region {region}{flags} -pin_names {{{r['spec']}}}")
        else:
            shared.setdefault(region, []).append(r["spec"])
    for region, specs in shared.items():
        out.append(f"set_io_pin_constraint -region {region} -pin_names {{{' '.join(specs)}}}")
    out.append(END)
    return out if len(out) > 2 else []


def set_block(text, rows):
    """Replace or append the managed block in an IO file's text."""
    lines = text.split("\n") if text else []
    span = block_span(lines)
    new = render(rows)
    if span:
        lines[span[0]:span[1] + 1] = new
        return "\n".join(lines)
    if not new:
        return text
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + ([""] if lines else []) + new + [""])


def managed_rows(text):
    """Rows the page wrote, from the block: [{spec, edge, interval, group, order}]."""
    rows = []
    for c in parse(text):
        if c["managed"] and not c["place"]:
            rows += [{"spec": p, "edge": c["edge"], "interval": c["interval"], "group": c["group"],
                      "order": c["order"]} for p in c["pins"]]
    return rows
