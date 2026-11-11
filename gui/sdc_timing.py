"""Timing settings of the Timing page, kept in one marked block of the design's SDC file.

The block holds three kinds of settings, rewritten as a whole on every change:
  - per clock: setup and hold uncertainty, transition, source and network latency;
  - design rules: the cell that drives the inputs, the load on the outputs, max transition, fanout and capacitance;
  - timing exceptions: false paths, multicycle paths, and max or min delays between clocks, ports, pins or cells.
Lines outside the block are never changed; the page lists the ones that set the same things, so nothing is hidden.
"""

import re

BEGIN = "# >>> ORFS Flow Runner: timing. Set on the Timing page; the lines up to the end marker are rewritten."
END = "# <<< ORFS Flow Runner: timing"

CLOCK_FIELDS = [  # key, label, SDC command written for it
    ("setup_uncertainty", "Setup uncertainty", "set_clock_uncertainty -setup"),
    ("hold_uncertainty", "Hold uncertainty", "set_clock_uncertainty -hold"),
    ("transition", "Transition", "set_clock_transition"),
    ("source_latency", "Source latency", "set_clock_latency -source"),
    ("network_latency", "Network latency", "set_clock_latency"),
]
RULE_FIELDS = [  # key, label, SDC command, object it applies to
    ("load", "Output load", "set_load", "[all_outputs]"),
    ("max_transition", "Max transition", "set_max_transition", "[current_design]"),
    ("max_fanout", "Max fanout", "set_max_fanout", "[current_design]"),
    ("max_capacitance", "Max capacitance", "set_max_capacitance", "[current_design]"),
]
EXCEPTIONS = {  # type: (label, command, takes a value)
    "false_path": ("False path", "set_false_path", False),
    "multicycle": ("Multicycle path", "set_multicycle_path", True),
    "max_delay": ("Max delay", "set_max_delay", True),
    "min_delay": ("Min delay", "set_min_delay", True),
}
OBJECT_KINDS = ["clocks", "ports", "pins", "cells", "nets"]
NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
OBJ_RE = r"\[get_(clocks|ports|pins|cells|nets) \{([^}]*)\}\]"
# An exception still being filled in is kept in the block as a comment, so the SDC stays valid meanwhile.
INCOMPLETE = "# incomplete: "


def empty():
    """A model with nothing set."""
    return {"clocks": {}, "rules": {}, "exceptions": []}


def _obj(kind, names):
    return f"[get_{kind} {{{names}}}]"


def render(model):
    """The block's lines for a model; [] when nothing is set."""
    out = []
    for clock in sorted(model["clocks"]):
        values = model["clocks"][clock]
        for key, _, cmd in CLOCK_FIELDS:
            if values.get(key, "") != "":
                out.append(f"{cmd} {values[key]} {_obj('clocks', clock)}")
    rules = model["rules"]
    if rules.get("driving_cell"):
        pin = f" -pin {rules['driving_pin']}" if rules.get("driving_pin") else ""
        out.append(f"set_driving_cell -lib_cell {rules['driving_cell']}{pin} [all_inputs -no_clocks]")
    for key, _, cmd, target in RULE_FIELDS:
        if rules.get(key, "") != "":
            out.append(f"{cmd} {rules[key]} {target}")
    for ex in model["exceptions"]:
        _, cmd, has_value = EXCEPTIONS[ex["type"]]
        parts = [cmd]
        if has_value:
            parts.append(ex["value"] or "?")
        if ex.get("check"):
            parts.append(f"-{ex['check']}")
        for end in ("from", "through", "to"):
            if ex.get(end):
                kind, names = ex[end]
                parts.append(f"-{end} {_obj(kind, names)}")
        out.append((INCOMPLETE if incomplete(ex) else "") + " ".join(parts))
    return [BEGIN] + out + [END] if out else []


def incomplete(ex):
    """True while an exception lacks its value, or has no From, Through or To with names."""
    if EXCEPTIONS[ex["type"]][2] and not ex.get("value"):
        return True
    ends = [ex[e] for e in ("from", "through", "to") if ex.get(e)]
    return not ends or any(not names.strip() for _, names in ends)


def block_span(lines):
    """(first, last) line index of the block, or None."""
    try:
        first = next(i for i, ln in enumerate(lines) if ln.strip() == BEGIN)
        last = next(i for i in range(first, len(lines)) if lines[i].strip() == END)
        return first, last
    except StopIteration:
        return None


def set_block(text, model):
    """The SDC text with the block replaced, added at the end, or removed when the model is empty."""
    lines = text.split("\n")
    span = block_span(lines)
    new = render(model)
    if span:
        lines[span[0]:span[1] + 1] = new
        return "\n".join(lines)
    if not new:
        return text
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines + [""] + new + [""])


def _parse_line(line, model):
    """Add one block line to the model; False if the line is not one the page writes."""
    for key, _, cmd in CLOCK_FIELDS:
        m = re.fullmatch(re.escape(cmd) + r" (\S+) " + OBJ_RE, line)
        if m and m.group(2) == "clocks":
            model["clocks"].setdefault(m.group(3), {})[key] = m.group(1)
            return True
    m = re.fullmatch(r"set_driving_cell -lib_cell (\S+)(?: -pin (\S+))? \[all_inputs -no_clocks\]", line)
    if m:
        model["rules"]["driving_cell"], model["rules"]["driving_pin"] = m.group(1), m.group(2) or ""
        return True
    for key, _, cmd, target in RULE_FIELDS:
        m = re.fullmatch(re.escape(cmd) + r" (\S+) " + re.escape(target), line)
        if m:
            model["rules"][key] = m.group(1)
            return True
    for typ, (_, cmd, has_value) in EXCEPTIONS.items():
        if line != cmd and not line.startswith(cmd + " "):
            continue
        rest = line[len(cmd):].strip()
        value = ""
        if has_value:
            value, _, rest = rest.partition(" ")
            value = "" if value == "?" else value
        ex = {"type": typ, "value": value, "check": "", "from": None, "through": None, "to": None}
        # The rest is options: -setup, -hold, or -from/-through/-to followed by one [get_<kind> {names}].
        options = re.findall(r"-(\w+)(?: " + OBJ_RE + r")?", rest)
        if re.sub(r"-(\w+)(?: " + OBJ_RE + r")?", "", rest).strip():
            return False
        for opt, kind, names in options:
            if opt in ("setup", "hold") and not kind:
                ex["check"] = opt
            elif opt in ("from", "through", "to") and kind:
                ex[opt] = (kind, names)
            else:
                return False
        model["exceptions"].append(ex)
        return True
    return False


def parse(text):
    """(model, lines inside the block the page can't read): the block's settings."""
    lines = text.split("\n")
    span = block_span(lines)
    model, unknown = empty(), []
    if span:
        for line in lines[span[0] + 1:span[1]]:
            s = line.strip()
            if s.startswith(INCOMPLETE):
                s = s[len(INCOMPLETE):]
            elif s.startswith("#"):
                continue
            if s and not _parse_line(s, model):
                unknown.append(s)
    return model, unknown


COMMANDS_RE = re.compile(r"^\s*(set_clock_uncertainty|set_clock_transition|set_clock_latency|set_driving_cell|"
                         r"set_load|set_max_transition|set_max_fanout|set_max_capacitance|set_false_path|"
                         r"set_multicycle_path|set_max_delay|set_min_delay)\b")


def outside(text):
    """Lines outside the block that set what the page sets: [(line number, text)], 1-based."""
    lines = text.split("\n")
    span = block_span(lines)
    return [(i + 1, ln.strip()) for i, ln in enumerate(lines)
            if COMMANDS_RE.match(ln) and not (span and span[0] < i < span[1])]


def problems(model, clocks):
    """What would make OpenROAD reject or misread the block, in plain words; [] when all is well."""
    out = []

    def number(value, what, whole=False, positive=False):
        if value in ("", None):
            return
        if not NUMBER_RE.match(value):
            out.append(f"{what}: '{value}' is not a number")
        elif whole and not re.fullmatch(r"\d+", value):
            out.append(f"{what}: '{value}' must be a whole number")
        elif positive and float(value) <= 0:
            out.append(f"{what}: '{value}' must be more than 0")

    for clock, values in sorted(model["clocks"].items()):
        if clock not in clocks:
            out.append(f"Clock {clock} is not defined in the SDC")
        for key, label, _ in CLOCK_FIELDS:
            number(values.get(key, ""), f"{clock} {label.lower()}")
    rules = model["rules"]
    if rules.get("driving_pin") and not rules.get("driving_cell"):
        out.append("A driving pin needs a driving cell")
    for key, label, _, _ in RULE_FIELDS:
        number(rules.get(key, ""), label, whole=key == "max_fanout", positive=True)
    for n, ex in enumerate(model["exceptions"], 1):
        label = f"Exception {n} ({EXCEPTIONS[ex['type']][0].lower()})"
        if not any(ex.get(e) for e in ("from", "through", "to")):
            out.append(f"{label}: set at least one of From, Through or To")
        if EXCEPTIONS[ex["type"]][2]:
            if ex["value"] in ("", None):
                out.append(f"{label}: needs a value")
            else:
                number(ex["value"], label, whole=ex["type"] == "multicycle", positive=ex["type"] == "multicycle")
        for end in ("from", "through", "to"):
            if ex.get(end):
                kind, names = ex[end]
                if not names.strip():
                    out.append(f"{label}: {end} has no names")
                elif kind == "clocks":
                    missing = [c for c in names.split() if c not in clocks and not set(c) & set("*?")]
                    if missing:
                        out.append(f"{label}: clock {', '.join(missing)} is not defined in the SDC")
                if end == "through" and kind == "clocks":
                    out.append(f"{label}: Through takes pins, cells or nets, not clocks")
    return out
