#!/usr/bin/env python3
"""compute_load.py (supervisor 2026-10-18): the job matcher on a fake /proc (analysis jobs counted; collector processes,
shell one-liners and unrelated processes not), the sampler's jsonl + summary on stubbed readers, and the unavailable
case. No real /proc needed.

    python3 test_compute_load.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import compute_load as C  # noqa: E402


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    return bool(cond)


def main():
    ok = True
    with tempfile.TemporaryDirectory() as td:
        proc = Path(td) / "proc"
        procs = {
            "101": "/home/u/LV/.venv/bin/python verify_session.py /data/youtube/yt_3 --ingest",
            "102": ".venv/bin/python extract_frames.py /data/twitch/tw_2",
            "103": "../.venv/bin/python exp.py --platform youtube --episodes 21 --auto",       # the collector
            "104": "../.venv/bin/python -u orchestrate.py --batch youtube=100",               # the collector
            "105": "bash -c cd /x && python verify_session.py /data/a",                        # a shell one-liner
            "106": "/usr/bin/google-chrome --remote-debugging-port=9222",
            "107": "/bin/bash /home/u/LV/scripts/heavy.sh brad python scripts/run_all_baselines.py --queue q",
        }
        for pid, cmd in procs.items():
            (proc / pid).mkdir(parents=True)
            (proc / pid / "cmdline").write_bytes(cmd.replace(" ", "\0").encode())
        (proc / "self").mkdir()
        jobs = C.compute_jobs(proc)
        ok &= check(jobs == ["extract_frames.py", "heavy.sh", "verify_session.py"], f"jobs matched: {jobs}")

        sd = Path(td) / "session"
        sd.mkdir()
        loads = iter([0.5, 2.5, 1.0])
        jl = iter([[], ["verify_session.py"], []])
        s = C.Sampler(sd, sample_s=0.01, load_fn=lambda: next(loads, 1.0), jobs_fn=lambda: next(jl, []))
        for _ in range(3):
            s.sample()
        summ = s.summary()
        ok &= check(summ["samples"] == 3 and summ["jobs_max"] == 1 and summ["share_with_jobs"] == 0.333
                    and summ["load1_max"] == 2.5 and summ["job_names"] == ["verify_session.py"], f"summary: {summ}")
        rows = [json.loads(l) for l in (sd / "compute_load.jsonl").read_text().splitlines()]
        ok &= check(len(rows) == 3 and rows[1]["jobs"] == 1, "jsonl rows written")
        u = C.Sampler(sd, load_fn=lambda: None).start()
        ok &= check(u.stop() == {"available": False}, "no /proc/loadavg -> available false, no thread")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
