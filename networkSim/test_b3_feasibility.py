"""B3 feasibility table (t68; interface 61a8bf36): structural zeros from offer x band rate x box, levi's C5 (no tier
feasible with zero offered), and the per-tier table. Synthetic offers."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ops" / "plan"))     # the suite of record collects networkSim/test_*.py only
import b3_feasibility as B  # noqa: E402


def _meta(pf, rends):
    return {"platform": pf, "offered_ladder": {"t": 0.0, "source": "test",
                                               "renditions": [{"w": w, "h": h, "bitrate_kbit": k} for w, h, k in rends]}}


YT = [(256, 144, 255), (640, 360, 380), (1280, 720, 880), (1920, 1080, 3040)]


def _tier(res, pf, t):
    return next(r for r in res["per_tier"] if r["platform"] == pf and r["tier"] == t)


def _cell(res, pf, t, g, d):
    return next(c for c in res["cells"] if (c["platform"], c["tier"], c["geometry"], c["dpr"]) == (pf, t, g, d))


def test_yt_1080_needs_a_1080_box_and_dpr2_opens_it():
    boxes = {"youtube": {"theater:1920x1080@1": [1243, 699], "theater:1920x1080@2": [2486, 1398]}}
    res = B.build([_meta("youtube", YT)], boxes, [1.0, 2.0], None, None, 0.0,
                  geometry={"youtube": ["theater:1920x1080"]})
    small = _cell(res, "youtube", "Q_1080p", "theater:1920x1080", 1.0)
    big = _cell(res, "youtube", "Q_1080p", "theater:1920x1080", 2.0)
    assert small["reasons"] == ["box_below_tier"] and not small["reachable"] and small["box_source"] == "measured"
    assert big["reachable"] and _tier(res, "youtube", "Q_1080p")["reachable_cells"] == ["theater:1920x1080@2"]


def test_an_offered_tier_above_every_band_is_a_structural_zero():
    res = B.build([_meta("twitch", [(284, 160, 230), (1920, 1080, 50000)])], {}, [1.0], None, None, 0.0,
                  geometry={"twitch": ["player:1274x717"]})
    c = _cell(res, "twitch", "Q_1080p", "player:1274x717", 1.0)
    assert c["reasons"] == ["no_band_reaches_need"] and _tier(res, "twitch", "Q_1080p")["structural_zero"]


def test_tw_has_no_size_cap_and_unoffered_tiers_are_never_reachable():
    res = B.build([_meta("twitch", [(284, 160, 230), (1920, 1080, 7066)])], {}, [1.0], None, None, 0.0,
                  geometry={"twitch": ["player:1274x717"]})
    assert _cell(res, "twitch", "Q_1080p", "player:1274x717", 1.0)["reachable"]      # 717 < 1080: no cap on twitch
    c = _cell(res, "twitch", "Q_480p", "player:1274x717", 1.0)
    assert "not_offered" in c["reasons"] and not c["reachable"]                         # levi's C5
    assert all(not (x["reachable"] and x["offered_share"] == 0) for x in res["cells"])


def test_band_share_and_expected_hours():
    assert B.band_share("10", 1000) == 1.0 and B.band_share("1", 5000) == 0.0
    s = B.band_share("8", 3000)                       # [2500, 4000] x 0.9 = [2250, 3600]: (3600 - 3000) / 1350
    assert abs(s - 600 / 1350) < 1e-9
    joint = {"per_platform": {"twitch": {"by_state": {"10": {"Q_1080p": 30.0, "Q_720p": 70.0}}}}}
    res = B.build([_meta("twitch", [(1920, 1080, 7066)])], {}, [1.0], joint, {"twitch": {"10": 1.0}}, 10.0,
                  geometry={"twitch": ["player:1274x717"]})
    r = _tier(res, "twitch", "Q_1080p")
    assert r["draw_mass_reachable"] == 1.0 and abs(r["expected_hours_next_window"] - 10 * 0.3 * B.band_share("10", 7066)) < 1e-3


def test_cli_writes_the_schema_and_prints_the_table(tmp_path, capsys):
    d = tmp_path / "youtube" / "yt_1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(_meta("youtube", YT)))
    out = tmp_path / "b3.json"
    assert B.main(["--offers-metas", str(d / "meta.json"), "--window", "w1", "--out", str(out), "--planned-hours", "2"]) == 0
    j = json.loads(out.read_text())
    assert j["schema"] == "b3_feasibility/1" and j["rules"]["ratio_lo"] == 0.9 and j["inputs"]["offers"]["sessions"] == 1
    assert "B3 youtube" in capsys.readouterr().out


if __name__ == "__main__":                            # run_suite.sh runs each networkSim test_*.py as a script too
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
