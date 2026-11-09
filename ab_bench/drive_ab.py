"""Drive ORFS Flow Runner through one run, with optional screenshots and CPU samples.

Usage: [APP_DIR=...] [NOSHOTS=1] GDK_BACKEND=x11 python3 drive_ab.py <name> <config> <yosys> <out dir> [to stage]
"""
import json
import os
import sys
import time

sys.path.insert(0, os.environ.get("APP_DIR", "/opt/orfs-flow-runner"))
NAME, CONFIG, YOSYS, OUT = sys.argv[1:5]
TO = sys.argv[5] if len(sys.argv) > 5 else "synth"
sys.argv = sys.argv[:1]
import orfs_app as a  # noqa: E402
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

ROOT = a.Path(__file__).resolve().parent.parent
FLOW = a.Path(os.environ.get("ORFS_FLOW") or next(
    (f for f in (ROOT / "OpenROAD-flow-scripts" / "flow", ROOT / "orfs" / "flow") if (f / "Makefile").exists())))
os.makedirs(OUT, exist_ok=True)
GLib.set_prgname(a.APP_ID)
win = a.App(FLOW)
win.show_all()
t0 = time.time()
events = []


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} +{time.time() - t0:7.0f}s {msg}"
    events.append(line)
    print(line, flush=True)


def snap(w, tag):
    if os.environ.get("NOSHOTS"):
        return
    while Gtk.events_pending():
        Gtk.main_iteration()
    gw = w.get_window()
    pb = Gdk.pixbuf_get_from_window(gw, 0, 0, gw.get_width(), gw.get_height())
    path = f"{OUT}/{NAME}_{len(os.listdir(OUT)):03d}_{tag}.png"
    pb.savev(path, "png", [], [])
    log(f"shot {path}")


def tab(n):
    win.nb.set_current_page(n)
    win.refresh_tab(n)


# ---- CPU / memory sampler: whole-machine busy cores from /proc/stat, plus the run's process group.
samples = []
last = None


def cpu_ticks():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + v[4]
    return sum(v) - idle, sum(v)


def sample():
    global last
    busy, total = cpu_ticks()
    if last and win.job and win.job.running():
        cores = (busy - last[0]) / max(1, total - last[1]) * (os.cpu_count() or 1)
        rss, names = a.group_rss(win.job.proc.pid)
        samples.append({"t": round(time.time() - win.job.started, 1), "cores": round(cores, 2),
                        "rss_mb": round(rss / 2**20), "abc": names.count("yosys-abc"),
                        "procs": sorted(set(names))})
    last = (busy, total)
    return True


# ---- steps
def select():
    win.platform_cb.set_active_id(os.environ.get("DRV_PLATFORM", "asap7"))
    win.design_cb.set_active_id(os.environ.get("DRV_DESIGN", "cva6"))
    win.config_cb.set_active_id(CONFIG)
    win.cores_cb.set_active_id(str(os.cpu_count()))
    win.from_cb.set_active_id("continue")
    win.to_cb.set_active_id(TO)
    GLib.timeout_add(500, wait_vars)
    return False


def wait_vars():
    if not win.vars:
        return True
    log(f"vars: {win.vars.get('RESULTS_DIR')}")
    GLib.timeout_add(1500, open_tools)
    return False


def open_tools():
    GLib.timeout_add(1500, fill_tools)
    win.choose_tools()  # modal; returns after fill_tools answers it
    GLib.timeout_add(8000, before_run)
    return False


def fill_tools():
    dlg = next(w for w in Gtk.Window.list_toplevels() if isinstance(w, Gtk.Dialog) and w.get_title() == "Tools")
    grid = dlg.get_content_area().get_children()[0]
    row = [t[0] for t in a.TOOLS].index("yosys") + 1
    grid.get_child_at(1, row).set_filename(YOSYS)
    GLib.timeout_add(1500, lambda: (snap(dlg, "tools"), dlg.response(Gtk.ResponseType.OK)) and False)
    return False


def before_run():
    log(f"tools saved: {win.tools()}")
    tab(3)
    snap(win, "debug_env")
    tab(0)
    snap(win, "ready")
    win.on_run()
    log(f"run started: {win.job.cmd}  log {win.job.log_path}")
    GLib.timeout_add_seconds(5, sample)
    for s in (45, 300, 900):
        GLib.timeout_add_seconds(s, lambda: (snap(win, "running"), False)[1])
    GLib.timeout_add_seconds(600, periodic)
    GLib.timeout_add_seconds(1200, lambda: (tab(2), snap(win, "logs_follow"), tab(0), False)[3])
    GLib.timeout_add_seconds(5, wait_done)
    return False


def periodic():
    if not win.job.running():
        return False
    snap(win, "running")
    return True


def wait_done():
    if win.job.running():
        return True
    win.tick_output()
    win.job.ended = time.time()
    log(f"run finished rc={win.job.rc} after {time.time() - win.job.started:.0f}s")
    snap(win, "done")
    tab(1)
    GLib.timeout_add_seconds(4, after_reports)
    return False


def after_reports():
    snap(win, "reports")
    tab(2)
    GLib.timeout_add_seconds(3, lambda: (snap(win, "logs"), tab(3), GLib.timeout_add_seconds(6, finish))[2] and False)
    return False


def finish():
    snap(win, "debug")
    json.dump({"name": NAME, "config": CONFIG, "yosys": YOSYS, "rc": win.job.rc,
               "wall_s": round(win.job.ended - win.job.started, 1), "log": str(win.job.log_path),
               "samples": samples, "events": events},
              open(f"{OUT}/{NAME}_run.json", "w"), indent=1)
    log("saved; quitting")
    Gtk.main_quit()
    return False


GLib.timeout_add(3000, select)
Gtk.main()
