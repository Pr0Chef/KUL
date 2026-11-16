"""Clock-domain-crossing (CDC) analysis on the flattened netlist Yosys writes (see designcheck.py).

Every flip-flop belongs to the domain of the net on its clock pin. A crossing is a flop whose data input is
reached, through combinational logic, from a flop of another domain. Each crossing is classified:

  synchronized   the data comes straight from one flop of the other domain and is followed by a second flop of
                 the receiving domain with nothing in between: a two-flop synchronizer. Reported as info.
  CDC-BUS        several bits of one register each go through their own synchronizer: the bits can arrive in
                 different cycles, so the value seen may never have existed. Use gray code or a handshake.
  CDC-NOSYNC     the data comes straight from the other domain, but no second flop follows: metastability
                 reaches the logic.
  CDC-LOGIC      combinational logic sits between the domains: a glitch on it can be captured.

Not covered: crossings through memories and latches, reset-domain crossings, and inputs of the top module (their
domain is not known from the RTL). A clean report is evidence, not proof, as with any structural CDC tool.
"""

import re

FLOPS = {"$dff", "$adff", "$dffe", "$adffe", "$sdff", "$sdffe", "$sdffce", "$dffsr", "$dffsre", "$aldff", "$aldffe"}
SEQUENTIAL = FLOPS | {"$dlatch", "$adlatch", "$dlatchsr", "$sr", "$mem", "$mem_v2", "$memrd", "$memrd_v2", "$memwr",
                      "$memwr_v2", "$meminit", "$meminit_v2", "$scopeinfo", "$print", "$check", "$assert", "$assume",
                      "$cover", "$live", "$fair"}
OUTPUTS = {"Y", "Q", "X", "CO", "O", "G", "P", "RD_DATA", "DATA"}


def _src(cell, root=None):
    src = cell.get("attributes", {}).get("src", "").split("|")[0]
    m = re.match(r"(.+?):(\d+)", src)
    if not m:
        return "", 0
    file = m.group(1)
    if root and file.startswith(str(root)):
        file = file[len(str(root)):].lstrip("/")
    return file, int(m.group(2))


class Netlist:
    """The flattened top module: who drives each net bit, and a readable name for it."""

    def __init__(self, module):
        self.cells = module.get("cells", {})
        self.names = {}        # bit: "name" or "name[i]"
        for name, net in module.get("netnames", {}).items():
            bits = net["bits"]
            for i, bit in enumerate(bits):
                if not isinstance(bit, int):
                    continue
                shown = name if len(bits) == 1 else f"{name}[{i}]"
                old = self.names.get(bit)
                # Prefer names the designer wrote over the ones Yosys made up, then the shorter one.
                if old is None or (old.startswith("$") and not shown.startswith("$")) or \
                        (old.startswith("$") == shown.startswith("$") and len(shown) < len(old)):
                    self.names[bit] = shown
        self.driver = {}       # bit: (cell name, cell) of the cell output that drives it
        for cname, cell in self.cells.items():
            dirs = cell.get("port_directions", {})
            for port, bits in cell.get("connections", {}).items():
                is_out = dirs.get(port) == "output" if dirs else port in OUTPUTS
                if is_out:
                    for bit in bits:
                        if isinstance(bit, int):
                            self.driver[bit] = (cname, cell)
        self.flops = {n: c for n, c in self.cells.items() if c["type"] in FLOPS}
        self.q_of = {}         # bit: flop name whose Q it is
        for n, c in self.flops.items():
            for bit in c["connections"].get("Q", []):
                if isinstance(bit, int):
                    self.q_of[bit] = n

    def name(self, bit):
        return self.names.get(bit, f"net{bit}") if isinstance(bit, int) else str(bit)

    def clock(self, flop):
        """The domain of a flop: the name of its clock net, followed back through buffers and inverters."""
        bits = self.cells[flop]["connections"].get("CLK", [])
        bit = bits[0] if bits else None
        for _ in range(20):
            drv = self.driver.get(bit)
            if not drv or drv[1]["type"] not in ("$not", "$pos", "$buf", "$_NOT_", "$_BUF_"):
                break
            bit = drv[1]["connections"].get("A", [None])[0]
        return self.name(bit) if bit is not None else "?"

    def sources(self, bit):
        """(flops whose output reaches this bit, True if any logic cell is on the way)."""
        found, logic, seen, todo = set(), False, set(), [bit]
        while todo:
            b = todo.pop()
            if b in seen or not isinstance(b, int):
                continue
            seen.add(b)
            if b in self.q_of:
                found.add((self.q_of[b], b))
                continue
            drv = self.driver.get(b)
            if not drv or drv[1]["type"] in SEQUENTIAL:
                continue
            logic = True
            dirs = drv[1].get("port_directions", {})
            for port, bits in drv[1]["connections"].items():
                if (dirs.get(port) == "input") if dirs else port not in OUTPUTS:
                    todo.extend(bits)
        return found, logic


def analyze(module, root=None):
    """{"domains": {clock: flop bit count}, "crossings": [...], "findings": [...]} for a flattened netlist.

    A crossing is {"from", "from_clock", "to", "to_clock", "kind", "file", "line"}; findings are the crossings
    that need attention, in the common finding form (source "cdc").
    """
    nl = Netlist(module)
    clock = {n: nl.clock(n) for n in nl.flops}
    domains = {}
    for n, c in nl.flops.items():
        domains[clock[n]] = domains.get(clock[n], 0) + len(c["connections"].get("Q", []))
    # Flops (by D bit) that take their data straight from a Q bit with no logic: the second stage of a synchronizer.
    direct_loads = {}          # Q bit: [flop names loading it directly]
    for n, c in nl.flops.items():
        for bit in c["connections"].get("D", []):
            if bit in nl.q_of:
                direct_loads.setdefault(bit, []).append(n)

    crossings = []
    for n, c in nl.flops.items():
        file, line = _src(c, root)
        for d_bit, q_bit in zip(c["connections"].get("D", []), c["connections"].get("Q", [])):
            srcs, logic = nl.sources(d_bit)
            foreign = sorted((s, b) for s, b in srcs if clock[s] != clock[n])
            if not foreign:
                continue
            followed = any(clock[x] == clock[n] for x in direct_loads.get(q_bit, []))
            if logic or len(srcs) > 1:
                kind = "CDC-LOGIC"
            elif followed:
                kind = "synchronized"
            else:
                kind = "CDC-NOSYNC"
            src_flop, src_bit = foreign[0]
            crossings.append({"from": nl.name(src_bit), "from_clock": clock[src_flop], "to": nl.name(q_bit),
                              "to_clock": clock[n], "kind": kind, "file": file, "line": line})

    # Several synchronized bits of one register: a bus synchronized bit by bit.
    def base(name):
        return re.sub(r"\[\d+\]$", "", name)
    buses = {}
    for x in crossings:
        if x["kind"] == "synchronized":
            buses.setdefault((base(x["from"]), x["to_clock"]), []).append(x)
    for group in buses.values():
        if len(group) > 1:
            for x in group:
                x["kind"] = "CDC-BUS"

    text = {"CDC-LOGIC": "combinational logic between the domains: a glitch can be captured. Register the signal "
                         "in the sending domain, then synchronize it",
            "CDC-NOSYNC": "no synchronizer: follow this flop with a second flop of the receiving clock before the "
                          "signal is used",
            "CDC-BUS": "a multi-bit value synchronized bit by bit can be seen with bits from different cycles: "
                       "use gray code, a handshake or an asynchronous FIFO"}
    findings, seen = [], set()
    for x in crossings:
        if x["kind"] == "synchronized":
            continue
        key = (base(x["from"]), base(x["to"]), x["kind"])
        if key in seen:
            continue
        seen.add(key)
        findings.append({"source": "cdc", "severity": "warning" if x["kind"] == "CDC-BUS" else "error",
                         "code": x["kind"], "file": x["file"], "line": x["line"],
                         "message": f"{base(x['from'])} ({x['from_clock']}) to {base(x['to'])} ({x['to_clock']}): "
                                    f"{text[x['kind']]}"})
    return {"domains": domains, "crossings": crossings, "findings": findings}


def summary(result):
    doms = ", ".join(f"{name} ({n} flop bits)" for name, n in sorted(result["domains"].items()))
    ok = sum(x["kind"] == "synchronized" for x in result["crossings"])
    bad = len(result["findings"])
    if len(result["domains"]) < 2:
        return f"One clock domain: {doms or 'no flops'}. Nothing crosses."
    return f"{len(result['domains'])} clock domains: {doms}. {ok} synchronized crossing bit{'s' if ok != 1 else ''}, " \
           f"{bad} finding{'s' if bad != 1 else ''}."
