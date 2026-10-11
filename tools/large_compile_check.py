"""Scale check for the Rust compiler: tile a real map's cell sources out to 51 x 48 cells and compile.

    python tools/large_compile_check.py <map folder> [--work DIR] [--cols 51] [--rows 48] [--threads 4]

Makes the cell sources of a real KnoxMap map (tools/map_source.py), repeats them across the grid
with new cell positions, compiles with knoxlots and reports time and the compiler's own peak memory
(Windows only for the memory figure). Then runs `knoxlots verify` on the result. Needs about 4 GB
of disk for 2,448 cells; the work folder is left in place so the lots can be inspected.
"""
from __future__ import annotations

import argparse
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def peak_memory_mib(proc: subprocess.Popen) -> float | None:
    """Poll a running process until it exits; its peak working set in MiB (None off Windows)."""
    if sys.platform != "win32":
        proc.wait()
        return None
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t)] + \
                   [(n, ctypes.c_size_t) for n in ("WorkingSetSize", "QP", "QPP", "QNP", "QNPP", "Pagefile", "PeakPagefile")]

    handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0400 | 0x0010, False, proc.pid)
    peak = 0
    while proc.poll() is None:
        m = PMC()
        m.cb = ctypes.sizeof(m)
        if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(m), m.cb):
            peak = max(peak, m.PeakWorkingSetSize)
        time.sleep(0.05)
    return round(peak / 2**20, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("--work")
    ap.add_argument("--cols", type=int, default=51)
    ap.add_argument("--rows", type=int, default=48)
    ap.add_argument("--threads", type=int, default=4)
    a = ap.parse_args()

    from compile_map import knoxlots_exe  # tools/ is on sys.path when run as a script
    exe = knoxlots_exe()
    if exe is None:
        sys.exit("knoxlots not found - cargo build --release in rust/knoxlots")
    from tools.map_source import make_map_source

    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="knoxlots_scale_"))
    base, tiled, lots = work / "base_src", work / "tiled_src", work / "lots"
    for d in (base, tiled, lots):
        shutil.rmtree(d, ignore_errors=True)
    make_map_source(a.project, str(base), seams=True)
    cells = {tuple(map(int, c.stem.split("_")[1:])): c for c in base.glob("cell_*.kcell")}
    x0, y0 = min(x for x, _ in cells), min(y for _, y in cells)
    bw, bh = max(x for x, _ in cells) - x0 + 1, max(y for _, y in cells) - y0 + 1
    tiled.mkdir()
    for f in base.iterdir():
        if f.suffix.lower() in (".bmp", ".txt"):
            shutil.copy(f, tiled / f.name)
    made = 0
    for gx in range(a.cols):
        for gy in range(a.rows):
            src = cells.get((x0 + gx % bw, y0 + gy % bh))
            if src is None:
                continue
            b = bytearray(src.read_bytes())
            b[8:16] = struct.pack("<II", x0 + gx, y0 + gy)  # cell_x, cell_y follow the magic and version
            (tiled / f"cell_{x0 + gx}_{y0 + gy}.kcell").write_bytes(b)
            made += 1

    t = time.time()
    p = subprocess.Popen([str(exe), "compile", str(tiled), str(lots), "--threads", str(a.threads)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = peak_memory_mib(p)
    out, err = p.communicate()
    seconds = round(time.time() - t, 1)
    verify = subprocess.run([str(exe), "verify", str(lots)], capture_output=True, text=True)
    res = {"exit_code": p.returncode, "seconds": seconds, "compiler_peak_working_set_mib": peak,
           "source_cells": made, "lotheaders": len(list(lots.glob("*.lotheader"))),
           "verify_exit_code": verify.returncode, "verify": verify.stdout.strip(), "stderr": err.strip()[-300:],
           "work": str(work)}
    print(json.dumps(res, indent=2))
    return 0 if p.returncode == 0 and verify.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
