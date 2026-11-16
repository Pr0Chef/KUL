"""Sign-off: one verdict for a project's RTL quality and verification, the plan behind it, and the reports.

The gate passes when every item that was run is clean and nothing required is missing:
  lint, design checks, CDC   no errors left (waived findings do not count; warnings are shown, not gating)
  tests                      every run passed
  formal                     every task passed
  coverage                   each kind with a goal reaches it
  plan                       every feature of the verification plan is covered by passing checks
An item that was never run is "not run": it does not pass the gate, because unverified is not verified.

A verification plan item is {"feature", "description", "tests": [names], "formal": [names], "cover": [points]}:
the feature counts as covered when all its linked checks pass and each linked cover point was hit.
"""

import html
import time
from xml.sax.saxutils import escape, quoteattr

import coverage
import designcheck as dc
import regress

GATING = ("lint", "design", "cdc", "tests", "formal", "coverage", "plan")


def plan_status(plan, results):
    """[(item, state, detail)] with state pass, fail, "not run" or "no checks"."""
    tests, proofs = results.get("tests", {}), results.get("formal", {})
    points = {p["name"]: p["count"] for p in results.get("coverage", {}).get("cover", {}).get("points", [])}
    rows = []
    for item in plan or []:
        states, missing = [], []
        for name in item.get("tests", []):
            # A test with several seeds passes when every one of its runs passed.
            runs = [r for k, r in tests.items() if k == name or r.get("test") == name]
            states.append("pass" if runs and all(r.get("state") == "pass" for r in runs) else
                          "not run" if not runs or any(r.get("state") in (None, "running") for r in runs) else "fail")
            if states[-1] != "pass":
                missing.append(f"test {name}: {states[-1]}")
        for name in item.get("formal", []):
            st = proofs.get(name, {}).get("state")
            states.append("pass" if st == "pass" else "not run" if st in (None, "running") else "fail")
            if states[-1] != "pass":
                missing.append(f"formal {name}: {states[-1]}")
        for name in item.get("cover", []):
            states.append("pass" if points.get(name, 0) > 0 else "not run" if name not in points else "fail")
            if states[-1] != "pass":
                missing.append(f"cover {name}: " + ("never hit" if name in points else "not measured"))
        if not states:
            state = "no checks"
        elif "fail" in states:
            state = "fail"
        elif "not run" in states:
            state = "not run"
        else:
            state = "pass"
        rows.append((item, state, "; ".join(missing) or f"{len(states)} checks pass"))
    return rows


def collect(project, findings, ran, results):
    """The sign-off items: [{"key", "name", "value", "state", "detail"}] and the verdict.

    findings: every finding of lint, design checks and CDC (with waivers applied); ran: {"lint": bool, "design":
    bool, "cdc": bool} says which of those checks have been run; results: the Verify page's results.
    Returns (items, "pass" | "fail").
    """
    items = []

    def add(key, name, value, state, detail=""):
        items.append({"key": key, "name": name, "value": value, "state": state, "detail": detail})

    for key, name in (("lint", "Lint"), ("design", "Design checks"), ("cdc", "Clock domain crossings")):
        mine = [f for f in findings if f.get("source", "lint") == key]
        errors, warnings, waived = dc.counts(mine)
        if not ran.get(key):
            add(key, name, "not run", "muted")
        else:
            detail = f"{warnings} warning{'s' if warnings != 1 else ''}" + (f", {waived} waived" if waived else "")
            add(key, name, f"{errors} error{'s' if errors != 1 else ''}", "fail" if errors else "pass", detail)

    runs = {k: r for k, r in results.get("tests", {}).items() if r.get("state") != "running"}
    tests = project["tests"]
    if not tests:
        add("tests", "Simulation tests", "no tests", "fail", "add a test in the Verify section")
    elif not runs:
        add("tests", "Simulation tests", "not run", "muted", f"{len(tests)} tests")
    else:
        t = regress.tally(runs)
        add("tests", "Simulation tests", f"{t['passed']} / {t['total']} pass",
            "pass" if t["passed"] == t["total"] else "fail",
            f"{t['failed']} failed" + (f", {t['other']} without a verdict" if t["other"] else ""))

    proofs = project["formal"]
    if proofs:
        states = [results.get("formal", {}).get(p["name"], {}).get("state") for p in proofs]
        if all(s in (None, "running") for s in states):
            add("formal", "Formal checks", "not run", "muted", f"{len(proofs)} tasks")
        else:
            ok = states.count("pass")
            add("formal", "Formal checks", f"{ok} / {len(proofs)} pass", "pass" if ok == len(proofs) else "fail")
    else:
        add("formal", "Formal checks", "none", "info", "optional: add properties in the RTL section")

    cov = results.get("coverage", {})
    if cov.get("line", {}).get("total"):
        rows = coverage.check_goals(cov, project["goals"])
        failed = [f"{label} {pct:.0f}% < {goal:.0f}%" for _k, label, pct, goal, st in rows if st == "fail"]
        shown = ", ".join(f"{label.lower()} {pct:.0f}%" for k, label, pct, _g, _s in rows if cov[k].get("total"))
        add("coverage", "Coverage", f"{cov['line']['percent']:.0f}% line", "fail" if failed else "pass",
            "; ".join(failed) or shown)
    else:
        add("coverage", "Coverage", "not measured", "muted", "run the tests with coverage (Verilator)")

    plan = plan_status(project["plan"], results)
    if plan:
        ok = sum(state == "pass" for _i, state, _d in plan)
        add("plan", "Verification plan", f"{ok} / {len(plan)} features", "pass" if ok == len(plan) else "fail",
            "; ".join(f"{i['feature']}: {state}" for i, state, _d in plan if state != "pass")[:200])
    else:
        add("plan", "Verification plan", "no plan", "info", "optional: list the features to verify")

    verdict = "pass" if all(i["state"] in ("pass", "info") for i in items) else "fail"
    return items, verdict


# ---------------------------------------------------------------- reports
def junit(project, results):
    """The regression as JUnit XML, which CI systems read."""
    runs = results.get("tests", {})
    t = regress.tally(runs)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name={quoteattr(project.name)} tests="{t["total"]}" failures="{t["failed"]}" '
             f'skipped="{t["other"]}" time="{sum(r.get("seconds", 0) for r in runs.values()):.1f}">']
    for name, r in sorted(runs.items()):
        lines.append(f'  <testcase classname={quoteattr(project.name)} name={quoteattr(name)} '
                     f'time="{r.get("seconds", 0)}">')
        state = r.get("state")
        if state in ("fail", "timeout", "error"):
            lines.append(f'    <failure message={quoteattr(str(r.get("why", state)))}>'
                         f'{escape(r.get("log", "")[-3000:])}</failure>')
        elif state != "pass":
            lines.append(f'    <skipped message={quoteattr(str(r.get("why", state or "not run")))}/>')
        lines.append("  </testcase>")
    for name, r in sorted(results.get("formal", {}).items()):
        lines.append(f'  <testcase classname={quoteattr(project.name + ".formal")} name={quoteattr(name)}>')
        if r.get("state") != "pass":
            lines.append(f'    <failure message={quoteattr(str(r.get("why", r.get("state"))))}/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


CSS = """body{font:14px/1.5 system-ui,sans-serif;margin:32px auto;max-width:1040px;padding:0 20px;color:#1c2230}
h1{font-size:24px;margin-bottom:0}h2{font-size:17px;margin-top:32px;border-bottom:1px solid #dfe3ea;padding-bottom:4px}
.sub{color:#6b7385}table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{text-align:left;padding:5px 10px;border-bottom:1px solid #eef1f6;vertical-align:top}th{color:#6b7385;font-size:12px}
.pass{color:#1e8e5a}.fail{color:#d64545}.muted,.info{color:#6b7385}.warn{color:#b7791f}
.verdict{display:inline-block;padding:4px 14px;border-radius:14px;font-weight:700;color:#fff}
.verdict.pass{background:#1e8e5a}.verdict.fail{background:#d64545}code{font-size:12px}
"""


def html_report(project, items, verdict, findings, results, history=()):
    """A self-contained HTML sign-off report."""
    e = html.escape
    out = [f"<!doctype html><html><head><meta charset='utf-8'><title>{e(project.name)} sign-off</title>"
           f"<style>{CSS}</style></head><body>",
           f"<h1>{e(project.name)}: sign-off report</h1>",
           f"<p class='sub'>Top module {e(project.top() or '?')} · {time.strftime('%Y-%m-%d %H:%M')} · "
           f"<span class='verdict {verdict}'>{'PASS' if verdict == 'pass' else 'NOT SIGNED OFF'}</span></p>",
           "<h2>Summary</h2><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>"]
    for i in items:
        out.append(f"<tr><td>{e(i['name'])}</td><td class='{i['state']}'>{e(i['value'])}</td>"
                   f"<td class='sub'>{e(i['detail'])}</td></tr>")
    out.append("</table>")

    plan = plan_status(project["plan"], results)
    if plan:
        out.append("<h2>Verification plan</h2><table><tr><th>Feature</th><th>Checks</th><th>Status</th>"
                   "<th>Detail</th></tr>")
        for item, state, detail in plan:
            checks = ", ".join(item.get("tests", []) + item.get("formal", []) + item.get("cover", []))
            cls = {"pass": "pass", "fail": "fail"}.get(state, "muted")
            out.append(f"<tr><td>{e(item['feature'])}<br><span class='sub'>{e(item.get('description', ''))}</span>"
                       f"</td><td>{e(checks)}</td><td class='{cls}'>{e(state)}</td><td class='sub'>{e(detail)}</td>"
                       "</tr>")
        out.append("</table>")

    runs = results.get("tests", {})
    if runs:
        out.append("<h2>Regression</h2><table><tr><th>Run</th><th>Seed</th><th>Result</th><th>Time</th>"
                   "<th>Why</th></tr>")
        for name, r in sorted(runs.items()):
            cls = "pass" if r.get("state") == "pass" else "fail" if r.get("state") in ("fail", "timeout", "error") \
                else "muted"
            out.append(f"<tr><td>{e(name)}</td><td>{e(str(r.get('seed') or ''))}</td>"
                       f"<td class='{cls}'>{e(str(r.get('state')))}</td><td>{r.get('seconds', 0)} s</td>"
                       f"<td class='sub'>{e(str(r.get('why', ''))[:160])}</td></tr>")
        out.append("</table>")
    proofs = results.get("formal", {})
    if proofs:
        out.append("<h2>Formal</h2><table><tr><th>Task</th><th>Result</th><th>Detail</th></tr>")
        for name, r in sorted(proofs.items()):
            cls = "pass" if r.get("state") == "pass" else "fail"
            out.append(f"<tr><td>{e(name)}</td><td class='{cls}'>{e(str(r.get('state')))}</td>"
                       f"<td class='sub'>{e(str(r.get('why', ''))[:200])}</td></tr>")
        out.append("</table>")

    cov = results.get("coverage", {})
    if cov.get("line", {}).get("total"):
        out.append("<h2>Coverage</h2><table><tr><th>Kind</th><th>Covered</th><th>Goal</th><th>Hit / total</th></tr>")
        for kind, label, pct, goal, state in coverage.check_goals(cov, project["goals"]):
            if cov[kind].get("total"):
                out.append(f"<tr><td>{label}</td><td class='{state}'>{pct:.1f}%</td>"
                           f"<td>{f'{goal:.0f}%' if goal else '–'}</td>"
                           f"<td>{cov[kind]['hit']} / {cov[kind]['total']}</td></tr>")
        out.append("</table>")
        missed = [(f, d["missed"]) for f, d in cov["line"].get("files", {}).items() if d["missed"]]
        if missed:
            out.append("<p class='sub'>Lines never run: " + "; ".join(
                f"{e(f)}: {', '.join(map(str, lines[:20]))}{'…' if len(lines) > 20 else ''}" for f, lines in missed)
                + "</p>")
        never = [p for p in cov.get("cover", {}).get("points", []) if not p["count"]]
        if never:
            out.append("<p class='sub'>Cover points never hit: " + ", ".join(
                f"{e(p['name'])} ({e(p['file'])}:{p['line']})" for p in never) + "</p>")

    if findings:
        out.append("<h2>Findings</h2><table><tr><th>Check</th><th>Severity</th><th>Code</th><th>Where</th>"
                   "<th>Message</th></tr>")
        order = sorted(findings, key=lambda f: (bool(f.get("waived")), f["severity"] != "error", f.get("source", ""),
                                                f.get("file", ""), f.get("line", 0)))
        for f in order:
            sev = "waived" if f.get("waived") else f["severity"]
            cls = "muted" if f.get("waived") else "fail" if f["severity"] == "error" else "warn"
            where = f"{f.get('file', '')}:{f.get('line', '')}" if f.get("file") else ""
            note = f"<br><span class='sub'>Waived: {e(str(f['waived']))}</span>" if f.get("waived") else ""
            out.append(f"<tr><td>{e(f.get('source', 'lint'))}</td><td class='{cls}'>{e(sev)}</td>"
                       f"<td><code>{e(f.get('code', ''))}</code></td><td><code>{e(where)}</code></td>"
                       f"<td>{e(f['message'])}{note}</td></tr>")
        out.append("</table>")

    if history:
        out.append("<h2>Regression history</h2><table><tr><th>When</th><th>Passed</th><th>Failed</th><th>Time</th>"
                   "<th>Line coverage</th></tr>")
        for h in list(history)[-15:][::-1]:
            line = h.get("coverage", {}).get("line")
            out.append(f"<tr><td>{e(h['when'])}</td><td>{h['passed']} / {h['total']}</td>"
                       f"<td class='{'fail' if h['failed'] else 'muted'}'>{h['failed']}</td><td>{h['seconds']} s</td>"
                       f"<td>{f'{line:.0f}%' if line is not None else '–'}</td></tr>")
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)
