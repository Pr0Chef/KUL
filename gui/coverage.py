"""Code and functional coverage from Verilator's coverage.dat files, merged over the runs of a regression.

Four kinds are measured, the ones a coverage closure review asks for:
  line    statements and branches of always blocks that ran
  toggle  each bit of each signal seen going 0->1 and 1->0
  branch  both arms of each if / case
  cover   SystemVerilog `cover property` points (functional coverage): how often each was hit

Only the project's design files count toward the percentages: a testbench covering itself proves nothing.
Each kind has a goal in the project's settings ("goals", percent); check_goals() says which are met.
"""

from pathlib import Path

KINDS = [("line", "Line"), ("branch", "Branch"), ("toggle", "Toggle"), ("cover", "Cover points")]


def read(dat_paths, root=None):
    """Merge coverage.dat files: {(kind, file, line, column, what, hierarchy): count}."""
    points = {}
    for path in dat_paths:
        try:
            text = Path(path).read_text(errors="replace")
        except OSError:
            continue
        for row in text.splitlines():
            if not row.startswith("C '"):
                continue
            end = row.rindex("'")
            fields = dict(p.split("\x02", 1) for p in row[3:end].split("\x01") if "\x02" in p)
            kind = {"user": "cover"}.get(fields.get("t"), fields.get("t"))
            if kind not in ("line", "toggle", "branch", "cover"):
                continue
            file = fields.get("f", "")
            if root and file.startswith(str(root)):
                file = file[len(str(root)):].lstrip("/")
            key = (kind, file, int(fields.get("l", 0) or 0), int(fields.get("n", 0) or 0), fields.get("o", ""),
                   fields.get("h", ""))
            try:
                points[key] = points.get(key, 0) + int(row[end + 1:].strip() or 0)
            except ValueError:
                pass
    return points


def summarize(points, design_files=None):
    """{"line": {...}, "branch": {...}, "toggle": {...}, "cover": {...}} for the design files (all files when
    design_files is None). Each has "hit", "total", "percent"; line and branch add "files": {file: {"hit",
    "total", "missed": [lines with a statement or arm that never ran]}}; toggle adds "missed": signals with a direction never seen; cover adds
    "points": [{"name", "file", "line", "count"}]."""
    keep = set(design_files) if design_files is not None else None
    out = {k: {"hit": 0, "total": 0, "percent": 0.0} for k, _ in KINDS}
    out["line"]["files"], out["branch"]["files"] = {}, {}
    out["toggle"]["missed"], out["cover"]["points"] = [], []
    for (kind, file, line, _col, what, hier), count in points.items():
        if keep is not None and file not in keep:
            continue
        if kind in ("line", "branch"):
            # Each statement or branch arm is a point; a line is "missed" when any point on it never ran.
            f = out[kind]["files"].setdefault(file, {"hit": 0, "total": 0, "missed": []})
            f["total"] += 1
            out[kind]["total"] += 1
            if count:
                f["hit"] += 1
                out[kind]["hit"] += 1
            elif line not in f["missed"]:
                f["missed"].append(line)
        elif kind == "toggle":
            out["toggle"]["total"] += 1
            if count:
                out["toggle"]["hit"] += 1
            else:
                out["toggle"]["missed"].append(f"{hier}.{what}  ({file}:{line})")
        else:
            out["cover"]["total"] += 1
            out["cover"]["hit"] += count > 0
            out["cover"]["points"].append({"name": what or hier.rsplit(".", 1)[-1], "file": file, "line": line,
                                           "count": count})
    for kind, _ in KINDS:
        k = out[kind]
        k["percent"] = round(100.0 * k["hit"] / k["total"], 1) if k["total"] else 0.0
        for f in k.get("files", {}).values():
            f["missed"].sort()
    out["toggle"]["missed"].sort()
    out["cover"]["points"].sort(key=lambda p: (p["count"] > 0, p["file"], p["line"]))
    return out


def check_goals(summary, goals):
    """[(kind, label, percent, goal, state)] with state "pass", "fail", or "info" when the kind has no goal or
    nothing to measure."""
    rows = []
    for kind, label in KINDS:
        k = summary.get(kind, {})
        goal = float((goals or {}).get(kind, 0) or 0)
        if not k.get("total"):
            state = "info"
        elif goal <= 0:
            state = "info"
        else:
            state = "pass" if k["percent"] >= goal else "fail"
        rows.append((kind, label, k.get("percent", 0.0), goal, state))
    return rows


def missed_lines(summary, file):
    """Lines of a design file no test ran, for marking in the editor."""
    return sorted(set(summary.get("line", {}).get("files", {}).get(file, {}).get("missed", []))
                  | set(summary.get("branch", {}).get("files", {}).get(file, {}).get("missed", [])))
