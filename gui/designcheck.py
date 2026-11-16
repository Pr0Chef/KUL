"""Structural checks on a project's RTL, and waivers for any check's findings.

design checks: Yosys elaborates the design and reports what synthesis would silently accept but a review would
not: inferred latches, combinational loops, nets with several drivers, nets used without a driver. The same run
writes the flattened netlist as JSON, which cdc.py reads for the clock-domain-crossing analysis.

A finding (from lint, the design checks or CDC) is a dict {"source", "severity", "code", "file", "line",
"message"}. A waiver accepts findings on purpose, with a reason: {"source", "code", "file", "match", "reason",
"when"}. It matches by code, file and a piece of the message, never by line number, so it survives edits above it.
"""

import json
import re
import time
from pathlib import Path

import eda_tools

SRC_RE = re.compile(r"([^\s:|$]+\.(?:sv|v|svh|vh)):(\d+)")


def netlist_file(project):
    return project.build("checks") / "netlist.json"


def script(project, top=None):
    """Yosys commands that elaborate the design, run the checks and write the flattened netlist."""
    top = top or project.top()
    inc = " ".join(f"-I{d[2:]}" for d in project.include_flags("-I"))
    defs = " ".join(f"-D{d}" for d in project["defines"])
    lines = [f"read_verilog -sv {inc} {defs} {f}".replace("  ", " ") for f in project.paths(project.sources())]
    lines += [f"hierarchy -check {'-top ' + top if top else '-auto-top'}", "proc", "check", "flatten",
              "opt_clean", f"write_json {netlist_file(project)}"]
    return "\n".join(lines) + "\n"


def command(project, chosen=None, top=None):
    """(folder, command): writes the script into the project's checks folder."""
    folder = project.build("checks")
    (folder / "checks.ys").write_text(script(project, top))
    netlist_file(project).unlink(missing_ok=True)
    return folder, [eda_tools.find("yosys", chosen), "-l", str(folder / "checks.log"), "-q", str(folder / "checks.ys")]


def _where(text, root):
    m = SRC_RE.search(text)
    if not m:
        return "", 0
    file = m.group(1)
    if root and file.startswith(str(root)):
        file = file[len(str(root)):].lstrip("/")
    return file, int(m.group(2))


def parse(log, root=None):
    """Findings in the Yosys log of a design-check run."""
    out = []

    def add(severity, code, message, where=""):
        file, line = _where(where or message, root)
        item = {"source": "design", "severity": severity, "code": code, "file": file, "line": line,
                "message": message}
        if item not in out:
            out.append(item)

    lines = log.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        m = re.match(r"Warning: Latch inferred for signal `\\?([^']+)' from process `([^']+)'", line)
        if m:
            signal = m.group(1).split(".\\")[-1]
            add("warning", "LATCH", f"Latch inferred for {signal}: it is not assigned on every path of a "
                                    "combinational block", m.group(2).replace("$", ":"))
            continue
        m = re.match(r"Warning: multiple conflicting drivers for (\S+?)\.\\(.+?):$", line)
        if m:
            add("error", "MULTIDRIVEN", f"{m.group(2)} in {m.group(1)} has more than one driver")
            continue
        m = re.match(r"Warning: Wire (\S+?)\.\\(\S+) is used but has no driver", line)
        if m:
            add("warning", "UNDRIVEN", f"{m.group(2)} in {m.group(1)} is used but nothing drives it")
            continue
        m = re.match(r"Warning: found logic loop in module (\S+?):", line)
        if m:
            # The lines that follow name the cells and wires on the loop.
            path = [ln.strip() for ln in lines[i + 1: i + 12] if ln.startswith("    ")]
            wires = [re.sub(r"^wire\s+\\?", "", p).split(" ")[0] for p in path if p.startswith("wire")]
            add("error", "COMBLOOP", f"Combinational loop in {m.group(1)}"
                + (f" through {', '.join(wires[:4])}" if wires else ""), " ".join(path))
            continue
        m = re.match(r"ERROR: (.*)", line)
        if m:
            add("error", "ELAB", m.group(1))
    return out


# ---------------------------------------------------------------- waivers
def key_text(message):
    """The part of a message a waiver matches: the text with numbers that change from run to run removed."""
    return re.sub(r"\$\d+|\b\d+\b", "#", message)[:120]


def waiver_for(finding, reason):
    return {"source": finding.get("source", "lint"), "code": finding.get("code", ""), "file": finding.get("file", ""),
            "match": key_text(finding.get("message", "")), "reason": reason, "when": time.strftime("%Y-%m-%d")}


def matches(waiver, finding):
    return (waiver.get("source", "lint") == finding.get("source", "lint")
            and waiver.get("code", "") == finding.get("code", "")
            and waiver.get("file", "") in ("", finding.get("file", ""))
            and waiver.get("match", "") in key_text(finding.get("message", "")))


def apply_waivers(findings, waivers):
    """Mark each finding a waiver accepts with "waived": the reason. Returns the findings."""
    for f in findings:
        w = next((w for w in waivers or [] if matches(w, f)), None)
        if w:
            f["waived"] = w.get("reason") or "waived"
        else:
            f.pop("waived", None)
    return findings


def unused_waivers(findings, waivers):
    """Waivers that match nothing any more: the code they excused is gone, so they should be removed."""
    return [w for w in waivers or [] if not any(matches(w, f) for f in findings)]


def counts(findings):
    """(errors, warnings, waived) with waived findings counted apart."""
    live = [f for f in findings if not f.get("waived")]
    errors = sum(f["severity"] == "error" for f in live)
    return errors, len(live) - errors, len(findings) - len(live)


def load_netlist(project):
    """The flattened netlist of the last design-check run: the top module's dict, or None."""
    try:
        data = json.loads(Path(netlist_file(project)).read_text())
    except (OSError, ValueError):
        return None
    mods = data.get("modules", {})
    top = next((m for m in mods.values() if m.get("attributes", {}).get("top")), None)
    return top or (next(iter(mods.values())) if mods else None)
