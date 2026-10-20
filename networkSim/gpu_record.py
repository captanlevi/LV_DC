#!/usr/bin/env python3
"""gpu_record.py: the device record (levi's Addendum T; rushi 2026-10-19: the device metrics video decoding depends on,
for every session of record, joinable to the labels). One JSON line per second, epoch ts on the box's clock:
  - gpu_busy_pct: every i915 engine's busy % (rcs0 Render/3D, bcs0 copy, vcs0 / vcs1 Video, vecs0 video enhance) from
    the i915 PMU's busy counters (perf_event_open; intel_gpu_top reads the same). At perf_event_paranoid > 0 (chaos: 4)
    they need root, so the recorder runs under `sudo -n` (supervisor: read-only counters, no system change). If the PMU
    refuses: DRM fdinfo (per class only: render / copy / video over its capacity / video-enhance), stated per line.
  - slots: each trio slot's Chrome process tree (/sys/fs/cgroup/lvdc.slice/<tag>): cpu_pct (% of one core, summed over
    the tree, from the cgroup's cpu.stat), by_type (browser / renderer / gpu / network / utility / other, from each
    process's /proc stat and its --type), procs, mem_mb (memory.current; when the memory controller is not delegated to the
    slot cgroups, the slot's VmRSS sum from /proc, a trend: mem_source names which).
  - cpu_pct (box, 0-100 over all cores, /proc/stat), mem_available_mb, swap_used_kb, oom_kill (/proc/vmstat).
A second whose sample failed is a line with "error"; a pause longer than 1.5 intervals is a "gap" line, so coverage
is never silently lost; a pass that raises is logged ("restart: ...") and started again with a new header. The header line carries the recorder's code sha256 and commit. Ops only: the collector does
not change (its sha does not move).

    sudo -n nice -n 10 python3 networkSim/gpu_record.py --out <run>/box_record.jsonl        (stop: SIGTERM / SIGINT)
    python3 networkSim/gpu_record.py --stamp <run>/box_record.jsonl <session_dir> ...        (after the round)

--stamp (after the sessions end; no root): per session, the record's lines inside [meta started_at - 1, ended_at + 1]
go to <session>/box_record.jsonl. Its header is the recorder's (code sha256, commit, GPU source) plus "stamp": the run
record's path and sha256, the window, the session's slot key (meta concurrency.cgroup's last part) and the lines
copied. meta.json is never edited (levi: it is the collector's raw record; hashes of it must not go stale).
Coverage (Addendum T: >= 0.95 of a counted session's seconds with a data line) and the join to label ticks are the
post-hoc readers' (brad / levi). Line keys: "t" epoch seconds on the box's clock (the second's end); a data line has
"gpu_busy_pct"; "error" and "gap" lines mark lost seconds.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PMU_ROOT = "/sys/bus/event_source/devices"
CG_SLICE = "/sys/fs/cgroup/lvdc.slice"
PERF_EVENT_OPEN_X86_64 = 298
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
GAP_FACTOR = 1.5


class PerfEventAttr(ctypes.Structure):
    """struct perf_event_attr up to config1 (PERF_ATTR_SIZE_VER0 = 64 bytes): a counting event, enabled at open."""
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("config", ctypes.c_uint64),
                ("sample_period", ctypes.c_uint64), ("sample_type", ctypes.c_uint64), ("read_format", ctypes.c_uint64),
                ("flags", ctypes.c_uint64), ("wakeup_events", ctypes.c_uint32), ("bp_type", ctypes.c_uint32),
                ("config1", ctypes.c_uint64)]


def code_sha() -> dict:
    """This file's sha256 and the checkout's commit (the record's provenance)."""
    out = {"file_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        out["lvdc_commit"] = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True,
                                            text=True, timeout=5).stdout.strip() or None
    except Exception:
        out["lvdc_commit"] = None
    return out


# ---- GPU ------------------------------------------------------------------------------------------------------------

def pmu_engines(root: str = PMU_ROOT) -> dict | None:
    """The i915 PMU: {"pmu", "type", "cpu", "engines": {name: config}} from its sysfs dir (events/<engine>-busy hold
    'config=0x...'), or None when there is none."""
    for d in sorted(Path(root).glob("i915*")):
        try:
            typ = int((d / "type").read_text().strip())
            cpu = int((d / "cpumask").read_text().strip().split(",")[0].split("-")[0]) if (d / "cpumask").exists() else 0
        except (OSError, ValueError):
            continue
        eng = {}
        for e in sorted((d / "events").glob("*-busy")):
            try:
                eng[e.name[:-len("-busy")]] = int(e.read_text().strip().split("=", 1)[1], 16)
            except (OSError, ValueError, IndexError):
                continue
        if eng:
            return {"pmu": d.name, "type": typ, "cpu": cpu, "engines": eng}
    return None


class PmuReader:
    """One perf counter per i915 engine; read() -> {engine: busy ns}. Opening fails without root at paranoid > 0."""
    source = "i915-pmu"
    capacity: dict = {}

    def __init__(self, info: dict):
        self.info, self.fds = info, {}
        libc = ctypes.CDLL(None, use_errno=True)
        for name, cfg in info["engines"].items():
            attr = PerfEventAttr(type=info["type"], size=ctypes.sizeof(PerfEventAttr), config=cfg)
            fd = libc.syscall(PERF_EVENT_OPEN_X86_64, ctypes.byref(attr), -1, info["cpu"], -1, 0)
            if fd < 0:
                err = ctypes.get_errno()
                self.close()
                raise OSError(err, f"perf_event_open {info['pmu']}/{name}-busy: {os.strerror(err)}")
            self.fds[name] = fd

    def read(self) -> dict:
        return {n: struct.unpack("Q", os.read(fd, 8))[0] for n, fd in self.fds.items()}

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = {}


def parse_fdinfo(text: str) -> dict | None:
    """One DRM fdinfo -> {"driver", "client", "pdev", "engines": {class: ns}, "capacity": {class: n}}, or None."""
    kv = {}
    for line in text.splitlines():
        k, _, v = line.partition(":")
        kv[k.strip()] = v.strip()
    if "drm-driver" not in kv:
        return None
    eng, cap = {}, {}
    for k, v in kv.items():
        if k.startswith("drm-engine-capacity-"):
            cap[k[len("drm-engine-capacity-"):]] = int(v)
        elif k.startswith("drm-engine-") and v.endswith(" ns"):
            eng[k[len("drm-engine-"):]] = int(v.split()[0])
    return {"driver": kv["drm-driver"], "client": kv.get("drm-client-id"), "pdev": kv.get("drm-pdev"),
            "engines": eng, "capacity": cap}


class FdinfoReader:
    """Without the PMU: every readable DRM client's i915 engine time, per class (a client counted once by its pdev and
    drm-client-id); read() -> {class: ns}; capacity {class: instances}."""
    source = "drm-fdinfo"

    def __init__(self, proc: str = "/proc"):
        self.proc, self.capacity = Path(proc), {}

    def read(self) -> dict:
        seen, tot = set(), {}
        for p in (self.proc.iterdir() if self.proc.is_dir() else ()):
            if not p.name.isdigit():
                continue
            try:
                fds = list((p / "fdinfo").iterdir())
            except OSError:
                continue
            for f in fds:
                try:
                    if not os.readlink(p / "fd" / f.name).startswith("/dev/dri/"):
                        continue
                    r = parse_fdinfo(f.read_text())
                except OSError:
                    continue
                if not r or r["driver"] != "i915" or (r["pdev"], r["client"]) in seen:
                    continue
                seen.add((r["pdev"], r["client"]))
                for c, ns in r["engines"].items():
                    tot[c] = tot.get(c, 0) + ns
                self.capacity.update(r["capacity"])
        return tot

    def close(self) -> None:
        pass


def open_reader(pmu_root: str = PMU_ROOT, proc: str = "/proc"):
    """(reader, why): the PMU when it opens, else fdinfo with the reason."""
    info = pmu_engines(pmu_root)
    if info is None:
        return FdinfoReader(proc), "no i915 PMU"
    try:
        return PmuReader(info), None
    except OSError as exc:
        return FdinfoReader(proc), repr(exc)[:200]


# ---- slots: each trio slot's Chrome process tree --------------------------------------------------------------------

def proc_type(cmdline: bytes) -> str:
    """A Chrome process's role from its argv: --type=renderer / gpu-process / utility (the network service apart);
    no --type = the browser process."""
    args = cmdline.split(b"\0")
    typ = next((a[len(b"--type="):].decode("utf-8", "replace") for a in args if a.startswith(b"--type=")), None)
    if typ is None:
        return "browser" if args and b"chrome" in args[0].lower() else "other"
    if typ == "gpu-process":
        return "gpu"
    if typ == "utility" and any(b"NetworkService" in a for a in args):
        return "network"
    return typ if typ in ("renderer", "utility") else "other"


def proc_rss_sum(pids: list[int], proc: str = "/proc") -> int | None:
    """The VmRSS sum (bytes) over pids from /proc/<pid>/status; None when none could be read."""
    total, n = 0, 0
    for pid in pids:
        try:
            for line in (Path(proc) / str(pid) / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    n += 1
                    break
        except (OSError, ValueError, IndexError):
            continue
    return total if n else None


def slot_read(slice_dir: str = CG_SLICE, proc: str = "/proc", types: dict | None = None) -> dict:
    """{tag: {"usage_usec", "mem_bytes", "ticks": {pid: (type, utime + stime)}}} for every cgroup under the slice.
    types caches each (pid, starttime)'s role (Chrome rewrites its title; its --type stays)."""
    types = {} if types is None else types
    out = {}
    for cg in (sorted(Path(slice_dir).iterdir()) if Path(slice_dir).is_dir() else ()):
        if not cg.is_dir():
            continue
        try:
            usage = int(next(line.split()[1] for line in (cg / "cpu.stat").read_text().splitlines()
                             if line.startswith("usage_usec ")))
        except (OSError, ValueError, StopIteration):
            continue
        try:
            mem, src = int((cg / "memory.current").read_text().strip()), "memory.current"
        except (OSError, ValueError):
            mem, src = None, None
        ticks = {}
        try:
            pids = [int(x) for x in (cg / "cgroup.procs").read_text().split()]
        except (OSError, ValueError):
            pids = []
        if mem is None and pids:              # t58 (shagun 07:57): the memory controller is not delegated to the slot
            rss = proc_rss_sum(pids, proc)    # cgroups, so memory.current is absent: the slot's VmRSS sum (a trend;
            if rss is not None:               # shared pages count once per process)
                mem, src = rss, "proc_rss_sum"
        for pid in pids:
            try:
                st = (Path(proc) / str(pid) / "stat").read_text()
                f = st[st.rindex(")") + 2:].split()          # fields from 3 (state) on: utime 14, stime 15, start 22
                key = (pid, f[19])
                if key not in types:
                    types[key] = proc_type((Path(proc) / str(pid) / "cmdline").read_bytes())
                ticks[pid] = (types[key], int(f[11]) + int(f[12]))
            except (OSError, ValueError, IndexError):
                continue
        out[cg.name] = {"usage_usec": usage, "mem_bytes": mem, "mem_source": src, "ticks": ticks}
    return out


def slot_line(s0: dict, s1: dict, dt: float) -> dict:
    """Per slot between two reads: cpu_pct (% of one core over the tree, from cpu.stat), by_type (% of one core per
    role; a process first seen in this interval counts its whole time), procs, mem_mb. A slot first seen now: its
    procs and memory only (no baseline yet)."""
    out = {}
    for tag, b in s1.items():
        a = s0.get(tag)
        rec: dict = {"procs": len(b["ticks"]), "mem_mb": None if b["mem_bytes"] is None else round(b["mem_bytes"] / 2**20, 1),
                     "mem_source": b.get("mem_source")}
        if a is not None and dt > 0:
            rec["cpu_pct"] = round(100.0 * (b["usage_usec"] - a["usage_usec"]) / (dt * 1e6), 2)
            by: dict = {}
            for pid, (typ, t) in b["ticks"].items():
                prev = a["ticks"].get(pid)
                d = t - (prev[1] if prev and prev[0] == typ else 0)
                by[typ] = by.get(typ, 0) + max(d, 0)
            rec["by_type"] = {k: round(100.0 * v / (CLK_TCK * dt), 2) for k, v in sorted(by.items())}
        out[tag] = rec
    return out


# ---- box ------------------------------------------------------------------------------------------------------------

def cpu_times(stat: str = "/proc/stat") -> tuple[int, int] | None:
    """(busy, total) jiffies of the aggregate cpu line (idle + iowait count as idle)."""
    try:
        f = [int(x) for x in Path(stat).read_text().splitlines()[0].split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle = f[3] + (f[4] if len(f) > 4 else 0)
    return sum(f) - idle, sum(f)


def mem_state(meminfo: str = "/proc/meminfo", vmstat: str = "/proc/vmstat") -> dict:
    out: dict = {"mem_available_mb": None, "swap_used_kb": None, "oom_kill": None}
    try:
        m = {k.strip(): int(v.split()[0]) for k, v in (line.split(":", 1) for line in Path(meminfo).read_text().splitlines())}
        out["mem_available_mb"] = round(m["MemAvailable"] / 1024, 1)
        out["swap_used_kb"] = m["SwapTotal"] - m["SwapFree"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        out["oom_kill"] = int(next(line.split()[1] for line in Path(vmstat).read_text().splitlines()
                                   if line.startswith("oom_kill ")))
    except (OSError, ValueError, StopIteration):
        pass
    return out


def busy_line(t: float, dt: float, gpu0: dict, gpu1: dict, source: str, capacity: dict, cpu0, cpu1, mem: dict) -> dict:
    """One record line from two reads dt seconds apart: busy % per engine (the PMU: per instance; fdinfo: per class
    over its capacity), box CPU busy %, memory, swap, OOM count."""
    busy = {}
    for k in gpu1:
        if k in gpu0 and dt > 0:
            busy[k] = round(100.0 * (gpu1[k] - gpu0[k]) / (dt * 1e9 * (capacity.get(k, 1) or 1)), 2)
    cpu = None
    if cpu0 and cpu1 and cpu1[1] > cpu0[1]:
        cpu = round(100.0 * (cpu1[0] - cpu0[0]) / (cpu1[1] - cpu0[1]), 2)
    return {"t": round(t, 3), "dt": round(dt, 3), "cpu_pct": cpu, "gpu_source": source, "gpu_busy_pct": busy, **mem}


# ---- the recorder ---------------------------------------------------------------------------------------------------

def record(out: Path, interval: float = 1.0, duration: float | None = None, stop: threading.Event | None = None,
           reader=None, clock=time.time, sleep=None, slots_fn=slot_read, reopen=open_reader) -> int:
    """Header, then one line per interval to `out` (flushed per line) until duration / stop. A failed sample writes an
    "error" line and the GPU reader is reopened; a pause beyond GAP_FACTOR intervals writes a "gap" line. Returns the
    data lines written."""
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    rd, why = (reader, None) if reader is not None else reopen()
    hdr = {"header": True, "what": "device record (Addendum T): i915 engine busy, slot Chrome-tree CPU, box CPU / memory",
           "gpu_source": rd.source, "fallback_why": why, "interval_s": interval, "pid": os.getpid(),
           "clk_tck": CLK_TCK, "ncpu": os.cpu_count(), **code_sha()}
    if isinstance(rd, PmuReader):
        hdr["pmu"] = rd.info
    n, types = 0, {}
    with open(out, "a") as fh:
        def put(rec):
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
        put(hdr)
        start = t0 = clock()
        g0, c0, s0 = rd.read(), cpu_times(), slots_fn(types=types)
        try:
            while not stop.is_set() and (duration is None or t0 - start < duration):
                sleep(interval)
                t1 = clock()
                try:
                    g1, c1, s1 = rd.read(), cpu_times(), slots_fn(types=types)
                    if t1 - t0 > GAP_FACTOR * interval:
                        put({"t": round(t1, 3), "gap": True, "from": round(t0, 3), "to": round(t1, 3)})
                    put({**busy_line(t1, t1 - t0, g0, g1, rd.source, rd.capacity, c0, c1, mem_state()),
                         "slots": slot_line(s0, s1, t1 - t0)})
                    n += 1
                    g0, c0, s0 = g1, c1, s1
                except Exception as exc:                 # never stop recording: log it, reopen the GPU reader
                    put({"t": round(t1, 3), "error": repr(exc)[:300], "trace": traceback.format_exc(limit=2)[-300:]})
                    try:
                        rd.close()
                        rd, why = reopen()
                        put({"t": round(clock(), 3), "reopened": rd.source, "fallback_why": why})
                        g0, c0, s0 = rd.read(), cpu_times(), slots_fn(types=types)
                    except Exception as exc2:
                        put({"t": round(clock(), 3), "error": "reopen: " + repr(exc2)[:300]})
                t0 = t1
        finally:
            rd.close()
    return n


# ---- reading a record back ------------------------------------------------------------------------------------------

def read(path: Path) -> tuple[dict | None, list[dict]]:
    hdr, rows = None, []
    for line in Path(path).read_text().splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue                                    # a line cut by a kill: skipped
        if r.get("header"):
            hdr = hdr or r
        else:
            rows.append(r)
    return hdr, rows


def _pct(xs: list, q: float) -> float | None:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = q * (len(xs) - 1)
    lo = int(i)
    return round(xs[lo] + (xs[min(lo + 1, len(xs) - 1)] - xs[lo]) * (i - lo), 2)


def summary(path: Path, t0: float | None = None, t1: float | None = None, report_pct: float = 85.0) -> dict:
    """A record (or its [t0, t1] window): box CPU and each engine's p50 / p95 (an engine above report_pct is listed: a
    report, not a gate), each slot's cpu_pct p50 / p95, swap growth (kB), OOM kills, error / gap lines, the header."""
    hdr, allrows = read(path)
    win = [r for r in allrows if (t0 is None or r["t"] >= t0) and (t1 is None or r["t"] <= t1)]
    rows = [r for r in win if "gpu_busy_pct" in r]
    eng = sorted({k for r in rows for k in r["gpu_busy_pct"]})
    per = {k: {"p50": _pct([r["gpu_busy_pct"].get(k) for r in rows], 0.5),
               "p95": _pct([r["gpu_busy_pct"].get(k) for r in rows], 0.95)} for k in eng}
    tags = sorted({k for r in rows for k in r.get("slots") or {}})
    slots = {k: {q: _pct([(r.get("slots") or {}).get(k, {}).get("cpu_pct") for r in rows], v)
                 for q, v in (("p50", 0.5), ("p95", 0.95))} for k in tags}
    sw = [r["swap_used_kb"] for r in rows if r.get("swap_used_kb") is not None]
    oo = [r["oom_kill"] for r in rows if r.get("oom_kill") is not None]
    return {"n": len(rows), "gpu_source": (hdr or {}).get("gpu_source"), "fallback_why": (hdr or {}).get("fallback_why"),
            "code": {k: (hdr or {}).get(k) for k in ("file_sha256", "lvdc_commit")},
            "cpu_pct": {"p50": _pct([r.get("cpu_pct") for r in rows], 0.5), "p95": _pct([r.get("cpu_pct") for r in rows], 0.95)},
            "gpu_busy_pct": per, "gpu_above_report_line": sorted(k for k, v in per.items() if (v["p95"] or 0) > report_pct),
            "slot_cpu_pct": slots, "swap_growth_kb": (sw[-1] - sw[0]) if sw else None,
            "oom_kills": (oo[-1] - oo[0]) if oo else None,
            "errors": sum(1 for r in win if "error" in r), "gaps": sum(1 for r in win if r.get("gap"))}


def stamp(record_path: Path, session_dirs: list[Path]) -> list[dict]:
    """After the round: each session's slice of the record to <session>/box_record.jsonl, its header the recorder's
    plus "stamp" (run record path / sha256, window, slot, lines). meta.json is read, never written. A session without
    started_at / ended_at gets no file and an "error" in the result."""
    record_path = Path(record_path)
    hdr, rows = read(record_path)
    rec_sha = hashlib.sha256(record_path.read_bytes()).hexdigest()
    out = []
    for sd in session_dirs:
        sd = Path(sd)
        meta = json.loads((sd / "meta.json").read_text())
        t0, t1 = meta.get("started_at"), meta.get("ended_at")
        cg = ((meta.get("concurrency") or {}).get("cgroup") or "")
        st = {"record": str(record_path), "record_sha256": rec_sha, "slot": cg.rsplit("/", 1)[-1] or None}
        if not (isinstance(t0, (int, float)) and isinstance(t1, (int, float)) and t1 > t0):
            out.append({"session": sd.name, **st, "error": "meta has no started_at / ended_at"})
            continue
        mine = [r for r in rows if t0 - 1 <= r["t"] <= t1 + 1]
        st.update(window=[t0 - 1, t1 + 1], lines=len(mine))
        with open(sd / "box_record.jsonl", "w") as fh:
            for r in [{**(hdr or {"header": True}), "stamp": st}] + mine:
                fh.write(json.dumps(r) + "\n")
        out.append({"session": sd.name, **st})
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, help="record to this file")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=None)
    ap.add_argument("--stamp", type=Path, help="a record: split it into the sessions given and stamp their meta")
    ap.add_argument("sessions", nargs="*", type=Path)
    a = ap.parse_args(argv)
    if a.stamp:
        res = stamp(a.stamp, a.sessions)
        print(json.dumps(res, indent=1))
        return 0 if not any(r.get("error") for r in res) else 1
    if not a.out:
        ap.error("--out or --stamp")
    ev = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: ev.set())
    n, t_end = 0, None if a.duration is None else time.time() + a.duration
    while not ev.is_set() and (t_end is None or time.time() < t_end):   # restart on failure (supervisor): a pass that
        try:                                                             # raises is logged and started again
            n += record(a.out, a.interval, None if t_end is None else max(t_end - time.time(), 0), ev)
        except Exception as exc:
            with open(a.out, "a") as fh:
                fh.write(json.dumps({"t": round(time.time(), 3), "error": "restart: " + repr(exc)[:300]}) + "\n")
            ev.wait(a.interval)
    print(json.dumps({"lines": n, "out": str(a.out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
