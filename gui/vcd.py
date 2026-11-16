"""Read VCD waveform files (the format $dumpvars writes) for the built-in waveform viewer.

A VCD has a header (timescale, a tree of scopes with their signals, each signal with a short id) and then value
changes grouped under #<time> lines. Signals that share an id are the same net seen from different scopes.
"""

import bisect
import re
from pathlib import Path

MAX_BYTES = 400 << 20   # larger dumps are refused: open them in GTKWave instead


class Signal:
    """One signal: where it is in the hierarchy, its width, and its changes as parallel lists."""

    def __init__(self, ident, name, scope, width, kind):
        self.ident, self.name, self.scope, self.width, self.kind = ident, name, scope, width, kind
        self.times, self.values = [], []    # value: a string of 0, 1, x, z (MSB first), or a real number text

    @property
    def path(self):
        return ".".join(self.scope + [self.name])

    def value_at(self, time):
        """The value at a time, or None before the first change."""
        i = bisect.bisect_right(self.times, time) - 1
        return self.values[i] if i >= 0 else None

    def index_at(self, time):
        return bisect.bisect_right(self.times, time) - 1


class Waves:
    """A parsed VCD: signals by path, the scope tree, the timescale and the last time stamp."""

    def __init__(self):
        self.signals = {}          # path: Signal
        self.by_id = {}            # id: Signal (the first one declared with that id)
        self.timescale = "1ns"
        self.end = 0

    def scopes(self):
        """{scope path tuple: [signal paths]} in declaration order."""
        out = {}
        for path, sig in self.signals.items():
            out.setdefault(tuple(sig.scope), []).append(path)
        return out

    def unit(self):
        """(number, unit) of one time step, such as (1, "ns")."""
        m = re.match(r"\s*(\d+)\s*([a-z]+)", self.timescale)
        return (int(m.group(1)), m.group(2)) if m else (1, "ns")


def format_value(value, width, radix="hex"):
    """A value string for display: hex, decimal or binary; x and z are kept visible."""
    if value is None:
        return ""
    if width == 1 or not set(value) <= set("01"):
        if width > 1 and radix == "hex" and set(value) <= set("01xXzZ"):
            # Hex digit by digit, with x or z for digits that hold any unknown bit.
            padded = value.rjust((len(value) + 3) // 4 * 4, "0" if value[0] in "01" else value[0])
            digits = []
            for i in range(0, len(padded), 4):
                chunk = padded[i:i + 4].lower()
                digits.append(format(int(chunk, 2), "x") if set(chunk) <= set("01") else
                              "z" if set(chunk) <= set("z") else "x")
            return "".join(digits)
        return value
    number = int(value, 2)
    if radix == "dec":
        return str(number)
    if radix == "bin":
        return value.rjust(width, "0")
    return format(number, "x").rjust((width + 3) // 4, "0")


def parse(path, max_bytes=MAX_BYTES):
    """Read a VCD file into Waves. Raises ValueError with a plain message when it can't be read."""
    path = Path(path)
    if path.suffix.lower() != ".vcd":
        raise ValueError(f"{path.name} is not a VCD file; the built-in viewer reads VCD. Open it in GTKWave.")
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path.name} is {size >> 20} MB: too large for the built-in viewer. Open it in GTKWave.")
    waves = Waves()
    scope = []
    with open(path, errors="replace") as f:
        # ---- header ----
        header = []
        for line in f:
            header.append(line)
            if "$enddefinitions" in line:
                break
        tokens = "".join(header).split()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok == "$scope":
                scope.append(tokens[i + 2])
            elif tok == "$upscope":
                if scope:
                    scope.pop()
            elif tok == "$timescale":
                j = tokens.index("$end", i)
                waves.timescale = "".join(tokens[i + 1:j])
                i = j
            elif tok == "$var":
                j = tokens.index("$end", i)
                kind, width, ident = tokens[i + 1], int(tokens[i + 2]), tokens[i + 3]
                name = tokens[i + 4]
                # "data [7:0]" keeps its range as a separate word; it is not part of the name.
                sig = Signal(ident, name, list(scope), width, kind)
                first = waves.by_id.setdefault(ident, sig)
                if first is not sig:
                    sig.times, sig.values = first.times, first.values   # same net, shared history
                key, n = sig.path, 1
                while key in waves.signals:
                    n += 1
                    key = f"{sig.path}#{n}"
                waves.signals[key] = sig
                i = j
            i += 1
        # ---- value changes ----
        time = 0
        by_id = waves.by_id
        for line in f:
            c = line[:1]
            if c == "#":
                try:
                    time = int(line[1:])
                except ValueError:
                    continue
            elif c in "01xXzZ":
                sig = by_id.get(line[1:].strip())
                if sig is not None:
                    _add(sig, time, c.lower())
            elif c in "bB":
                value, _, ident = line[1:].partition(" ")
                sig = by_id.get(ident.strip())
                if sig is not None:
                    _add(sig, time, value.lower())
            elif c in "rR":
                value, _, ident = line[1:].partition(" ")
                sig = by_id.get(ident.strip())
                if sig is not None:
                    _add(sig, time, value)
        waves.end = time
    if not waves.signals:
        raise ValueError(f"{path.name} has no signals: the simulation may not have reached $dumpvars.")
    return waves


def _add(sig, time, value):
    if sig.times and sig.times[-1] == time:
        sig.values[-1] = value
    elif not sig.values or sig.values[-1] != value:
        sig.times.append(time)
        sig.values.append(value)


def summary(waves, paths, times):
    """Values of some signals at some times, as text rows: for reports and for the assistant."""
    rows = []
    for p in paths:
        sig = waves.signals.get(p)
        if sig:
            rows.append(f"{p}: " + ", ".join(f"@{t}={format_value(sig.value_at(t), sig.width) or '-'}"
                                             for t in times))
    return rows
