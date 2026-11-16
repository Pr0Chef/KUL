"""The open source RTL and FPGA tools the app drives, and where it finds them.

Simulation, lint, formal and FPGA tools come from OSS CAD Suite (YosysHQ's prebuilt bundle: Icarus Verilog,
Verilator, SymbiYosys, EQY, nextpnr, IceStorm, Trellis, Apicula, openFPGALoader, GTKWave), or from PATH. The suite
is used only for RTL and FPGA work: the ASIC flow keeps the OpenROAD and Yosys chosen under Tools…, so installing
the suite never changes ASIC results.
"""

import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

APP_ID = "orfs-flow-runner"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_ID
SUITE_HOME = DATA_DIR / "oss-cad-suite"          # where the app installs the suite
RELEASES = "https://api.github.com/repos/YosysHQ/oss-cad-suite-build/releases/latest"

# name: (what it is for, arguments that print its version)
TOOLS = {
    "iverilog": ("Icarus Verilog: compiles a simulation", ["-V"]),
    "vvp": ("Icarus Verilog: runs a simulation", ["-V"]),
    "verilator": ("Verilator: lint, and fast compiled simulation", ["--version"]),
    "yosys": ("Yosys: synthesis for FPGA and formal", ["-V"]),
    "sby": ("SymbiYosys: formal property checking", ["--version"]),
    "eqy": ("EQY: equivalence checking (LEC)", ["--version"]),
    "gtkwave": ("GTKWave: external waveform viewer", ["--version"]),
    "nextpnr-ice40": ("nextpnr: place and route for Lattice iCE40", ["--version"]),
    "nextpnr-ecp5": ("nextpnr: place and route for Lattice ECP5", ["--version"]),
    "nextpnr-himbaechel": ("nextpnr: place and route for Gowin", ["--version"]),
    "icepack": ("IceStorm: iCE40 bitstream", None),
    "icetime": ("IceStorm: iCE40 timing", None),
    "ecppack": ("Trellis: ECP5 bitstream", None),
    "gowin_pack": ("Apicula: Gowin bitstream", None),
    "openFPGALoader": ("openFPGALoader: program a board", ["--Version"]),
}


def suite_dirs():
    """Folders that may hold an OSS CAD Suite, most specific first."""
    dirs = [os.environ.get("OSS_CAD_SUITE"), SUITE_HOME, Path.home() / "oss-cad-suite", "/opt/oss-cad-suite"]
    return [Path(d) for d in dirs if d]


def suite():
    """The OSS CAD Suite folder in use, or None."""
    return next((d for d in suite_dirs() if (d / "bin" / "yosys").exists()), None)


def find(name, chosen=None):
    """Path of a tool: the one chosen in settings, else the suite's, else the one on PATH; None if not found.

    chosen: {tool: path} from the app's settings ("rtl_tools").
    """
    pick = (chosen or {}).get(name)
    if pick:
        return pick if os.access(pick, os.X_OK) else None
    root = suite()
    if root and (root / "bin" / name).exists():
        return str(root / "bin" / name)
    # The ASIC flow's own Yosys is not used here: it may lack the FPGA and formal passes of a recent build.
    return shutil.which(name)


def env(chosen=None):
    """Environment for RTL and FPGA jobs: the suite's bin folder first, so tools find each other (sby calls
    yosys, nextpnr's helpers call python)."""
    e = dict(os.environ)
    dirs = []
    root = suite()
    if root:
        dirs.append(str(root / "bin"))
    for path in (chosen or {}).values():
        if path and str(Path(path).parent) not in dirs:
            dirs.insert(0, str(Path(path).parent))
    e["PATH"] = os.pathsep.join(dirs + [e.get("PATH", "")])
    return e


def version(name, chosen=None):
    """First line a tool prints for its version, '' if it has no version flag, None if the tool is missing."""
    path = find(name, chosen)
    if not path:
        return None
    args = TOOLS.get(name, (None, None))[1]
    if not args:
        return ""
    try:
        out = subprocess.run([path] + args, capture_output=True, text=True, timeout=30, env=env(chosen))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = (out.stdout or out.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def missing(names, chosen=None):
    """The tools among names that are not installed."""
    return [n for n in names if not find(n, chosen)]


# ---------------------------------------------------------------- installing the suite
def release_asset():
    """(name, download URL, size in bytes) of the latest OSS CAD Suite build for this machine."""
    arch = {"x86_64": "linux-x64", "aarch64": "linux-arm64"}.get(platform.machine())
    if not arch or platform.system() != "Linux":
        raise RuntimeError(f"No OSS CAD Suite build for {platform.system()} {platform.machine()} is handled here")
    req = urllib.request.Request(RELEASES, headers={"Accept": "application/vnd.github+json", "User-Agent": APP_ID})
    with urllib.request.urlopen(req, timeout=30) as r:
        release = json.load(r)
    for asset in release["assets"]:
        if arch in asset["name"] and asset["name"].endswith(".tgz"):
            return asset["name"], asset["browser_download_url"], asset["size"]
    raise RuntimeError("The latest OSS CAD Suite release has no build for this machine")


def install_suite(progress=lambda text, fraction: None, cancelled=lambda: False):
    """Download and unpack OSS CAD Suite into the app's data folder; returns the folder.

    progress(text, fraction 0..1) is called as it goes. About 700 MB to download and 2 GB on disk. The archive
    is unpacked next to the final folder and moved into place, so a failed install leaves no half-made suite.
    """
    name, url, size = release_asset()
    free = shutil.disk_usage(DATA_DIR.parent if DATA_DIR.parent.exists() else Path.home()).free
    if free < size * 5:
        raise RuntimeError(f"Not enough disk space: the suite needs about {size * 5 >> 30} GB free, "
                           f"{free >> 30} GB is available")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    archive, staging = DATA_DIR / name, DATA_DIR / "oss-cad-suite.new"
    done = archive.stat().st_size if archive.exists() else 0
    if done != size:
        req = urllib.request.Request(url, headers={"User-Agent": APP_ID})
        with urllib.request.urlopen(req, timeout=60) as r, open(archive, "wb") as f:
            done = 0
            while chunk := r.read(1 << 20):
                if cancelled():
                    f.close()
                    archive.unlink()
                    raise RuntimeError("Cancelled")
                f.write(chunk)
                done += len(chunk)
                progress(f"Downloading {name}: {done >> 20} of {size >> 20} MB", done / size * 0.8)
    progress("Unpacking…", 0.8)
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(staging, filter="tar")
    inner = staging / "oss-cad-suite"
    if not (inner / "bin" / "yosys").exists():
        raise RuntimeError("The archive does not look like OSS CAD Suite")
    shutil.rmtree(SUITE_HOME, ignore_errors=True)
    inner.rename(SUITE_HOME)
    shutil.rmtree(staging, ignore_errors=True)
    archive.unlink()
    progress("Installed", 1.0)
    return SUITE_HOME
