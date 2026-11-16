"""FPGA flow with open source tools: Yosys synthesis, nextpnr place and route, the family's bitstream packer, and
openFPGALoader to program a board.

Three families are supported, each with its own constraint file written from the app's pin table:
  Lattice iCE40   nextpnr-ice40, IceStorm (icepack), pins in a .pcf
  Lattice ECP5    nextpnr-ecp5, Trellis (ecppack), pins in a .lpf
  Gowin           nextpnr-himbaechel, Apicula (gowin_pack), pins in a .cst
A board sets the family, device, package, clock pin and frequency, and openFPGALoader's board name; "Custom"
lets every field be set by hand. Settings live in the project's flowrunner.json under "fpga".
"""

import json
import re
from pathlib import Path

import eda_tools

FAMILIES = {
    "ice40": {"label": "Lattice iCE40", "synth": "synth_ice40", "pnr": "nextpnr-ice40", "pack": "icepack",
              "constraints": ".pcf", "devices": ["up5k", "hx8k", "hx1k", "lp8k", "hx4k", "up3k", "lp1k"],
              "packages": ["sg48", "ct256", "tq144", "cm81", "bg121", "cb132", "vq100", "uwg30"]},
    "ecp5": {"label": "Lattice ECP5", "synth": "synth_ecp5", "pnr": "nextpnr-ecp5", "pack": "ecppack",
             "constraints": ".lpf", "devices": ["25k", "45k", "85k", "um5g-85k", "12k"],
             "packages": ["CABGA256", "CABGA381", "CSFBGA285", "CABGA554", "TQFP144"]},
    "gowin": {"label": "Gowin", "synth": "synth_gowin", "pnr": "nextpnr-himbaechel", "pack": "gowin_pack",
              "constraints": ".cst",
              "devices": ["GW1NR-LV9QN88PC6/I5", "GW2AR-LV18QN88C8/I7", "GW1NSR-LV4CQN48PC7/I6",
                          "GW1N-LV1QN48C6/I5", "GW5A-LV25MG121NC1/I0"],
              "packages": [""]},
}
# Gowin devices name the chip family nextpnr and gowin_pack need.
GOWIN_FAMILY = {"GW1NR-LV9QN88PC6/I5": "GW1N-9C", "GW2AR-LV18QN88C8/I7": "GW2A-18C",
                "GW1NSR-LV4CQN48PC7/I6": "GW1NS-4", "GW1N-LV1QN48C6/I5": "GW1N-1", "GW5A-LV25MG121NC1/I0": "GW5A-25A"}
BOARDS = {
    "iCEBreaker": {"family": "ice40", "device": "up5k", "package": "sg48", "clock_pin": "35", "mhz": 12,
                   "loader": "icebreaker", "leds": ["11", "37", "26", "27", "25", "23", "21"], "buttons": ["10", "20", "19", "18"]},
    "iCEstick": {"family": "ice40", "device": "hx1k", "package": "tq144", "clock_pin": "21", "mhz": 12,
                 "loader": "ice40_generic", "leds": ["99", "98", "97", "96", "95"], "buttons": []},
    "ULX3S (85F)": {"family": "ecp5", "device": "85k", "package": "CABGA381", "clock_pin": "G2", "mhz": 25,
                    "loader": "ulx3s", "leds": ["B2", "C2", "C1", "D2", "D1", "E2", "E1", "H3"], "buttons": ["D6"]},
    "OrangeCrab (25F)": {"family": "ecp5", "device": "25k", "package": "CSFBGA285", "clock_pin": "A9", "mhz": 48,
                         "loader": "orangeCrab", "leds": ["K4", "M3", "J3"], "buttons": ["J17"]},
    "Tang Nano 9K": {"family": "gowin", "device": "GW1NR-LV9QN88PC6/I5", "package": "", "clock_pin": "52",
                     "mhz": 27, "loader": "tangnano9k", "leds": ["10", "11", "13", "14", "15", "16"],
                     "buttons": ["3", "4"]},
    "Tang Nano 20K": {"family": "gowin", "device": "GW2AR-LV18QN88C8/I7", "package": "", "clock_pin": "4",
                      "mhz": 27, "loader": "tangnano20k", "leds": ["15", "16", "17", "18", "19", "20"],
                      "buttons": ["88", "87"]},
    "Custom": {"family": "ice40", "device": "up5k", "package": "sg48", "clock_pin": "", "mhz": 12, "loader": "",
               "leds": [], "buttons": []},
}
IO_STANDARDS = {"ice40": [""], "ecp5": ["LVCMOS33", "LVCMOS25", "LVCMOS18"],
                "gowin": ["LVCMOS33", "LVCMOS25", "LVCMOS18"]}


def defaults(board="iCEBreaker"):
    b = BOARDS[board]
    return {"board": board, "family": b["family"], "device": b["device"], "package": b["package"], "mhz": b["mhz"],
            "pins": {}, "io_standard": IO_STANDARDS[b["family"]][0], "top": ""}


def port_bits(module):
    """Each top-level port bit as the constraint files name it: ("led[3]", "output"), ("clk", "input")."""
    out = []
    for direction, name, bits in module.ports:
        if isinstance(bits, tuple):
            hi, lo = bits
            step = 1 if hi >= lo else -1
            out += [(f"{name}[{i}]", direction) for i in range(lo, hi + step, step)]
        else:
            out.append((name, direction))
    return out


def suggest_pins(module, board):
    """A first pin for each port bit from the board: the clock to the clock pin, outputs to LEDs in order,
    inputs to buttons. Returns {bit: pin}; bits it can't place are left out."""
    b = BOARDS[board]
    pins, leds, buttons = {}, list(b["leds"]), list(b["buttons"])
    import hdl
    clock, reset, _ = hdl.clock_and_reset(module)
    for bit, direction in port_bits(module):
        if bit == clock and b["clock_pin"]:
            pins[bit] = b["clock_pin"]
        elif direction == "output" and leds:
            pins[bit] = leds.pop(0)
        elif direction == "input" and buttons and bit != clock:
            pins[bit] = buttons.pop(0)
    return pins


def constraints(settings, clock=None):
    """The constraint file text for the family: pins, IO standard and the clock's frequency."""
    family, pins, std = settings["family"], settings.get("pins", {}), settings.get("io_standard", "")
    lines = [f"# Pins of {settings.get('top') or 'the design'}, written by ORFS Flow Runner from its pin table."]
    for bit, pin in sorted(pins.items()):
        if not pin:
            continue
        if family == "ice40":
            lines.append(f"set_io {bit} {pin}")
        elif family == "ecp5":
            lines.append(f'LOCATE COMP "{bit}" SITE "{pin}";')
            if std:
                lines.append(f'IOBUF PORT "{bit}" IO_TYPE={std};')
        else:
            lines.append(f'IO_LOC "{bit}" {pin};')
            if std:
                lines.append(f'IO_PORT "{bit}" IO_TYPE={std};')
    if clock and family == "ecp5":
        lines.append(f'FREQUENCY PORT "{clock}" {settings.get("mhz", 12)} MHZ;')
    return "\n".join(lines) + "\n"


def check(settings, module):
    """Why a build can't start, or None: unknown pins, pins used twice, ports without a pin."""
    if module is None:
        return "The project has no top module."
    bits = [b for b, _ in port_bits(module)]
    pins = {b: p for b, p in settings.get("pins", {}).items() if p}
    stray = [b for b in pins if b not in bits]
    if stray:
        return f"The pin table names ports the top module does not have: {', '.join(stray[:6])}"
    used = {}
    for b, p in pins.items():
        used.setdefault(p, []).append(b)
    twice = [f"{p} ({', '.join(bs)})" for p, bs in used.items() if len(bs) > 1]
    if twice:
        return "Pins used twice: " + "; ".join(twice[:4])
    free = [b for b in bits if b not in pins]
    if free:
        return f"These ports have no pin: {', '.join(free[:8])}" + ("…" if len(free) > 8 else "")
    return None


def commands(project, settings, chosen=None):
    """(build folder, [commands]) for synthesis, place and route and the bitstream; the constraint file and
    the synthesis script are written into the folder."""
    fam = FAMILIES[settings["family"]]
    top = settings.get("top") or project.top()
    out = project.build("fpga")
    files = project.paths(project.sources())
    defines = " ".join(f"-D{d}" for d in project["defines"])
    incs = " ".join(f"-I{d[2:]}" for d in project.include_flags("-I"))
    script = [f"read_verilog -sv {defines} {incs} {f}".replace("  ", " ") for f in files]
    script += [f"{fam['synth']} -top {top} -json {out / 'synth.json'}", f"tee -o {out / 'synth_stat.txt'} stat"]
    (out / "synth.ys").write_text("\n".join(script) + "\n")
    import hdl
    module = project.modules().get(top)
    clock = hdl.clock_and_reset(module)[0] if module else None
    cfile = out / f"pins{fam['constraints']}"
    cfile.write_text(constraints(dict(settings, top=top), clock))
    yosys = eda_tools.find("yosys", chosen)
    pnr = eda_tools.find(fam["pnr"], chosen)
    pack = eda_tools.find(fam["pack"], chosen)
    freq = str(settings.get("mhz", 12))
    report = str(out / "report.json")
    steps = [[yosys, "-q", "-l", str(out / "synth.log"), str(out / "synth.ys")]]
    if settings["family"] == "ice40":
        steps.append([pnr, f"--{settings['device']}", "--package", settings["package"], "--json",
                      str(out / "synth.json"), "--pcf", str(cfile), "--asc", str(out / "design.asc"), "--freq", freq,
                      "--report", report, "--timing-allow-fail"])
        steps.append([pack, str(out / "design.asc"), str(out / "design.bin")])
    elif settings["family"] == "ecp5":
        steps.append([pnr, f"--{settings['device']}", "--package", settings["package"], "--json",
                      str(out / "synth.json"), "--lpf", str(cfile), "--textcfg", str(out / "design.config"),
                      "--freq", freq, "--report", report, "--timing-allow-fail"])
        steps.append([pack, str(out / "design.config"), str(out / "design.bit")])
    else:
        family = GOWIN_FAMILY.get(settings["device"], "GW1N-9C")
        steps.append([pnr, "--json", str(out / "synth.json"), "--write", str(out / "pnr.json"), "--device",
                      settings["device"], "--vopt", f"family={family}", "--vopt", f"cst={cfile}", "--freq", freq,
                      "--report", report, "--timing-allow-fail"])
        steps.append([pack, "-d", family, "-o", str(out / "design.fs"), str(out / "pnr.json")])
    return out, steps


def bitstream(folder, family):
    name = {"ice40": "design.bin", "ecp5": "design.bit", "gowin": "design.fs"}[family]
    path = Path(folder) / name
    return path if path.exists() else None


def read_report(folder):
    """Utilization and Fmax from nextpnr's report: {"cells": [(type, used, available)], "clocks": [(clock,
    achieved MHz, wanted MHz)]}; empty lists when there is no report yet."""
    path = Path(folder) / "report.json"
    out = {"cells": [], "clocks": []}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return out
    for cell, v in sorted(data.get("utilization", {}).items()):
        if v.get("used"):
            out["cells"].append((cell, v["used"], v.get("available", 0)))
    for clock, v in sorted(data.get("fmax", {}).items()):
        name = re.sub(r"^\$glbnet\$|_IBUF_I_O$|_SB_IO.*$|\$TRELLIS_IO_IN$", "", clock)
        out["clocks"].append((name or clock,
                              round(v.get("achieved", 0), 2), round(v.get("constraint", 0), 2)))
    return out


def program_command(settings, folder, chosen=None, to_flash=False):
    """openFPGALoader command that loads the bitstream into the board (SRAM, or flash when to_flash)."""
    board = BOARDS.get(settings.get("board", ""), {})
    loader = settings.get("loader") or board.get("loader")
    cmd = [eda_tools.find("openFPGALoader", chosen)]
    cmd += ["-b", loader] if loader else ["-c", "ft2232"]
    if to_flash:
        cmd.append("-f")
    return cmd + [str(bitstream(folder, settings["family"]))]
