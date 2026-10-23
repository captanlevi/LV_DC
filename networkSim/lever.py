"""lever.py: the v3 STALL lever (t67 B4; prereg b6bc9246, levi's interface 19:29). In a lever window only (B5's stratum),
the shaper's band drops below the session's lowest offered rung for d seconds, then the drawn schedule resumes.

- Clock: t0 = the shaper's first apply (levi 19:58); onset_s counts from it. Episodes: onsets a Poisson process from
  FIRST_ONSET_MIN_S on (levi 19:57: the rung is fixed before
  any episode) at rate lambda_per_s, durations d ~ U[d_lo_s, d_hi_s], the whole
  plan drawn at session start from random.Random("lever:<session name>"): never tied to elapsed state, the player or
  the labels (an onset inside a running episode is skipped and counted).
- Rate: RATE_SHARE x the session's lowest offered rung, fixed ONCE for every episode (levi 19:49 / 19:57): exp.py reads
  it after playback started (browser.start returns then) and, while only the fallback is known, retries off the shaper
  path until the first onset (the first read on exp.py's main thread, the retries on a daemon thread; browser._current_rung: twitch's HLS master, yt / tt page JS). The shaper path makes no
  live player read. FALLBACK_KBIT when no offer was read by the first onset (noted in meta.json lever.rate).
- The episode replaces only the applied rate (a step's delay / loss stay the state's); net_labels rows carry its id.
- meta.json lever: the parameters as passed, the plan, t0, fix_t, rung_source, fix_margin_s = t0 + first onset - fix_t
  (the calibration requires > 0 where rung_source is an offer), late_reads_s, each episode's first apply and the
  planned episodes never applied (an episode applies only when a shaper item starts inside it: items are the walk's
  sub-steps, < WALK_STEP_S + WALK_LAST_MIN_S = 3 s), and the session's abort if it fell inside an episode
  (aborts_in_episode; the calibration requires 0).
"""
from __future__ import annotations

import random
import threading

RATE_SHARE = 0.5
FIRST_ONSET_MIN_S = 60.0        # levi 19:57: no episode before the rung is fixed
# 0.5 x stuck.FALLBACK_RUNG_KBIT (yt 130, twitch 230, tiktok 1800): only when the session's own rung is not read
FALLBACK_KBIT = {"youtube": 65, "twitch": 115, "tiktok": 900}
ABORT_GRACE_S = 30.0            # an abort this long after an episode's end still counts as the episode's


def plan(session_name: str, session_s: float, lambda_per_s: float, d_lo_s: float, d_hi_s: float) -> dict:
    if not (lambda_per_s > 0 and 0 < d_lo_s <= d_hi_s and session_s > 0):
        raise ValueError(f"lever: lambda {lambda_per_s}, d [{d_lo_s}, {d_hi_s}], session {session_s} s")
    rng = random.Random(f"lever:{session_name}")
    eps, skipped, t, end = [], 0, FIRST_ONSET_MIN_S, -1.0
    while True:
        t += rng.expovariate(lambda_per_s)
        if t >= session_s:
            break
        d = rng.uniform(d_lo_s, d_hi_s)
        if t < end:                              # inside the running episode: no overlap
            skipped += 1
            continue
        eps.append({"id": len(eps) + 1, "onset_s": round(t, 3), "d_s": round(d, 3)})
        end = t + d
    return {"episodes": eps, "skipped_overlapping": skipped}


def rate_kbit(platform: str, rung: dict | None) -> tuple[int, str]:
    """(kbit, source): RATE_SHARE x the session's lowest rung when read from the stream, else the platform fallback."""
    if rung and rung.get("kbit") and rung.get("source") not in (None, "fallback"):
        return max(1, int(round(RATE_SHARE * float(rung["kbit"])))), str(rung["source"])
    return FALLBACK_KBIT[platform], "fallback"


class Lever:
    """The session's lever: the plan, its clock (t0 = the shaper's first apply) and the one rate every episode applies."""

    def __init__(self, platform: str, session_name: str, session_s: float, params: dict):
        self.platform, self.params = platform, dict(params)
        p = plan(session_name, session_s, float(params["lambda_per_s"]), float(params["d_lo_s"]),
                 float(params["d_hi_s"]))
        self.episodes, self.skipped = p["episodes"], p["skipped_overlapping"]
        self.t0: float | None = None
        self.rate: dict | None = None
        self.first_applied: dict[int, float] = {}
        self.late_reads: list[float] = []                     # offers read past the deadline, never applied (s late)
        self._lock = threading.Lock()                          # the start reader and the shaper may race to fix it

    @property
    def first_onset_s(self) -> float | None:
        return self.episodes[0]["onset_s"] if self.episodes else None

    def fix_rate(self, rung: dict | None, at: float, note: str | None = None,
                 deadline: float | None = None) -> dict | None:
        """Once per session: the rate from a rung already read (a dict, never a callable; nothing is read here). The
        first fix wins; a later one returns None. `at` is taken after the read returned: a fix at or past `deadline`
        (the start reader's, 2 s before the first onset; brad 20:06: a slow read can return late) is refused and
        counted, so a reader's fix always precedes every episode."""
        with self._lock:
            if deadline is not None and at >= deadline:
                self.late_reads.append(round(at - deadline, 3))
                return None
            if self.rate is not None:
                return None
            kbit, src = rate_kbit(self.platform, rung)
            self.rate = {"kbit": kbit, "source": src, "at": at, "note": note,
                         "rung": {k: rung.get(k) for k in ("kbit", "source", "error")} if rung else None}
            return self.rate

    def active(self, now: float) -> dict | None:
        if self.t0 is None:
            self.t0 = now
        t = now - self.t0
        for ep in self.episodes:
            if ep["onset_s"] <= t < ep["onset_s"] + ep["d_s"]:
                return ep
        return None

    def rate_for(self, ep: dict, now: float) -> int:
        if self.rate is None:                                  # no offer read by the first onset: the fallback, stated
            self.fix_rate(None, now, note="no offer read by the first onset; fallback")
        self.first_applied.setdefault(ep["id"], now)
        return self.rate["kbit"]

    def record(self, abort_at: float | None = None, abort_reason: str | None = None) -> dict:
        hit = None
        if abort_at is not None and self.t0 is not None:
            t = abort_at - self.t0
            hit = next((ep["id"] for ep in self.episodes if ep["onset_s"] <= t <= ep["onset_s"] + ep["d_s"] + ABORT_GRACE_S),
                       None)
        r = self.rate or {}
        return {"params": self.params, "rate_share": RATE_SHARE, "fallback_kbit": FALLBACK_KBIT[self.platform],
                "first_onset_min_s": FIRST_ONSET_MIN_S, "t0": self.t0, "fix_t": r.get("at"),
                "fix_margin_s": (round((self.t0 + self.first_onset_s) - r["at"], 3)   # levi 20:06: > 0 required
                                            if r.get("at") is not None and self.t0 is not None and self.episodes
                                            else None),
                "late_reads_s": self.late_reads,
                "rung_source": r.get("source"), "episodes": self.episodes, "skipped_overlapping": self.skipped,
                "rate": self.rate, "first_applied": {str(k): v for k, v in self.first_applied.items()},
                "episodes_never_applied": [e["id"] for e in self.episodes if e["id"] not in self.first_applied],
                "abort_reason": abort_reason,
                "aborts_in_episode": 1 if hit is not None else 0, "abort_episode": hit,
                "abort_grace_s": ABORT_GRACE_S}
