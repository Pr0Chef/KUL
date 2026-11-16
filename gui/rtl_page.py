"""RTL section: write and browse a project's RTL, and check it.

Left: the project's files and its module hierarchy. Middle: the editor, one tab per open file. Bottom: the
findings of the checks (double-click one to go to it) and the tools' output. Three checks feed the findings:
lint (Verilator, Icarus, Yosys, or all of them), the design checks (latches, loops, drivers) and the clock domain
crossing analysis. A finding can be waived with a reason; waived findings stay listed but no longer count. New
files start from templates; a testbench, a cocotb test or a formal property file can be generated from a module.
"""

import json
import re
import shutil
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

import cdc  # noqa: E402
import coverage  # noqa: E402
import designcheck as dc  # noqa: E402
import dialogs  # noqa: E402
import eda_tools  # noqa: E402
import hdl  # noqa: E402
import lint  # noqa: E402
import workspace as ws  # noqa: E402
from code_editor import CodeView, language_of  # noqa: E402
from input_page import MUTED, markup  # noqa: E402
from runner import Console  # noqa: E402

MAX_OPEN = 4 << 20   # bytes; larger files are not opened in the editor
TEXT_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".tcl", ".sdc", ".mk", ".txt", ".md", ".json", ".sby", ".pcf", ".lpf",
                 ".cst", ".py", ".yaml", ".yml", ".cfg", ".f", ".hex", ".mem", ".gitignore", ".c", ".h", ".cpp",
                 ".xdc", ".eqy", ".ys", ".sh", ".csv", ".log", ".rpt"}


class RtlPage:
    name, title = "rtl", "RTL"

    def __init__(self, app):
        self.app = app
        self.docs = {}        # path: {"view": CodeView, "label": Gtk.Label, "mtime": float}
        self.sets = {"lint": [], "design": [], "cdc": []}    # findings of each check, from its last run
        self.ran = {"lint": False, "design": False, "cdc": False}
        self.cdc_result = None
        self.cov = None       # coverage summary, to tint lines no test ran
        self.widget = self.build()

    @property
    def findings(self):
        """Every finding of every check, with the project's waivers applied."""
        p = self.app.project
        return dc.apply_waivers(self.sets["lint"] + self.sets["design"] + self.sets["cdc"], p["waivers"] if p else [])

    @property
    def problems(self):
        return self.findings

    # ---------- layout ----------
    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bar = Gtk.Box(spacing=6)
        bar.get_style_context().add_class("inputs-head")
        self.project_label = Gtk.Label(xalign=0)
        bar.pack_start(self.project_label, False, False, 0)
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)

        def button(label, handler, tip, suggested=False):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            if suggested:
                b.get_style_context().add_class("suggested-action")
            bar.pack_start(b, False, False, 0)
            return b
        button("New file…", self.on_new_file, "Add a file to the project from a template")
        button("New folder…", self.on_new_folder, "Add a folder to the project")
        self.save_btn = button("Save", lambda *_: self.save_current(), "Save the file in the editor (Ctrl+S)", True)
        button("Save all", lambda *_: self.save_all(), "Save every changed file")
        button("Find", lambda *_: self.current_view() and self.current_view().show_find(), "Find in the file (Ctrl+F)")
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        self.linter_cb = Gtk.ComboBoxText(tooltip_text="The linter to run")
        bar.pack_start(self.linter_cb, False, False, 0)
        self.strict = Gtk.CheckButton(label="strict", active=True,
                                      tooltip_text="Also report style, width and unused-signal warnings (-Wall)")
        bar.pack_start(self.strict, False, False, 0)
        button("Lint", lambda *_: self.run_lint(), "Check the project's design files with the linter")
        self.lint_on_save = Gtk.CheckButton(label="on save", tooltip_text="Lint every time a file is saved")
        bar.pack_start(self.lint_on_save, False, False, 0)
        bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4)
        button("Testbench…", self.on_testbench, "Write a testbench for a module (Verilog, or Python with cocotb) "
                                               "and add it as a test")
        button("Properties…", self.on_formal, "Write a formal property file for a module")
        button("Instance…", self.on_instance, "Insert an instance of a module at the cursor")
        self.position = Gtk.Label(xalign=1, hexpand=True)
        self.position.get_style_context().add_class("muted")
        bar.pack_end(self.position, True, True, 0)
        outer.pack_start(bar, False, False, 0)

        # Left: files and hierarchy.
        side = Gtk.Notebook(width_request=260)
        self.fstore = Gtk.TreeStore(str, str, bool)   # name, path, is a folder
        self.ftree = Gtk.TreeView(model=self.fstore, headers_visible=False, activate_on_single_click=True)
        self.ftree.append_column(Gtk.TreeViewColumn("File", Gtk.CellRendererText(), text=0))
        self.ftree.connect("row-activated", self.on_file_row)
        fbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(self.ftree)
        fbox.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6, margin=6)
        for label, handler in (("Rename…", self.on_rename), ("Delete…", self.on_delete), ("Refresh", self.fill_files)):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            row.pack_start(b, True, True, 0)
        fbox.pack_start(row, False, False, 0)
        side.append_page(fbox, Gtk.Label(label="Files"))
        self.hstore = Gtk.TreeStore(str, str, int)   # label, file, line
        htree = Gtk.TreeView(model=self.hstore, headers_visible=False)
        htree.append_column(Gtk.TreeViewColumn("Module", Gtk.CellRendererText(), markup=0))
        htree.connect("row-activated", lambda tv, path, _c: self.hstore[path][1] and self.open_file(
            Path(self.hstore[path][1]), self.hstore[path][2]))
        self.htree = htree
        sw = Gtk.ScrolledWindow()
        sw.add(htree)
        side.append_page(sw, Gtk.Label(label="Hierarchy"))

        # Middle: editor tabs over problems and output.
        self.nb = Gtk.Notebook(scrollable=True)
        self.nb.connect("switch-page", lambda *_: GLib.idle_add(self.update_position))
        self.empty = Gtk.Label(label="Open a file from the list on the left, or add one with New file…")
        self.empty.get_style_context().add_class("muted")
        self.editor_stack = Gtk.Stack()
        self.editor_stack.add_named(self.empty, "empty")
        self.editor_stack.add_named(self.nb, "editor")

        bottom = Gtk.Notebook()
        # severity, file, line, message, color, check, index into self.shown
        self.pstore = Gtk.ListStore(str, str, int, str, str, str, int)
        ptv = Gtk.TreeView(model=self.pstore)
        ptv.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(weight=700), text=0, foreground=4))
        ptv.append_column(Gtk.TreeViewColumn("Check", Gtk.CellRendererText(foreground=MUTED), text=5))
        ptv.append_column(Gtk.TreeViewColumn("File", Gtk.CellRendererText(), text=1))
        ptv.append_column(Gtk.TreeViewColumn("Line", Gtk.CellRendererText(xalign=1), text=2))
        ptv.append_column(Gtk.TreeViewColumn("Problem", Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END),
                                             text=3, foreground=4))
        ptv.connect("row-activated", self.on_problem)
        ptv.set_tooltip_column(3)
        self.ptv = ptv
        self.shown = []
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(ptv)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        prow = Gtk.Box(spacing=6, margin=6)
        for label, handler, tip in (
                ("Design checks", lambda *_: self.run_design_checks(),
                 "Elaborate the design with Yosys and report inferred latches, combinational loops, nets with "
                 "several drivers and nets without one"),
                ("CDC", lambda *_: self.run_cdc(),
                 "Clock domain crossings: signals that pass between clocks without a synchronizer"),
                ("Check all", lambda *_: self.run_all_checks(), "Lint, design checks and CDC, one after another"),
                ("Waive…", self.on_waive, "Accept the selected finding, with a reason; it stops counting"),
                ("Remove waiver", self.on_unwaive, "Make the selected waived finding count again")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            prow.pack_start(b, False, False, 0)
        self.show_waived = Gtk.CheckButton(label="Show waived", active=True)
        self.show_waived.connect("toggled", lambda *_: self.show_problems())
        prow.pack_start(self.show_waived, False, False, 0)
        self.checks_label = Gtk.Label(xalign=1, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.checks_label.get_style_context().add_class("muted")
        prow.pack_end(self.checks_label, True, True, 0)
        pbox.pack_start(prow, False, False, 0)
        pbox.pack_start(sw, True, True, 0)
        self.problems_label = Gtk.Label(label="Problems")
        bottom.append_page(pbox, self.problems_label)
        self.console = Console("Lint and check output")
        holder = Gtk.Box(margin=8)
        holder.pack_start(self.console, True, True, 0)
        bottom.append_page(holder, Gtk.Label(label="Output"))
        self.bottom = bottom

        vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, position=520)
        vpaned.pack1(self.editor_stack, True, False)
        vpaned.pack2(bottom, False, True)
        hpaned = Gtk.Paned(position=270)
        hpaned.pack1(side, False, False)
        hpaned.pack2(vpaned, True, False)
        outer.pack_start(hpaned, True, True, 0)
        return outer

    # ---------- project ----------
    def flow_changed(self):
        self.linter_cb.remove_all()
        tools = lint.available(self.app.rtl_tools())
        for tool, label in tools:
            self.linter_cb.append(tool, label)
        if len(tools) > 1:
            self.linter_cb.append("all", "All linters")
        self.linter_cb.set_active(0)

    def project_changed(self):
        # Another project: close its files (asking was done by the app before switching).
        for path in list(self.docs):
            self.close(path, ask=False)
        p = self.app.project
        self.project_label.set_markup(f"Project <b>{GLib.markup_escape_text(p.name)}</b>" if p else
                                      markup("No project: make or clone one in the Project section.", MUTED))
        self.sets = {"lint": [], "design": [], "cdc": []}
        self.ran = {"lint": False, "design": False, "cdc": False}
        self.cdc_result, self.cov = None, None
        self.load_checks()
        self.show_problems(focus=False)
        self.fill_files()
        if p:
            top = p.top_module()
            if top:
                self.open_file(top.file, top.line)

    def project_updated(self):
        self.fill_files()

    def fill_files(self, *_):
        self.fstore.clear()
        self.hstore.clear()
        p = self.app.project
        if not p:
            return
        expanded = None

        def add(parent, folder):
            for item in sorted(folder.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
                if item.name in ws.SKIP_DIRS or item.name.startswith(".git"):
                    continue
                it = self.fstore.append(parent, [item.name, str(item), item.is_dir()])
                if item.is_dir():
                    add(it, item)
        try:
            add(expanded, p.dir)
        except OSError:
            pass
        self.ftree.expand_all() if len(self.fstore) < 40 else None
        mods = p.modules()
        top = p.top()

        def add_node(parent, node, depth=0):
            mod = mods.get(node["module"])
            label = (f"<b>{GLib.markup_escape_text(node['instance'])}</b>" if depth == 0 else
                     GLib.markup_escape_text(node["instance"])) + markup(f"  {node['module']}", MUTED)
            it = self.hstore.append(parent, [label, str(mod.file) if mod else "", mod.line if mod else 0])
            if depth < 12:
                for child in node["children"]:
                    add_node(it, child, depth + 1)
        if top and top in mods:
            add_node(None, hdl.hierarchy(mods, top))
        for name in hdl.tops(mods):
            if name != top:
                add_node(None, hdl.hierarchy(mods, name))
        self.htree.expand_row(Gtk.TreePath.new_first(), False) if len(self.hstore) else None

    # ---------- documents ----------
    def current_path(self):
        page = self.nb.get_nth_page(self.nb.get_current_page())
        return next((p for p, d in self.docs.items() if d["view"] is page), None)

    def current_view(self):
        path = self.current_path()
        return self.docs[path]["view"] if path else None

    def is_dirty(self):
        return any(d["view"].buffer.get_modified() for d in self.docs.values())

    def open_file(self, path, line=None):
        """Open a text file in the editor (or switch to its tab) and go to a line; False if it can't be shown."""
        path = Path(path)
        if path in self.docs:
            view = self.docs[path]["view"]
        else:
            try:
                if path.stat().st_size > MAX_OPEN:
                    self.app.set_status(f"{path.name} is too large to edit here.")
                    return False
                text = path.read_text(errors="replace")
            except OSError as e:
                self.app.set_status(f"Could not open {path}: {e}")
                return False
            view = CodeView(language_of(path))
            view.set_text(text)
            label = Gtk.Label(label=path.name)
            close = Gtk.Button(label="×", relief=Gtk.ReliefStyle.NONE, tooltip_text="Close")
            close.connect("clicked", lambda *_: self.close(path))
            tab = Gtk.Box(spacing=4)
            tab.pack_start(label, True, True, 0)
            tab.pack_start(close, False, False, 0)
            tab.show_all()
            tab.set_tooltip_text(str(path))
            self.docs[path] = {"view": view, "label": label, "mtime": path.stat().st_mtime}
            view.buffer.connect("modified-changed", lambda buf: self.update_tab(path))
            view.buffer.connect("notify::cursor-position", lambda *_: self.update_position())
            view.view.connect("key-press-event", self.on_editor_key)
            view.show_all()
            self.nb.append_page(view, tab)
            self.mark_problems(path)
        self.editor_stack.set_visible_child_name("editor")
        self.nb.set_current_page(self.nb.page_num(view))
        if line:
            view.goto(line)
        self.update_position()
        return True

    def doc_of(self, path):
        path = Path(path).resolve()
        return next((d for p, d in self.docs.items() if Path(p).resolve() == path), None)

    def reload_file(self, path):
        """Show a file changed outside the editor (by the assistant); a tab with unsaved edits is left alone."""
        doc = self.doc_of(path)
        if doc and not doc["view"].buffer.get_modified():
            doc["view"].set_text(Path(path).read_text(errors="replace"))
            doc["view"].buffer.set_modified(False)
            doc["mtime"] = Path(path).stat().st_mtime
        self.fill_files()

    def on_editor_key(self, _view, event):
        from gi.repository import Gdk
        if event.state & Gdk.ModifierType.CONTROL_MASK and Gdk.keyval_name(event.keyval) in ("s", "S"):
            self.save_current()
            return True
        if Gdk.keyval_name(event.keyval) == "F12":
            self.go_to_definition()
            return True
        return False

    def update_tab(self, path):
        doc = self.docs.get(path)
        if doc:
            doc["label"].set_text(("● " if doc["view"].buffer.get_modified() else "") + path.name)

    def update_position(self):
        view, path = self.current_view(), self.current_path()
        if view and path:
            line, col = view.cursor()
            try:
                rel = path.relative_to(self.app.project.dir) if self.app.project else path
            except ValueError:
                rel = path
            self.position.set_text(f"{rel}   line {line}, column {col}")
        else:
            self.position.set_text("")
        return False

    def close(self, path, ask=True):
        doc = self.docs.get(path)
        if not doc:
            return True
        if ask and doc["view"].buffer.get_modified() and not self.app.confirm(
                f"Close {path.name} without saving?", "Its changes are lost."):
            return False
        self.nb.remove_page(self.nb.page_num(doc["view"]))
        del self.docs[path]
        if not self.docs:
            self.editor_stack.set_visible_child_name("empty")
        return True

    def save(self, path):
        doc = self.docs[path]
        try:
            if path.exists() and path.stat().st_mtime != doc["mtime"] and not self.app.confirm(
                    f"{path.name} changed on disk since it was opened", "Save anyway and replace those changes?"):
                return False
            text = doc["view"].text()
            path.write_text(text if text.endswith("\n") or not text else text + "\n")
            doc["mtime"] = path.stat().st_mtime
        except OSError as e:
            self.app.message(f"Could not save {path.name}: {e}")
            return False
        doc["view"].buffer.set_modified(False)
        return True

    def save_current(self):
        path = self.current_path()
        if path and self.save(path):
            self.after_save([path])

    def save_all(self):
        saved = [p for p, d in self.docs.items() if d["view"].buffer.get_modified() and self.save(p)]
        if saved:
            self.after_save(saved)
        return True

    def after_save(self, paths):
        self.app.set_status("Saved " + ", ".join(p.name for p in paths))
        if any(p.suffix.lower() in hdl.HDL_SUFFIXES for p in paths):
            self.fill_files()
            self.app.project_updated(self)
            if self.lint_on_save.get_active():
                self.run_lint()

    # ---------- files ----------
    def selected_path(self):
        model, it = self.ftree.get_selection().get_selected()
        return Path(model[it][1]) if it else None

    def target_dir(self):
        """Where a new file goes: the selected folder, the selected file's folder, or the project folder."""
        sel = self.selected_path()
        if sel is None:
            return self.app.project.dir
        return sel if sel.is_dir() else sel.parent

    def on_file_row(self, tv, tpath, _col):
        path, is_dir = Path(self.fstore[tpath][1]), self.fstore[tpath][2]
        if is_dir:
            tv.collapse_row(tpath) if tv.row_expanded(tpath) else tv.expand_row(tpath, False)
        elif path.suffix.lower() in TEXT_SUFFIXES or path.name in ("Makefile", "LICENSE", "README"):
            self.open_file(path)
        else:
            self.app.set_status(f"{path.name} is not a text file the editor opens.")

    def new_file(self, rel, text, open_it=True):
        """Create a file in the project (rel is relative to its folder); the path, or None if it exists."""
        path = self.app.project.dir / rel
        if path.exists():
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        self.fill_files()
        self.app.project_updated(self)
        if open_it:
            self.open_file(path)
        return path

    def on_new_file(self, *_):
        p = self.app.project
        if not p:
            return
        folder = self.target_dir()

        def check(v):
            if not v["name"] or "/" in v["name"] or v["name"].startswith("."):
                return "Give a plain name: letters, digits and underscores."
            fname, _text = hdl.template(v["kind"], v["name"])
            if "." in v["name"]:
                fname = v["name"]
            return f"{fname} already exists in that folder." if (folder / fname).exists() else None
        vals = dialogs.ask_fields(
            self.app, "New file", [("kind", "Start from", "Empty module", list(hdl.TEMPLATES)),
                                   ("name", "Module or file name", "", "text")],
            intro=f"Created in {folder.relative_to(p.dir.parent)}. A name without an extension makes "
                  "<name>.v with a module of that name; a name with an extension (top.sv, notes.md) is used as is.",
            ok="Create", check=check)
        if vals:
            fname, text = hdl.template(vals["kind"], Path(vals["name"]).stem)
            if "." in vals["name"]:
                fname = vals["name"]
                if Path(fname).suffix.lower() not in hdl.HDL_SUFFIXES:
                    text = ""
            self.new_file((folder / fname).relative_to(p.dir), text)

    def on_new_folder(self, *_):
        p = self.app.project
        if not p:
            return
        folder = self.target_dir()
        vals = dialogs.ask_fields(self.app, "New folder", [("name", "Name", "", "text")], ok="Create",
                                  check=lambda v: None if v["name"] and "/" not in v["name"]
                                  and not (folder / v["name"]).exists() else "Give a new, plain folder name.")
        if vals:
            (folder / vals["name"]).mkdir()
            self.fill_files()

    def on_rename(self, *_):
        path = self.selected_path()
        if not path:
            return
        vals = dialogs.ask_fields(self.app, f"Rename {path.name}", [("name", "New name", path.name, "text")],
                                  ok="Rename", check=lambda v: None if v["name"] and "/" not in v["name"]
                                  and not (path.parent / v["name"]).exists() else "Give a new, plain name.")
        if vals and self.close(path):
            path.rename(path.parent / vals["name"])
            self.fill_files()
            self.app.project_updated(self)

    def on_delete(self, *_):
        path = self.selected_path()
        if path and self.app.confirm(f"Delete {path.name}?", f"{path} is deleted from disk."
                                     + (" Everything in the folder goes with it." if path.is_dir() else "")):
            for open_path in [p for p in self.docs if p == path or path in p.parents]:
                self.close(open_path, ask=False)
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            self.fill_files()
            self.app.project_updated(self)

    # ---------- generated files ----------
    def pick_module(self, title, intro, ok="Create"):
        """Ask which design module to use; the Module, or None."""
        p = self.app.project
        mods = hdl.parse_files(p.paths(p.sources())) if p else {}
        if not mods:
            self.app.message("The project has no design modules yet.")
            return None
        names = [p.top()] + sorted(n for n in mods if n != p.top()) if p.top() in mods else sorted(mods)
        vals = dialogs.ask_fields(self.app, title, [("module", "Module", names[0], names)], intro=intro, ok=ok)
        return mods[vals["module"]] if vals else None

    def on_testbench(self, *_):
        p = self.app.project
        mods = hdl.parse_files(p.paths(p.sources())) if p else {}
        if not mods:
            self.app.message("The project has no design modules yet.")
            return
        names = [p.top()] + sorted(n for n in mods if n != p.top()) if p.top() in mods else sorted(mods)
        kinds = ["Verilog testbench", "cocotb test (Python)"]
        vals = dialogs.ask_fields(
            self.app, "New testbench", [("module", "Module", names[0], names), ("kind", "Kind", kinds[0], kinds)],
            intro="Verilog: writes tb/<module>_tb.v with a clock, a reset, a waveform dump, a seed, a place for your "
                  "stimulus and a PASS / FAIL line. cocotb: writes tests/test_<module>.py, where Python coroutines "
                  "drive the module (run with Icarus Verilog). Either is added to the tests in the Verify section.",
            ok="Create")
        if vals:
            (self.make_cocotb if vals["kind"] == kinds[1] else self.make_testbench)(mods[vals["module"]])

    def make_cocotb(self, mod):
        """Write a cocotb test module for a design module and register it as a test; its path, or None."""
        p = self.app.project
        rel = f"tests/test_{mod.name}.py"
        path = self.new_file(rel, hdl.cocotb_test(mod))
        if path is None:
            self.app.message(f"{rel} already exists; open it from the file list.")
            return None
        name = f"{mod.name}_cocotb"
        p["tests"] = [t for t in p["tests"] if t.get("name") != name] + [
            {"name": name, "kind": "cocotb", "top": mod.name, "module": rel, "simulator": "iverilog"}]
        p.save()
        self.app.project_updated(self)
        return path

    def make_testbench(self, mod):
        """Write a testbench for a module and register it as a test; returns its path (None if it exists)."""
        p = self.app.project
        path = self.new_file(f"tb/{mod.name}_tb.v", hdl.testbench(mod))
        if path is None:
            self.app.message(f"tb/{mod.name}_tb.v already exists; open it from the file list.")
            return None
        tests = [t for t in p["tests"] if t.get("name") != f"{mod.name}_tb"]
        tests.append({"name": f"{mod.name}_tb", "top": f"{mod.name}_tb", "files": [f"tb/{mod.name}_tb.v"],
                      "simulator": "iverilog", "args": ""})
        p["tests"] = tests
        if p["testbenches"] is not None:
            p["testbenches"] = sorted(set(p["testbenches"]) | {f"tb/{mod.name}_tb.v"})
        p.save()
        self.app.project_updated(self)
        return path

    def on_formal(self, *_):
        mod = self.pick_module("New formal properties", "Writes formal/<module>_formal.sv, bound to the module, "
                                                        "with a place for assertions and covers, and adds a "
                                                        "formal task in the Verify section.")
        if mod:
            self.make_formal(mod)

    def make_formal(self, mod):
        p = self.app.project
        path = self.new_file(f"formal/{mod.name}_formal.sv", hdl.formal_wrapper(mod))
        if path is None:
            self.app.message(f"formal/{mod.name}_formal.sv already exists; open it from the file list.")
            return None
        tasks = [t for t in p["formal"] if t.get("name") != f"{mod.name}_bmc"]
        tasks.append({"name": f"{mod.name}_bmc", "top": f"{mod.name}_formal", "mode": "bmc", "depth": 20,
                      "files": [f"formal/{mod.name}_formal.sv"]})
        p["formal"] = tasks
        p.save()
        self.app.project_updated(self)
        return path

    # ---------- lint ----------
    # ---------- checks: lint, design checks, CDC ----------
    def checks_file(self):
        p = self.app.project
        return p.build("checks") / "findings.json" if p else None

    def load_checks(self):
        path = self.checks_file()
        try:
            data = json.loads(path.read_text()) if path else {}
        except (OSError, ValueError):
            data = {}
        for key in self.sets:
            self.sets[key] = data.get("sets", {}).get(key, [])
            self.ran[key] = bool(data.get("ran", {}).get(key))
        self.cdc_result = data.get("cdc")

    def save_checks(self):
        path = self.checks_file()
        if path:
            path.write_text(json.dumps({"sets": self.sets, "ran": self.ran, "cdc": self.cdc_result,
                                        "when": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1))

    def finished(self, source, findings):
        """A check ended: keep its findings, show them, and tell the sign-off page."""
        self.sets[source], self.ran[source] = findings, True
        self.save_checks()
        self.show_problems()
        verify = self.app.section_pages.get("verify")
        if verify:
            verify.refresh_signoff()

    def run_lint(self, tool=None, then=None):
        """Lint the project's design sources; then(findings of lint) is called when it is done. tool "all" runs
        every installed linter and merges what they find."""
        p = self.app.project
        tool = tool or self.linter_cb.get_active_id()
        if not p or not tool:
            self.app.set_status("No linter found: install the RTL tools from Tools…." if p else "No project.")
            return False
        if not p.sources():
            self.app.set_status("The project has no design files to lint.")
            return False
        if self.console.busy():
            self.app.set_status("A check is still running.")
            return False
        self.save_all()
        tools = [t for t, _ in lint.available(self.app.rtl_tools())] if tool == "all" else [tool]
        found = []

        def step(task=None, used=None):
            if task is not None:
                for f in lint.parse(used, task.text, root=p.dir):
                    # Two linters often report the same thing: keep the first report of a line and code.
                    if not any(o["file"] == f["file"] and o["line"] == f["line"] and
                               (o["code"] == f["code"] or o["tool"] != f["tool"] and o["severity"] == f["severity"])
                               for o in found):
                        found.append(f)
            if tools:
                t = tools.pop(0)
                self.console.run(lint.command(t, p, chosen=self.app.rtl_tools(), strict=self.strict.get_active()),
                                 cwd=str(p.dir), env=eda_tools.env(self.app.rtl_tools()),
                                 on_done=lambda task, t=t: step(task, t), label=f"Lint ({dict(lint.LINTERS)[t]})")
                return
            self.finished("lint", found)
            mine = [f for f in self.findings if f["source"] == "lint"]
            label = "all linters" if tool == "all" else dict(lint.LINTERS)[tool]
            self.app.set_status(f"Lint with {label}: {self.count_text(mine)}")
            if then:
                then(mine)
        step()
        return True

    def run_design_checks(self, then=None):
        """Structural checks with Yosys; then(findings of the design checks). Returns False if it can't start."""
        p = self.app.project
        if not p or not p.sources() or self.console.busy():
            self.app.set_status("A check is still running." if p and p.sources() else "No design files to check.")
            return False
        if not eda_tools.find("yosys", self.app.rtl_tools()):
            self.app.message("Yosys is not installed; install the RTL tools from Tools….")
            return False
        self.save_all()
        folder, cmd = dc.command(p, self.app.rtl_tools())

        def done(task):
            log = (folder / "checks.log").read_text(errors="replace") if (folder / "checks.log").exists() else ""
            self.finished("design", dc.parse(log + "\n" + task.text, root=p.dir))
            mine = [f for f in self.findings if f["source"] == "design"]
            self.app.set_status(f"Design checks: {self.count_text(mine)}")
            if then:
                then(mine)
        self.console.run(cmd, cwd=str(folder), env=eda_tools.env(self.app.rtl_tools()), on_done=done,
                         label="Design checks")
        return True

    def run_cdc(self, then=None):
        """Clock domain crossing analysis (after the design checks, whose netlist it reads); then(result)."""
        p = self.app.project

        def analyze(_findings):
            module = dc.load_netlist(p)
            if module is None:
                self.console.append("CDC needs the design to elaborate: fix the errors above first.\n", "err")
                self.finished("cdc", [{"source": "cdc", "severity": "error", "code": "ELAB", "file": "", "line": 0,
                                       "message": "The design did not elaborate, so crossings could not be "
                                                  "analyzed"}])
                result = None
            else:
                result = cdc.analyze(module, root=p.dir)
                self.cdc_result = {"domains": result["domains"], "crossings": result["crossings"][:500]}
                self.console.append("\nClock domain crossings\n", "cmd")
                self.console.append(cdc.summary(result) + "\n")
                for x in result["crossings"][:200]:
                    self.console.append(f"  {x['kind']:13} {x['from']} ({x['from_clock']}) -> {x['to']} "
                                        f"({x['to_clock']})  {x['file']}:{x['line']}\n",
                                        "ok" if x["kind"] == "synchronized" else "err")
                self.finished("cdc", result["findings"])
                self.app.set_status("CDC: " + cdc.summary(result))
            if then:
                then(result)
        return self.run_design_checks(then=analyze)

    def run_all_checks(self, then=None):
        """Lint, then the design checks and CDC; then() at the end."""
        def after_lint(_f=None):
            if not self.run_cdc(then=lambda _r: then and then()) and then:
                then()
        if not self.run_lint(None, then=after_lint):
            after_lint()

    @staticmethod
    def count_text(findings):
        errors, warnings, waived = dc.counts(findings)
        if not findings:
            return "no findings"
        return f"{errors} error{'s' if errors != 1 else ''}, {warnings} warning{'s' if warnings != 1 else ''}" + \
            (f", {waived} waived" if waived else "")

    def show_problems(self, focus=True):
        from input_page import ERROR, WARN
        self.pstore.clear()
        every = self.findings
        waived_too = self.show_waived.get_active()
        self.shown = sorted((f for f in every if waived_too or not f.get("waived")),
                            key=lambda x: (bool(x.get("waived")), x["severity"] != "error", x.get("source", ""),
                                           x["file"], x["line"]))
        for i, pr in enumerate(self.shown):
            code = f" [{pr['code']}]" if pr["code"] else ""
            if pr.get("waived"):
                label, color, text = "waived", MUTED, f"{pr['message']}{code}  (waived: {pr['waived']})"
            else:
                label = "error" if pr["severity"] == "error" else "warning"
                color, text = (ERROR if pr["severity"] == "error" else WARN), pr["message"] + code
            self.pstore.append([label, pr["file"], pr["line"], text, color, pr.get("source", "lint"), i])
        errors, warnings, _waived = dc.counts(every)
        live = errors + warnings
        self.problems_label.set_text(f"Problems ({live})" if live else "Problems")
        not_run = [name for key, name in (("lint", "lint"), ("design", "design checks"), ("cdc", "CDC"))
                   if not self.ran[key]]
        self.checks_label.set_text((self.count_text(every) if every else "No findings") +
                                   (f" · not run yet: {', '.join(not_run)}" if not_run else ""))
        if focus and live:
            self.bottom.set_current_page(0)
        for path in self.docs:
            self.mark_problems(path)

    def selected_finding(self):
        model, it = self.ptv.get_selection().get_selected()
        return self.shown[model[it][6]] if it else None

    def on_waive(self, *_):
        f, p = self.selected_finding(), self.app.project
        if not f or not p:
            self.app.set_status("Select a finding to waive.")
            return
        if f.get("waived"):
            self.app.set_status("That finding is already waived.")
            return
        vals = dialogs.ask_fields(
            self.app, "Waive finding", [("reason", "Reason", "", "text")], ok="Waive",
            intro=f"{f['message']}\n\nA waiver accepts this finding on purpose. It is kept in the project "
                  "(flowrunner.json), shown in the sign-off report with its reason, and matches by file, code and "
                  "message, so it survives edits elsewhere in the file.",
            check=lambda v: None if len(v["reason"]) >= 3 else "Say why it is acceptable.")
        if vals:
            p["waivers"] = p["waivers"] + [dc.waiver_for(f, vals["reason"])]
            p.save()
            self.after_waivers()

    def on_unwaive(self, *_):
        f, p = self.selected_finding(), self.app.project
        if f and p and f.get("waived"):
            p["waivers"] = [w for w in p["waivers"] if not dc.matches(w, f)]
            p.save()
            self.after_waivers()

    def after_waivers(self):
        self.show_problems()
        verify = self.app.section_pages.get("verify")
        if verify:
            verify.refresh_signoff()

    # ---------- coverage marks, go to definition, instances ----------
    def set_coverage(self, cov):
        """Tint the lines no test ran in every open design file (cov: the Verify page's coverage summary)."""
        self.cov = cov
        for path in self.docs:
            self.mark_problems(path)

    def go_to_definition(self):
        """F12: open the module named under the cursor, or go to where the signal is declared in this file."""
        view, p = self.current_view(), self.app.project
        word = view.word_at_cursor() if view else ""
        if not word or not p:
            return
        mod = p.modules().get(word)
        if mod:
            self.open_file(mod.file, mod.line)
            return
        decl = re.compile(r"^\s*(?:input|output|inout|wire|reg|logic|bit|integer|int|parameter|localparam|genvar|"
                          r"typedef|function|task)\b[^;]*?\b" + re.escape(word) + r"\b", re.M)
        m = decl.search(view.text())
        if m:
            view.goto(view.text().count("\n", 0, m.start()) + 1 + m.group(0).count("\n"))
        else:
            self.app.set_status(f"No declaration of {word} in this file or module of that name in the project.")

    def on_instance(self, *_):
        view = self.current_view()
        if not view:
            self.app.set_status("Open the file to insert the instance into first.")
            return
        mod = self.pick_module("Insert instance", "Inserts an instance of the module at the cursor, with every "
                                                  "port connected to a signal of the same name.", ok="Insert")
        if mod:
            view.insert_at_cursor(self.instance_text(mod))

    @staticmethod
    def instance_text(mod):
        params = ""
        if mod.params:
            params = " #(\n" + ",\n".join(f"    .{n}({n})" for n, _ in mod.params) + "\n  )"
        ports = ",\n".join(f"    .{name}({name})" for _d, name, _b in mod.ports)
        return f"  {mod.name}{params} u_{mod.name} (\n{ports}\n  );\n"

    def problem_path(self, file):
        p = self.app.project
        path = Path(file)
        return path if path.is_absolute() else (p.dir / file if p else path)

    def mark_problems(self, path):
        marks = {}
        for pr in self.findings:
            if pr["file"] and not pr.get("waived") and self.problem_path(pr["file"]).resolve() == path.resolve():
                marks[pr["line"]] = (marks.get(pr["line"], "") + "\n" + pr["message"]).strip()
        view, p = self.docs[path]["view"], self.app.project
        view.set_problems(marks)
        missed = []
        if self.cov and p:
            try:
                missed = coverage.missed_lines(self.cov, str(path.resolve().relative_to(p.dir.resolve())))
            except ValueError:
                pass
        view.set_uncovered(missed)

    def on_problem(self, _tv, tpath, _col):
        row = self.pstore[tpath]
        if row[1]:
            self.open_file(self.problem_path(row[1]), row[2] or None)
