"""compute_load.py: the box's compute load during a session, as a covariate (supervisor 2026-10-18).

The collection box also runs analysis jobs (verify / frames / M0 / M1 / baselines) at boundaries and sometimes in the
background. A session overlapping one should carry it. Every SAMPLE_S seconds a daemon thread records the 1-min load
average (/proc/loadavg) and the running non-collector compute jobs (process command lines matching JOB_PATTERNS, the
collector's own processes never match) to <session>/compute_load.jsonl; summary() goes to meta.json `compute_load`.
Per sample, not per tick: labels.csv is written by the extension, and 5 s joins to ticks by timestamp. Linux only;
elsewhere it records {"available": false}.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

SAMPLE_S = 5.0
# non-collector compute: LVQoE analysis entry points and the heavy-job lock (scripts/heavy.sh)
JOB_PATTERNS = re.compile(r"(verify_session\.py|extract_frames\.py|m0_frame_structure\.py|m1_stage1_bound\.py|"
                          r"run_all_baselines\.py|run_baseline_[a-z_]+\.py|cache_inventory\.py|frames_recompute_check\.py|"
                          r"media_label_agreement\.py|build_[a-z_]*cache[a-z_]*\.py|heavy\.sh|compare_preds_roots\.py)")


def load1() -> float | None:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except Exception:
        return None


def compute_jobs(proc: Path = Path("/proc")) -> list[str]:
    """Names of the running non-collector compute jobs (the matched script name per process)."""
    out = []
    for d in proc.iterdir() if proc.exists() else []:
        if not d.name.isdigit():
            continue
        try:
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        except Exception:
            continue
        m = JOB_PATTERNS.search(cmd)
        if m and "bash -c" not in cmd[:10]:
            out.append(m.group(1))
    return sorted(out)


class Sampler:
    def __init__(self, session_dir: Path, sample_s: float = SAMPLE_S, load_fn=load1, jobs_fn=compute_jobs):
        self.path = Path(session_dir) / "compute_load.jsonl"
        self.sample_s, self.load_fn, self.jobs_fn = sample_s, load_fn, jobs_fn
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def sample(self) -> dict:
        jobs = self.jobs_fn()
        row = {"ts": time.time(), "load1": self.load_fn(), "jobs": len(jobs), "job_names": jobs}
        self.rows.append(row)
        try:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except Exception:
            pass
        return row

    def _run(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self.sample_s)

    def start(self) -> "Sampler":
        if self.load_fn() is None:
            return self
        self._t = threading.Thread(target=self._run, name="compute_load", daemon=True)
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=2 * self.sample_s)
        return self.summary()

    def summary(self) -> dict:
        r = [x for x in self.rows if x.get("load1") is not None]
        if not r:
            return {"available": False}
        loads = [x["load1"] for x in r]
        with_jobs = [x for x in r if x["jobs"]]
        names = sorted({n for x in r for n in x["job_names"]})
        return {"available": True, "sample_s": self.sample_s, "samples": len(r),
                "load1_mean": round(sum(loads) / len(loads), 2), "load1_max": round(max(loads), 2),
                "jobs_max": max(x["jobs"] for x in r), "share_with_jobs": round(len(with_jobs) / len(r), 3),
                "job_names": names, "file": self.path.name}
