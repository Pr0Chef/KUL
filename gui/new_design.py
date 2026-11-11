"""New design: start a design for the flow from RTL files, without writing any file by hand.

The dialog asks for a name, a platform, the RTL files, the top module, the clock and its period, and the core
utilization. It then creates, under the flow folder:
  designs/<platform>/<name>/config.mk       what the flow needs: top module, sources, SDC and floorplan size;
  designs/<platform>/<name>/constraint.sdc  the clock, plus input and output delays of 20% of the period;
  designs/src/<name>/                       a copy of the RTL files (unless "use the files where they are").
Everything else is set afterwards on the Inputs pages.
"""

import re
import shutil
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

import sdc_clocks as sc  # noqa: E402
from input_page import ERROR, MUTED, hint_label, markup  # noqa: E402

HDL_SUFFIXES = (".v", ".sv", ".vh", ".svh")
NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][\w$]*)", re.M)
KEYWORDS = {"module", "endmodule", "input", "output", "inout", "wire", "reg", "logic", "assign", "always",
            "always_ff", "always_comb", "always_latch", "if", "else", "case", "begin", "end", "for", "generate",
            "parameter", "localparam", "function", "task", "initial", "typedef", "struct", "enum", "import"}


def modules(files):
    """{module: file} for every module defined in the files."""
    out = {}
    for f in files:
        try:
            text = sc.strip_comments(Path(f).read_text(errors="replace"))
        except OSError:
            continue
        for m in MODULE_RE.finditer(text):
            out.setdefault(m.group(1), Path(f))
    return out


def top_candidates(files):
    """Modules no other module instantiates, most likely top first (the one with the most code)."""
    defined = modules(files)
    used, size = set(), {}
    for f in {str(p) for p in defined.values()}:
        text = sc.strip_comments(Path(f).read_text(errors="replace"))
        for name in defined:
            body = sc.module_text(text, name)
            if not body:
                continue
            size[name] = len(body)
            # An instance looks like "<module> [#(...)] <instance> (": the module's name followed by a name.
            for other in defined:
                inst = r"\b" + re.escape(other) + r"(\s*#\s*\(|\s+[A-Za-z_\\])"
                if other != name and re.search(inst, body):
                    used.add(other)
    tops = [m for m in defined if m not in used]
    return sorted(tops, key=lambda m: -size.get(m, 0)) or sorted(defined)


def config_text(name, top, platform, sources, utilization):
    lines = ["# Created in ORFS Flow Runner (New design).",
             f"export DESIGN_NAME = {top}",
             f"export DESIGN_NICKNAME = {name}",
             f"export PLATFORM = {platform}",
             "",
             "export VERILOG_FILES = " + " \\\n                      ".join(sources),
             f"export SDC_FILE = $(DESIGN_HOME)/$(PLATFORM)/{name}/constraint.sdc",
             "",
             f"export CORE_UTILIZATION = {utilization}",
             ""]
    return "\n".join(lines)


def sdc_text(top, clock, period):
    """A starting SDC: the clock on its port (or a virtual clock when there is none), and IO delays."""
    lines = [f"current_design {top}", ""]
    if clock:
        lines += [f"set clk_name {clock}", f"set clk_port_name {clock}", f"set clk_period {period}",
                  "set clk_io_pct 0.2", "",
                  "create_clock -name $clk_name -period $clk_period [get_ports $clk_port_name]", "",
                  "set non_clock_inputs [all_inputs -no_clocks]"]
    else:
        lines += ["# No clock port: a virtual clock times the paths from inputs to outputs.",
                  "set clk_name vclk", f"set clk_period {period}", "set clk_io_pct 0.2", "",
                  "create_clock -name $clk_name -period $clk_period", "",
                  "set non_clock_inputs [all_inputs]"]
    lines += ["set_input_delay [expr $clk_period * $clk_io_pct] -clock $clk_name $non_clock_inputs",
              "set_output_delay [expr $clk_period * $clk_io_pct] -clock $clk_name [all_outputs]", ""]
    return "\n".join(lines)


def create(flow, name, platform, files, top, clock, period, utilization, copy=True):
    """Write the design's files; returns the config path. Raises ValueError with a plain message when it can't."""
    flow = Path(flow)
    if not NAME_RE.fullmatch(name):
        raise ValueError("Use letters, digits and underscores for the name, starting with a letter.")
    ddir, sdir = flow / "designs" / platform / name, flow / "designs" / "src" / name
    if ddir.exists():
        raise ValueError(f"designs/{platform}/{name} already exists.")
    if copy and sdir.exists():
        raise ValueError(f"designs/src/{name} already exists; pick another name or use the files where they are.")
    files = [Path(f) for f in files]
    if not files:
        raise ValueError("Add the design's RTL files.")
    if len({f.name for f in files}) != len(files):
        raise ValueError("Two files have the same name; copying them would overwrite one.")
    try:
        float(period)
    except ValueError:
        raise ValueError(f"The clock period '{period}' is not a number.") from None
    if copy:
        sdir.mkdir(parents=True)
        for f in files:
            shutil.copy2(f, sdir / f.name)
        sources = [f"$(DESIGN_HOME)/src/{name}/{f.name}" for f in files]
    else:
        sources = [str(f.resolve()) for f in files]
    ddir.mkdir(parents=True)
    (ddir / "config.mk").write_text(config_text(name, top, platform, sources, utilization))
    (ddir / "constraint.sdc").write_text(sdc_text(top, clock, period))
    return ddir / "config.mk"


class NewDesignDialog(Gtk.Dialog):
    """Asks for everything create() needs; run() then create_design() returns (platform, name) or None."""

    def __init__(self, app):
        super().__init__(title="New design", transient_for=app, modal=True, default_width=640)
        self.app = app
        self.files = []
        self.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        self.name = Gtk.Entry(hexpand=True, placeholder_text="for example my_core")
        self.platform = Gtk.ComboBoxText()
        for pname, p in app.platforms.items():
            if p["available"]:
                self.platform.append(pname, pname)
        self.platform.set_active_id(app.platform_cb.get_active_id())
        self.platform.connect("changed", lambda *_: self.update_unit())
        self.file_label = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        add = Gtk.Button(label="Add files…")
        add.connect("clicked", self.on_add_files)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", lambda *_: self.set_files([]))
        self.copy = Gtk.CheckButton(label="Copy the files into the flow (designs/src/<name>)", active=True)
        self.top = Gtk.ComboBoxText()
        self.top.connect("changed", lambda *_: self.fill_clocks())
        self.clock = Gtk.ComboBoxText()
        self.clock_note = hint_label("")
        self.period = Gtk.Entry(text="10", width_chars=10)
        self.unit = Gtk.Label(xalign=0)
        self.util = Gtk.SpinButton.new_with_range(5, 90, 5)
        self.util.set_value(50)
        self.error = Gtk.Label(xalign=0, wrap=True)

        rows = [("Name", self.name), ("Platform", self.platform)]
        r = 0
        for label, widget in rows:
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(widget, 1, r, 2, 1)
            r += 1
        grid.attach(Gtk.Label(label="RTL files", xalign=0, yalign=0), 0, r, 1, 1)
        grid.attach(self.file_label, 1, r, 2, 1)
        r += 1
        btns = Gtk.Box(spacing=6)
        btns.pack_start(add, False, False, 0)
        btns.pack_start(clear, False, False, 0)
        grid.attach(btns, 1, r, 2, 1)
        r += 1
        grid.attach(self.copy, 1, r, 2, 1)
        r += 1
        for label, widget in (("Top module", self.top), ("Clock port", self.clock)):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(widget, 1, r, 2, 1)
            r += 1
        grid.attach(self.clock_note, 1, r, 2, 1)
        r += 1
        grid.attach(Gtk.Label(label="Clock period", xalign=0), 0, r, 1, 1)
        grid.attach(self.period, 1, r, 1, 1)
        grid.attach(self.unit, 2, r, 1, 1)
        r += 1
        grid.attach(Gtk.Label(label="Core utilization (%)", xalign=0), 0, r, 1, 1)
        grid.attach(self.util, 1, r, 1, 1)
        r += 1
        grid.attach(hint_label("The flow sizes the core so the cells fill this share of it. Clocks, IO pins, "
                               "floorplan and every other input can be changed on the Inputs pages afterwards."),
                    0, r, 3, 1)
        r += 1
        grid.attach(self.error, 0, r, 3, 1)
        self.get_content_area().add(grid)
        self.set_default_response(Gtk.ResponseType.OK)
        self.set_files([])
        self.update_unit()
        self.show_all()

    def update_unit(self):
        ps = self.platform.get_active_id() == "asap7"
        self.unit.set_text("ps" if ps else "ns")
        if self.period.get_text() in ("10", "1000"):
            self.period.set_text("1000" if ps else "10")

    def on_add_files(self, *_):
        dlg = Gtk.FileChooserDialog(title="RTL files", transient_for=self, action=Gtk.FileChooserAction.OPEN,
                                    select_multiple=True)
        dlg.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        hdl = Gtk.FileFilter()
        hdl.set_name("Verilog and SystemVerilog")
        for suffix in HDL_SUFFIXES:
            hdl.add_pattern("*" + suffix)
        dlg.add_filter(hdl)
        if dlg.run() == Gtk.ResponseType.OK:
            self.set_files(self.files + [Path(f) for f in dlg.get_filenames() if Path(f) not in self.files])
        dlg.destroy()

    def set_files(self, files):
        self.files = files
        self.file_label.set_markup(markup("\n".join(str(f) for f in files) if files else "No files yet.", MUTED))
        self.top.remove_all()
        for m in top_candidates(files) if files else []:
            self.top.append(m, m)
        self.top.set_active(0)
        if files and not self.name.get_text().strip() and self.top.get_active_id():
            self.name.set_text(self.top.get_active_id())
        self.fill_clocks()

    def fill_clocks(self):
        self.clock.remove_all()
        top = self.top.get_active_id()
        if not top:
            self.clock_note.set_markup(markup("Add the RTL files to find the top module and its clock.", MUTED))
            return
        _path, ports, clocks, _bits = sc.read_rtl([str(f) for f in self.files], top)
        for name, why in clocks.items():
            self.clock.append(name, f"{name}  ({why})")
        for d, name in ports:
            if d == "input" and name not in clocks:
                self.clock.append(name, name)
        self.clock.append("", "no clock (combinational: a virtual clock times it)")
        self.clock.set_active(0)
        self.clock_note.set_markup(markup(
            f"{top} has {len(ports)} ports; {len(clocks)} look like clocks." if ports else
            f"Could not read the ports of {top}.", MUTED))

    def create_design(self):
        """Create the design from the dialog's answers; (platform, name), or None with the error shown."""
        name, platform = self.name.get_text().strip(), self.platform.get_active_id()
        try:
            if not platform:
                raise ValueError("Pick a platform.")
            if not self.top.get_active_id():
                raise ValueError("Add the RTL files and pick the top module.")
            create(self.app.flow, name, platform, self.files, self.top.get_active_id(),
                   self.clock.get_active_id() or "", self.period.get_text().strip(),
                   int(self.util.get_value()), copy=self.copy.get_active())
        except (ValueError, OSError) as e:
            self.error.set_markup(markup(str(e), ERROR))
            return None
        return platform, name
