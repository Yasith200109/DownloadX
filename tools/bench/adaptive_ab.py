"""Adaptive DownloadX A/B benchmark: pre-supervisor baseline vs adaptive candidate.

Windows-only. Build both revisions separately, then run:
  python tools/bench/adaptive_ab.py --baseline-exe PATH --adaptive-exe PATH --labserver-exe PATH

The runner starts a fresh client instance and isolated MDM_DATA_DIR for every trial,
alternates baseline/candidate order, performs one warm-up per scenario/build, records
raw timings and process CPU/peak working set, and preserves the lab server request log.
It makes no performance claim; analyze the raw data only after checksum and failure review.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8098
SCENARIOS = [
    ("fast-1g", "1 GiB fast range transfer", "1G", "", 10, 300),
    ("capped-100m", "100 MiB at 2 MB/s per connection", "100M", "rate=2M", 10, 240),
    ("latency-100m", "100 MiB with 150 ms latency", "100M", "rate=4M&latency_ms=150", 10, 240),
    ("slowconn-32m", "32 MiB with one slow connection in three", "32M",
     "rate=4M&slow_every=3&slow_rate=128K", 10, 360),
    ("maxconns-50m", "50 MiB, server caps at two connections", "50M",
     "rate=2M&max_conns=2", 10, 300),
    ("drops-50m", "50 MiB with periodic dropped connections", "50M",
     "rate=4M&drop_after=2M&drop_every=2", 10, 300),
    ("small-256k", "256 KiB small file", "256K", "", 15, 90),
]

kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def open_process(pid: int):
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
    if not handle:
        raise OSError(f"OpenProcess failed for PID {pid}: {ctypes.get_last_error()}")
    return handle


def process_times(handle):
    c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
    if not kernel32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e),
                                    ctypes.byref(k), ctypes.byref(u)):
        raise OSError("GetProcessTimes failed")
    ticks = lambda f: (f.dwHighDateTime << 32 | f.dwLowDateTime) / 1e7
    return ticks(k) + ticks(u)


def working_set(handle):
    m = PMC()
    m.cb = ctypes.sizeof(PMC)
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(m), m.cb):
        raise OSError("GetProcessMemoryInfo failed")
    return m.WorkingSetSize


def pid_of(image: str) -> list[int]:
    r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
                       capture_output=True, text=True, check=False)
    found = []
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            fields = line.strip().strip('"').split('","')
            if len(fields) >= 2 and fields[1].isdigit():
                found.append(int(fields[1]))
    return found


def size_bytes(value: str) -> int:
    n, unit = value[:-1], value[-1].upper()
    return int(float(n) * {"K": 2**10, "M": 2**20, "G": 2**30}[unit])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def wait_for_server(url: str, timeout: float = 20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                r.read()
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"labserver did not become ready: {url}")


def calibrate(name: str) -> float:
    a = time.perf_counter()
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/gen/1/cal-{name}", timeout=5) as r:
        r.read()
    b = time.perf_counter()
    return round((a + b) / 2 * 1000, 3)


def one_trial(label: str, exe: Path, out: Path, dest: Path, server_name: str,
              scenario: tuple, run_index: int, warmup: bool) -> dict:
    sid, _title, size, faults, _count, timeout = scenario
    name = f"{sid}-{'warmup' if warmup else f'{run_index:02d}'}.bin"
    final = dest / name
    for p in (final, dest / (name + ".mdm.part"), dest / (name + ".mdm.json")):
        if p.exists():
            p.unlink()

    trial_tag = "w" if warmup else f"{run_index:02d}"
    data_dir = out / "client-data" / f"{label.lower()}-{sid}-{trial_tag}"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    env = dict(os.environ, MDM_DATA_DIR=str(data_dir))
    app = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handle = None
    try:
        time.sleep(3)
        if app.poll() is not None:
            raise RuntimeError(f"{label} app exited during startup: {app.returncode}")
        handle = open_process(app.pid)
        cpu0 = process_times(handle)
        peak = working_set(handle)
        sampler_stop = False

        # Sample the app process while the command is active.
        import threading
        def sample():
            nonlocal peak
            while not sampler_stop:
                try:
                    peak = max(peak, working_set(handle))
                except OSError:
                    pass
                time.sleep(0.02)
        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()

        cal_ms = calibrate(name)
        t0 = time.perf_counter()
        subprocess.Popen([str(exe), "/d",
                          f"http://127.0.0.1:{PORT}/gen/{size}/{name}" + (f"?{faults}" if faults else ""),
                          "/p", str(dest), "/f", name, "/n"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        done = None
        while time.perf_counter() - t0 < timeout:
            try:
                if final.stat().st_size == size_bytes(size):
                    done = time.perf_counter()
                    break
            except OSError:
                pass
            if app.poll() is not None:
                break
            time.sleep(0.002)

        sampler_stop = True
        sampler.join(timeout=2)
        time.sleep(0.25)
        cpu_ms = round((process_times(handle) - cpu0) * 1000, 2)
        actual_size = final.stat().st_size if final.exists() else None
        digest = sha256(final) if done is not None and actual_size == size_bytes(size) else None
        return {
            "client": label, "scenario": sid, "name": name, "run": run_index,
            "warmup": warmup, "ok": done is not None and actual_size == size_bytes(size),
            "e2e_ms": round((done - t0) * 1000, 2) if done else None,
            "t0_ms": round(t0 * 1000, 3), "done_ms": round(done * 1000, 3) if done else None,
            "cal_ms": cal_ms, "file_bytes": actual_size, "sha256": digest,
            "cpu_ms": cpu_ms, "peak_ws_mb": round(peak / 2**20, 2),
            "expected_bytes": size_bytes(size),
        }
    finally:
        if handle:
            kernel32.CloseHandle(handle)
        app.terminate()
        try:
            app.wait(timeout=5)
        except subprocess.TimeoutExpired:
            app.kill()
            app.wait()


def main():
    if os.name != "nt":
        raise SystemExit("This benchmark runner requires Windows.")
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-exe", required=True, type=Path)
    ap.add_argument("--adaptive-exe", required=True, type=Path)
    ap.add_argument("--labserver-exe", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=0, help="override measured runs per scenario")
    ap.add_argument("--only", default="", help="comma-separated scenario IDs")
    ap.add_argument("--out", type=Path, default=Path(".study/bench/adaptive-" + dt.datetime.now().strftime("%Y%m%d-%H%M")))
    args = ap.parse_args()
    for p in (args.baseline_exe, args.adaptive_exe, args.labserver_exe):
        if not p.is_file():
            raise SystemExit(f"Executable not found: {p}")
    scenarios = [s for s in SCENARIOS if not args.only or s[0] in args.only.split(",")]
    if not scenarios:
        raise SystemExit("No scenarios selected.")
    args.out.mkdir(parents=True, exist_ok=True)
    dest = args.out / "downloads"
    dest.mkdir(exist_ok=True)
    server_log = args.out / "server.jsonl"
    lab = subprocess.Popen([str(args.labserver_exe), "--bind", f"127.0.0.1:{PORT}",
                             "--log-file", str(server_log)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    records = []
    try:
        wait_for_server(f"http://127.0.0.1:{PORT}/")
        for scenario in scenarios:
            sid, title, _size, _faults, default_runs, _timeout = scenario
            count = args.runs or default_runs
            print(f"== {sid}: {title} ({count} measured trials/build + warm-up)", flush=True)
            for i in range(-1, count):
                order = ["baseline", "adaptive"] if i % 2 == 0 else ["adaptive", "baseline"]
                for label in order:
                    exe = args.baseline_exe if label == "baseline" else args.adaptive_exe
                    r = one_trial(label, exe, args.out, dest, "labserver", scenario, max(i, 0), i < 0)
                    records.append(r)
                    trial_label = "warmup" if i < 0 else f"{i:02d}"
                    print(f"  {trial_label:>7} {label}: "
                          f"{r['e2e_ms']} ms; CPU {r['cpu_ms']} ms; "
                          f"WS {r['peak_ws_mb']} MB; {'OK' if r['ok'] else 'FAILED'}", flush=True)
                    (args.out / "runs.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
                    time.sleep(1)
    finally:
        lab.terminate()
        try:
            lab.wait(timeout=5)
        except subprocess.TimeoutExpired:
            lab.kill()
    manifest = {
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "os": platform.platform(),
        "cpu": subprocess.run(["powershell", "-NoProfile", "-Command",
                               "(Get-CimInstance Win32_Processor).Name"],
                              capture_output=True, text=True).stdout.strip(),
        "ram_gb": round(int(subprocess.run(["powershell", "-NoProfile", "-Command",
                                            "(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory"],
                                           capture_output=True, text=True).stdout.strip()) / 2**30, 1),
        "python": sys.version, "baseline_exe": str(args.baseline_exe.resolve()),
        "adaptive_exe": str(args.adaptive_exe.resolve()),
        "labserver_exe": str(args.labserver_exe.resolve()),
        "baseline_sha256": sha256(args.baseline_exe),
        "adaptive_sha256": sha256(args.adaptive_exe),
        "labserver_sha256": sha256(args.labserver_exe),
        "runs_per_scenario": args.runs or "scenario defaults",
        "scenarios": [s[0] for s in scenarios],
        "notes": "Fresh MDM_DATA_DIR and app process per trial; warmups excluded by analysis.",
    }
    (args.out / "environment.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Raw benchmark data: {args.out.resolve()}", flush=True)


if __name__ == "__main__":
    main()
