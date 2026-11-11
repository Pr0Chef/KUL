"""Drive ORFS Flow Runner in tests, off screen: the app runs on a Broadway display, never on the desktop.

Start a GUI test through run_gui_tests.sh, which starts broadwayd and sets GDK_BACKEND=broadway. A test imports
this module, builds the app with app_for(), and uses the helpers to click, wait and check:

    h = Harness(flow)                 # builds the app on a private settings folder
    h.select("nangate45", "gcd", "config.mk")
    h.check("clock found", "core_clock" in h.inp.clock_names())
    h.done()                          # prints the summary and exits non-zero on any failure

Message boxes the app shows are answered with OK and kept in h.messages; any Python exception raised inside a
GTK callback fails the test.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

GUI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(GUI))

# A private settings, data and cache folder, so tests never touch the user's own.
_HOME = Path(tempfile.mkdtemp(prefix="orfs-runner-test-"))
for _k in ("CONFIG", "DATA", "CACHE"):
    os.environ[f"XDG_{_k}_HOME"] = str(_HOME / _k.lower())
sys.argv = sys.argv[:1]

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import orfs_app  # noqa: E402

KUL_FLOW = GUI.parent / "orfs" / "flow"


def temp_design(flow, platform, design, name):
    """Copy designs/<platform>/<design> to designs/<platform>/<name> for a test to change freely.

    The copy's config points its own files (SDC, PDN and so on) at the copy, keeps the original RTL, and sets
    DESIGN_NICKNAME so its results go to their own folder. Remove it with remove_design().
    """
    src, dst = Path(flow) / "designs" / platform / design, Path(flow) / "designs" / platform / name
    remove_design(flow, platform, name)
    shutil.copytree(src, dst)
    cfg = dst / "config.mk"
    text = cfg.read_text().replace("$(PLATFORM)/$(DESIGN_NAME)/", f"$(PLATFORM)/{name}/")
    cfg.write_text(text + f"\nexport DESIGN_NICKNAME = {name}\n")
    return dst


RAM2_V = """// Two 64x32 RAM macros behind a register: small enough to synthesize in seconds, with macros to place.
module ram2 (input clk, input sel, input we, input [5:0] addr, input [31:0] wd, output reg [31:0] rd);
  wire [31:0] rd0, rd1;
  fakeram45_64x32 ram0 (.clk(clk), .ce_in(1'b1), .we_in(we & ~sel), .addr_in(addr), .wd_in(wd),
                        .w_mask_in(32'hffffffff), .rd_out(rd0));
  fakeram45_64x32 ram1 (.clk(clk), .ce_in(1'b1), .we_in(we & sel), .addr_in(addr), .wd_in(wd),
                        .w_mask_in(32'hffffffff), .rd_out(rd1));
  always @(posedge clk) rd <= sel ? rd1 : rd0;
endmodule
"""


def macro_design(flow, name):
    """A nangate45 design with two RAM macros and a fixed 200 x 200 micron die, for macro placement tests."""
    remove_design(flow, "nangate45", name)
    src, ddir = Path(flow) / "designs" / "src" / name, Path(flow) / "designs" / "nangate45" / name
    src.mkdir(parents=True)
    ddir.mkdir(parents=True)
    (src / "ram2.v").write_text(RAM2_V)
    (ddir / "config.mk").write_text(
        "export DESIGN_NAME = ram2\n"
        f"export DESIGN_NICKNAME = {name}\n"
        "export PLATFORM = nangate45\n"
        f"export VERILOG_FILES = $(DESIGN_HOME)/src/{name}/ram2.v\n"
        f"export SDC_FILE = $(DESIGN_HOME)/$(PLATFORM)/{name}/constraint.sdc\n"
        "export ADDITIONAL_LEFS = $(PLATFORM_DIR)/lef/fakeram45_64x32.lef\n"
        "export ADDITIONAL_LIBS = $(PLATFORM_DIR)/lib/fakeram45_64x32.lib\n"
        "export DIE_AREA = 0 0 200 200\n"
        "export CORE_AREA = 10 10 190 190\n"
        "export PLACE_DENSITY = 0.4\n")
    (ddir / "constraint.sdc").write_text(
        "set clk_period 4\ncreate_clock -name clk -period $clk_period [get_ports clk]\n"
        "set_input_delay 0.8 -clock clk [all_inputs -no_clocks]\nset_output_delay 0.8 -clock clk [all_outputs]\n")
    return ddir


def run_make(flow, config, target, env, timeout=1800):
    """Run one flow target the way the app does; (exit code, last lines of output)."""
    import subprocess
    out = subprocess.run(["make", f"DESIGN_CONFIG={config}", target], cwd=flow, env=env, capture_output=True,
                         text=True, timeout=timeout)
    return out.returncode, (out.stdout + out.stderr)[-3000:]


def odb_query(env, odb, tcl):
    """Run Tcl in OpenROAD on a result file and return what it prints."""
    import subprocess
    script = f"read_db {{{odb}}}\nset block [ord::get_db_block]\n{tcl}\n"
    out = subprocess.run([env.get("OPENROAD_EXE", "openroad"), "-no_init", "-no_splash", "-exit"], input=script,
                         capture_output=True, text=True, timeout=300, env=env)
    return out.stdout


def remove_design(flow, platform, name):
    """Delete a test design and everything the flow made for it. Only names starting with test_ are accepted,
    so a slip can never delete a real design."""
    if not name.startswith("test_"):
        raise ValueError(f"refusing to delete {name}: test design names start with test_")
    for sub in ("designs", "results", "logs", "objects", "reports"):
        path = Path(flow) / sub / platform / name
        if path.exists():
            shutil.rmtree(path)
    src = Path(flow) / "designs" / "src" / name
    if src.exists() and not (Path(flow) / "designs" / platform / name).exists():
        shutil.rmtree(src)


class Harness:
    def __init__(self, flow=KUL_FLOW, shots=None):
        self.results, self.messages, self.errors = [], [], []
        self.dialogs = []          # functions that fill the next non-message dialog, in order
        self.shots = Path(shots) if shots else None
        sys.excepthook = self._excepthook
        orfs_app.SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        tools = {}
        for name in ("openroad", "yosys"):
            path = orfs_app.tool_path(name, {})
            if path:
                tools[name] = path
        orfs_app.SETTINGS.write_text(json.dumps({"tools": tools}))
        self.app = orfs_app.App(Path(flow))
        self.app.show_all()
        self.inp = self.app.inputs
        GLib.timeout_add(200, self._tick)
        self.pump(1)

    # ---- event loop ----
    def _excepthook(self, typ, value, tb):
        self.errors.append("".join(traceback.format_exception(typ, value, tb)))
        sys.__excepthook__(typ, value, tb)

    def _tick(self):
        for w in Gtk.Window.list_toplevels():
            if isinstance(w, Gtk.MessageDialog) and w.get_visible():
                self.messages.append((w.get_property("text") or "") + " " + (w.get_property("secondary-text") or ""))
                w.response(Gtk.ResponseType.OK)
                return True
        if self.dialogs:
            for w in Gtk.Window.list_toplevels():
                if isinstance(w, Gtk.Dialog) and not isinstance(w, Gtk.MessageDialog) and w.get_visible():
                    fill = self.dialogs.pop(0)
                    response = fill(w)
                    w.response(Gtk.ResponseType.OK if response is None else response)
                    return True
        return True

    def pump(self, seconds=0.2):
        end = time.time() + seconds
        while time.time() < end:
            while Gtk.events_pending():
                Gtk.main_iteration_do(False)
            time.sleep(0.01)

    def wait(self, cond, seconds=60):
        end = time.time() + seconds
        while time.time() < end:
            try:
                if cond():
                    return True
            except Exception:  # noqa: BLE001 - a condition may look at state that is not there yet
                pass
            self.pump(0.1)
        try:
            return bool(cond())
        except Exception:  # noqa: BLE001
            return False

    # ---- actions ----
    def select(self, platform, design, config, seconds=90):
        """Pick a config in the side panel and wait until the Inputs tab has read it."""
        app = self.app
        app.platform_cb.set_active_id(platform)
        self.pump(0.3)
        app.design_cb.set_active_id(design)
        self.pump(0.3)
        app.config_cb.set_active_id(config)
        want = f"designs/{platform}/{design}/{config}"
        ok = self.wait(lambda: self.inp.cfg == want and self.inp.effective.get("DESIGN_NAME"), seconds)
        self.pump(0.5)
        return ok

    def page(self, name):
        self.app.show_page("inputs")
        self.inp.stack.set_visible_child_name(name)
        self.pump(0.4)
        return next(p for p in self.inp.pages if p.name == name) if any(p.name == name for p in self.inp.pages) \
            else None

    def answer_next_dialog(self, fill):
        """fill(dialog) runs when the next dialog opens; return a response, or None for OK."""
        self.dialogs.append(fill)

    def saved(self, seconds=60):
        """Wait for a save to finish: nothing unsaved and the config read again."""
        return self.wait(lambda: not self.inp.is_dirty() and self.app.run_btn.get_sensitive(), seconds)

    def shot(self, name, widget=None):
        if not self.shots:
            return
        self.shots.mkdir(parents=True, exist_ok=True)
        self.pump(0.5)
        win = (widget or self.app).get_window()
        pb = Gdk.pixbuf_get_from_window(win, 0, 0, win.get_width(), win.get_height())
        pb.savev(str(self.shots / f"{name}.png"), "png", [], [])

    # ---- results ----
    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"\n      {str(detail)[:600]}"), flush=True)
        return ok

    def done(self):
        self.check("no Python errors in callbacks", not self.errors, "\n".join(self.errors)[-2000:])
        passed = sum(ok for _, ok in self.results)
        print(f"\n{passed}/{len(self.results)} passed", flush=True)
        self.app.destroy()
        sys.exit(0 if passed == len(self.results) else 1)
