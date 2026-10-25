#!/usr/bin/env python3
"""The collector's experiment-defining constants as a golden (levi's FROZEN_COLLECTOR audit, 2026-10-18: a silent drift
changes the experiment). Every value below must equal fixtures/frozen_constants.json: the T table and the frozen-run
measure (Python and content.js), the stuck rule (T2, rung margin, re-entry windows, recoveries, rung fallbacks), the tt
dead-player rule and its diagnostics, the classes and their switches, the entry rule, the QA checks, the shaper
matrices (tables, low-state caps, initial-state rules), the low-state window, the episodes, the coverage rule and
floors, the per-platform decode path, the entry-quality exception (Q am. 10; off by default), the twitch
category floor (restart prereg §5) and the concurrent path's constants (brad's t58 F-2). A deliberate change edits the json in the same commit
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
import offloads as OFF  # noqa: E402
import record as REC  # noqa: E402
import schedule_occupancy as SO  # noqa: E402
import yt_channels as YT  # noqa: E402
import channel_cap as CC  # noqa: E402
import n_draw as ND  # noqa: E402
import tw_categories as TC  # noqa: E402
import gpu_record as GR  # noqa: E402
import not_usable as NU  # noqa: E402
import trio_slots as TS  # noqa: E402
import tw_latency as TL  # noqa: E402
import orchestrate as O  # noqa: E402
import stuck as S  # noqa: E402
import walk_simulation as WS  # noqa: E402
from utils import netStat as NS, shaping as SH  # noqa: E402

GOLDEN = HERE / "fixtures" / "frozen_constants.json"


def _js_consts(names: list[str]) -> dict:
    src = (HERE.parent / "labeling" / "content.js").read_text()
    return {n: json.loads(re.search(rf"^const {n} = ([^;]+);", src, re.M).group(1)) for n in names}


def _matrices(m: dict) -> dict:
    return {k: {"transitions": v[0], "low_state_cap": v[1], "initial_state_rule": v[2]} for k, v in sorted(m.items())}


def current() -> dict:
    # Q am. 17: exp.py runs the matrix's schedule (shaping.schedule_of): the base episode is shaping.BASE_EPISODE_S
    assert 'episode_time_in_seconds=SCHEDULE["episode_s"]' in (HERE / "exp.py").read_text()
    episode_s = SH.BASE_EPISODE_S
    return json.loads(json.dumps({
        "t_table": {"FROZEN_PLAYHEAD_MAX_S": CS.FROZEN_PLAYHEAD_MAX_S, "PLAYER_DEAD_MAX_S": CS.PLAYER_DEAD_MAX_S,
                    "FROZEN_PLAYHEAD_TOL_S": CS.FROZEN_PLAYHEAD_TOL_S},
        # addendum N's stall-only run (levi 2026-10-19: the 2026-10-18 10-tick measure reverted): its only constant is
        # the 1 s playhead tolerance (t_table's FROZEN_PLAYHEAD_TOL_S, content.js's FROZEN_TOL_S); plus the loop covariate
        # WA2 r1 (levi 00:28 / 00:30): plus the frozen-PICTURE run (clock advancing, <= FROZEN_FRAMES_MAX decoded frames)
        "frozen_measure": {"python": {"definition": "addendum N stall-only run + frozen-picture run (WA2 r1)",
                                      "STALL_LOOP_PLAY_MAX_S": CS.STALL_LOOP_PLAY_MAX_S,
                                      "FROZEN_FRAMES_MAX": CS.FROZEN_FRAMES_MAX},
                           "content_js": _js_consts(["FROZEN_TOL_S", "FROZEN_FRAMES_MAX", "AD_MAX_DURATION_S",
                                                     "RESOLUTION_POLL_INTERVAL_MS"])},
        "stuck": {"T2_S": S.T2_S, "RUNG_MARGIN": S.RUNG_MARGIN, "REENTRY_CONFIRM_S": S.REENTRY_CONFIRM_S,
                  "RECOVERIES_PER_SESSION": S.RECOVERIES_PER_SESSION, "FALLBACK_RUNG_KBIT": S.FALLBACK_RUNG_KBIT,
                  "TT_REENTRY_CONFIRM_S": B.TT_REENTRY_CONFIRM_S, "CONFIRM_MIN_STEP": B.CONFIRM_MIN_STEP,
                  "TWITCH_160P_TRANSCODE_KBIT": S.TWITCH_160P_TRANSCODE_KBIT},
        "yt_tw_low_hold": {"LOW_HOLD_STATES": sorted(S.LOW_HOLD_STATES), "LOW_HOLD_GRACE_S": S.LOW_HOLD_GRACE_S,
                           "LOWSTATE_FREE_MAX": B.LOWSTATE_FREE_MAX},
        "stream_ended": {"STREAM_RESET_DROP_S": B.STREAM_RESET_DROP_S, "STREAM_RESET_TO_S": B.STREAM_RESET_TO_S,
                         "STREAM_FIXED_N": B.STREAM_FIXED_N, "STREAM_FIXED_TOL_S": B.STREAM_FIXED_TOL_S,
                         "STREAM_VOD_PLAY_S": B.STREAM_VOD_PLAY_S, "STREAM_VOD_WITHIN": B.STREAM_VOD_WITHIN,
                         "STREAM_ENDED_PLATFORMS": list(B.STREAM_ENDED_PLATFORMS)},
        "tt_dead": {"PLAYER_DEAD_REENTRIES": B.PLAYER_DEAD_REENTRIES, "TT_AUTOPLAY_GRACE_S": B.TT_AUTOPLAY_GRACE_S,
                    "TT_ENDED_FLAT_S": B.TT_ENDED_FLAT_S, "TT_STALL_GRACE_S": B.TT_STALL_GRACE_S,
                    "TT_RX_IDLE_BYTES": B.TT_RX_IDLE_BYTES, "TT_RX_WINDOW_S": B.TT_RX_WINDOW_S,
                    "TT_LOW_STATES": sorted(B.TT_LOW_STATES),
                    "dead_window": {"PLAYER_DEAD_ALIVE_SHARE": B.PLAYER_DEAD_ALIVE_SHARE,
                                    "PLAYING_REF_S": B.PLAYING_REF_S,
                                    "PLAYING_REF_MIN_PROGRESS": B.PLAYING_REF_MIN_PROGRESS},
                    "MEDIA_SNAPSHOT_MAX_S": B.MEDIA_SNAPSHOT_MAX_S, "LADDER_PROBE_FROZEN_S": B.LADDER_PROBE_FROZEN_S,
                    "LADDER_PROBE_ATTEMPTS": B.LADDER_PROBE_ATTEMPTS, "MENU_OVERLAY_S": CS.MENU_OVERLAY_S},
        "player_error": {"ERROR_PAGE_CONFIRM_S": B.ERROR_PAGE_CONFIRM_S, "AD_NO_PLAYER_MAX_S": B.AD_NO_PLAYER_MAX_S,
                         "AD_FROZEN_MAX_S": B.AD_FROZEN_MAX_S, "AD_FROZEN_TOL_S": B.AD_FROZEN_TOL_S,
                         "PLAYER_ERROR_PLATFORMS": B.PLAYER_ERROR_PLATFORMS},
        "join": {"JOIN_BASE_S": B.JOIN_BASE_S, "JOIN_CAP_S": B.JOIN_CAP_S, "JOIN_BYTES": B.JOIN_BYTES,     # B9
                 "JOIN_RX_MIN_BYTES": B.JOIN_RX_MIN_BYTES,
                 "JOIN_TIMEOUT_PREFIX": B.JOIN_TIMEOUT_PREFIX, "JOIN_TIMEOUT_MARK": E.JOIN_TIMEOUT_MARK},
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
        # restart prereg 2026-10-21 §2: the walk in linear rate and the per-session delay / loss factor
        "shaping_walk": {"WALK_RULE": NS.WALK_RULE, "WALK_STEP_S": NS.WALK_STEP_S, "WALK_LAST_MIN_S": NS.WALK_LAST_MIN_S,
                         "WALK_SPAN_SHARE": NS.WALK_SPAN_SHARE, "WALK_REF_STEPS": NS.WALK_REF_STEPS,
                         "WALK_SIGMA_PER_BAND": NS.WALK_SIGMA_PER_BAND, "DL_FACTOR_RULE": NS.DL_FACTOR_RULE,
                         "DL_FACTOR_LO": NS.DL_FACTOR_LO, "DL_FACTOR_HI": NS.DL_FACTOR_HI,
                         "SHAPING_SEED": NS.SHAPING_SEED, "WALK_SIM_SHA256": WS.WALK_SIM_SHA256,
                         "WALK_SIM_SEED": WS.SIM_SEED, "WALK_SIM_SESSIONS": WS.SIM_SESSIONS},
        "episodes": {"EPISODES_V2": O.EPISODES_V2, "episode_s": episode_s},
        # the restart (prereg §1 / §4): N per round and the session's length
        "n_draw": {"N_SUPPORT": list(ND.N_SUPPORT), "N_WEIGHTS": {str(k): v for k, v in ND.N_WEIGHTS.items()},
                   "N_DRAW_SEED": ND.N_DRAW_SEED, "ROTATION": list(ND.ROTATION), "ROTATING": list(ND.ROTATING),
                   "N_DRAW_RULE": ND.N_DRAW_RULE},
        "session_length": {"SESSION_MINUTES": list(SH.SESSION_MINUTES), "SESSION_LEN_SEED": SH.SESSION_LEN_SEED,
                           "SESSION_LEN_RULE": SH.SESSION_LEN_RULE, "FAST_EPISODE_S": list(SH.FAST_EPISODE_S),
                           "FAST_EPISODE_RULE": SH.FAST_EPISODE_RULE},
        "abr_only": {"ABR_ONLY": [list(x) for x in CR.ABR_ONLY], "ABR_ONLY_RULE": CR.ABR_ONLY_RULE},
        "qualification": {"EXPECTED_SESSION_MINUTES": CR.EXPECTED_SESSION_MINUTES},
        "windows": {"RECORD_WINDOW_MIN": O.RECORD_WINDOW_MIN, "WINDOW_ALLOWANCE_S": O.WINDOW_ALLOWANCE_S,
                    "EXIT_WINDOW_READ": O.EXIT_WINDOW_READ},
        "schedules": {"SCHEDULES": {k: {**v, "redraw_s": list(v["redraw_s"])} for k, v in SH.SCHEDULES.items()},
                      "FAMILY_BASE": SH.FAMILY_BASE, "FAMILY_FLOOR": CR.FAMILY_FLOOR, "DEFICIT_RANK": CR.DEFICIT_RANK,
                      "DEFICIT_TIE_SEED": CR.DEFICIT_TIE_SEED, "DEFICIT_TIE_RULE": CR.DEFICIT_TIE_RULE,
                      "CHANNEL_TIE_SEED": YT.CHANNEL_TIE_SEED, "DIVERSITY_RULE": YT.DIVERSITY_RULE,
                      "CHANNEL_FLOOR": YT.CHANNEL_FLOOR, "CHANNEL_CAP_MULT": YT.CHANNEL_CAP_MULT,
                      "TW_TT_CAP_SHARE": CC.CAP_SHARE, "TW_TT_CAP_MIN": CC.CAP_MIN, "TW_TT_CAP_RULE": CC.CAP_RULE,
                      "TW_CATEGORIES": TC.TW_CATEGORIES, "TW_CATEGORY_FLOOR": TC.CATEGORY_FLOOR,
                      "TW_CATEGORY_HOLD_AT": TC.CATEGORY_HOLD_AT, "TW_CATEGORY_TIE_SEED": TC.CATEGORY_TIE_SEED,
                      "TW_CATEGORY_RULE": TC.CATEGORY_RULE,
                      "OCCUPANCY_SHA256": SO.OCCUPANCY_SHA256, "SIM_SEED": SO.SIM_SEED, "SIM_SESSIONS": SO.SIM_SESSIONS},
        "coverage": {"MATRIX_RULE": O.MATRIX_RULE, "MATRIX_IDS": O.MATRIX_IDS, "COVERAGE_ALLOWED": O.COVERAGE_ALLOWED,
                     "RULE_VERSION": CR.RULE_VERSION, "FLOOR_SESSIONS": CR.FLOOR_SESSIONS,
                     "FLOOR_MINUTES": CR.FLOOR_MINUTES, "STALL_FLOOR_ONSETS": CR.STALL_FLOOR_ONSETS,
                     "TT_ENTRY_FLOOR": CR.TT_ENTRY_FLOOR, "EXPLORE_MIN": CR.EXPLORE_MIN,
                     "EXPLORE_RULE": CR.EXPLORE_RULE, "EXPLORE_SEED": CR.EXPLORE_SEED,
                     "EXPLORE_REPEAT_CAP": CR.EXPLORE_REPEAT_CAP,
                     "DEFAULT_MATRIX": CR.DEFAULT_MATRIX, "FLAG_AFTER_SESSIONS": CR.FLAG_AFTER_SESSIONS,
                     "STEERING_RULE": CR.STEERING_RULE, "SESSION_EPISODES": CR.SESSION_EPISODES,
                     "STATE_RATE": CR.STATE_RATE, "RUNG_MARGIN": CR.RUNG_MARGIN,
                     "RUNGS": {p: {c: list(v) for c, v in r.items()} for p, r in CR.RUNGS.items()},
                     "YT_RUNG_CODEC": CR.YT_RUNG_CODEC, "TT_RUNG_REGIME": CR.TT_RUNG_REGIME,
                     "GEOMETRY_FOR_CLASS": {f"{p}/{c}": g for (p, c), g in CR.GEOMETRY_FOR_CLASS.items()},
                     "INITIAL_STATE_RULE": CR.INITIAL_STATE_RULE,
                     "LOW_YIELD_MIN_PER_SESSION": CR.LOW_YIELD_MIN_PER_SESSION,
                     "LOW_YIELD_MIN_SESSIONS": CR.LOW_YIELD_MIN_SESSIONS,
                     "MATRIX_TARGETS": {f"{p}/{m}": list(v) for (p, m), v in CR.MATRIX_TARGETS.items()}},
        "decode_path": {"PLATFORM_DECODE": DP.PLATFORM_DECODE, "DEFAULT": DP.DEFAULT},
        "geometry": {"GEOMETRY_CHOICES": GEO.GEOMETRY_CHOICES, "MODE_CHROME_ARGS": GEO.MODE_CHROME_ARGS,
                     "DRAW_RULE": GEO.DRAW_RULE, "DRAW_WEIGHTS": GEO.DRAW_WEIGHTS,
                     "SESSION_DRAW_SEED": O.SESSION_DRAW_SEED, "GEOMETRY_UNCOUPLED": CR.GEOMETRY_UNCOUPLED,
                     "INSTRUCTION": GEO.INSTRUCTION, "LEGACY_NAME": GEO.LEGACY_NAME,          # t67 B1
                     "YT_TIER_CAP": {f"{m}@{d}": c for (m, d), c in GEO.YT_TIER_CAP.items()}},
        "tw_low_latency": {"KEY": TL.KEY, "P_ON": TL.P_ON, "RULE": TL.RULE, "MODE_ON": TL.MODE_ON,
                           "MODE_OFF": TL.MODE_OFF, "READ_TRIES": TL.READ_TRIES, "READ_GAP_S": TL.READ_GAP_S},
        # brad's t58 F-2 (levi: pins, any time): the concurrent path's constants, unpinned before
        "concurrent": {"IFB_OFF": OFF.IFB_OFF, "TRIO_STAGGER_S": O.TRIO_STAGGER_S, "TC_SAMPLE_S": TS.TC_SAMPLE_S,
                       "IW_EVERY": TS.IW_EVERY, "FLAGGED_SHARE_QA": TS.FLAGGED_SHARE_QA, "TCPDUMP_B_KIB": TS.TCPDUMP_B_KIB,
                       "SWEEPER": {"FAST_S": TS.CgroupSweeper.FAST_S, "FAST_EVERY_S": TS.CgroupSweeper.FAST_EVERY_S,
                                   "SLOW_EVERY_S": TS.CgroupSweeper.SLOW_EVERY_S},
                       "GPU_GAP_FACTOR": GR.GAP_FACTOR, "NOT_USABLE_FAILURES": NU.NOT_USABLE_FAILURES,
                       "resume": {"SELF_PAUSE_HOLD_S": B.SELF_PAUSE_HOLD_S, "SELF_PAUSE_MIN_BUFFER_S": B.SELF_PAUSE_MIN_BUFFER_S,
                                  "RESUME_BACKOFF_S": B.RESUME_BACKOFF_S, "RESUME_CHECK_S": B.RESUME_CHECK_S,
                                  "RESUME_RULE": B.RESUME_RULE, "PAUSE_DEADLOCK_S": B.PAUSE_DEADLOCK_S,
                                  "PAUSE_GROWTH_EPS_S": B.PAUSE_GROWTH_EPS_S,
                                  "FROZEN_HOLD_POLL_MAX_AGE_S": B.FROZEN_HOLD_POLL_MAX_AGE_S,
                                  "FROZEN_HOLD_RULE": B.FROZEN_HOLD_RULE}},
        "capture_offloads": {"OFF": OFF.OFF}, "record_era": REC.ERA, "record_eras": list(REC.RECORD_ERAS), "record_lines": dict(REC.RECORD_LINES), "record_boundary": REC.RECORD_BOUNDARY, "stated_eras": list(REC.STATED_ERAS),
        "shot_origin": {"SHOT_CALIB_MARGIN_PX": B.SHOT_CALIB_MARGIN_PX, "SHOT_CALIB_SCALE": B.SHOT_CALIB_SCALE,
                        "SHOT_CALIB_MAX_RESIDUAL": B.SHOT_CALIB_MAX_RESIDUAL, "SHOT_CALIB_MIN_GAP": B.SHOT_CALIB_MIN_GAP},
        # v3 shots (levi 16:57; WA2 r1 levi 00:53): the cadence and the event-shot gap (Python and content.js)
        "shots_v3": {"SHOT_INTERVAL_S": B.SHOT_INTERVAL_S, "SHOT_EVENT_MIN_GAP_S": B.SHOT_EVENT_MIN_GAP_S,
                     "SHOT_GRID": list(B.SHOT_GRID),   # #8' (levi 88410f0b): the occlusion grid, cols x rows
                     "SHOT_VIDEO_CELL_MAX_UP": B.SHOT_VIDEO_CELL_MAX_UP, "SHOT_VIDEO_CELL_RULE": B.SHOT_VIDEO_CELL_RULE,   # levi 9048534c
                     "content_js": _js_consts(["SHOT_EVENT_MIN_GAP_S"])},
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


def test_frozen_constants():
    assert main() == 0


def test_low_state_max_matches_netstat():
    """check_session's low-state run (QA) and the shaper's low-state cap (utils/netStat) name the same low states: two
    copies of one number (code-health audit A4), the golden pins each but not that they agree."""
    assert CS.LOW_STATE_MAX == NS._LOW_STATE_THRESHOLD


if __name__ == "__main__":
    test_low_state_max_matches_netstat()
    sys.exit(main())
