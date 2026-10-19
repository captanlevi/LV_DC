#!/usr/bin/env python3
"""ops/round_stop_checks.py (supervisor 2026-10-19): the chain stops only on the reader's gating failures (ok False),
the run's teardown, a session in _failed / missing from the reader, or device-record coverage < 0.95; a reported item
(4a's device_limited fps, an inventory reason with ok None) never stops it."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("round_stop_checks", HERE.parent / "ops" / "round_stop_checks.py")
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)


def _sess(root: Path, name: str, covered: int = 100, secs: int = 100) -> Path:
    sd = root / "twitch" / name
    sd.mkdir(parents=True)
    hdr = {"header": True, "stamp": {"window": [999.0, 1000.0 + secs + 1]}}
    rows = [{"t": 1000.0 + i + 0.5, "gpu_busy_pct": {}} for i in range(covered)]
    (sd / "box_record.jsonl").write_text("".join(json.dumps(x) + "\n" for x in [hdr] + rows))
    return sd


def _row(name, **over):
    r = {"session": f"/d/twitch/{name}", "items1_2": {"1": {"ok": True, "reading": "PASS"},
                                                      "2": {"ok": None, "reading": "reported, excluded as in single mode"}},
         "item4a": {"ok": True, "device_limited_tiers": ["Q_720p"]}, "item5": {"ok": None}, "item7": {"ok": True},
         "item9": {"ok": True}, "item11": {"ok": True}, "item12": {"ok": True}, "item13": {"ok": True},
         "item14": {"ok": True}, "teardown": {"ok": True}}
    r.update(over)
    return r


def test_stop_rule():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        a, b = _sess(root, "tw_100005"), _sess(root, "tw_100006", covered=90)
        g = {"sessions": [_row("tw_100005"), _row("tw_100006")]}
        res = R.check(g, {"clean": True, "checks": {}}, [a])
        assert res["ok"] and "item4a device_limited ['Q_720p'] (reported)" in res["report"]["tw_100005"]["reported"], res
        assert res["report"]["tw_100005"]["device_record"]["covered_share"] == 1.0
        res = R.check(g, None, [a, b])                                     # 90 of 100 s covered: stop
        assert not res["ok"] and any("device record: 0.9" in x for x in res["gating"]), res
        g["sessions"][0]["item9"] = {"ok": False, "dropped_kernel_total": 3}
        g["sessions"][0]["items1_2"]["1"] = {"ok": False, "reading": "gates: chunk recall # vs logged responses"}
        res = R.check(g, {"clean": False, "checks": {"x": {"ok": False}}}, [a])
        assert len(res["gating"]) == 3 and res["gating"][0].startswith("run teardown"), res
        assert R.check({"sessions": []}, None, [a])["gating"] == ["tw_100005: not in the reader's output"]
        # levi: a session in _failed for inventory-only reasons (tw_100005's player_frozen) is reported, not a stop
        f = root / "_failed" / "twitch" / "tw_100007"
        f.parent.mkdir(parents=True)
        _sess(root, "tw_100007").rename(f)
        res = R.check({"sessions": [_row("tw_100007")]}, None, [f])
        assert res["ok"] and res["report"]["tw_100007"]["reported"][0].startswith("in _failed"), res
        bad2 = _row("tw_100007", items1_2={"2": {"ok": False, "reading": "gates: class:new_thing"}})
        assert not R.check({"sessions": [bad2]}, None, [f])["ok"]
        assert R.check({"sessions": []}, None, [root / "gone"])["gating"] == ["gone: session dir missing"]
        (a / "box_record.jsonl").unlink()
        assert any("no box_record" in x for x in R.check({"sessions": [_row("tw_100005")]}, None, [a])["gating"])
        (root / "g.json").write_text(json.dumps({"sessions": [_row("tw_100005")]}))
        assert R.main(["--gate2", str(root / "g.json"), str(a)]) == 1                 # no device record: BAD, exit 1


if __name__ == "__main__":
    test_stop_rule()
    print("test_round_stop_checks: ok")
