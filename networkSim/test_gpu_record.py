#!/usr/bin/env python3
"""The device record (gpu_record.py, levi's Addendum T): the PMU's engine list from sysfs, the perf_event_attr layout,
DRM fdinfo parsing and the per-class fallback (a client counted once), the busy % arithmetic, each slot's Chrome
process tree (cgroup cpu.stat, per-role /proc ticks, a slot first seen without a baseline), a record written with an
error that reopens the reader and a gap line, the summary, the post-round stamp (each session's slice; meta untouched), and the
CLI on a box with neither PMU nor /proc."""
import ctypes
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gpu_record as G  # noqa: E402

FDINFO = """pos:\t0
flags:\t02100002
drm-driver:\ti915
drm-client-id:\t44
drm-pdev:\t0000:00:02.0
drm-total-system0:\t138332 KiB
drm-engine-render:\t1722577811292 ns
drm-engine-copy:\t0 ns
drm-engine-video:\t5000000000 ns
drm-engine-capacity-video:\t2
drm-engine-video-enhance:\t0 ns
"""


def test_attr_and_pmu_engines():
    assert ctypes.sizeof(G.PerfEventAttr) == 64                     # PERF_ATTR_SIZE_VER0
    with tempfile.TemporaryDirectory() as d:
        ev = Path(d) / "i915" / "events"
        ev.mkdir(parents=True)
        (Path(d) / "i915" / "type").write_text("34\n")
        (Path(d) / "i915" / "cpumask").write_text("0\n")
        for name, cfg in (("rcs0", "0x0"), ("bcs0", "0x1000"), ("vcs0", "0x2000"), ("vcs1", "0x2010"), ("vecs0", "0x3000")):
            (ev / f"{name}-busy").write_text(f"config={cfg}\n")
            (ev / f"{name}-busy.unit").write_text("ns\n")
        (ev / "actual-frequency").write_text("config=0x100000\n")
        info = G.pmu_engines(d)
        assert info == {"pmu": "i915", "type": 34, "cpu": 0,
                        "engines": {"bcs0": 0x1000, "rcs0": 0, "vcs0": 0x2000, "vcs1": 0x2010, "vecs0": 0x3000}}, info
        assert G.pmu_engines(str(Path(d) / "none")) is None


def test_fdinfo():
    r = G.parse_fdinfo(FDINFO)
    assert r["driver"] == "i915" and r["client"] == "44" and r["capacity"] == {"video": 2}
    assert r["engines"] == {"render": 1722577811292, "copy": 0, "video": 5000000000, "video-enhance": 0}, r
    assert G.parse_fdinfo("pos:\t0\nflags:\t1\n") is None
    with tempfile.TemporaryDirectory() as d:                       # two processes share client 44: counted once
        for pid, fd, target in (("100", "5", "/dev/dri/renderD128"), ("101", "7", "/dev/dri/renderD128"),
                                ("102", "3", "/tmp/not-drm")):
            (Path(d) / pid / "fdinfo").mkdir(parents=True)
            (Path(d) / pid / "fd").mkdir()
            (Path(d) / pid / "fdinfo" / fd).write_text(FDINFO)
            os.symlink(target, Path(d) / pid / "fd" / fd)
        (Path(d) / "self").mkdir()
        rd = G.FdinfoReader(d)
        assert rd.read() == {"render": 1722577811292, "copy": 0, "video": 5000000000, "video-enhance": 0}
        assert rd.capacity == {"video": 2}
    assert G.FdinfoReader("/nonexistent-proc").read() == {}


def test_busy_line():
    ln = G.busy_line(10.0, 1.0, {"rcs0": 0, "vcs0": 0}, {"rcs0": 500_000_000, "vcs0": 1_000_000_000}, "i915-pmu", {},
                     (100, 1000), (150, 1100), {"swap_used_kb": 0, "oom_kill": 0})
    assert ln["gpu_busy_pct"] == {"rcs0": 50.0, "vcs0": 100.0} and ln["cpu_pct"] == 50.0, ln
    ln = G.busy_line(10.0, 2.0, {"video": 0}, {"video": 2_000_000_000}, "drm-fdinfo", {"video": 2}, None, None, {})
    assert ln["gpu_busy_pct"] == {"video": 50.0} and ln["cpu_pct"] is None, ln


def _proc(root: Path, pid: int, argv: list[str], utime: int, stime: int, start: int = 777) -> None:
    d = root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    rest = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 6 + [str(start)] + ["0"] * 20
    (d / "stat").write_text(f"{pid} (chrome (x)) " + " ".join(rest) + "\n")


def test_slots():
    """A slot's tree: cpu_pct from cpu.stat (% of one core), by_type from the processes' ticks per role."""
    assert G.proc_type(b"/opt/google/chrome/chrome\0--type=renderer\0") == "renderer"
    assert G.proc_type(b"/opt/google/chrome/chrome\0--type=gpu-process\0") == "gpu"
    assert G.proc_type(b"/opt/google/chrome/chrome\0--type=utility\0--utility-sub-type=network.mojom.NetworkService\0") == "network"
    assert G.proc_type(b"/opt/google/chrome/chrome\0--remote-debugging-port=9223\0") == "browser"
    assert G.proc_type(b"/usr/bin/cat\0") == "other"
    with tempfile.TemporaryDirectory() as d:
        sl, pr = Path(d) / "lvdc.slice", Path(d) / "proc"
        (sl / "yt").mkdir(parents=True)
        (sl / "yt" / "cgroup.procs").write_text("10\n11\n")
        (sl / "yt" / "memory.current").write_text(str(512 * 2**20))
        (sl / "yt" / "cpu.stat").write_text("usage_usec 1000000\nuser_usec 800000\n")
        _proc(pr, 10, ["/opt/google/chrome/chrome", "--remote-debugging-port=9223"], 100, 20)
        _proc(pr, 11, ["/opt/google/chrome/chrome", "--type=renderer"], 300, 50)
        types: dict = {}
        s0 = G.slot_read(str(sl), str(pr), types)
        first = G.slot_line({}, s0, 1.0)
        assert first == {"yt": {"procs": 2, "mem_mb": 512.0, "mem_source": "memory.current"}}, first   # no baseline
        # t58 (shagun 07:57): no memory.current (the controller not delegated): the slots' VmRSS sum, named
        (sl / "yt" / "memory.current").unlink()
        for pid, kb in ((10, 200000), (11, 300000)):
            (pr / str(pid) / "status").write_text(f"Name:\tchrome\nVmRSS:\t{kb} kB\n")
        fb = G.slot_line({}, G.slot_read(str(sl), str(pr), types), 1.0)
        assert fb == {"yt": {"procs": 2, "mem_mb": round(500000 * 1024 / 2**20, 1), "mem_source": "proc_rss_sum"}}, fb
        assert G.proc_rss_sum([999], str(pr)) is None                              # nothing readable: None
        (sl / "yt" / "memory.current").write_text(str(512 * 2**20))
        (sl / "yt" / "cpu.stat").write_text("usage_usec 2500000\n")                # 1.5 cores over 1 s
        (sl / "yt" / "cgroup.procs").write_text("10\n11\n12\n")
        _proc(pr, 10, ["/opt/google/chrome/chrome", "--remote-debugging-port=9223"], 110, 20)
        _proc(pr, 11, ["/opt/google/chrome/chrome", "--type=renderer"], 380, 70)
        _proc(pr, 12, ["/opt/google/chrome/chrome", "--type=gpu-process"], 15, 5)   # new: its whole time
        s1 = G.slot_read(str(sl), str(pr), types)
        ln = G.slot_line(s0, s1, 1.0)["yt"]
        tck = G.CLK_TCK
        assert ln["cpu_pct"] == 150.0 and ln["procs"] == 3, ln
        assert ln["by_type"] == {"browser": round(100.0 * 10 / tck, 2), "gpu": round(100.0 * 20 / tck, 2),
                                 "renderer": round(100.0 * 100 / tck, 2)}, ln
        assert G.slot_read(str(Path(d) / "absent"), str(pr)) == {}


class Fake:
    source, capacity = "i915-pmu", {}

    def __init__(self, fail_at=None):
        self.ns, self.calls, self.fail_at = 0, 0, fail_at

    def read(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise OSError(5, "EIO")
        self.ns += 900_000_000                                     # 90 % busy each second
        return {"rcs0": self.ns, "vcs0": self.ns // 9}

    def close(self):
        pass


def test_record_error_gap_and_summary():
    ticks = iter([0.0, 1.0, 2.0, 3.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0])        # a 3 s pause before 6.0
    reopened = []

    def reopen():
        reopened.append(1)
        return Fake(), None
    slots = lambda types=None: {"yt": {"usage_usec": 0, "mem_bytes": None, "ticks": {}}}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "box.jsonl"
        n = G.record(out, 1.0, duration=8.0, reader=Fake(fail_at=3), clock=lambda: next(ticks), sleep=lambda s: None,
                     slots_fn=slots, reopen=reopen)
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        assert lines[0]["header"] and lines[0]["gpu_source"] == "i915-pmu" and len(lines[0]["file_sha256"]) == 64
        kinds = [("header" if x.get("header") else "error" if "error" in x else "reopened" if "reopened" in x
                  else "gap" if x.get("gap") else "data") for x in lines]
        assert kinds[:4] == ["header", "data", "error", "reopened"] and "gap" in kinds and reopened, kinds
        data = [x for x in lines if "gpu_busy_pct" in x]
        assert n == len(data) and all(abs(x["gpu_busy_pct"]["rcs0"] - 90.0 / x["dt"]) < 1e-6 for x in data), data
        assert all(x["slots"]["yt"]["cpu_pct"] == 0.0 for x in data)
        rows = [{"t": float(i), "cpu_pct": float(i * 10), "gpu_busy_pct": {"rcs0": float(i * 10)}, "swap_used_kb": 100 + i,
                 "oom_kill": 2, "slots": {"yt": {"cpu_pct": 50.0}}} for i in range(11)]
        out.write_text(json.dumps(lines[0]) + "\n" + "".join(json.dumps(r) + "\n" for r in rows) + '{"t": 11.0, "gap": true}\n{"cut')
        s = G.summary(out)
        assert s["n"] == 11 and s["cpu_pct"] == {"p50": 50.0, "p95": 95.0} and s["gpu_busy_pct"]["rcs0"]["p95"] == 95.0, s
        assert s["gpu_above_report_line"] == ["rcs0"] and s["swap_growth_kb"] == 10 and s["oom_kills"] == 0, s
        assert s["slot_cpu_pct"] == {"yt": {"p50": 50.0, "p95": 50.0}} and s["gaps"] == 1 and s["errors"] == 0, s
        assert G.summary(out, t0=0, t1=5)["gpu_above_report_line"] == []


def test_stamp():
    """Each session's slice with the recorder's header plus the stamp (record sha256, window, slot, lines); meta.json is
    never written; a session without times gets no file and an error."""
    with tempfile.TemporaryDirectory() as d:
        rec = Path(d) / "box_record.jsonl"
        hdr = {"header": True, "gpu_source": "i915-pmu", "file_sha256": "a" * 64, "lvdc_commit": "c" * 40}
        rec.write_text(json.dumps(hdr) + "\n" + "".join(json.dumps({"t": 100.0 + i, "gpu_busy_pct": {}}) + "\n"
                                                         for i in range(20)))
        a, b = Path(d) / "yt_100001", Path(d) / "tw_100002"
        for sd, m in ((a, {"started_at": 104.5, "ended_at": 110.2, "concurrency": {"cgroup": "lvdc.slice/yt"}}),
                      (b, {"started_at": None})):
            sd.mkdir()
            (sd / "meta.json").write_text(json.dumps(m))
        metas = {sd: (sd / "meta.json").read_bytes() for sd in (a, b)}
        res = G.stamp(rec, [a, b])
        assert all((sd / "meta.json").read_bytes() == raw for sd, raw in metas.items())   # meta untouched
        sl = [json.loads(x) for x in (a / "box_record.jsonl").read_text().splitlines()]
        h = sl[0]
        assert h["header"] and h["file_sha256"] == "a" * 64 and h["lvdc_commit"] == "c" * 40, h
        assert h["stamp"]["slot"] == "yt" and h["stamp"]["lines"] == 8 and h["stamp"]["window"] == [103.5, 111.2], h
        assert len(h["stamp"]["record_sha256"]) == 64 and [x["t"] for x in sl[1:]] == [100.0 + i for i in range(4, 12)]
        assert "error" in res[1] and not (b / "box_record.jsonl").exists()
        assert G.main(["--stamp", str(rec), str(a)]) == 0 and G.main(["--stamp", str(rec), str(b)]) == 1


def test_cli_without_pmu():
    """The laptop: no i915 PMU, no /proc: the fdinfo fallback with its reason, lines still written (CPU None)."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "b.jsonl"
        saved = G.open_reader.__defaults__
        G.open_reader.__defaults__ = (str(Path(d) / "nopmu"), str(Path(d) / "noproc"))
        try:
            assert G.main(["--out", str(out), "--interval", "0.05", "--duration", "0.2"]) == 0
        finally:
            G.open_reader.__defaults__ = saved
        lines = [json.loads(x) for x in out.read_text().splitlines()]
        assert lines[0]["gpu_source"] == "drm-fdinfo" and lines[0]["fallback_why"] == "no i915 PMU" and len(lines) >= 3
        out2, real, calls = Path(d) / "c.jsonl", G.record, []        # a pass that raises: logged, started again

        def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(*args, **kw)
        G.record = flaky
        G.open_reader.__defaults__ = (str(Path(d) / "nopmu"), str(Path(d) / "noproc"))
        try:
            assert G.main(["--out", str(out2), "--interval", "0.05", "--duration", "0.3"]) == 0
        finally:
            G.record, G.open_reader.__defaults__ = real, saved
        lines = [json.loads(x) for x in out2.read_text().splitlines()]
        assert lines[0]["error"].startswith("restart: RuntimeError") and lines[1]["header"] and len(calls) == 2, lines



def test_host_state_on_blocks_only():
    """t93 row 14 (levi 00124): cpufreq per core and thermal per zone, only on data lines inside an ON block of the
    epoch clock; no --frame-log, no host key (the record as before)."""
    with tempfile.TemporaryDirectory() as d:
        for c, khz in ((0, 2400000), (1, 800000), (10, 3100000)):
            p = Path(d) / f"cpu/cpu{c}/cpufreq"
            p.mkdir(parents=True)
            (p / "scaling_cur_freq").write_text(f"{khz}\n")
        for z, typ, mc in ((0, "acpitz", 45000), (1, "x86_pkg_temp", 61000)):
            p = Path(d) / f"thermal/thermal_zone{z}"
            p.mkdir(parents=True)
            (p / "type").write_text(typ + "\n")
            (p / "temp").write_text(f"{mc}\n")
        hs = G.host_state(str(Path(d) / "cpu/cpu*/cpufreq/scaling_cur_freq"), str(Path(d) / "thermal/thermal_zone*"))
        assert hs == {"cpu_khz": [2400000, 800000, 3100000], "thermal_mc": {"zone0:acpitz": 45000, "zone1:x86_pkg_temp": 61000}}
        (Path(d) / "cpu/cpu1/cpufreq/scaling_cur_freq").write_text("")          # absent paths: unreadable -> null
        (Path(d) / "thermal/thermal_zone1/temp").unlink()
        hs = G.host_state(str(Path(d) / "cpu/cpu*/cpufreq/scaling_cur_freq"), str(Path(d) / "thermal/thermal_zone*"))
        assert hs["cpu_khz"] == [2400000, None, 3100000] and hs["thermal_mc"]["zone1:x86_pkg_temp"] is None
        assert G.host_state(str(Path(d) / "none/*"), str(Path(d) / "none/*")) == {"cpu_khz": [], "thermal_mc": {}}
    assert (G.frame_log_block("1"), G.frame_log_block("alt60"), G.frame_log_block(None), G.frame_log_block("alt0")) == (0, 60, None, None)
    assert G.frame_log_on(0, 5.0) and G.frame_log_on(60, 65.0) and not G.frame_log_on(60, 125.0) and not G.frame_log_on(None, 65.0)
    slots = lambda types=None: {}
    for fl, want in ((None, [False] * 3), (60, [True, True, False])):     # lines at 60, 61 (block 1 ON), 120 (block 2 OFF)
        ticks = iter([58.0, 60.0, 61.0, 120.0, 121.0])
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "box.jsonl"
            G.record(out, 1.0, duration=62.0, reader=Fake(), clock=lambda: next(ticks), sleep=lambda s: None,
                     slots_fn=slots, fl_block=fl, host_fn=lambda: {"cpu_khz": [1], "thermal_mc": {}})
            lines = [json.loads(x) for x in out.read_text().splitlines()]
            data = [x for x in lines if "gpu_busy_pct" in x]
            assert ["host" in x for x in data] == want, (fl, data)
            assert ("frame_log_block_s" in lines[0]) == (fl is not None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_gpu_record: ok")
