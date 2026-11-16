"""Lint a project's RTL: the command for each linter, and the problems read out of its output.

Three linters, best first: Verilator (the most thorough: width mismatches, unused signals, latches), Icarus
Verilog (syntax and elaboration), and Yosys (syntax and hierarchy; always there, since the ASIC flow needs it).
"""

import re

import eda_tools

LINTERS = [("verilator", "Verilator"), ("iverilog", "Icarus Verilog"), ("yosys", "Yosys")]
# file:line[:column]: text, in the three tools' spellings.
VERILATOR_RE = re.compile(r"^%(Error|Warning)(?:-(\w+))?: ([^:\s]+):(\d+)(?::(\d+))?: (.*)$")
IVERILOG_RE = re.compile(r"^([^:\s]+):(\d+): (?:(error|warning|sorry): )?(.*)$")
YOSYS_AT_RE = re.compile(r"^([^:\s]+\.s?vh?):(\d+)(?:\.\d+-\d+\.\d+)?: (ERROR|Warning|error|warning): (.*)$")
YOSYS_RE = re.compile(r"^(ERROR|Warning): (.*)$")


def available(chosen=None):
    """Linters that are installed, best first: [(tool, label)]."""
    return [(tool, label) for tool, label in LINTERS if eda_tools.find(tool, chosen)]


def command(tool, project, top=None, chosen=None, extra_files=(), strict=True):
    """The lint command for a project's design sources (plus extra_files), as an argument list. strict adds the
    style and unused-signal checks (-Wall); without it only likely bugs are reported."""
    exe = eda_tools.find(tool, chosen)
    files = project.paths(project.sources()) + list(extra_files)
    top = top or project.top()
    defines = project["defines"]
    if tool == "verilator":
        cmd = [exe, "--lint-only", "-Wno-fatal", "--timing", "-sv"] + (["-Wall"] if strict else [])
        cmd += project.include_flags("-I")
        cmd += [f"-D{d}" for d in defines]
        return cmd + (["--top-module", top] if top else []) + files
    if tool == "iverilog":
        cmd = [exe, "-g2012", "-t", "null"] + (["-Wall"] if strict else []) + project.include_flags("-I")
        cmd += [f"-D{d}" for d in defines]
        return cmd + (["-s", top] if top else []) + files
    # Yosys reads every file, then checks the hierarchy under the top module.
    inc = " ".join(f"-I{d[2:]}" for d in project.include_flags("-I"))
    defs = " ".join(f"-D{d}" for d in defines)
    script = "; ".join([f"read_verilog -sv {inc} {defs} {f}" for f in files]
                       + [f"hierarchy -check {'-top ' + top if top else '-auto-top'}", "proc", "check"])
    return [exe, "-q", "-p", script]


def parse(tool, output, root=None):
    """Problems in a linter's output: [{"source": "lint", "tool", "file", "line", "column", "severity", "code",
    "message"}].

    severity is "error" or "warning"; file is as the tool printed it (made relative to root when it is inside).
    """
    out = []

    def add(file, line, column, severity, code, message):
        if root and file.startswith(str(root)):
            file = file[len(str(root)):].lstrip("/")
        item = {"source": "lint", "tool": tool, "file": file, "line": int(line), "column": int(column or 0),
                "severity": "error" if severity.lower() in ("error", "sorry") else "warning",
                "code": code or "", "message": message.strip()}
        if item not in out:
            out.append(item)

    for raw in output.splitlines():
        line = raw.rstrip()
        if tool == "verilator":
            m = VERILATOR_RE.match(line)
            if m:
                add(m.group(3), m.group(4), m.group(5), m.group(1), m.group(2), m.group(6))
        elif tool == "iverilog":
            m = IVERILOG_RE.match(line)
            if m and not line.startswith(("Elaboration failed", "I give up")):
                add(m.group(1), m.group(2), 0, m.group(3) or "error", "", m.group(4))
        else:
            m = YOSYS_AT_RE.match(line)
            if m:
                add(m.group(1), m.group(2), 0, m.group(3), "", m.group(4))
                continue
            m = YOSYS_RE.match(line)
            if m:
                # Yosys names the place inside the message: "... at file.v:12.3-12.9" or "file.v:12: ...".
                where = re.search(r"((?:/|\b)[\w./\-]+\.(?:sv|v|svh|vh)):(\d+)", m.group(2))
                add(where.group(1) if where else "", where.group(2) if where else 0, 0, m.group(1), "",
                    m.group(2))
    return out


def summary(problems):
    errors = sum(p["severity"] == "error" for p in problems)
    warnings = len(problems) - errors
    if not problems:
        return "No problems found."
    return f"{errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}"
