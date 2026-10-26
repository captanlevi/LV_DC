#!/usr/bin/env python3
"""codex item 2 (levi 18:15, supervisor 18:27 boundary bundle): exp.run() returns the rc; a shaper apply (make slow / rate)
with rc != 0 is recorded on its net_labels row (apply_rc) and then ends the session as error (ShaperApplyError), never a
state labelled as applied that the link did not get; experiment() no longer swallows a shaper exception; a startup failure
after the slot came up undoes what it started (codex triage (b)).

    python3 test_apply_rc.py        (exit 0 = pass)
"""
import ast
import asyncio
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_join_at_j as TJ  # noqa: E402

SRC = TJ.SRC


class ShaperApplyError(RuntimeError):
    pass


def _ns_rc(rcs):
    """test_join_at_j's _ossilate namespace, the apply stub returning rcs in order (then 0)."""
    ns, applied, lever = TJ._ns(settle_s=0.05)
    seq = list(rcs)

    def apply(net_stat):
        applied.append((0.0, net_stat))
        return seq.pop(0) if seq else 0
    ns.update(_apply_net_stat=apply, ShaperApplyError=ShaperApplyError)
    return ns, applied


def _go(ns, labels, play=True):
    try:
        return TJ._run(ns, labels, play_after=0.02 if play else None)
    except ShaperApplyError as e:
        return e


def test_run_returns_the_rc_and_records_a_failed_make_target():
    (fn,) = TJ._fns("run")

    class P:
        def __init__(self, rc):
            self.rc, self.pid = rc, 0

        def wait(self):
            return self.rc
    rcs = iter([2, 0, 3])
    ns = {"subprocess": SimpleNamespace(Popen=lambda args, preexec_fn=None: P(next(rcs))), "os": os, "signal": signal,
          "MAKE_RC": {}, "print": lambda *a, **k: None}
    exec(compile(ast.Module([fn], []), "exp", "exec"), ns)
    assert ns["run"](["make", "slow", "BANDWIDTH=1kbit"]) == 2 and ns["MAKE_RC"] == {"slow": 2}
    assert ns["run"](["make", "rate"]) == 0 and ns["MAKE_RC"] == {"slow": 2}          # an rc 0 is not recorded
    assert ns["run"](["true"]) == 3 and ns["MAKE_RC"] == {"slow": 2}                  # only make targets
    ns2 = {"subprocess": subprocess, "os": os, "signal": signal, "MAKE_RC": {}}
    exec(compile(ast.Module([fn], []), "exp", "exec"), ns2)
    assert ns2["run"](["sh", "-c", "exit 3"]) == 3 and ns2["run"](["true"]) == 0     # the real Popen's rc


def test_every_applied_row_carries_apply_rc_0():
    ns, applied = _ns_rc([])
    labels = []
    assert _go(ns, labels) is True
    assert [l.apply_rc for l in labels] == [0, 0, 0] and labels[0].phase == "join"


def test_a_failed_schedule_apply_is_recorded_then_ends_the_session():
    ns, applied = _ns_rc([0, 2])                       # J ok, the schedule's first apply fails
    labels = []
    e = _go(ns, labels)
    assert isinstance(e, ShaperApplyError) and "rc 2" in str(e) and "make slow" in str(e)
    assert [l.apply_rc for l in labels] == [0, 2]      # the failed row is on record, the next change never ran
    assert len(applied) == 2


def test_a_failed_j_apply_never_starts_the_join():
    ns, applied = _ns_rc([1])
    labels = []
    e = _go(ns, labels, play=False)                    # the join never navigates: nothing to play
    assert isinstance(e, ShaperApplyError) and "state 8" in str(e)
    assert len(labels) == 1 and labels[0].phase == "join" and labels[0].apply_rc == 1
    assert not ns["JOIN_FIRST"].is_set()               # no navigation on a link that is not at J


def test_experiment_reraises_a_shaper_exception():
    (fn,) = TJ._fns("experiment")
    seg = ast.get_source_segment(SRC, fn)
    i = seg.index("if osc_task in done:")
    assert seg.index("osc_task.result()", i) < seg.index("break", i)    # before: break, the exception never retrieved


def test_startup_undo_stops_what_started_and_never_raises():
    (fn,) = TJ._fns("startup_undo")
    calls = []

    def boom():
        calls.append("network")
        raise OSError("slot down")
    ns = {"trio_slots": SimpleNamespace(dns_capture_stop=lambda p: calls.append(("cap", p))),
          "run": lambda args: calls.append(tuple(args)) or 0, "SLOT_VARS": ["SLOT=x"],
          "kill_chrome": lambda prof: calls.append(("chrome", prof)), "finalize_network": boom,
          "print": lambda *a, **k: None}
    exec(compile(ast.Module([fn], []), "exp", "exec"), ns)
    done = ns["startup_undo"]([None, "p1", "p2"], capture=True, chrome_profile="prof")
    assert calls == [("cap", "p1"), ("cap", "p2"), ("make", "stop-capture", "SLOT=x"), ("chrome", "prof"), "network"]
    assert done["caps"] == done["capture"] == done["chrome"] == "ok" and "slot down" in done["network"]
    calls.clear()
    assert set(ns["startup_undo"]()) == {"network"} and calls == ["network"]      # P3's case: only the slot was up


def test_startup_failures_after_the_slot_call_undo_before_raising():
    for msg in ('raise ValueError(f"P3: capture IFB offloads not off', 'raise ValueError("Cannot initialize pcap capture")',
                'raise ValueError("Cannot open chrome with SSL key log enabled")'):
        i = SRC.index(msg)
        assert "startup_undo(" in SRC[SRC.rindex("\n    if ", 0, i):i], msg


def test_teardown_records_a_failed_capture_stop_and_goes_on():
    i = SRC.index("if is_capture and finalize_pcap(session_dir) == False:")
    assert "raise" not in SRC[i:SRC.index("\n", SRC.index("\n", i) + 1)]
    assert SRC.index("make_rc=dict(MAKE_RC)") < SRC.index('print(f"Run complete')


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_") and callable(f):
            f()
            print("ok", name)
