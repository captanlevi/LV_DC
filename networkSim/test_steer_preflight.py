#!/usr/bin/env python3
"""levi 22:18 (B8): a steered stratum on record never starts on a table its sessions would fall back from. orchestrate's
steer_table_refusal reads <trio root>/_summaries/coverage_joint.json with steering.load_table and judges each steered
platform by steering.fallback_reason (steer()'s own rule): no table, an unreadable / invalid one, another era's, or a
platform with no seconds of this era each refuse before trio_up, named; a usable v3 table passes. steer()'s records are
unchanged by the refactor (fallback_reason is its one definition).

    python3 test_steer_preflight.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import orchestrate as O  # noqa: E402
import record  # noqa: E402
import steering as S  # noqa: E402

ERA = record.ERA
ALL = O.STEER_PLATFORMS_ALL


def _pp(sec=600.0):
    return {"tiers": {"Q_480p": sec, "Q_720p": sec}, "stall_s": sec / 10, "by_state": {"5": {"Q_480p": sec}}}


def _root(d, body):
    """A trio root whose _summaries/coverage_joint.json holds `body` (a dict, raw text, or None = no file)."""
    troot = Path(d) / "_trio"
    (troot / "_summaries").mkdir(parents=True)
    if body is not None:
        (troot / "_summaries" / S.TABLE_NAME).write_text(body if isinstance(body, str) else json.dumps(body))
    return troot


def test_a_usable_v3_table_passes():
    with tempfile.TemporaryDirectory() as d:
        t = _root(d, {"era": ERA, "seed": {"what": "v3 seed (B8)"}, "per_platform": {p: _pp() for p in ALL}})
        assert O.steer_table_refusal(t, ALL, ERA) is None


def test_every_fallback_reason_refuses_named():
    cases = {
        "no file": (None, "base schedule (no table: FileNotFoundError"),
        "unreadable": ("{not json", "base schedule (no table: JSONDecodeError"),
        "bad class name": ({"era": ERA, "per_platform": {p: {**_pp(), "tiers": {"720p": 1.0}} for p in ALL}},
                           "class names not in"),
        "bad cell": ({"era": ERA, "per_platform": {p: {**_pp(), "stall_s": -1.0} for p in ALL}},
                     "non-numeric / NaN / inf / negative cells"),
        "t58 era": ({"era": record.T58_ERA, "per_platform": {p: _pp() for p in ALL}},
                    f"era-start: table era {record.T58_ERA!r}"),
        "no era stamp": ({"per_platform": {p: _pp() for p in ALL}}, "era-start: table era None"),
        "no tt seconds": ({"era": ERA, "per_platform": {**{p: _pp() for p in ALL}, "tiktok": _pp(0.0)}},
                          "tiktok: era-start: no tiktok seconds of this era"),
        "tt missing": ({"era": ERA, "per_platform": {p: _pp() for p in ("youtube", "twitch")}},
                       "tiktok: era-start: no tiktok seconds of this era"),
    }
    for name, (body, want) in cases.items():
        with tempfile.TemporaryDirectory() as d:
            r = O.steer_table_refusal(_root(d, body), ALL, ERA)
            assert r and want in r and "refused before trio_up" in r and S.TABLE_NAME in r, (name, r)


def test_only_the_steered_platforms_are_judged():
    with tempfile.TemporaryDirectory() as d:
        t = _root(d, {"era": ERA, "per_platform": {**{p: _pp() for p in ALL}, "tiktok": _pp(0.0)}})
        assert O.steer_table_refusal(t, {"youtube", "twitch"}, ERA) is None
        assert "tiktok" in O.steer_table_refusal(t, ALL, ERA)


def test_steer_records_unchanged_by_the_refactor():
    """steer()'s fallback, allowed_by_flag and table_era are fallback_reason's, case by case."""
    trans = {"1": {"1": 0.5, "2": 0.5}, "2": {"1": 0.5, "2": 0.5}}
    with tempfile.TemporaryDirectory() as d:
        for i, (body, era, allow) in enumerate([
                (None, ERA, None),
                ({"era": record.T58_ERA, "per_platform": {"twitch": _pp()}}, ERA, None),
                ({"era": record.T58_ERA, "per_platform": {"twitch": _pp()}}, ERA, record.T58_ERA),
                ({"era": ERA, "per_platform": {"twitch": _pp(0.0)}}, ERA, None),
                ({"era": ERA, "per_platform": {"twitch": _pp()}}, None, None)]):
            p = Path(d) / f"t{i}.json"
            if body is not None:
                p.write_text(json.dumps(body))
            m, start, rec = S.steer("twitch", trans, p, "tw_1", era=era, allow_table_era=allow)
            table, trec = S.load_table(p)
            why, allowed = S.fallback_reason(table, trec, "twitch", era, allow)
            assert rec.get("fallback") == why, (i, rec.get("fallback"), why)
            assert (m is trans and start is None) == (why is not None), i
            if table is not None and era is not None:
                assert rec["table_era"] == table.get("era") and rec["allowed_by_flag"] is allowed, (i, rec)
            else:
                assert "table_era" not in rec, (i, rec)


def _args(**kw):
    import argparse
    return argparse.Namespace(**{"trio_record": False, "trio_steer_platforms": None, **kw})


def test_preflight_scope_record_only_and_steered_only():
    """steer_preflight: only a steered run, only on record (--trio-record or the data root's marker); the steered
    platforms from --trio-steer-platforms."""
    with tempfile.TemporaryDirectory() as d:
        data = Path(d)
        _root(d, {"era": record.T58_ERA, "per_platform": {p: _pp() for p in ALL}})   # a t58 table: would fall back
        env = {"LVDC_DATA_ROOT": str(data)}
        assert O.steer_preflight(_args(), env, False) is None                         # unsteered: never read
        assert O.steer_preflight(_args(), env, True) is None                          # not record: steer() decides
        r = O.steer_preflight(_args(trio_record=True), env, True)
        assert r and "era-start: table era" in r and all(p in r for p in ALL)
        r = O.steer_preflight(_args(trio_record=True, trio_steer_platforms="youtube"), env, True)
        assert r and "youtube:" in r and "tiktok:" not in r
        import data_root as DR
        (data / DR.RECORD_MARKER).write_text("{}")                                      # the record marker alone
        assert "era-start: table era" in O.steer_preflight(_args(), env, True)


def test_wired_before_trio_up_and_in_check_args():
    src = (HERE / "orchestrate.py").read_text()
    call = "_sref = steer_preflight(args, os.environ, TRIO_STEER)"
    assert call in src and src.index(call) < src.index("steer_balancer(TRIO_STEER_PLATFORMS)\n        if args.concurrent:")
    st = src[src.index("def _static_refusal("):src.index("def check_args(")]
    assert "r = steer_preflight(args, environ, steer)" in st


def main() -> int:
    test_a_usable_v3_table_passes()
    test_every_fallback_reason_refuses_named()
    test_only_the_steered_platforms_are_judged()
    test_steer_records_unchanged_by_the_refactor()
    test_preflight_scope_record_only_and_steered_only()
    test_wired_before_trio_up_and_in_check_args()
    print("test_steer_preflight: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
