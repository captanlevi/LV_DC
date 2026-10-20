"""The short per-item dry runs' minimum support per gate (LVQoE restart_2026-10-21_prereg_levi.md, "Short per-item dry
runs", rushi 2026-10-20: 5-10 min of box time; the full-length set per N only at the final tip). A short run is a valid
read only if every gate it claims meets its minimum support; a gate below its minimum reads "insufficient", which is
not a pass, and the full run decides it. Pure functions, so they test without root.

Each check returns {"gate", "minimum", "support", "sufficient"}; status() folds a reader's own pass / fail with it:
a pass below the minimum reads "insufficient"; a fail stays a fail. Per slot (the prereg's table):
  goodput        >= 30 s multi-stream
  capture_drops  >= 60 s with every slot at its top state under bulk
  bulk_ratio     3 states x >= 30 non-straddling seconds each
  fast           >= 30 changes (the @fast change-instant drops)
  walk           a >= 120 s segment, >= 30 changes, >= 20 non-straddling backlogged seconds for the ratio
  player_a       >= 10 s summed qualifying per state, 3 states x >= 60 s; h3-exercised over >= 20 h3 segments
  no_leak        any segment with player traffic on every slot (binary)
  decode_stress  >= 5 min with every slot at its top shaping state and all slots running, at whatever tier the ABR
                 holds (levi, LVQoE prereg 40843f81: the top tier is reported per slot, not required); CPU p95, swap
                 and OOM read over the same seconds
"""
from __future__ import annotations

MINIMUMS = {
    "goodput": {"seconds": 30.0},
    "capture_drops": {"seconds_all_top": 60.0},
    "bulk_ratio": {"states": 3, "non_straddling_s": 30.0},
    "fast": {"changes": 30},
    "walk": {"segment_s": 120.0, "changes": 30, "non_straddling_s": 20.0},
    "player_a": {"states": 3, "state_s": 60.0, "qualifying_s": 10.0, "h3_segments": 20},
    "no_leak": {"slots_with_player_traffic": "every slot"},
    "decode_stress": {"all_top_state_s": 300.0},
}
# The short profile's durations: each minimum with a small margin (trio_dry_run --short). The bulk state's read window
# is the state less its first 3 s and last 0.5 s, and the sampled span inside it is up to ~1 s shorter at each end, so
# 40 s states read >= 30 s; the player's states are levi's 60 s plus 5; the top-state capture segment's 3 s settle is
# outside its 63 s read; @fast is sized from its seeded plan to FAST_CHANGES changes per slot (levi's 30 plus 2).
SHORT = {"goodput_s": 35.0, "ping_s": 10.0, "state_s": 40.0, "player_state_s": 65.0, "top_capture_s": 66.0,
         "fast_changes": 32, "walk_s": 130.0}
FULL = {"goodput_s": 60.0, "ping_s": 20.0, "state_s": 60.0, "player_state_s": None, "top_capture_s": 66.0,
        "fast_changes": None, "walk_s": 0.0}     # levi: the full set runs the capture-drops segment at every N
TOP_SETTLE_S = 3.0
# decode_stress --short's held sessions, base episodes of 60 s: the all-top window opens at the last slot's first shaped
# row (N = 5: the 4 x STAGGER_S starts and tt's ~90 s before its first tc change) and closes at the first slot's end;
# at 975a493's 1/2/2 short read 7 episodes gave a 236.5 s window, so 9 (+120 s) gives ~356 s >= the 300 s minimum
DECODE_SHORT_MINUTES = 9
DECODE_SHORT_START_S = 4 * 30.0 + 90.0     # the window's late start at N = 5 (stagger + tt's lead-in), tested


def _chk(gate: str, support: dict, ok: bool) -> dict:
    return {"gate": gate, "minimum": MINIMUMS[gate], "support": support, "sufficient": bool(ok)}


def goodput(seconds: float | None) -> dict:
    return _chk("goodput", {"seconds": seconds}, (seconds or 0) >= MINIMUMS["goodput"]["seconds"])


def capture_drops(seconds_all_top: float | None) -> dict:
    return _chk("capture_drops", {"seconds_all_top": seconds_all_top},
                (seconds_all_top or 0) >= MINIMUMS["capture_drops"]["seconds_all_top"])


def bulk_ratio(spans_s: list[float | None]) -> dict:
    """One slot's bulk states: each state's sampled, non-straddling span (summarise's _span)."""
    m = MINIMUMS["bulk_ratio"]
    good = [s for s in spans_s if s is not None and s >= m["non_straddling_s"]]
    return _chk("bulk_ratio", {"states": len(spans_s), "states_meeting_s": len(good),
                               "spans_s": [round(s, 1) if s is not None else None for s in spans_s]},
                len(good) >= m["states"])


def fast(changes: int | None) -> dict:
    return _chk("fast", {"changes": changes}, (changes or 0) >= MINIMUMS["fast"]["changes"])


def walk(segment_s: float | None, changes: int | None, non_straddling_s: float | None) -> dict:
    """The walk segment (restart prereg §2, linear rate; trio_dry_run --walk-segment-s): its length, each slot's
    changes and its non-straddling qualifying seconds."""
    m = MINIMUMS["walk"]
    return _chk("walk", {"segment_s": segment_s, "changes": changes, "non_straddling_s": non_straddling_s},
                (segment_s or 0) >= m["segment_s"] and (changes or 0) >= m["changes"]
                and (non_straddling_s or 0) >= m["non_straddling_s"])


def player_a(state_s: float | None, qualifying_s: list[float | None]) -> dict:
    """One slot's player states: the state length and each state's summed qualifying seconds (option A)."""
    m = MINIMUMS["player_a"]
    good = [q for q in qualifying_s if q is not None and q >= m["qualifying_s"]]
    return _chk("player_a", {"state_s": state_s, "states": len(qualifying_s), "states_meeting_qualifying": len(good),
                             "qualifying_s": qualifying_s},
                (state_s or 0) >= m["state_s"] and len(good) >= m["states"])


def h3(attempted_h3_segments: int | None) -> dict:
    return {**_chk("player_a", {"h3_segments": attempted_h3_segments},
                   (attempted_h3_segments or 0) >= MINIMUMS["player_a"]["h3_segments"]), "gate": "h3_exercised"}


def no_leak(tuples_per_slot: dict[str, int]) -> dict:
    return _chk("no_leak", {"player_tuples": dict(tuples_per_slot)},
                bool(tuples_per_slot) and all((n or 0) > 0 for n in tuples_per_slot.values()))


def decode_stress(all_top_state_s: float | None) -> dict:
    """The seconds with every slot at its top shaping state and every slot's session running (levi, prereg 40843f81:
    at whatever tier the ABR holds; was >= 5 min at every slot's top tier)."""
    return _chk("decode_stress", {"all_top_state_s": all_top_state_s},
                (all_top_state_s or 0) >= MINIMUMS["decode_stress"]["all_top_state_s"])


def status(reading: str | bool | None, chk: dict) -> str:
    """A reader's verdict (True / "pass", False / "fail..." , anything else as given) folded with its support check:
    a pass below the minimum reads "insufficient", never "pass"; a fail stays a fail at any support (a fault seen in a
    short run is not hidden behind its length)."""
    if reading is False or (isinstance(reading, str) and reading.startswith("fail")):
        return "fail"
    if not chk["sufficient"]:
        return "insufficient"
    if reading is True or reading == "pass":
        return "pass"
    return str(reading)


def combine(statuses: list[str]) -> str:
    """Over slots: any fail is a fail; else any insufficient (or unread) is insufficient; else pass."""
    if not statuses:
        return "insufficient"
    if any(s.startswith("fail") for s in statuses):
        return "fail"
    return "pass" if all(s == "pass" for s in statuses) else "insufficient"


def fast_seconds(plans: dict[str, list[dict]], changes: int) -> float:
    """The @fast segment length that gives every slot `changes` changes: plans are each slot's seeded plan over a long
    horizon ([{t, ...}], index 0 the initial state, change i at plans[s][i]["t"]); a shorter segment's plan is a prefix
    of it (fast_segment.schedule stops at the first step at or past the end). Returns the last needed change's time + 2 s
    (its settle snapshot and one sample after it)."""
    need = []
    for s, p in plans.items():
        if len(p) <= changes:
            raise ValueError(f"{s}: the plan holds {len(p) - 1} changes < {changes}; extend its horizon")
        need.append(p[changes]["t"])
    return round(max(need) + 2.0, 1)


def short_total_s(d: dict, n_states: int, phases: dict) -> dict:
    """The short form's box seconds per phase (nominal: no tc / sampling overhead), and the total."""
    out = {}
    if phases.get("goodput"):
        out["goodput"] = d["goodput_s"]
    if phases.get("idle_ping"):
        out["idle_ping"] = n_states * (d["ping_s"] + 2.0)
    out["bulk"] = n_states * d["state_s"]
    if phases.get("top_capture"):
        out["top_capture"] = d["top_capture_s"] + TOP_SETTLE_S
    if phases.get("fast"):
        out["fast"] = float(phases["fast"]) + 3.0
    if phases.get("player"):
        out["player"] = n_states * d["player_state_s"]
    out["total"] = round(sum(out.values()), 1)
    return out
