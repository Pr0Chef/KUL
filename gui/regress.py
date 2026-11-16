"""Regressions: run many tests in parallel, with seeds, timeouts and tags, and keep a history.

A test (see sim.py) may add:
  "seeds":   how many random seeds to run it with (default 1). Each seed is its own run, "<test>.s<seed>".
  "seed":    the first seed (default 1), or "random" for new seeds every regression.
  "timeout": seconds a run may take before it is stopped and marked "timeout" (default 600).
  "tags":    words to select tests by, such as "smoke" or "nightly".
The seed reaches the testbench as the plusarg +seed=<n> (read it with $value$plusargs("seed=%d", seed)).

A test is compiled once, in build/<project>/sim/<test>/; each seed runs that build in its own subfolder, so
waveforms, logs and coverage of different seeds never mix. Regression runs the builds and runs on a pool of worker
threads; it has no GTK in it, so the callbacks arrive on worker threads.
"""

import json
import os
import queue
import random
import signal
import subprocess
import threading
import time
from pathlib import Path

import eda_tools
import sim

HISTORY = 50      # regressions kept in the history file
LOG_TAIL = 6000   # characters of a run's log kept in the results


def expand(tests, tags=None):
    """The runs of a regression: [{"id", "test", "seed"}], one per test and seed, for tests with any of tags."""
    runs = []
    for t in tests:
        if tags and not set(tags) & set(t.get("tags", [])):
            continue
        n = max(int(t.get("seeds", 1) or 1), 1)
        first = t.get("seed", 1)
        if n == 1 and first in (None, "", 1):
            runs.append({"id": t["name"], "test": t, "seed": None})
            continue
        seeds = random.sample(range(1, 2 ** 31), n) if first == "random" else [int(first) + i for i in range(n)]
        runs += [{"id": f"{t['name']}.s{s}", "test": t, "seed": s} for s in seeds]
    return runs


def _run(cmd, cwd, env, log, timeout, stopped):
    """Run one command, appending its output to log. Returns (exit code, "timeout" | "stopped" | "")."""
    with open(log, "ab") as out:
        out.write(("$ " + " ".join(str(c) for c in cmd) + "\n").encode())
        out.flush()
        try:
            proc = subprocess.Popen([str(c) for c in cmd], cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            out.write(f"Could not start: {e}\n".encode())
            return 127, ""
        end = time.time() + timeout
        while proc.poll() is None:
            if stopped.is_set() or time.time() > end:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return -9, "stopped" if stopped.is_set() else "timeout"
            time.sleep(0.05)
        return proc.returncode, ""


class Regression:
    """Runs a list of runs (from expand) with `jobs` in parallel.

    on_update(run id, result) is called when a run starts ("running") and when it ends; on_done(results) once at
    the end. results: {run id: {"state", "why", "seconds", "when", "seed", "test", "folder", "log"}} with state
    pass, fail, ran, timeout, error or stopped.
    """

    def __init__(self, project, runs, jobs=2, chosen=None, coverage=False, on_update=None, on_done=None):
        self.project, self.runs, self.jobs, self.chosen = project, list(runs), max(int(jobs), 1), chosen
        self.coverage, self.on_update, self.on_done = coverage, on_update, on_done
        self.results, self.stopped, self.started = {}, threading.Event(), time.time()
        self.lock = threading.Lock()
        self.tasks = queue.Queue()
        self.left = 0
        self.thread = None

    def start(self):
        groups = {}
        for run in self.runs:
            groups.setdefault(run["test"]["name"], []).append(run)
        self.left = len(self.runs)
        for runs in groups.values():
            self.tasks.put(("build", runs))
        if not self.runs:
            self._finish()
            return self
        self.workers = [threading.Thread(target=self._work, daemon=True) for _ in range(self.jobs)]
        for w in self.workers:
            w.start()
        return self

    def stop(self):
        self.stopped.set()

    def running(self):
        return self.left > 0

    def wait(self, seconds=None):
        end = time.time() + seconds if seconds else None
        while self.running() and (end is None or time.time() < end):
            time.sleep(0.05)
        return not self.running()

    # ---- workers ----
    def _work(self):
        while True:
            try:
                kind, item = self.tasks.get(timeout=0.2)
            except queue.Empty:
                if self.left <= 0:
                    return
                continue
            try:
                if kind == "build":
                    self._build(item)
                else:
                    self._simulate(*item)
            except Exception as e:  # noqa: BLE001 - a broken run must not stop the other runs
                for run in ([item[0]] if kind == "run" else item):
                    if run["id"] not in self.results or self.results[run["id"]]["state"] == "running":
                        self._end(run, "error", f"{type(e).__name__}: {e}", 0, None)

    def _test(self, run):
        t = dict(run["test"])
        if self.coverage and t.get("simulator") == "verilator":
            t["coverage"] = True
        return t

    def _build(self, runs):
        test = self._test(runs[0])
        tool = "iverilog" if test.get("kind") == "cocotb" else test.get("simulator", "iverilog")
        for run in runs:
            self._update(run, {"state": "running", "seed": run["seed"], "test": test["name"]})
        if not eda_tools.find(tool, self.chosen):
            for run in runs:
                self._end(run, "error", f"{tool} is not installed", 0, None)
            return
        folder, cmds = sim.commands(self.project, test, self.chosen)
        log = folder / "build.log"
        log.write_bytes(b"")
        env = eda_tools.env(self.chosen)
        started = time.time()
        rc, why = _run(cmds[0], str(folder), env, log, 1800, self.stopped)
        if rc != 0:
            text = log.read_text(errors="replace")
            for run in runs:
                self._end(run, "stopped" if why == "stopped" else "fail", why or "the build failed",
                          time.time() - started, folder, text)
            return
        for run in runs:
            self.tasks.put(("run", (run, folder, cmds[1:], test)))

    def _simulate(self, run, folder, cmds, test):
        cwd = folder if run["seed"] is None else folder / f"s{run['seed']}"
        cwd.mkdir(parents=True, exist_ok=True)
        for old in list(cwd.glob("*.vcd")) + list(cwd.glob("*.fst")) + [cwd / "coverage.dat"]:
            old.unlink(missing_ok=True)
        log = cwd / "run.log"
        log.write_bytes((folder / "build.log").read_bytes() if run["seed"] is None else b"")
        env = eda_tools.env(self.chosen)
        started = time.time()
        rc, why = 0, ""
        for cmd in cmds:
            cmd = list(cmd) + ([f"+seed={run['seed']}"] if run["seed"] is not None and cmd is cmds[0] else [])
            rc, why = _run(cmd, str(cwd), env, log, float(test.get("timeout", 600) or 600), self.stopped)
            if rc != 0:
                break
        text = log.read_text(errors="replace")
        if why:
            state, reason = why, f"stopped after {test.get('timeout', 600)} s" if why == "timeout" else "stopped"
        else:
            state, reason = sim.verdict(rc, text, test.get("kind", ""))
        self._end(run, state, reason, time.time() - started, cwd, text)

    # ---- results ----
    def _update(self, run, result):
        with self.lock:
            self.results[run["id"]] = result
        if self.on_update:
            self.on_update(run["id"], result)

    def _end(self, run, state, why, seconds, folder, text=""):
        self._update(run, {"state": state, "why": why, "seconds": round(seconds, 1), "seed": run["seed"],
                           "test": run["test"]["name"], "when": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "folder": str(folder) if folder else "", "log": text[-LOG_TAIL:]})
        with self.lock:
            self.left -= 1
            done = self.left <= 0
        if done:
            self._finish()

    def _finish(self):
        self.seconds = round(time.time() - self.started, 1)
        if self.on_done:
            self.on_done(self.results)

    def coverage_files(self):
        """coverage.dat of every run that wrote one."""
        return [Path(r["folder"]) / "coverage.dat" for r in self.results.values()
                if r.get("folder") and (Path(r["folder"]) / "coverage.dat").exists()]


def tally(results):
    """{"total", "passed", "failed", "other"} over run results; timeouts and errors count as failed."""
    states = [r.get("state") for r in results.values()]
    passed = states.count("pass")
    failed = sum(s in ("fail", "timeout", "error") for s in states)
    return {"total": len(states), "passed": passed, "failed": failed, "other": len(states) - passed - failed}


# ---------------------------------------------------------------- history
def history_file(project):
    return project.build("sim").parent / "regressions.json"


def load_history(project):
    try:
        return json.loads(history_file(project).read_text())
    except (OSError, ValueError):
        return []


def add_history(project, results, seconds, coverage=None):
    """Append a regression to the project's history (the newest HISTORY are kept); returns the history."""
    entry = dict(tally(results), when=time.strftime("%Y-%m-%d %H:%M:%S"), seconds=seconds,
                 failed_runs=sorted(k for k, r in results.items() if r.get("state") in ("fail", "timeout", "error")))
    if coverage:
        entry["coverage"] = {k: v.get("percent") for k, v in coverage.items() if isinstance(v, dict)
                             and v.get("total")}
    hist = (load_history(project) + [entry])[-HISTORY:]
    history_file(project).write_text(json.dumps(hist, indent=1))
    return hist
