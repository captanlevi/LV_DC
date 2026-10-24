"""B3 feasibility table (t68; interface 61a8bf36): structural zeros from offer x band rate x box, levi's C5 (no tier
feasible with zero offered), and the per-tier table. Synthetic offers."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "ops" / "plan"))     # the suite of record collects networkSim/test_*.py only
import b3_feasibility as B  # noqa: E402


def _meta(pf, rends):
    import offer as O                                 # the collector's own writer: the fixture is its output schema
    return {"platform": pf, "offer": {"platform": pf, "missing": False, "t_first": 0.0, "source": "test",
                                      "renditions": [O._rend(f"{h}p", w, h, k, None, None) for w, h, k in rends]}}


YT = [(256, 144, 255), (640, 360, 380), (1280, 720, 880), (1920, 1080, 3040)]


def _tier(res, pf, t):
    return next(r for r in res["per_tier"] if r["platform"] == pf and r["tier"] == t)


def _cell(res, pf, t, g, d):
    return next(c for c in res["cells"] if (c["platform"], c["tier"], c["geometry"], c["dpr"]) == (pf, t, g, d))


def test_yt_size_rule_is_b1s_tier_cap():
    """B1's YT_TIER_CAP is the one yt size rule: 1080 only in fullscreen, 720 in a 855x481 box (which a box rule would
    forbid), and a DPR with no entry reaches nothing (exp.py refuses the session)."""
    boxes = {"youtube": {"theater:1243x699@1": [1243, 699]}}
    g = ["theater:1243x699", "player:855x481", "fullscreen:1920x1080"]
    res = B.build([_meta("youtube", YT)], boxes, [1.0, 2.0], None, None, 0.0, geometry={"youtube": g})
    th = _cell(res, "youtube", "Q_1080p", "theater:1243x699", 1.0)
    assert th["reasons"] == ["above_tier_cap"] and not th["size_ok"] and th["box_source"] == "measured"
    assert _cell(res, "youtube", "Q_720p", "player:855x481", 1.0)["reachable"]
    assert _cell(res, "youtube", "Q_1080p", "fullscreen:1920x1080", 1.0)["reachable"]
    assert _cell(res, "youtube", "Q_720p", "player:855x481", 2.0)["reasons"] == ["no_tier_cap"]
    assert _tier(res, "youtube", "Q_1080p")["reachable_cells"] == ["fullscreen:1920x1080@1"]


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


def test_twitch_plans_on_configured_rates_and_realised_is_a_sensitivity():
    """levi 16:25 / 16:30: the 19:31 realised rule is withdrawn; the primary cells are always configured x RATIO_LO, and
    a passed --realised adds *_realised columns beside them, never replacing them."""
    res0 = B.build([_meta("twitch", [(1920, 1080, 7066)])], {}, [1.0], None, None, 0.0,
                   geometry={"twitch": ["player:1274x717"]})
    assert res0["rate_source"]["twitch"] == "configured x RATIO_LO"
    # state 10 realised at 0.75: 9000 x 0.75 = 6750 < 7066, so only state 11 reaches 1080 (12000 x 0.75 = 9000)
    res = B.build([_meta("twitch", [(1920, 1080, 7066)])], {}, [1.0], None, None, 0.0,
                  geometry={"twitch": ["player:1274x717"]}, realised={"twitch": {s: 0.75 for s in B.SCENARIOS}})
    c = _cell(res, "twitch", "Q_1080p", "player:1274x717", 1.0)
    # primary (configured x 0.9): state 10 reaches 1080 (9000 x 0.9 = 8100 >= 7066), unchanged by --realised
    assert c["bands_reaching"] == _cell(res0, "twitch", "Q_1080p", "player:1274x717", 1.0)["bands_reaching"]
    assert "10" in c["bands_reaching"] and c["reachable"]
    assert c["bands_reaching_realised"] == ["11"] and c["reachable_realised"]               # the sensitivity
    assert res["rate_source"]["twitch"] == "configured x RATIO_LO (+ realised sensitivity columns)"
    t = next(x for x in res["per_tier"] if (x["platform"], x["tier"]) == ("twitch", "Q_1080p"))
    assert t["reachable_cells"] and t["reachable_cells_realised"] and "band_share_reaching_realised" in t
    assert res["rate_source"]["youtube"] == "not built"                      # no youtube geometry in this build


def test_cli_writes_the_schema_and_prints_the_table(tmp_path, capsys):
    d = tmp_path / "youtube" / "yt_1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(_meta("youtube", YT)))
    out = tmp_path / "b3.json"
    assert B.main(["--offers-metas", str(d / "meta.json"), "--window", "w1", "--out", str(out), "--planned-hours", "2"]) == 0
    j = json.loads(out.read_text())
    assert j["schema"] == "b3_feasibility/1" and j["rules"]["ratio_lo"] == 0.9 and j["inputs"]["offers"]["sessions"] == 1
    assert "B3 youtube" in capsys.readouterr().out


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
