"""The assistant panel: a chat with Claude beside every section of the app.

The panel shows Claude's replies as they stream, its reasoning summary (folded), each tool it uses with the
result, and an approval card before any edit or run. Bridge is how the worker thread that talks to the API
reaches the GTK thread; see agent.py for the agent and its tools.
"""

import os
import re
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

import agent  # noqa: E402
import dialogs  # noqa: E402
import theme  # noqa: E402
from runner import Task  # noqa: E402

SUGGESTIONS = ["Review my RTL for bugs and lint it", "Write a testbench for the top module and run it",
               "Add formal properties for the top module and prove them", "Draw the architecture of this design",
               "Build it for my FPGA board and report timing", "Why is my ASIC run failing timing?"]
TOOL_LABELS = {"get_overview": "Looked at the project", "list_files": "Listed files", "read_file": "Read",
               "search": "Searched", "get_hierarchy": "Read the module hierarchy", "get_results": "Read results",
               "edit_file": "Edit", "write_file": "Write", "set_project": "Change project settings",
               "create_project": "Create project", "make_testbench": "Generate a testbench",
               "make_formal": "Generate formal checks", "draw_diagram": "Draw diagram", "run_lint": "Lint", "run_checks": "Design checks and CDC", "run_signoff": "Run sign-off",
               "run_tests": "Run tests", "run_formal": "Run formal", "run_lec": "Run LEC",
               "run_fpga": "Build for FPGA", "run_asic": "Run the ASIC flow"}
KEY_FILE = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "orfs-flow-runner" / "assistant_key"


def render(text):
    """Pango markup for Claude's Markdown: code blocks, inline code, bold, headings and bullets."""
    out, code = [], False
    for line in text.split("\n"):
        if line.strip().startswith("```"):
            code = not code
            continue
        esc = GLib.markup_escape_text(line)
        if code:
            out.append(f'<span font_family="monospace" background="{theme.color("code_bg")}">{esc or " "}</span>')
            continue
        esc = re.sub(r"`([^`]+)`", r'<span font_family="monospace">\1</span>', esc)
        esc = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", esc)
        m = re.match(r"(#{1,4})\s+(.*)", esc)
        if m:
            esc = f"<b>{m.group(2)}</b>"
        esc = re.sub(r"^(\s*)[-*] ", r"\1• ", esc)
        out.append(esc)
    return "\n".join(out)


def label(text="", css=None, mark=False, wrap=True, select=True):
    w = Gtk.Label(xalign=0, wrap=wrap, selectable=select)
    w.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if mark:
        w.set_markup(text)
    else:
        w.set_text(text)
    if css:
        w.get_style_context().add_class(css)
    return w


class Bridge:
    """Called from the agent's worker thread; runs things on the GTK thread and waits for them."""

    def __init__(self, page):
        self.page = page

    def call(self, fn, **kw):
        box, ev = {}, threading.Event()

        def run():
            try:
                box["value"] = fn(**kw)
            except Exception as e:  # noqa: BLE001 - handed back to the worker, which reports it to Claude
                box["error"] = e
            ev.set()
            return False
        GLib.idle_add(run)
        ev.wait()
        if "error" in box:
            raise box["error"]
        return box["value"]

    def start(self, fn, **kw):
        """Start a run tool and wait for its done(text), or until the user stops the chat."""
        box, ev = {}, threading.Event()

        def done(text):
            if not ev.is_set():
                box["value"] = text
                ev.set()

        def run():
            try:
                fn(done, **kw)
            except Exception as e:  # noqa: BLE001 - reported, not raised
                box["error"] = e
                ev.set()
            return False
        GLib.idle_add(run)
        cancelled = self.page.agent.cancelled
        waited = 0.0
        while not ev.wait(0.5):
            waited += 0.5
            if cancelled.is_set():
                return "The user stopped the chat; the run may still be going in its section."
            if waited > agent.LONG_RUN:
                return "Still running after the time limit; check its section later."
        if "error" in box:
            raise box["error"]
        return box["value"]

    def approve(self, name, kind, detail):
        box, ev = {}, threading.Event()
        GLib.idle_add(self.page.show_approval, name, kind, detail, box, ev)
        cancelled = self.page.agent.cancelled
        while not ev.wait(0.3):
            if cancelled.is_set():
                return "no"
        return box.get("answer", "no")

    def event(self, kind, payload):
        GLib.idle_add(self.page.on_event, kind, payload)


class AgentPanel:
    """The assistant: a panel the header's Assistant button shows beside the sections."""

    def __init__(self, app):
        self.app = app
        cfg = app.settings.get("assistant", {})
        self.agent = agent.Agent(agent.Toolbox(app), model=cfg.get("model", agent.MODELS[0][0]),
                                 effort=cfg.get("effort", "high"), api_key=self.load_key())
        self.bridge = Bridge(self)
        self.worker = None
        self.reply = None          # the label Claude's current answer streams into
        self.reply_text = ""
        self.thinking = None
        self.tool_rows = {}
        self._render_due = False
        self.widget = self.build()

    # ---------- settings ----------
    @staticmethod
    def load_key():
        try:
            return KEY_FILE.read_text().strip()
        except OSError:
            return ""

    def save_key(self, key):
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if key:
            KEY_FILE.touch(mode=0o600)
            os.chmod(KEY_FILE, 0o600)
            KEY_FILE.write_text(key)
        else:
            KEY_FILE.unlink(missing_ok=True)

    def on_settings(self, *_):
        names = [m for m, _ in agent.MODELS]
        vals = dialogs.ask_fields(
            self.app, "Assistant settings",
            [("model", "Model", self.agent.model, names), ("effort", "Effort", self.agent.effort, agent.EFFORTS),
             ("key", "API key", self.agent.api_key, "secret")],
            intro="The assistant uses the Claude API. Leave the key empty to use ANTHROPIC_API_KEY or an "
                  "`ant auth login` profile. A key typed here is kept in ~/.config/orfs-flow-runner/assistant_key, "
                  "readable only by you. Higher effort thinks longer: better on hard bugs, slower and costlier.",
            ok="Save")
        if not vals:
            return
        self.agent.model, self.agent.effort = vals["model"], vals["effort"]
        if vals["key"] != self.agent.api_key:
            self.agent.api_key, self.agent.client = vals["key"], None
            self.save_key(vals["key"])
        self.app.settings["assistant"] = {"model": self.agent.model, "effort": self.agent.effort}
        self.app.save_settings()
        self.update_setup()

    # ---------- layout ----------
    def build(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=380)
        box.get_style_context().add_class("assistant")
        head = Gtk.Box(spacing=6, margin=10)
        title = Gtk.Label(label="Assistant", xalign=0)
        title.get_style_context().add_class("card-title")
        self.model_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.model_label.get_style_context().add_class("muted")
        new = Gtk.Button(label="New chat", tooltip_text="Forget this conversation and start again")
        new.connect("clicked", self.on_new_chat)
        gear = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        gear.set_tooltip_text("Model, effort and API key")
        gear.connect("clicked", self.on_settings)
        for w, expand in ((title, False), (self.model_label, True), (new, False), (gear, False)):
            head.pack_start(w, expand, expand, 0)
        box.pack_start(head, False, False, 0)

        self.setup = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=10, margin_end=10)
        box.pack_start(self.setup, False, False, 0)

        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=10)
        self.scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.add(self.list)
        box.pack_start(self.scroll, True, True, 0)
        self.list.connect("size-allocate", lambda *_: self.stick_to_end())
        self._stick = True
        self.scroll.get_vadjustment().connect("value-changed", self.on_scrolled)

        self.status = Gtk.Label(xalign=0, margin_start=12)
        self.status.get_style_context().add_class("muted")
        box.pack_start(self.status, False, False, 0)
        entry_box = Gtk.Box(spacing=6, margin=10)
        self.entry = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, left_margin=8,
                                  right_margin=8, top_margin=6, bottom_margin=6)
        self.entry.get_style_context().add_class("chat-entry")
        self.entry.connect("key-press-event", self.on_key)
        sw = Gtk.ScrolledWindow(hexpand=True, min_content_height=64, max_content_height=180,
                                propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(self.entry)
        buttons = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.send_btn = Gtk.Button(label="Send", tooltip_text="Send (Ctrl+Enter)")
        self.send_btn.get_style_context().add_class("suggested-action")
        self.send_btn.connect("clicked", lambda *_: self.on_send())
        self.stop_btn = Gtk.Button(label="Stop", sensitive=False, tooltip_text="Stop after the current step")
        self.stop_btn.connect("clicked", lambda *_: self.agent.cancelled.set())
        buttons.pack_start(self.send_btn, False, False, 0)
        buttons.pack_start(self.stop_btn, False, False, 0)
        entry_box.pack_start(sw, True, True, 0)
        entry_box.pack_start(buttons, False, False, 0)
        box.pack_start(entry_box, False, False, 0)
        self.update_setup()
        self.show_welcome()
        box.show_all()
        return box

    def update_setup(self):
        for child in self.setup.get_children():
            self.setup.remove(child)
        self.model_label.set_text(dict(agent.MODELS).get(self.agent.model, self.agent.model) +
                                  f" · {self.agent.effort}")
        if agent.load_sdk() is None:
            self.setup.pack_start(label("The Claude SDK for Python is not installed yet. Install it into the "
                                        "app's own folder (about 15 MB, no admin rights needed).", "muted"),
                                  False, False, 0)
            btn = Gtk.Button(label="Install Claude SDK")
            btn.connect("clicked", self.on_install)
            self.setup.pack_start(btn, False, False, 0)
        elif not agent.has_credentials(self.agent.api_key):
            self.setup.pack_start(label("Add a Claude API key (from console.anthropic.com) or sign in with "
                                        "`ant auth login`.", "muted"), False, False, 0)
            btn = Gtk.Button(label="Set API key…")
            btn.connect("clicked", self.on_settings)
            self.setup.pack_start(btn, False, False, 0)
        self.setup.show_all()

    def on_install(self, btn):
        cmd = agent.install_command()
        if not cmd:
            self.app.message("Neither uv nor pip is available. Install uv (https://docs.astral.sh/uv/) and try "
                             "again.")
            return
        btn.set_sensitive(False)
        btn.set_label("Installing…")

        def done(task):
            if task.rc != 0:
                self.app.message("Installing the SDK failed:\n" + task.text[-1500:])
            self.update_setup()
        Task(cmd, on_done=done)

    def show_welcome(self):
        intro = label("Ask about your design, or have the assistant do the work: write and fix RTL, add "
                      "testbenches and formal checks, run them, draw diagrams, build for FPGA, and run and debug "
                      "the ASIC flow. It asks before it changes a file or starts a run.", "muted")
        self.list.pack_start(intro, False, False, 0)
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=1, row_spacing=4)
        for text in SUGGESTIONS:
            b = Gtk.Button(label=text, relief=Gtk.ReliefStyle.NONE)
            b.get_child().set_xalign(0)
            b.get_style_context().add_class("suggestion")
            b.connect("clicked", lambda _b, t=text: self.on_send(t))
            flow.add(b)
        self.list.pack_start(flow, False, False, 0)
        self.welcome = [intro, flow]
        self.list.show_all()

    def on_new_chat(self, *_):
        if self.busy():
            return
        self.agent.reset()
        for child in self.list.get_children():
            self.list.remove(child)
        self.reply, self.tool_rows = None, {}
        self.status.set_text("")
        self.show_welcome()

    # ---------- scrolling ----------
    def on_scrolled(self, adj):
        self._stick = adj.get_value() + adj.get_page_size() >= adj.get_upper() - 40

    def stick_to_end(self):
        if self._stick:
            adj = self.scroll.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())

    # ---------- sending ----------
    def busy(self):
        return bool(self.worker and self.worker.is_alive())

    def on_key(self, _w, event):
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and event.state & Gdk.ModifierType.CONTROL_MASK:
            self.on_send()
            return True
        return False

    def context(self):
        """What the user is looking at, sent with each message so 'this file' and 'here' make sense."""
        a = self.app
        parts = [f"section shown: {a.sections.get_visible_child_name()}"]
        if a.project:
            parts.append(f"project: {a.project.name}")
        rtl = a.section_pages.get("rtl")
        path = rtl.current_path() if rtl else None
        if path:
            line, _ = rtl.current_view().cursor()
            parts.append(f"open in the RTL editor: {path} (cursor at line {line})")
        if a.flow and a.config_rel():
            parts.append(f"ASIC design: {a.config_rel()}")
        return "; ".join(parts)

    def on_send(self, text=None):
        if self.busy():
            return
        buf = self.entry.get_buffer()
        text = (text or buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)).strip()
        if not text:
            return
        if agent.load_sdk() is None or not agent.has_credentials(self.agent.api_key):
            self.update_setup()
            self.status.set_text("Finish the setup above first.")
            return
        buf.set_text("")
        for w in getattr(self, "welcome", []):
            if w.get_parent():
                self.list.remove(w)
        self.welcome = []
        bubble = label(text, "chat-user")
        bubble.set_halign(Gtk.Align.END)
        self.add(bubble)
        self.reply, self.thinking = None, None
        self.send_btn.set_sensitive(False)
        self.stop_btn.set_sensitive(True)
        self._stick = True
        context = self.context()
        self.worker = threading.Thread(target=self.work, args=(text, context), daemon=True)
        self.worker.start()

    def work(self, text, context):
        self.agent.send(text, context, self.bridge)
        GLib.idle_add(self.finished)

    def finished(self):
        self.send_btn.set_sensitive(True)
        self.stop_btn.set_sensitive(False)
        if self.status.get_text() in ("Thinking…", "Working…"):
            self.status.set_text("")
        self.flush_render()
        return False

    def add(self, widget):
        self.list.pack_start(widget, False, False, 0)
        widget.show_all()

    # ---------- events from the agent ----------
    def on_event(self, kind, payload):
        if kind == "text":
            if self.reply is None:
                self.reply, self.reply_text = label(css="chat-reply", mark=True), ""
                self.add(self.reply)
            self.reply_text += payload
            if not self._render_due:
                self._render_due = True
                GLib.timeout_add(80, self.flush_render)
        elif kind == "thinking":
            if self.thinking is None:
                exp = Gtk.Expander(label="Reasoning")
                exp.get_style_context().add_class("muted")
                self.thinking = label(css="chat-thinking")
                exp.add(self.thinking)
                self.add(exp)
            self.thinking.set_text(self.thinking.get_text() + payload)
        elif kind == "tool":
            self.flush_render()
            self.reply, self.thinking = None, None   # text after a tool is a new paragraph
            self.add_tool_row(payload)
        elif kind == "tool_done":
            self.finish_tool_row(payload)
        elif kind == "status":
            self.status.set_text(payload)
        elif kind == "error":
            self.add(label(GLib.markup_escape_text(payload), "chat-error", mark=True))
            self.status.set_text("")
        return False

    def flush_render(self):
        self._render_due = False
        if self.reply is not None:
            self.reply.set_markup(render(self.reply_text))
        return False

    def describe(self, name, args):
        what = TOOL_LABELS.get(name, name)
        target = args.get("path") or args.get("module") or args.get("name") or args.get("kind") or \
            args.get("pattern") or args.get("to_stage") or ""
        if name == "run_tests" and args.get("names"):
            target = ", ".join(args["names"])
        return f"{what} {target}".strip()

    def add_tool_row(self, payload):
        exp = Gtk.Expander()
        head = Gtk.Box(spacing=6)
        spinner = Gtk.Spinner(active=True)
        text = Gtk.Label(label=self.describe(payload["name"], payload["input"]), xalign=0,
                         ellipsize=Pango.EllipsizeMode.MIDDLE)
        head.pack_start(spinner, False, False, 0)
        head.pack_start(text, True, True, 0)
        exp.set_label_widget(head)
        exp.get_style_context().add_class("chat-tool")
        body = label(css="chat-tool-body")
        body.get_style_context().add_class("monospace")
        exp.add(body)
        self.tool_rows[payload["id"]] = (exp, spinner, head, body)
        self.add(exp)
        self.status.set_text("Working…")

    def finish_tool_row(self, payload):
        row = self.tool_rows.get(payload["id"])
        if not row:      # a result without a row: the input was rejected before the tool started
            self.add_tool_row({"id": payload["id"], "name": payload["name"], "input": {}})
            row = self.tool_rows[payload["id"]]
        _exp, spinner, head, body = row
        head.remove(spinner)
        mark = Gtk.Label(label="✕" if payload["error"] else "✓")
        mark.get_style_context().add_class("chip-fail" if payload["error"] else "chip-pass")
        head.pack_start(mark, False, False, 0)
        head.reorder_child(mark, 0)
        head.show_all()
        text = payload["text"]
        body.set_text(text if len(text) < 6000 else text[:6000] + "\n…")

    def show_approval(self, name, kind, detail, box, ev):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("card")
        card.get_style_context().add_class("approval")
        what = "change your files" if kind == "edit" else "start a run"
        card.pack_start(label(f"<b>The assistant wants to {what}:</b> "
                              f"{GLib.markup_escape_text(TOOL_LABELS.get(name, name))}", mark=True), False, False, 0)
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.CHAR, left_margin=6,
                            top_margin=4)
        buf = view.get_buffer()
        add_tag = buf.create_tag("add", foreground=theme.color("success"))
        del_tag = buf.create_tag("del", foreground=theme.color("danger"))
        for line in detail.splitlines(keepends=True):
            tag = add_tag if line.startswith("+") and not line.startswith("+++") else \
                del_tag if line.startswith("-") and not line.startswith("---") else None
            if tag:
                buf.insert_with_tags(buf.get_end_iter(), line, tag)
            else:
                buf.insert(buf.get_end_iter(), line)
        sw = Gtk.ScrolledWindow(min_content_height=min(60 + 16 * detail.count("\n"), 320),
                                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        sw.add(view)
        card.pack_start(sw, False, False, 0)
        row = Gtk.Box(spacing=6)
        result = label(css="muted", select=False)

        def answer(_b, value):
            box["answer"] = value
            for b in buttons:
                row.remove(b)
            result.set_text({"yes": "Allowed.", "always": f"Allowed; further {kind}s in this chat run without "
                             "asking.", "no": "Denied."}[value])
            ev.set()
        buttons = []
        for text, value, css in (("Allow", "yes", "suggested-action"), ("Allow for this chat", "always", None),
                                 ("Deny", "no", "destructive-action")):
            b = Gtk.Button(label=text)
            if css:
                b.get_style_context().add_class(css)
            b.connect("clicked", answer, value)
            buttons.append(b)
            row.pack_start(b, False, False, 0)
        row.pack_start(result, True, True, 0)
        card.pack_start(row, False, False, 0)
        self.add(card)
        self._stick = True
        self.status.set_text("Waiting for your approval…")
        return False
