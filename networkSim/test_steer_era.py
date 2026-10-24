#!/usr/bin/env python3
"""levi 19:35 (the first counted v3 window has no v3 deficit table): steering.steer, given the collector's era, falls back
to the base schedule on a table of another era ("era-start: table era <x>") unless the dry-only --steer-allow-table-era
names it (recorded allowed_by_flag), and on a table with no seconds for the platform ("era-start: no <platform> seconds
of this era"); a missing table falls back as before. orchestrate refuses the flag with --trio-record and on a record root;
exp.py passes record.ERA and the flag's env.

    python3 test_steer_era.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as O  # noqa: E402
import steering as S  # noqa: E402
from utils.shaping import TWITCH_TRANSITIONS  # noqa: E402

V3, T58 = "v3-collector-2026-10-24", "t58-restart-2026-10-21"
STATES = sorted(TWITCH_TRANSITIONS, key=int)


def _table(era, seconds=True):
    by = {s: ({"STALL": 1.0} if s in ("1", "2") else {"Q_720p": 1.0}) if seconds else {} for s in STATES}
    t = {"era": era, "per_platform": {"twitch": {"tiers": {"Q_720p": 100.0} if seconds else {},
                                                 "stall_s": 10.0 if seconds else 0.0, "by_state": by}},
         "built_at": "t", "code": "c"}
    d = Path(tempfile.mkdtemp())
    (d / S.TABLE_NAME).write_text(json.dumps(t))
    return d / S.TABLE_NAME


def test_era_start():
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, _table(T58), "tw_1", era=V3)
    assert m is TWITCH_TRANSITIONS and start is None and not rec["applied"]
    assert rec["fallback"] == f"era-start: table era {T58!r}" and rec["table_era"] == T58 and rec["allowed_by_flag"] is False
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, _table(T58), "tw_1", era=V3, allow_table_era=T58)
    assert rec["applied"] and rec["allowed_by_flag"] is True and rec["table_era"] == T58          # W-B's S round
    assert not S.steer("twitch", TWITCH_TRANSITIONS, _table(T58), "tw_1", era=V3, allow_table_era="other")[2]["applied"]
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, _table(V3), "tw_1", era=V3)
    assert rec["applied"] and rec["allowed_by_flag"] is False                                   # this era's table
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, _table(V3, seconds=False), "tw_1", era=V3)
    assert m is TWITCH_TRANSITIONS and rec["fallback"] == "era-start: no twitch seconds of this era"
    m, start, rec = S.steer("twitch", TWITCH_TRANSITIONS, str(_table(V3)) + ".missing", "tw_1", era=V3)
    assert rec["fallback"].startswith("base schedule (no table:")                              # unchanged
    assert S.steer("twitch", TWITCH_TRANSITIONS, _table(T58), "tw_1")[2]["applied"]             # no era given: no check


def test_flag_refusals():
    import data_root as DR
    base = ["--batch", "youtube=9,twitch=9,tiktok=9", "--concurrent", "--n-draw", "--window-min",
            str(O.RECORD_WINDOW_MIN), "--trio-yt-fullscreen", "--trio-layout", "composite", "--trio-steer",
            "--stratum-force", "steered", "--steer-allow-table-era", T58]
    dry, rec = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    (rec / DR.RECORD_MARKER).write_text(json.dumps({"uuid": "TEST-VOL"}))
    r = lambda argv, root: O.arg_refusal(O.build_parser().parse_args(argv), {"LVDC_DATA_ROOT_UUID": "0000-test",
                                                                             "LVDC_DATA_ROOT": str(root)})
    assert r(base, dry) is None
    got = r(base, rec)
    assert got and "refused on a record root" in got, got
    counted = [a for a in base if a not in ("--stratum-force", "steered")] + ["--trio-record", "--expect-stratum", "steered"]
    got = r(counted, rec)
    assert got and "never with --trio-record" in got, got


def test_exp_wiring():
    src = (HERE / "exp.py").read_text()
    assert "tier_cap=_cap, era=_record.ERA," in src
    assert 'allow_table_era=os.environ.get("LVDC_STEER_ALLOW_TABLE_ERA") or None)' in src
    # levi / brad 19:51: exp.py refuses the env on a record run itself, before any session dir
    i = src.index('if os.environ.get("LVDC_STEER_ALLOW_TABLE_ERA"):')
    assert 'os.environ.get("LVDC_TRIO_RECORD") == "1" or _drx.record_root_check(_drx.DATA_ROOT)["marker"]' in src[i:i + 400]
    assert i < src.index("session_dir = (make_session_dir(platform)")
    o = (HERE / "orchestrate.py").read_text()
    assert 'os.environ["LVDC_STEER_ALLOW_TABLE_ERA"] = args.steer_allow_table_era' in o


def main() -> int:
    test_era_start()
    test_flag_refusals()
    test_exp_wiring()
    print("test_steer_era: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
