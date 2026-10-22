#!/usr/bin/env python3
"""traces/replay.py, the §9 trace regime's replay (levi's rulings efb5e4cc): the per-session draw on its own generator,
rate_band_state, the plan (equal-weight source, offset, same-source concatenation without looping), the clip and the tc
changes (make slow first, make rate after; state None, rate_band_state set). No root, no network; the real cache is
read only when LVDC_TRACE_CACHE is set and holds it.

    python3 test_trace_replay.py        (exit 0 = pass)
"""
import os
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from traces import replay as R  # noqa: E402

MAN = {"schema": "lvdc_trace_manifest_v1", "sources": {
    "a": {"files": [{"name": "a1", "sha256": "x1", "seconds": 300}, {"name": "a2", "sha256": "x2", "seconds": 200},
                    {"name": "a3", "sha256": "x3", "seconds": 100}]},
    "b": {"files": [{"name": "b1", "sha256": "y1", "seconds": 5000}]}}}


def test_draw():
    keys = [f"R:tw1:{i}" for i in range(4000)]
    random.seed(1)
    before = random.random()
    random.seed(1)
    hits = [R.drawn(k) for k in keys]
    assert random.random() == before                               # the global random untouched
    assert hits == [R.drawn(k) for k in keys]                      # deterministic per key
    assert 0.08 < sum(hits) / len(keys) < 0.12, sum(hits) / len(keys)
    assert not any(R.drawn(k, p=0.0) for k in keys) and all(R.drawn(k, p=1.0) for k in keys)


def test_rate_band_state():
    assert R.lower_kbit() == 150
    assert [R.rate_band_state(x) for x in (150, 279.9, 280, 449, 450, 999, 1000, 8999, 9000, 12000)] == \
        ["1", "1", "2", "2", "3", "4", "5", "10", "11", "11"]
    assert R.rate_band_state(100) == "1" and R.rate_band_state(50000) == "11"


def test_plan_and_generator():
    p1 = R.plan("R:yt1:3", 450, MAN)
    assert p1 == R.plan("R:yt1:3", 450, MAN) and p1["source"] in MAN["sources"]
    names = [s["trace"] for s in p1["segments"]]
    assert len(names) == len(set(names))                           # no looping: each trace once
    if p1["source"] == "a":
        assert p1["covered_s"] >= 450 or p1["short"]
    srcs = {R.plan(f"k{i}", 100, MAN)["source"] for i in range(200)}
    assert srcs == {"a", "b"}                                      # equal weight: both drawn
    pl = {"source": "a", "segments": [{"trace": "a1", "offset_s": 10, "start_s": 0, "seconds": 3},
                                      {"trace": "a2", "offset_s": 0, "start_s": 3, "seconds": 5}]}
    xs = [100.0, 100.4, 500.0, 500.2, 20000.0, 20000.0, 700.0, 700.0]
    out = list(R.trace_episode_generator(xs, pl, 150, 8000, 40, 8))
    assert [(o.rate, o.duration, o.walk_step, o.clip, o.rate_band_state) for o in out] == [
        (150, 2.0, 0, "low", "1"), (500, 2.0, 1, None, "3"), (8000, 2.0, 2, "high", "10"), (700, 2.0, 3, None, "4")], out
    assert all(o.state is None and o.regime == "trace" and o.delay_ms == 40 and o.loss_pct == 0.0 for o in out)
    assert [(o.trace_id, o.trace_offset_s) for o in out] == [("a1", 10), ("a1", 12), ("a2", 1), ("a2", 3)]
    assert R.clip_shares(xs, 150, 8000, 8) == {"seconds": 8, "low_s": 2, "high_s": 2, "low_share": 0.25, "high_share": 0.25}
    assert len(list(R.trace_episode_generator(xs, pl, 150, 8000, 40, 5))) == 3       # total_s bounds the series
    try:
        list(R.trace_episode_generator(xs, pl, 150, 100, 40, 8))
        raise AssertionError("upper below lower accepted")
    except ValueError:
        pass


def test_net_label_rows():
    """A state change's net_labels row is the pre-§9 row byte for byte; a trace change's carries its fields."""
    import json
    from dataclasses import asdict, fields
    from utils.dataModels import NetLabel, NetStat, TRACE_FIELDS, net_label_row, trace_label_fields
    st = NetStat(rate=900, duration=2.0, delay_ms=12, loss_pct=0.1, state="4", redraw_kbit=880, redraw_delay_ms=10,
                 redraw_loss_pct=0.1, walk_step=1)
    assert trace_label_fields(st, 5.0) == {}
    lab = NetLabel(timestamp=1.5, speed=900, state="4", delay_ms=12, loss_pct=0.1, redraw_kbit=880, redraw_delay_ms=10,
                   redraw_loss_pct=0.1, walk_step=1, **trace_label_fields(st, 5.0))
    pre = {k: v for k, v in asdict(lab).items() if k not in TRACE_FIELDS}          # the row before §9
    assert json.dumps(net_label_row(lab)) == json.dumps(pre)
    assert [f.name for f in fields(NetLabel)][:9] == list(pre)                       # field order unchanged
    pl = {"source": "a", "segments": [{"trace": "a1", "offset_s": 0, "start_s": 0, "seconds": 3}]}
    tr = next(R.trace_episode_generator([400.0, 400.0, 400.0], pl, 150, 8000, 40, 3))
    row = net_label_row(NetLabel(timestamp=2.0, speed=tr.rate, state=tr.state, delay_ms=tr.delay_ms,
                                 loss_pct=tr.loss_pct, walk_step=tr.walk_step, **trace_label_fields(tr, 1.9)))
    assert row["state"] is None and row["regime"] == "trace" and row["rate_band_state"] == "2" and row["scheduled_t"] == 1.9
    src = (HERE / "exp.py").read_text()
    assert "save_data = [net_label_row(obj) for obj in data]" in src and "tr = trace_label_fields(net_stat, scheduled)" in src
    assert "and TRACE is None:" in src and '_fb = "trace regime"' in src and "(fail closed)" in src


def test_hold_keys_on_rate_band_state():
    """levi ruling 2: a trace row (state null) is held exactly as a state row of its band: crossing into and out of a
    low band opens and closes the hold (+ grace); a state row reads its state as before."""
    import stuck as S
    from utils.dataModels import NetLabel
    tr = lambda t, k: {"timestamp": t, "speed": k, "state": None, "regime": "trace", "rate_band_state": R.rate_band_state(k)}
    labs = [tr(100.0, 3000), tr(110.0, 400), tr(130.0, 900), tr(140.0, 1500)]          # bands 8, 2, 4, 6
    assert [S.hold_state(x) for x in labs] == ["8", "2", "4", "6"]
    assert S.low_spans(labs) == [(110.0, 140.0)]
    assert S.low_hold_end(labs, 105.0) is None and S.low_hold_end(labs, 150.0) == 140.0 + S.LOW_HOLD_GRACE_S
    obj = NetLabel(timestamp=1.0, speed=400, state=None, regime="trace", rate_band_state="2")
    assert S.hold_state(obj) == "2" and S.hold_state({"timestamp": 1, "speed": 400, "state": "3"}) == "3"
    assert S.hold_state({"timestamp": 1, "speed": 400, "state": None}) is None      # a null state outside the regime
    assert "int(stuck.hold_state(labels[-1]))" in (HERE / "browser.py").read_text()


def test_real_cache():
    cache = Path(os.environ.get("LVDC_TRACE_CACHE", ""))
    if not os.environ.get("LVDC_TRACE_CACHE") or not cache.exists():
        print("  (real cache not set: skipped)")
        return
    man = R.load_manifest()
    for i in range(6):
        pl = R.plan(f"real:{i}", 1800, man)
        xs = R.series(pl, cache, man)
        assert len(xs) >= min(1800, pl["covered_s"]) - 1, (pl["source"], len(xs), pl["covered_s"])
        ch = list(R.trace_episode_generator(xs, pl, R.lower_kbit(), 6000, 40, 1800))
        assert ch[0].walk_step == 0 and sum(c.duration for c in ch) == min(len(xs), 1800)


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_trace_replay: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
