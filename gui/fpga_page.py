"""FPGA section: pick a board (or a device), map the top module's ports to pins, build, read the results, program.

The pin table lists every port bit of the top module; Suggest pins fills it from the board (clock, LEDs, buttons).
Build runs Yosys, nextpnr and the bitstream packer (fpga.py) and shows utilization and the Fmax nextpnr reached
against the clock's frequency. Program sends the bitstream to the board with openFPGALoader.
"""

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

import eda_tools  # noqa: E402
import fpga  # noqa: E402
import theme  # noqa: E402
from input_page import ERROR, MUTED, card, markup, page_header  # noqa: E402
from runner import Console  # noqa: E402


class FpgaPage:
    name, title = "fpga", "FPGA"

    def __init__(self, app):
        self.app = app
        self._filling = False
        self.last = None   # result of the last build: {"state", "report"}
        self.widget = self.build()

    def build(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.pack_start(page_header("FPGA", "Build the project for an FPGA board with open source tools, read its "
                                             "size and speed, and program the board."), False, False, 0)
        paned = Gtk.Paned(position=560)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=20, margin_end=8,
                       margin_top=12, margin_bottom=16)
        target = card("Target")
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self.board_cb = Gtk.ComboBoxText()
        for b in fpga.BOARDS:
            self.board_cb.append(b, b)
        self.family_cb = Gtk.ComboBoxText()
        for f, v in fpga.FAMILIES.items():
            self.family_cb.append(f, v["label"])
        self.device_cb = Gtk.ComboBoxText(has_entry=True)
        self.package_cb = Gtk.ComboBoxText(has_entry=True)
        self.mhz = Gtk.Entry(width_chars=8)
        self.std_cb = Gtk.ComboBoxText()
        self.top_label = Gtk.Label(xalign=0)
        rows = [("Board", self.board_cb), ("Family", self.family_cb), ("Device", self.device_cb),
                ("Package", self.package_cb), ("Clock (MHz)", self.mhz), ("IO standard", self.std_cb),
                ("Top module", self.top_label)]
        for r, (label, w) in enumerate(rows):
            grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
            grid.attach(w, 1, r, 1, 1)
            w.set_hexpand(True)
        self.board_cb.connect("changed", self.on_board)
        self.family_cb.connect("changed", self.on_family)
        for w in (self.device_cb, self.package_cb):
            w.get_child().connect("changed", lambda *_: self.store_settings())
        self.mhz.connect("changed", lambda *_: self.store_settings())
        self.std_cb.connect("changed", lambda *_: self.store_settings())
        target.pack_start(grid, False, False, 0)
        left.pack_start(target, False, False, 0)

        pins = card("Pins", "Every bit of the top module's ports needs a pin of the package. Suggest pins puts the "
                            "clock on the board's clock and outputs on its LEDs.")
        self.pstore = Gtk.ListStore(str, str, str)   # port bit, direction, pin
        tv = Gtk.TreeView(model=self.pstore)
        tv.append_column(Gtk.TreeViewColumn("Port", Gtk.CellRendererText(), text=0))
        tv.append_column(Gtk.TreeViewColumn("Direction", Gtk.CellRendererText(foreground=MUTED), text=1))
        cell = Gtk.CellRendererText(editable=True)
        cell.connect("edited", self.on_pin)
        tv.append_column(Gtk.TreeViewColumn("Pin", cell, text=2))
        sw = Gtk.ScrolledWindow(vexpand=True, min_content_height=160)
        sw.add(tv)
        pins.pack_start(sw, True, True, 0)
        row = Gtk.Box(spacing=6)
        for label, handler in (("Suggest pins", self.on_suggest), ("Clear pins", self.on_clear)):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            row.pack_start(b, False, False, 0)
        pins.pack_start(row, False, False, 0)
        self.pin_problem = Gtk.Label(xalign=0, wrap=True)
        pins.pack_start(self.pin_problem, False, False, 0)
        left.pack_start(pins, True, True, 0)
        paned.pack1(left, False, False)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=8, margin_end=20,
                        margin_top=12, margin_bottom=16)
        actions = card("Build and program")
        row = Gtk.Box(spacing=8)
        self.build_btn = Gtk.Button(label="Build")
        self.build_btn.get_style_context().add_class("suggested-action")
        self.build_btn.connect("clicked", lambda *_: self.run_build())
        row.pack_start(self.build_btn, False, False, 0)
        for label, flash in (("Program (SRAM)", False), ("Program flash", True)):
            b = Gtk.Button(label=label, tooltip_text="Load the bitstream with openFPGALoader"
                                                     + (": it stays after power-off" if flash else ": it is lost at "
                                                                                                   "power-off"))
            b.connect("clicked", lambda _b, f=flash: self.on_program(f))
            row.pack_start(b, False, False, 0)
        actions.pack_start(row, False, False, 0)
        self.result_label = Gtk.Label(xalign=0, wrap=True)
        actions.pack_start(self.result_label, False, False, 0)
        right.pack_start(actions, False, False, 0)

        results = card("Results")
        self.rstore = Gtk.ListStore(str, str, str, str)   # what, used, available, share
        rtv = Gtk.TreeView(model=self.rstore)
        for i, title in enumerate(("Resource", "Used", "Available", "Share")):
            rtv.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(xalign=0 if i == 0 else 1), markup=i))
        results.pack_start(rtv, False, False, 0)
        right.pack_start(results, False, False, 0)
        self.console = Console("Yosys, nextpnr and openFPGALoader output")
        holder = card()
        holder.pack_start(self.console, True, True, 0)
        right.pack_start(holder, True, True, 0)
        paned.pack2(right, True, False)
        outer.pack_start(paned, True, True, 0)
        return outer

    # ---------- settings ----------
    def settings(self):
        p = self.app.project
        s = fpga.defaults()
        if p and p["fpga"]:
            s.update(p["fpga"])
        return s

    def store_settings(self):
        p = self.app.project
        if not p or self._filling:
            return
        s = self.settings()
        s.update(board=self.board_cb.get_active_id(), family=self.family_cb.get_active_id(),
                 device=self.device_cb.get_child().get_text().strip(),
                 package=self.package_cb.get_child().get_text().strip(),
                 io_standard=self.std_cb.get_active_id() or "",
                 pins={r[0]: r[2] for r in self.pstore if r[2]})
        try:
            s["mhz"] = float(self.mhz.get_text())
        except ValueError:
            pass
        p["fpga"] = s
        p.save()
        self.show_problem()

    def flow_changed(self):
        pass

    def project_changed(self):
        self.fill()

    def project_updated(self):
        self.fill()

    def fill(self):
        p = self.app.project
        self._filling = True
        s = self.settings()
        self.board_cb.set_active_id(s["board"])
        self.family_cb.set_active_id(s["family"])
        self.fill_family(s["family"])
        self.device_cb.get_child().set_text(s["device"])
        self.package_cb.get_child().set_text(s["package"])
        self.mhz.set_text(f"{s['mhz']:g}")
        self.std_cb.set_active_id(s.get("io_standard", ""))
        mod = p.top_module() if p else None
        self.top_label.set_markup(f"<b>{GLib.markup_escape_text(mod.name)}</b>" if mod else
                                  markup("none: set it in the Project section", MUTED))
        self.pstore.clear()
        if mod:
            for bit, direction in fpga.port_bits(mod):
                self.pstore.append([bit, direction, s["pins"].get(bit, "")])
        self._filling = False
        self.show_problem()
        self.show_report()

    def fill_family(self, family):
        fam = fpga.FAMILIES[family]
        for cb, items in ((self.device_cb, fam["devices"]), (self.package_cb, fam["packages"])):
            cb.remove_all()
            for item in items:
                cb.append_text(item)
        self.std_cb.remove_all()
        for std in fpga.IO_STANDARDS[family]:
            self.std_cb.append(std, std or "(the package's default)")
        self.std_cb.set_active(0)

    def on_board(self, cb):
        if self._filling or not self.app.project:
            return
        b = fpga.BOARDS[cb.get_active_id()]
        self._filling = True
        self.family_cb.set_active_id(b["family"])
        self.fill_family(b["family"])
        self.device_cb.get_child().set_text(b["device"])
        self.package_cb.get_child().set_text(b["package"])
        self.mhz.set_text(f"{b['mhz']:g}")
        self._filling = False
        # Pin names belong to a board: drop the old board's and start from this board's suggestions.
        mod = self.app.project.top_module()
        pins = fpga.suggest_pins(mod, cb.get_active_id()) if mod else {}
        for row in self.pstore:
            row[2] = pins.get(row[0], "")
        self.store_settings()

    def on_family(self, cb):
        if self._filling:
            return
        self._filling = True
        self.fill_family(cb.get_active_id())
        fam = fpga.FAMILIES[cb.get_active_id()]
        self.device_cb.get_child().set_text(fam["devices"][0])
        self.package_cb.get_child().set_text(fam["packages"][0])
        self.board_cb.set_active_id("Custom")
        self._filling = False
        self.store_settings()

    def on_pin(self, _cell, path, text):
        self.pstore[path][2] = text.strip()
        self.store_settings()

    def on_suggest(self, *_):
        p = self.app.project
        mod = p.top_module() if p else None
        if not mod:
            return
        pins = fpga.suggest_pins(mod, self.board_cb.get_active_id())
        for row in self.pstore:
            if row[0] in pins and not row[2]:
                row[2] = pins[row[0]]
        self.store_settings()

    def on_clear(self, *_):
        for row in self.pstore:
            row[2] = ""
        self.store_settings()

    def show_problem(self):
        p = self.app.project
        why = fpga.check(self.settings(), p.top_module() if p else None)
        self.pin_problem.set_markup(markup(why, ERROR) if why else markup("Every port has a pin.", MUTED))
        return why

    # ---------- build ----------
    def run_build(self, then=None):
        p = self.app.project
        why = self.show_problem()
        if not p or why:
            self.app.set_status(why or "No project.")
            if then:
                then({"state": "error", "why": why})
            return False
        s = self.settings()
        fam = fpga.FAMILIES[s["family"]]
        missing = eda_tools.missing(["yosys", fam["pnr"], fam["pack"]], self.app.rtl_tools())
        if missing:
            self.app.message(f"Not installed: {', '.join(missing)}. Install the RTL and FPGA tools from Tools….")
            return False
        self.app.section_pages["rtl"].save_all()
        folder, steps = fpga.commands(p, s, self.app.rtl_tools())
        for old in ("report.json", "design.bin", "design.bit", "design.fs"):
            (folder / old).unlink(missing_ok=True)
        self.build_btn.set_sensitive(False)

        def done(task, output):
            self.build_btn.set_sensitive(True)
            report = fpga.read_report(folder)
            bit = fpga.bitstream(folder, s["family"])
            state = "pass" if task.rc == 0 and bit else "fail"
            met = all(a >= w for _, a, w in report["clocks"]) if report["clocks"] else None
            self.last = {"state": state, "report": report, "bitstream": str(bit) if bit else "", "timing_met": met}
            self.show_report()
            if then:
                then(self.last)
        self.console.run_all(steps, cwd=str(folder), env=eda_tools.env(self.app.rtl_tools()), on_done=done,
                             label="FPGA build")
        return True

    def show_report(self):
        p = self.app.project
        self.rstore.clear()
        if not p:
            return
        folder = p.build("fpga")
        report = fpga.read_report(folder)
        s = self.settings()
        bit = fpga.bitstream(folder, s["family"])
        for name, achieved, wanted in report["clocks"]:
            ok = achieved >= wanted
            color = theme.color("success" if ok else "danger")
            self.rstore.append([f"<b>Fmax of {GLib.markup_escape_text(name)}</b>",
                                f'<span foreground="{color}">{achieved:g} MHz</span>', f"{wanted:g} MHz wanted",
                                "met" if ok else "<b>not met</b>"])
        for cell, used, avail in report["cells"]:
            share = f"{100 * used / avail:.1f}%" if avail else ""
            self.rstore.append([GLib.markup_escape_text(cell), str(used), str(avail), share])
        if bit:
            self.result_label.set_markup(markup(f"Bitstream: {bit}", MUTED))
        elif self.last and self.last["state"] == "fail":
            self.result_label.set_markup(markup("The build failed: see the output below.", ERROR))
        else:
            self.result_label.set_markup(markup("Not built yet.", MUTED))

    def on_program(self, flash):
        p = self.app.project
        s = self.settings()
        folder = p.build("fpga") if p else None
        if not folder or not fpga.bitstream(folder, s["family"]):
            self.app.message("Build the design first.")
            return
        if not eda_tools.find("openFPGALoader", self.app.rtl_tools()):
            self.app.message("openFPGALoader is not installed; install the RTL and FPGA tools from Tools….")
            return
        if flash and not self.app.confirm("Write the bitstream to the board's flash?",
                                          "It replaces what the board loads at power-on."):
            return
        self.console.run(fpga.program_command(s, folder, self.app.rtl_tools(), flash),
                         env=eda_tools.env(self.app.rtl_tools()), label="Program")
