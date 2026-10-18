#!/usr/bin/env python3
"""The collector's experiment-defining constants as a golden (levi's FROZEN_COLLECTOR audit, 2026-10-18: a silent drift
changes the experiment). Every value below must equal fixtures/frozen_constants.json: the T table and the frozen-run
measure (Python and content.js), the stuck rule (T2, rung margin, re-entry windows, recoveries, rung fallbacks), the tt
dead-player rule and its diagnostics, the classes and their switches, the entry rule, the QA checks, the shaper
matrices (tables, low-state caps, initial-state rules), the low-state window, the episodes, the coverage rule and
floors, the per-platform decode path, and the entry-quality exception (Q am. 10; off by default). A deliberate change edits the json in the same commit
(`python3 test_frozen_constants.py --write`) with a FROZEN_COLLECTOR.md change-log row.

    python3 test_frozen_constants.py            (exit 0 = pass)
"""
import json
import re
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:                       # browser.py needs only the name here (as in test_player_dead.py)
    sys.modules["websocket"] = types.SimpleNamespace(WebSocket=object, WebSocketTimeoutException=Exception)
import browser as B  # noqa: E402
import check_session as CS  # noqa: E402
import coverage_report as CR  # noqa: E402
import decode_path as DP  # noqa: E402
import entry_quality as EQ  # noqa: E402
import entries as E  # noqa: E402
import geometry as GEO  # noqa: E402
import orchestrate as O  # noqa: E402
import stuck as S  # noqa: E402
from utils import netStat as NS, shaping as SH  # noqa: E402

GOLDEN = HERE / "fixtures" / "frozen_constants.json"


def _js_consts(names: list[str]) -> dict:
    src = (HERE.parent / "labeling" / "content.js").read_text()
    return {n: json.loads(re.search(rf"^const {n} = ([^;]+);", src, re.M).group(1)) for n in names}


def _matrices(m: dict) -> dict:
    return {k: {"transitions": v[0], "low_state_cap": v[1], "initial_state_rule": v[2]} for k, v in sorted(m.items())}


def current() -> dict:
    episode_s = int(re.search(r"episode_time_in_seconds=(\d+)", (HERE / "exp.py").read_text()).group(1))
    return json.loads(json.dumps({
        "t_table": {"FROZEN_PLAYHEAD_MAX_S": CS.FROZEN_PLAYHEAD_MAX_S, "PLAYER_DEAD_MAX_S": CS.PLAYER_DEAD_MAX_S,
                    "FROZEN_PLAYHEAD_TOL_S": CS.FROZEN_PLAYHEAD_TOL_S},
        "frozen_measure": {"python": {"FROZEN_ADVANCE_TICKS": CS.FROZEN_ADVANCE_TICKS,
                                      "FROZEN_ADV_STEP_S": CS.FROZEN_ADV_STEP_S},
                           "content_js": _js_consts(["FROZEN_TOL_S", "FROZEN_ADVANCE_TICKS", "FROZEN_ADV_MIN_S",
                                                     "FROZEN_ADV_MAX_S", "AD_MAX_DURATION_S",
                                                     "RESOLUTION_POLL_INTERVAL_MS"])},
        "stuck": {"T2_S": S.T2_S, "RUNG_MARGIN": S.RUNG_MARGIN, "REENTRY_CONFIRM_S": S.REENTRY_CONFIRM_S,
                  "RECOVERIES_PER_SESSION": S.RECOVERIES_PER_SESSION, "FALLBACK_RUNG_KBIT": S.FALLBACK_RUNG_KBIT,
                  "TT_REENTRY_CONFIRM_S": B.TT_REENTRY_CONFIRM_S,
                  "TWITCH_160P_TRANSCODE_KBIT": S.TWITCH_160P_TRANSCODE_KBIT},
        "tt_dead": {"PLAYER_DEAD_REENTRIES": B.PLAYER_DEAD_REENTRIES, "TT_AUTOPLAY_GRACE_S": B.TT_AUTOPLAY_GRACE_S,
                    "TT_ENDED_FLAT_S": B.TT_ENDED_FLAT_S, "TT_STALL_GRACE_S": B.TT_STALL_GRACE_S,
                    "TT_RX_IDLE_BYTES": B.TT_RX_IDLE_BYTES, "TT_RX_WINDOW_S": B.TT_RX_WINDOW_S,
                    "TT_LOW_STATES": sorted(B.TT_LOW_STATES),
                    "dead_window": {"PLAYER_DEAD_ALIVE_SHARE": B.PLAYER_DEAD_ALIVE_SHARE,
                                    "PLAYING_REF_S": B.PLAYING_REF_S,
                                    "PLAYING_REF_MIN_PROGRESS": B.PLAYING_REF_MIN_PROGRESS},
                    "MEDIA_SNAPSHOT_MAX_S": B.MEDIA_SNAPSHOT_MAX_S, "LADDER_PROBE_FROZEN_S": B.LADDER_PROBE_FROZEN_S,
                    "LADDER_PROBE_ATTEMPTS": B.LADDER_PROBE_ATTEMPTS, "MENU_OVERLAY_S": CS.MENU_OVERLAY_S},
        "classes": {"CLASS_PRECEDENCE": E.CLASS_PRECEDENCE, "RECOVERED_DEAD_CLASS": E.RECOVERED_DEAD_CLASS,
                    "RECOVERED_FROZEN_CLASS": E.RECOVERED_FROZEN_CLASS, "RECOVERY_SLACK_S": E.RECOVERY_SLACK_S},
        "entries": {"SUSTAIN_WINDOW_S": E.SUSTAIN_WINDOW_S, "SUSTAIN_RUN_S": E.SUSTAIN_RUN_S,
                    "SLOW_START_MAX_STATE": E.SLOW_START_MAX_STATE},
        "qa": {"CHECKS": CS.CHECKS, "STALL_ADVANCING_RATE": CS.STALL_ADVANCING_RATE,
               "STALL_ADVANCING_SHARE_MAX": CS.STALL_ADVANCING_SHARE_MAX},
        "matrices": {"tiktok": _matrices(SH.TIKTOK_MATRICES), "twitch": _matrices(SH.TWITCH_MATRICES),
                     "youtube": _matrices(SH.YOUTUBE_MATRICES)},
        "low_state": {"_LOW_STATE_THRESHOLD": NS._LOW_STATE_THRESHOLD, "_MAX_CONSECUTIVE_LOW": NS._MAX_CONSECUTIVE_LOW,
                      "_MIN_RECOVERY_STATE": NS._MIN_RECOVERY_STATE},
        "episodes": {"EPISODES_V2": O.EPISODES_V2, "episode_s": episode_s},
        "coverage": {"MATRIX_RULE": O.MATRIX_RULE, "MATRIX_IDS": O.MATRIX_IDS, "COVERAGE_ALLOWED": O.COVERAGE_ALLOWED,
                     "RULE_VERSION": CR.RULE_VERSION, "FLOOR_SESSIONS": CR.FLOOR_SESSIONS,
                     "FLOOR_MINUTES": CR.FLOOR_MINUTES, "STALL_FLOOR_ONSETS": CR.STALL_FLOOR_ONSETS,
                     "TT_ENTRY_FLOOR": CR.TT_ENTRY_FLOOR, "EXPLORE_MIN": CR.EXPLORE_MIN,
                     "DEFAULT_MATRIX": CR.DEFAULT_MATRIX, "FLAG_AFTER_SESSIONS": CR.FLAG_AFTER_SESSIONS,
                     "STEERING_RULE": CR.STEERING_RULE, "SESSION_EPISODES": CR.SESSION_EPISODES,
                     "STATE_RATE": CR.STATE_RATE, "RUNG_MARGIN": CR.RUNG_MARGIN,
                     "RUNGS": {p: {c: list(v) for c, v in r.items()} for p, r in CR.RUNGS.items()},
                     "YT_RUNG_CODEC": CR.YT_RUNG_CODEC,
                     "GEOMETRY_FOR_CLASS": {f"{p}/{c}": g for (p, c), g in CR.GEOMETRY_FOR_CLASS.items()}},
        "decode_path": {"PLATFORM_DECODE": DP.PLATFORM_DECODE, "DEFAULT": DP.DEFAULT},
        "geometry": {"GEOMETRY_CHOICES": GEO.GEOMETRY_CHOICES},
        "shot_origin": {"SHOT_CALIB_MARGIN_PX": B.SHOT_CALIB_MARGIN_PX, "SHOT_CALIB_SCALE": B.SHOT_CALIB_SCALE,
                        "SHOT_CALIB_MAX_RESIDUAL": B.SHOT_CALIB_MAX_RESIDUAL, "SHOT_CALIB_MIN_GAP": B.SHOT_CALIB_MIN_GAP},
        "entry_quality": {"ENTRY_QUALITY_PLATFORMS": EQ.ENTRY_QUALITY_PLATFORMS, "ENTRY_RULE": EQ.ENTRY_RULE,
                          "AD_WAIT_S": EQ.AD_WAIT_S, "PICK_HOLD_MAX_S": EQ.PICK_HOLD_MAX_S,
                          "SETTLE_RECORD_S": EQ.SETTLE_RECORD_S, "MIN_OPTIONS": EQ.MIN_OPTIONS,
                          "YT_QUALITY_HEIGHT": EQ.YT_QUALITY_HEIGHT, "orchestrate_default_on": O.ENTRY_QUALITY},
    }))


def _diff(a, b, path="") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in _diff(a.get(k, "<missing>"), b.get(k, "<missing>"), f"{path}.{k}")]
    return [] if a == b else [f"{path}: golden {json.dumps(b)[:120]} != code {json.dumps(a)[:120]}"]


def main() -> int:
    cur = current()
    if "--write" in sys.argv:
        GOLDEN.write_text(json.dumps(cur, indent=1, sort_keys=True) + "\n")
        print(f"wrote {GOLDEN.name}")
        return 0
    bad = _diff(cur, json.loads(GOLDEN.read_text()))
    assert not bad, "constants drifted from fixtures/frozen_constants.json (a change edits the json and the change log):\n  " + "\n  ".join(bad)
    print("test_frozen_constants: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
