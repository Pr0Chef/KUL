"""Run a tool in the background and show its output: used by every page that starts simulators, FPGA tools or git.

Task runs one command off the GTK thread and reports back on it. Console is the widget that shows a task's output
with errors in red and warnings in amber, a Stop button, and the command that ran.
"""

import os
import re
import shlex
import signal
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

import theme  # noqa: E402

ERROR_RE = re.compile(r"%Error|\berror\b|\bERROR:|\bFAIL(ED)?\b|\bfatal\b|Assertion failed|\*\*\* ", re.I)
NO_ERRORS_RE = re.compile(r"\b0 errors?\b", re.I)
WARNING_RE = re.compile(r"%Warning|\bwarning\b", re.I)
OK_RE = re.compile(r"\bPASS(ED)?\b|DONE \(PASS|Info: Program finished normally|successfully", re.I)


class Task:
    """One command in its own process group. on_output(text) and on_done(task) are called on the GTK thread."""

    def __init__(self, cmd, cwd=None, env=None, on_output=None, on_done=None):
        self.cmd = cmd if isinstance(cmd, str) else shlex.join(str(c) for c in cmd)
        self.output, self.rc, self.started, self.seconds = [], None, time.time(), 0.0
        self.on_output, self.on_done = on_output, on_done
        self.stopped = False
        try:
            self.proc = subprocess.Popen(["bash", "-c", self.cmd] if isinstance(cmd, str) else [str(c) for c in cmd],
                                         cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            self.proc = None
            self.output.append(f"Could not start: {e}\n")
            self.rc = 127
            GLib.idle_add(self._finish)
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for chunk in iter(lambda: self.proc.stdout.read1(65536), b""):
            text = chunk.decode(errors="replace")
            self.output.append(text)
            if self.on_output:
                GLib.idle_add(self.on_output, text)
        self.rc = self.proc.wait()
        GLib.idle_add(self._finish)

    def _finish(self):
        self.seconds = time.time() - self.started
        if self.on_done:
            self.on_done(self)
        return False

    @property
    def text(self):
        return "".join(self.output)

    def running(self):
        return self.rc is None

    def stop(self):
        if self.proc and self.running():
            self.stopped = True
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


def run_sync(cmd, cwd=None, env=None, timeout=600):
    """Run a command and wait: (exit code, output). For tests and for quick queries off the GTK thread."""
    try:
        out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                             shell=isinstance(cmd, str), stdin=subprocess.DEVNULL)
        return out.returncode, out.stdout + out.stderr
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or "") + f"\nTimed out after {timeout} s\n"
    except OSError as e:
        return 127, f"Could not start: {e}\n"


class Console(Gtk.Box):
    """Output of the page's tasks: a title, Stop and Clear, and the text with errors and warnings colored."""

    def __init__(self, title="Output"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.task = None
        bar = Gtk.Box(spacing=8)
        self.title = Gtk.Label(label=title, xalign=0)
        self.title.get_style_context().add_class("card-title")
        self.state = Gtk.Label(xalign=0, hexpand=True)
        self.state.get_style_context().add_class("muted")
        self.stop_btn = Gtk.Button(label="Stop", sensitive=False)
        self.stop_btn.get_style_context().add_class("destructive-action")
        self.stop_btn.connect("clicked", lambda *_: self.task and self.task.stop())
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.clear())
        for w, expand in ((self.title, False), (self.state, True), (self.stop_btn, False), (clear, False)):
            bar.pack_start(w, expand, expand, 0)
        self.pack_start(bar, False, False, 0)
        self.view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, left_margin=8, top_margin=6)
        self.view.get_style_context().add_class("monospace")
        buf = self.view.get_buffer()
        buf.create_tag("err", foreground=theme.color("danger"))
        buf.create_tag("warn", foreground=theme.color("warning"))
        buf.create_tag("ok", foreground=theme.color("success"))
        buf.create_tag("cmd", foreground=theme.color("info"), weight=700)
        sw = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        sw.add(self.view)
        self.pack_start(sw, True, True, 0)

    def clear(self):
        self.view.get_buffer().set_text("")

    def append(self, text, tag=None):
        buf = self.view.get_buffer()
        for line in text.splitlines(keepends=True):
            bad = ERROR_RE.search(line) and not NO_ERRORS_RE.search(line)
            use = tag or ("err" if bad else "warn" if WARNING_RE.search(line)
                          else "ok" if OK_RE.search(line) else None)
            if use:
                buf.insert_with_tags_by_name(buf.get_end_iter(), line, use)
            else:
                buf.insert(buf.get_end_iter(), line)
        mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.view.scroll_mark_onscreen(mark)
        buf.delete_mark(mark)

    def busy(self):
        return bool(self.task and self.task.running())

    def run(self, cmd, cwd=None, env=None, on_done=None, label=None):
        """Start a command and stream its output here; on_done(task) when it ends. One task at a time."""
        if self.busy():
            self.append("\nAnother command is still running here; stop it first.\n", "warn")
            return None
        shown = cmd if isinstance(cmd, str) else shlex.join(str(c) for c in cmd)
        self.append(f"\n$ {shown}\n", "cmd")
        self.state.set_text(label or "Running…")
        self.stop_btn.set_sensitive(True)

        def done(task):
            self.stop_btn.set_sensitive(False)
            verdict = "stopped" if task.stopped else "finished" if task.rc == 0 else f"failed (exit code {task.rc})"
            self.state.set_text(f"{label or 'Command'} {verdict} after {task.seconds:.1f} s")
            self.append(f">> exit code {task.rc} after {task.seconds:.1f} s\n", "ok" if task.rc == 0 else "err")
            if on_done:
                on_done(task)
        self.task = Task(cmd, cwd=cwd, env=env, on_output=self.append, on_done=done)
        return self.task

    def run_all(self, steps, cwd=None, env=None, on_done=None, label=None):
        """Run commands one after another, stopping at the first that fails; on_done(last task, all output)."""
        steps = list(steps)
        collected = []

        def next_step(task=None):
            if task is not None:
                collected.append(task.text)
            if task is not None and (task.rc != 0 or not steps):
                if on_done:
                    on_done(task, "".join(collected))
                return
            if not steps:
                return
            self.run(steps.pop(0), cwd=cwd, env=env, on_done=next_step, label=label)
        if steps:
            next_step()
