"""lever.py: the v3 STALL lever (t67 B4; prereg b6bc9246, levi's interface 19:29). In a lever window only (B5's stratum),
the shaper's band drops below the session's lowest offered rung for d seconds, then the drawn schedule resumes.

- Episodes: onsets a Poisson process over session time at rate lambda_per_s, durations d ~ U[d_lo_s, d_hi_s], the whole
  plan drawn at session start from random.Random("lever:<session name>"): never tied to elapsed state, the player or
  the labels (an onset inside a running episode is skipped and counted).
- Rate: RATE_SHARE x the session's lowest offered rung as read at the episode's onset (browser._current_rung: twitch's
  HLS master, yt / tt page JS); FALLBACK_KBIT when only the fallback rung is known (logged per episode as fallback).
- The episode replaces only the applied rate (a step's delay / loss stay the state's); net_labels rows carry its id.
- meta.json lever: the parameters as passed, the plan, each episode's rate and its source, and the session's abort
  if it fell inside an episode (aborts_in_episode; the calibration requires 0).
"""
from __future__ import annotations

import random

RATE_SHARE = 0.5
# 0.5 x stuck.FALLBACK_RUNG_KBIT (yt 130, twitch 230, tiktok 1800): only when the session's own rung is not read
FALLBACK_KBIT = {"youtube": 65, "twitch": 115, "tiktok": 900}
ABORT_GRACE_S = 30.0            # an abort this long after an episode's end still counts as the episode's


def plan(session_name: str, session_s: float, lambda_per_s: float, d_lo_s: float, d_hi_s: float) -> dict:
    if not (lambda_per_s > 0 and 0 < d_lo_s <= d_hi_s and session_s > 0):
        raise ValueError(f"lever: lambda {lambda_per_s}, d [{d_lo_s}, {d_hi_s}], session {session_s} s")
    rng = random.Random(f"lever:{session_name}")
    eps, skipped, t, end = [], 0, 0.0, -1.0
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
    """The session's lever: the plan, its clock (t0 = the shaper's first apply) and each episode's rate, read once."""

    def __init__(self, platform: str, session_name: str, session_s: float, params: dict, rung_fn=None):
        self.platform, self.params, self.rung_fn = platform, dict(params), rung_fn
        p = plan(session_name, session_s, float(params["lambda_per_s"]), float(params["d_lo_s"]),
                 float(params["d_hi_s"]))
        self.episodes, self.skipped = p["episodes"], p["skipped_overlapping"]
        self.t0: float | None = None
        self.rates: dict[int, dict] = {}

    def active(self, now: float) -> dict | None:
        if self.t0 is None:
            self.t0 = now
        t = now - self.t0
        for ep in self.episodes:
            if ep["onset_s"] <= t < ep["onset_s"] + ep["d_s"]:
                return ep
        return None

    def rate_for(self, ep: dict, now: float) -> int:
        if ep["id"] not in self.rates:
            rung = None
            try:
                rung = self.rung_fn() if self.rung_fn else None
            except Exception as exc:                         # never fatal: the fallback, stated
                rung = {"source": "fallback", "error": repr(exc)[:120]}
            kbit, src = rate_kbit(self.platform, rung)
            self.rates[ep["id"]] = {"kbit": kbit, "source": src, "first_applied_t": now,
                                    "rung": {k: rung.get(k) for k in ("kbit", "source")} if rung else None}
        return self.rates[ep["id"]]["kbit"]

    def record(self, abort_at: float | None = None, abort_reason: str | None = None) -> dict:
        hit = None
        if abort_at is not None and self.t0 is not None:
            t = abort_at - self.t0
            hit = next((ep["id"] for ep in self.episodes if ep["onset_s"] <= t <= ep["onset_s"] + ep["d_s"] + ABORT_GRACE_S),
                       None)
        return {"params": self.params, "rate_share": RATE_SHARE, "fallback_kbit": FALLBACK_KBIT[self.platform],
                "t0": self.t0, "episodes": self.episodes, "skipped_overlapping": self.skipped,
                "rates": {str(k): v for k, v in self.rates.items()}, "abort_reason": abort_reason,
                "aborts_in_episode": 1 if hit is not None else 0, "abort_episode": hit,
                "abort_grace_s": ABORT_GRACE_S}
