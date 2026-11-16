"""Work areas and projects: where designs live on disk, and what the app remembers about each one.

A work area is a folder the flow runs in. It holds its own designs/ (with the RTL under designs/src/<project>/),
and its own results, logs and reports; the flow's Makefile, scripts, platforms and util are links to an
OpenROAD-flow-scripts flow folder, so a work area costs almost no disk and several can share one ORFS install.

A project is one folder of RTL under designs/src/. What the app knows about it (top module, which files are
design sources and which are testbenches, its tests, formal tasks, FPGA settings) is kept in flowrunner.json in
that folder, so it travels with the sources and can be committed with them.
"""

import json
import re
import shutil
import time
from pathlib import Path

import hdl

LINKS = ["Makefile", "scripts", "platforms", "util"]   # shared with the ORFS flow folder
META = "flowrunner.json"
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
SKIP_DIRS = {".git", "build", "obj_dir", "__pycache__", "node_modules", ".flowrunner"}


def is_flow(path):
    """True for an ORFS flow folder or a work area: it has the flow's Makefile."""
    return (Path(path) / "Makefile").exists() and (Path(path) / "scripts").exists()


def orfs_source(flow):
    """The real ORFS flow folder behind a work area (or the folder itself when it is not a work area)."""
    return (Path(flow) / "Makefile").resolve().parent


def create_work_area(path, orfs_flow):
    """Make a work area at path that uses the flow in orfs_flow; returns its path.

    Raises ValueError with a plain message when the folder can't be used.
    """
    path, source = Path(path).expanduser(), orfs_source(orfs_flow)
    if not is_flow(source):
        raise ValueError(f"{source} is not an OpenROAD-flow-scripts flow folder (no Makefile and scripts).")
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"{path} already exists and is not empty.")
    if source == path.resolve() or source in path.resolve().parents:
        raise ValueError("Make the work area outside the ORFS flow folder.")
    path.mkdir(parents=True, exist_ok=True)
    for name in LINKS:
        if (source / name).exists():
            (path / name).symlink_to(source / name)
    (path / "designs" / "src").mkdir(parents=True)
    # A platform is listed in the app once it has a designs/<platform> folder with a design in it.
    (path / "workarea.json").write_text(json.dumps(
        {"orfs_flow": str(source), "created": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1) + "\n")
    (path / ".gitignore").write_text("results/\nlogs/\nobjects/\nreports/\nbuild/\n")
    return path


def platforms(flow):
    """Platforms whose PDK is in the flow: names under platforms/ that have a config.mk."""
    pdir = Path(flow) / "platforms"
    return sorted(p.name for p in pdir.iterdir() if (p / "config.mk").exists()) if pdir.is_dir() else []


def src_dir(flow):
    return Path(flow) / "designs" / "src"


def project_names(flow):
    """Folders under designs/src, each one a project."""
    root = src_dir(flow)
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")) if root.is_dir() \
        else []


def build_dir(flow, project, kind):
    """Where a project's simulation, formal or FPGA outputs go: <work area>/build/<project>/<kind>."""
    return Path(flow) / "build" / project / kind


def hdl_files(root):
    """Every Verilog and SystemVerilog file under root, as paths relative to it, skipping build folders."""
    out = []
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in hdl.HDL_SUFFIXES and p.is_file() and not (set(p.relative_to(root).parts[:-1])
                                                                          & SKIP_DIRS):
            out.append(str(p.relative_to(root)))
    return out


class Project:
    """One RTL project: its folder and the settings in flowrunner.json."""

    DEFAULTS = {"top": "", "sources": None, "testbenches": None, "include_dirs": [], "defines": [],
                "tests": [], "formal": [], "fpga": {}, "notes": ""}

    def __init__(self, flow, name):
        self.flow, self.name = Path(flow), name
        self.dir = src_dir(flow) / name
        self.data = dict(self.DEFAULTS)
        self.load()

    # ---- settings ----
    def load(self):
        try:
            self.data.update(json.loads((self.dir / META).read_text()))
        except (OSError, ValueError):
            pass

    def save(self):
        (self.dir / META).write_text(json.dumps(self.data, indent=1) + "\n")

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key))

    def __setitem__(self, key, value):
        self.data[key] = value

    # ---- files ----
    def all_hdl(self):
        return hdl_files(self.dir)

    def modules(self):
        """{module: hdl.Module} for every HDL file of the project (cached until a file changes)."""
        files = [self.dir / f for f in self.all_hdl()]
        key = tuple((str(f), f.stat().st_mtime) for f in files)
        if getattr(self, "_mods_key", None) != key:
            self._mods_key, self._mods = key, hdl.parse_files(files)
        return self._mods

    def testbenches(self):
        """Files that hold testbenches: the ones listed in the settings, else files whose modules look like
        testbenches or that sit in a tb / test / sim / bench folder."""
        if self["testbenches"] is not None:
            return [f for f in self["testbenches"] if (self.dir / f).exists()]
        mods = self.modules()
        tb_files = {str(m.file.relative_to(self.dir)) for m in mods.values() if hdl.is_testbench(m)}
        for f in self.all_hdl():
            parts = [p.lower() for p in Path(f).parts[:-1]]
            if any(p in ("tb", "test", "tests", "sim", "bench", "testbench", "dv", "verif") for p in parts) \
                    or re.search(r"(^tb_|_tb\.|^test_|_test\.|testbench)", Path(f).name, re.I):
                tb_files.add(f)
        return sorted(tb_files)

    def sources(self):
        """Design sources (relative paths): the ones listed in the settings, else every HDL file that is not a
        testbench or a formal property file."""
        if self["sources"] is not None:
            return [f for f in self["sources"] if (self.dir / f).exists()]
        tbs = set(self.testbenches())
        return [f for f in self.all_hdl() if f not in tbs and not re.search(r"_formal\.s?v$", f)
                and Path(f).suffix.lower() in (".v", ".sv")]

    def paths(self, files):
        return [str(self.dir / f) for f in files]

    def top(self):
        """The top module: the one in the settings, else the first candidate among the design sources."""
        if self["top"]:
            return self["top"]
        mods = hdl.parse_files(self.paths(self.sources()))
        tops = hdl.tops(mods)
        return tops[0] if tops else ""

    def top_module(self):
        return self.modules().get(self.top())

    def include_flags(self, flag="-I"):
        dirs = [self.dir] + [self.dir / d for d in self["include_dirs"]]
        return [f"{flag}{d}" for d in dirs]

    def build(self, kind):
        d = build_dir(self.flow, self.name, kind)
        d.mkdir(parents=True, exist_ok=True)
        return d


def new_project(flow, name, top=None):
    """Create an empty project with one starter module; returns the Project. ValueError on a bad name."""
    if not NAME_RE.fullmatch(name):
        raise ValueError("Use letters, digits, dash and underscore for the project name, starting with a letter.")
    pdir = src_dir(flow) / name
    if pdir.exists():
        raise ValueError(f"A project called {name} already exists.")
    pdir.mkdir(parents=True)
    module = re.sub(r"\W", "_", top or name)
    fname, text = hdl.template("Empty module", module)
    (pdir / fname).write_text(text)
    project = Project(flow, name)
    project["top"] = module
    project.save()
    return project


def repo_name(url):
    """The folder name git would use for a repository URL: its last part without .git."""
    name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name) or "repo"


def clone_command(url, dest, branch="", shallow=True):
    """The git command that clones url into dest."""
    cmd = ["git", "clone", "--progress"]
    if shallow:
        cmd += ["--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    return cmd + [url, str(dest)]


def check_clone(flow, url, name):
    """Why a clone can't start, or None: a missing URL, a bad name, or a folder that is already there."""
    if not url.strip():
        return "Give the repository's URL."
    if re.match(r"^\s*-", url):
        return "The URL can't start with a dash."
    if not NAME_RE.fullmatch(name):
        return "Use letters, digits, dash and underscore for the project name, starting with a letter."
    if (src_dir(flow) / name).exists():
        return f"A project called {name} already exists."
    return None


def git_summary(run, path):
    """One line about a folder's git state: branch and changed files, '' when it is not a repository.

    run(cmd, cwd) -> (exit code, output), so the caller decides how the command runs.
    """
    rc, out = run(["git", "status", "--porcelain=v1", "--branch"], str(path))
    if rc != 0:
        return ""
    lines = out.splitlines()
    branch = lines[0][3:].split("...")[0] if lines and lines[0].startswith("##") else "?"
    changed = [ln for ln in lines[1:] if ln.strip()]
    return f"git: {branch}, " + (f"{len(changed)} changed file{'s' if len(changed) != 1 else ''}" if changed
                                 else "clean")


def delete_project(flow, name):
    """Remove a project's folder and its build outputs."""
    shutil.rmtree(src_dir(flow) / name, ignore_errors=True)
    shutil.rmtree(Path(flow) / "build" / name, ignore_errors=True)
