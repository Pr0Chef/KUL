"""Formal property checking with SymbiYosys. (Equivalence checks of ASIC netlists are in lec.py.)

A formal task is {"name", "top", "mode" (bmc, prove or cover), "depth", "files"}: the design sources plus the
task's property files, read with -formal so assert, assume and cover work. SymbiYosys writes its verdict as a
"DONE (PASS / FAIL / UNKNOWN ...)" line and, on a failure or a cover, a trace.vcd the waveform viewer opens.
"""

import re
from pathlib import Path

import eda_tools

MODES = ["bmc", "prove", "cover"]
DONE_RE = re.compile(r"DONE \((PASS|FAIL|UNKNOWN|ERROR|TIMEOUT)[^)]*\)")


def task_dir(project, task):
    return project.build("formal") / re.sub(r"[^\w\-]", "_", task["name"])


def sby_text(project, task):
    """The .sby file of a task. Files are copied by sby into its work folder, so paths are absolute."""
    files = project.paths(project.sources()) + [f for f in project.paths(task.get("files", []))
                                                if f not in project.paths(project.sources())]
    defines = " ".join(f"-D{d}" for d in project["defines"])
    incs = " ".join(f"-I{d[2:]}" for d in project.include_flags("-I"))
    lines = ["[options]", f"mode {task.get('mode', 'bmc')}", f"depth {int(task.get('depth', 20))}", "",
             "[engines]", "smtbmc" + (" boolector" if task.get("mode") != "prove" else ""), "", "[script]"]
    lines += [f"read -formal {defines} {incs} {Path(f).name}".replace("  ", " ") for f in files]
    lines += [f"prep -top {task['top']}", "", "[files]"] + files + [""]
    return "\n".join(lines)


def sby_command(project, task, chosen=None):
    """(folder, command) that runs a task; the .sby file is written into the folder."""
    folder = task_dir(project, task)
    folder.mkdir(parents=True, exist_ok=True)
    sby = folder / f"{task['name']}.sby"
    sby.write_text(sby_text(project, task))
    return folder, [eda_tools.find("sby", chosen), "-f", sby.name]


def verdict(output):
    """("pass" | "fail" | "unknown" | "error", the DONE line) from SymbiYosys's output."""
    m = None
    for m in DONE_RE.finditer(output):
        pass
    if not m:
        return "error", "no DONE line: see the output"
    word = m.group(1).lower()
    return ("pass" if word == "pass" else "fail" if word == "fail" else "unknown" if word in ("unknown", "timeout")
            else "error"), m.group(0)


def traces(folder, task):
    """Waveforms SymbiYosys wrote for a task (counterexamples, covers), newest first."""
    work = Path(folder) / task["name"]
    found = list(work.glob("engine_*/trace*.vcd")) if work.exists() else []
    return sorted(found, key=lambda p: -p.stat().st_mtime)


def failed_properties(output):
    """Assertions that failed, as SymbiYosys names them ("file:line" and the property)."""
    return sorted(set(re.findall(r"Assert failed in \S+: (\S+)", output)))
