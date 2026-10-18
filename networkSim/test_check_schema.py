#!/usr/bin/env python3
"""check.json's schema and the one-recovery revocation (levi's FROZEN_COLLECTOR audit, 2026-10-18). LVQoE's ingest
reads check.json, so its keys and the types of its scalar fields are pinned here, on a session built from the ti_37
fixture (labels, net_labels, the re-entry record) and run through check_session.py: a key added, removed or retyped
fails this test and needs a FROZEN_COLLECTOR.md row. The labels block (label_checks) is pinned key by key. And
label_checks' revocation is pinned on the same fixture: a dead recovery is revoked when a frozen re-entry also
continued (one recovery of either kind per session, Q am. 6), and kept otherwise.

    python3 test_check_schema.py        (exit 0 = pass)
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402
import entries as E  # noqa: E402

FX = HERE / "fixtures" / "tt_ti37_recovered_2026-10-18.json"
NONE = type(None)
TOP = {"session": str, "platform": str, "verdict": str, "session_class": str, "entries": list,
       "slow_start_shaped": bool, "reasons": list, "thresholds": dict, "max_background_share": (float, NONE),
       "labels": dict, "traffic": dict, "shots": dict, "ad_actions": dict, "player_pin": (dict, NONE),
       "meta_missing": list, "player_stats": (dict, NONE), "motion": (dict, NONE), "matrix": (str, NONE),
       "low_state_cap": (int, NONE), "stream_regime": (dict, NONE), "suspicious": list, "censored": (dict, NONE),
       "lowest_rung": (dict, NONE), "decode_path": (dict, NONE), "decode_hevc": (dict, NONE),
       "template_note": (str, NONE), "entry_quality": (dict, NONE)}
LABELS = ["ad_span_seconds", "ad_spans_per_signal", "ad_spans_span_type_set", "ad_ticks_per_signal",
          "advancing_share_while_playing", "dead_player_T_s", "dead_player_max_all_s", "dead_player_max_s",
          "duration_s", "frozen_playhead_T_s", "frozen_playhead_max_all_s", "frozen_playhead_max_s",
          "login_wall_text_ticks", "login_wall_ticks", "n_res_meta", "paused_while_playing_share",
          "quality_countdown_ticks", "recovered_dead", "recovered_dead_runs", "recovered_frozen", "shot_effects",
          "src_changed_ticks", "stall_advancing_share", "stall_share", "swap_only_ad_spans", "tick_presence",
          "tier_changes", "tier_changes_per_min", "tier_changes_with_swap_within_2s_share", "tier_mix_quality_time",
          "tt_cap_resets_55_75s", "tt_explore_ticks", "tt_flv_tokens_per_room", "tt_room_changes", "tt_room_ticks",
          "tt_room_visits", "usable_share", "v2_columns"]


def _fixture():
    fx = json.loads(FX.read_text())
    return fx, [dict(zip(fx["columns"], r)) for r in fx["rows"]]


def test_check_json_schema() -> None:
    fx, _ = _fixture()
    with tempfile.TemporaryDirectory() as tmp:
        sd = Path(tmp) / "data" / "tiktok" / "ti_99"
        sd.mkdir(parents=True)
        with open(sd / "labels.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(fx["columns"])
            w.writerows(fx["rows"])
        (sd / "net_labels.txt").write_text("".join(json.dumps(x) + "\n" for x in fx["net_labels"]))
        (sd / "meta.json").write_text(json.dumps({
            "platform": "tiktok", "started_at": fx["started_at"], "room_rotation": fx["room_rotation"],
            "player_dead_reentries": fx["player_dead_reentries"], "channel_url": "https://www.tiktok.com/@room/live",
            "shaper": {"matrix_id": "TT-M", "low_state_cap": 4}}))
        subprocess.run([sys.executable, str(HERE / "check_session.py"), str(sd)], capture_output=True, text=True,
                       cwd=HERE)
        c = json.loads((sd / "check.json").read_text())
    assert sorted(c) == sorted(TOP), f"check.json keys changed: +{sorted(set(c) - set(TOP))} -{sorted(set(TOP) - set(c))}"
    def ok(v, t) -> bool:   # bool is an int: accepted only where bool is the type
        ts = t if isinstance(t, tuple) else (t,)
        return isinstance(v, ts) and (not isinstance(v, bool) or bool in ts)
    bad = {k: type(c[k]).__name__ for k, t in TOP.items() if not ok(c[k], t)}
    assert not bad, f"check.json types changed: {bad}"
    assert c["verdict"] in ("PASS", "FAIL") and c["session_class"] in E.CLASS_PRECEDENCE
    assert sorted(c["labels"]) == LABELS, f"labels keys changed: +{sorted(set(c['labels']) - set(LABELS))} -{sorted(set(LABELS) - set(c['labels']))}"
    assert c["labels"]["recovered_dead"] and c["labels"]["dead_player_T_s"] == CS.PLAYER_DEAD_MAX_S["tiktok"]


def test_dead_recovery_revoked_by_a_frozen_continue() -> None:
    fx, rows = _fixture()
    re_ = fx["player_dead_reentries"]
    T = CS.PLAYER_DEAD_MAX_S["tiktok"]
    kept = CS.label_checks(rows, "tiktok", re_, [])
    assert kept["recovered_dead"] and kept["dead_player_max_s"] < T                    # ti_37: the one recovery
    frozen = [{"t_nav": re_[0]["t_nav"] + 100.0, "outcome": "continue"}]
    lab = CS.label_checks(rows, "tiktok", re_, frozen)
    assert lab["recovered_dead"] is None and lab["dead_player_max_s"] == lab["dead_player_max_all_s"] >= T, lab
    assert E.session_class("tiktok", "PASS", None, [], lab["dead_player_max_s"], T, 0, 720) == "player_dead"
    stalled = [{"t_nav": re_[0]["t_nav"] + 100.0, "outcome": "player_frozen"}]          # a re-entry that did not continue
    assert CS.label_checks(rows, "tiktok", re_, stalled)["recovered_dead"]


def main() -> int:
    test_check_json_schema()
    test_dead_recovery_revoked_by_a_frozen_continue()
    print("test_check_schema: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
