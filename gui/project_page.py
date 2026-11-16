"""Project section: work areas, projects, cloning repositories, and what each file of a project is for.

A work area is where the flow runs and where projects live (see workspace.py). This page creates and opens work
areas, makes new projects, clones git repositories into the work area, and sets each project's top module and
which files are design sources or testbenches. Git output and errors show in the console at the bottom.
"""

import shutil
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

import dialogs  # noqa: E402
import hdl  # noqa: E402
import workspace as ws  # noqa: E402
from input_page import MUTED, card, hint_label, markup, page_header  # noqa: E402
from runner import Console, run_sync  # noqa: E402

ROLES = ["Design", "Testbench", "Not used"]


class ProjectPage:
    name, title = "project", "Project"

    def __init__(self, app):
        self.app = app
        self._filling = False
        self.widget = self.build()

    # ---------- layout ----------
    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(page_header("Project", "The work area the flow runs in, and the RTL projects in it: "
                                                "create them, clone them from git, and say what each file is for."),
                         False, False, 0)
        paned = Gtk.Paned(position=420)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=20, margin_end=8, margin_top=12,
                       margin_bottom=16)
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=8, margin_end=20,
                        margin_top=12, margin_bottom=16)

        # Work area.
        area = card("Work area")
        self.area_path = Gtk.Label(xalign=0, selectable=True, ellipsize=Pango.EllipsizeMode.START)
        self.area_note = hint_label("")
        row = Gtk.Box(spacing=8)
        for label, handler, tip in (
                ("Open…", lambda *_: self.app.choose_flow(), "Open another work area or ORFS flow folder"),
                ("New work area…", self.on_new_area, "Make a new, empty work area that shares this ORFS flow")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        for w in (self.area_path, self.area_note, row):
            area.pack_start(w, False, False, 0)
        left.pack_start(area, False, False, 0)

        # Projects: name, summary.
        projects = card("Projects")
        self.store = Gtk.ListStore(str, str)
        self.tv = Gtk.TreeView(model=self.store, headers_visible=False)
        self.tv.append_column(Gtk.TreeViewColumn("Project", Gtk.CellRendererText(weight=600), text=0))
        self.tv.append_column(Gtk.TreeViewColumn("About", Gtk.CellRendererText(foreground=MUTED), text=1))
        self.tv.get_selection().connect("changed", self.on_pick)
        sw = Gtk.ScrolledWindow(vexpand=True, min_content_height=160)
        sw.add(self.tv)
        projects.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=8)
        for label, handler, tip in (
                ("New project…", self.on_new_project, "An empty project with a starter module"),
                ("Clone repository…", self.on_clone, "Clone a git repository into this work area as a project"),
                ("Import folder…", self.on_import, "Copy a folder of RTL into this work area as a project"),
                ("Delete…", self.on_delete, "Delete the selected project's folder")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        projects.pack_start(row, False, False, 0)
        left.pack_start(projects, True, True, 0)

        # The selected project.
        self.detail = card("No project selected")
        self.detail_title = self.detail.get_children()[0]
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.top_cb = Gtk.ComboBoxText(hexpand=True, has_entry=True)
        self.top_cb.connect("changed", self.on_top)
        self.include = Gtk.Entry(hexpand=True, placeholder_text="folders inside the project, separated by spaces")
        self.defines = Gtk.Entry(hexpand=True, placeholder_text="NAME or NAME=value, separated by spaces")
        for entry, key in ((self.include, "include_dirs"), (self.defines, "defines")):
            entry.connect("changed", self.on_list_entry, key)
        for r, (label, w) in enumerate((("Top module", self.top_cb), ("Include folders", self.include),
                                        ("Defines", self.defines))):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
        self.detail.pack_start(grid, False, False, 0)
        self.git_label = hint_label("")
        self.detail.pack_start(self.git_label, False, False, 0)
        row = Gtk.Box(spacing=8)
        self.project_buttons = []
        for label, handler, tip in (
                ("Edit RTL", lambda *_: self.app.show_section("rtl"), "Open the project in the RTL editor"),
                ("Create ASIC design…", self.on_asic, "Make an ORFS design (config and SDC) from this project"),
                ("Filelist…", self.on_filelist, "Import the design files from a .f filelist, or export one"),
                ("Pull", self.on_pull, "git pull in the project's folder"),
                ("Commit…", self.on_commit, "git add and commit the project's changes")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
            self.project_buttons.append(b)
        self.detail.pack_start(row, False, False, 0)
        right.pack_start(self.detail, False, False, 0)

        # Files and their roles: path, role, modules in the file.
        files = card("Files", "Design files are synthesized and are the sources of every flow; testbenches are "
                              "only used for simulation. The app guesses the role; change it where it is wrong.")
        self.fstore = Gtk.ListStore(str, str, str)
        ftv = Gtk.TreeView(model=self.fstore)
        ftv.append_column(Gtk.TreeViewColumn("File", Gtk.CellRendererText(), text=0))
        roles = Gtk.ListStore(str)
        for r in ROLES:
            roles.append([r])
        cell = Gtk.CellRendererCombo(model=roles, text_column=0, has_entry=False, editable=True)
        cell.connect("changed", lambda _c, path, it: self.on_role(path, roles[it][0]))
        ftv.append_column(Gtk.TreeViewColumn("Role", cell, text=1))
        ftv.append_column(Gtk.TreeViewColumn("Modules", Gtk.CellRendererText(
            foreground=MUTED, ellipsize=Pango.EllipsizeMode.END), text=2))
        sw = Gtk.ScrolledWindow(vexpand=True, min_content_height=140)
        sw.add(ftv)
        files.pack_start(sw, True, True, 0)
        right.pack_start(files, True, True, 0)

        self.console = Console("Git and file operations")
        self.console.set_size_request(-1, 150)
        holder = card()
        holder.pack_start(self.console, True, True, 0)
        right.pack_start(holder, False, False, 0)

        paned.pack1(left, False, False)
        paned.pack2(right, True, False)
        outer.pack_start(paned, True, True, 0)
        return outer

    # ---------- refresh ----------
    def flow_changed(self):
        flow = self.app.flow
        self.area_path.set_text(str(flow))
        source = ws.orfs_source(flow)
        self.area_note.set_markup(markup(
            (f"A work area that uses the ORFS flow in {source}." if source != Path(flow).resolve() else
             "This is an ORFS flow folder itself. Make a work area to keep your designs and results apart from it.")
            if ws.is_flow(flow) else "No flow here yet: open an ORFS flow folder or a work area.", MUTED))
        self.fill_projects()

    def fill_projects(self):
        self._filling = True
        self.store.clear()
        for name in ws.project_names(self.app.flow):
            n = len(ws.hdl_files(ws.src_dir(self.app.flow) / name))
            self.store.append([name, f"{n} HDL file{'s' if n != 1 else ''}"])
        self._filling = False
        current = self.app.project.name if self.app.project else None
        for row in self.store:
            if row[0] == current:
                self.tv.get_selection().select_path(row.path)

    def project_changed(self):
        p = self.app.project
        for b in self.project_buttons:
            b.set_sensitive(p is not None)
        self._filling = True
        self.top_cb.remove_all()
        self.fstore.clear()
        if p is None:
            self.detail_title.set_text("No project selected")
            self.top_cb.get_child().set_text("")
            self.git_label.set_text("")
            self._filling = False
            return
        self.detail_title.set_text(f"Project {p.name}")
        mods = p.modules()
        design = hdl.parse_files(p.paths(p.sources()))
        for name in hdl.tops(design) + sorted(set(design) - set(hdl.tops(design))):
            self.top_cb.append_text(name)
        self.top_cb.get_child().set_text(p.top())
        self.include.set_text(" ".join(p["include_dirs"]))
        self.defines.set_text(" ".join(p["defines"]))
        sources, tbs = set(p.sources()), set(p.testbenches())
        by_file = {}
        for m in mods.values():
            by_file.setdefault(str(m.file.relative_to(p.dir)), []).append(m.name)
        for f in p.all_hdl():
            role = "Design" if f in sources else "Testbench" if f in tbs else "Not used"
            self.fstore.append([f, role, ", ".join(sorted(by_file.get(f, [])))])
        self._filling = False
        self.refresh_git()
        for row in self.store:
            if row[0] == p.name and not self.tv.get_selection().iter_is_selected(row.iter):
                self.tv.get_selection().select_path(row.path)

    def refresh_git(self):
        p = self.app.project
        if p:
            self.git_label.set_markup(markup(ws.git_summary(lambda cmd, cwd: run_sync(cmd, cwd=cwd, timeout=20),
                                                            p.dir) or "Not a git repository.", MUTED))

    # ---------- project settings ----------
    def on_pick(self, sel):
        model, it = sel.get_selected()
        if it is not None and not self._filling:
            self.app.set_project(model[it][0])

    def on_top(self, combo):
        p = self.app.project
        if p is None or self._filling:
            return
        top = combo.get_child().get_text().strip()
        if top != p["top"]:
            p["top"] = top
            p.save()
            self.app.project_updated(self)

    def on_list_entry(self, entry, key):
        p = self.app.project
        if p is None or self._filling:
            return
        p[key] = entry.get_text().split()
        p.save()

    def on_role(self, path, role):
        p = self.app.project
        self.fstore[path][1] = role
        # Once a role is set by hand, both lists are written out, so the guess no longer applies to any file.
        p["sources"] = [r[0] for r in self.fstore if r[1] == "Design"]
        p["testbenches"] = [r[0] for r in self.fstore if r[1] == "Testbench"]
        p.save()
        self.app.project_updated(self)
        GLib.idle_add(self.project_changed)

    # ---------- work areas and projects ----------
    def on_new_area(self, *_):
        default = Path.home() / "orfs-work" / "area1"
        n = 1
        while default.exists():
            n += 1
            default = default.with_name(f"area{n}")

        def check(v):
            try:
                self._made = ws.create_work_area(v["path"], self.app.flow)
            except (ValueError, OSError) as e:
                return str(e)
            return None
        vals = dialogs.ask_fields(
            self.app, "New work area", [("path", "Folder", str(default), "text")], ok="Create", check=check,
            intro="A work area holds its own designs, results, logs and reports. The flow's Makefile, scripts and "
                  f"platforms are shared with {ws.orfs_source(self.app.flow)}, so it takes almost no disk space.")
        if vals:
            self.app.open_flow(self._made)
            self.console.append(f"Created work area {self._made}\n", "ok")

    def on_new_project(self, *_):
        def check(v):
            try:
                self._made = ws.new_project(self.app.flow, v["name"])
            except (ValueError, OSError) as e:
                return str(e)
            return None
        vals = dialogs.ask_fields(self.app, "New project", [("name", "Name", "", "text")], ok="Create", check=check,
                                  intro="Creates designs/src/<name>/ in the work area with a starter module. Write "
                                        "the RTL in the RTL section.")
        if vals:
            self.fill_projects()
            self.app.set_project(vals["name"])

    def on_clone(self, *_):
        def check(v):
            return ws.check_clone(self.app.flow, v["url"], v["name"] or ws.repo_name(v["url"]))
        dlg = dialogs.FieldsDialog(
            self.app, "Clone repository",
            [("url", "Repository URL", "", "text"), ("name", "Project name", "", "text"),
             ("branch", "Branch or tag", "", "text"), ("shallow", "Latest commit only (faster, smaller)", True,
                                                      "check")],
            intro="Runs git clone into designs/src/<project name>/ in this work area. Leave the name empty to use "
                  "the repository's name, and the branch empty for its default branch.", ok="Clone", check=check)
        vals = dlg.ask()
        if not vals:
            return
        name = vals["name"] or ws.repo_name(vals["url"])
        dest = ws.src_dir(self.app.flow) / name
        dest.parent.mkdir(parents=True, exist_ok=True)

        def done(task):
            if task.rc == 0:
                self.fill_projects()
                self.app.set_project(name)
                self.app.set_status(f"Cloned {vals['url']} into {dest}")
            else:
                # A failed clone can leave a partial folder; remove it so the name is free again.
                shutil.rmtree(dest, ignore_errors=True)
                self.app.set_status("Clone failed: see the console on the Project page.")
        self.console.run(ws.clone_command(vals["url"], dest, vals["branch"], vals["shallow"]),
                         on_done=done, label="Clone")

    def on_import(self, *_):
        folder = dialogs.choose_folder(self.app, "Folder of RTL to import")
        if not folder:
            return
        name = ws.repo_name(folder)
        dest = ws.src_dir(self.app.flow) / name
        if dest.exists():
            self.app.message(f"A project called {name} already exists.")
            return
        try:
            shutil.copytree(folder, dest, ignore=shutil.ignore_patterns(".git", "build", "obj_dir"))
        except OSError as e:
            self.app.message(f"Could not copy the folder: {e}")
            return
        self.console.append(f"Copied {folder} to {dest}\n", "ok")
        self.fill_projects()
        self.app.set_project(name)

    def on_delete(self, *_):
        p = self.app.project
        if p and self.app.confirm(f"Delete project {p.name}?",
                                  f"This deletes {p.dir} and its simulation and FPGA build outputs. ASIC designs "
                                  "that use its files are not deleted, but will no longer find them."):
            ws.delete_project(self.app.flow, p.name)
            self.console.append(f"Deleted {p.dir}\n", "warn")
            self.app.set_project(None)
            self.fill_projects()

    def on_filelist(self, *_):
        """Import a .f filelist into the project, or export the project's compile order as one."""
        p = self.app.project
        if not p:
            return
        vals = dialogs.ask_fields(
            self.app, "Filelist", [("what", "Do", "Export", ["Export", "Import"]),
                                   ("path", "File", str(p.dir / f"{p.name}.f"), "text")],
            intro="A filelist (.f) names the design files in compile order with +incdir+ and +define+ lines; "
                  "simulators, linters and synthesis tools all read it. Export writes the project's files to it. "
                  "Import makes the files, include folders and defines it lists the project's own.", ok="Go")
        if not vals:
            return
        if vals["what"] == "Export":
            path = ws.write_filelist(p, vals["path"])
            self.console.append(f"Wrote {path}\n", "ok")
        else:
            self.import_filelist(vals["path"])

    def import_filelist(self, path):
        p = self.app.project
        try:
            outside = ws.apply_filelist(p, path)
        except OSError as e:
            self.app.message(f"Could not read {path}: {e}")
            return
        self.console.append(f"Read {path}: {len(p['sources'])} design files, {len(p['include_dirs'])} include "
                            f"folders, {len(p['defines'])} defines\n", "ok")
        if outside:
            self.console.append("Not in the project folder, so not used (copy or import them first):\n  "
                                + "\n  ".join(outside[:20]) + "\n", "warn")
        self.app.project_updated(None)
        self.project_changed()

    def on_asic(self, *_):
        p = self.app.project
        if p:
            self.app.on_new_design(files=[Path(f) for f in p.paths(p.sources())], name=p.name.replace("-", "_"),
                                   top=p.top(), copy=False)

    # ---------- git ----------
    def on_pull(self, *_):
        p = self.app.project
        if p:
            self.console.run(["git", "pull", "--ff-only"], cwd=str(p.dir), label="Pull",
                             on_done=lambda t: (self.app.project_updated(None), self.project_changed()))

    def on_commit(self, *_):
        p = self.app.project
        if not p:
            return
        rc, _ = run_sync(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(p.dir), timeout=20)
        intro = "Stages every change in the project's folder and commits it." if rc == 0 else \
            "This project is not a git repository yet: this makes one (git init), then commits every file."
        vals = dialogs.ask_fields(self.app, "Commit", [("message", "Message", "", "text")], intro=intro, ok="Commit",
                                  check=lambda v: None if v["message"] else "Write a commit message.")
        if vals:
            steps = ([] if rc == 0 else [["git", "init"]]) + [["git", "add", "-A"],
                                                               ["git", "commit", "-m", vals["message"]]]
            self.console.run_all(steps, cwd=str(p.dir), label="Commit", on_done=lambda *_: self.refresh_git())
