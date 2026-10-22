#!/usr/bin/env python3
"""§9 identity proof (i) and (ii) (levi efb5e4cc: against goldens at the landing base sha, T* d7433b03): with the trace
regime's code present and its draw off, the state path is the T* one byte for byte.

  (i)   for fixed session keys x every platform x every matrix (base, @slow, @fast, and a held state), the sequence of
        NetStat changes the shaper applies (the generators exp.py runs, seeded as exp seeds them: the global random by
        the key, the walk and the delay / loss factor by session_shaping) and the make argv of each change
        (exp._apply_net_stat's: `make slow` with rate / delay / loss, `make rate` for a walk step);
  (ii)  orchestrate's exp command line and env keys per refill in a run_trio with the draw off (the harness of
        test_trio_slots, run id and temp paths normalised).

Run in a tree: `python3 make_trace_off_goldens.py <networkSim dir>` prints the JSON (build()). The committed golden
(trace_off_goldens_d7433b03.json) was written by running it in a T* worktree; test_trace_replay.test_trace_off_identity
runs it in this tree and compares. Only the fields T* had are compared; the regime's new NetStat fields must be None."""
from __future__ import annotations

import json
import random
import sys
from dataclasses import asdict
from pathlib import Path

NS = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NS))

OLD_NETSTAT = ("rate", "duration", "delay_ms", "loss_pct", "state", "redraw_kbit", "redraw_delay_ms", "redraw_loss_pct",
               "walk_step")
KEYS = ("golden:1", "golden:2")


def make_argv(ns) -> list[str]:
    if ns.walk_step:
        return ["make", "rate", f"BANDWIDTH={ns.rate}kbit"]
    return ["make", "slow", f"BANDWIDTH={ns.rate}kbit", f"DELAY={ns.delay_ms}ms", f"PLR={ns.loss_pct}%"]


def generators() -> dict:
    from utils.netStat import family_episode_generator, net_episode_generator, session_shaping
    from utils.shaping import PLATFORM_MATRICES, hold_schedule, initial_state_stationary, schedule_of
    out = {}
    for platform, table in sorted(PLATFORM_MATRICES.items()):
        for mid in sorted(table):
            transitions, cap, init_rule = table[mid]
            for key in KEYS:
                random.seed(f"{key}:{platform}:{mid}")
                walk_rng, rec = session_shaping(f"{key}:{platform}:{mid}")
                f = rec["dl_factor"]["value"]
                sc = schedule_of(mid, cap, 30.0)
                init = initial_state_stationary if init_rule == "stationary" else None
                if sc["schedule"] == "base":
                    gen = net_episode_generator(sc["episode_s"], transitions, max_episodes=3, max_consecutive_low=cap,
                                                initial_state_fn=init, walk_rng=walk_rng, dl_factor=f)
                else:
                    gen = family_episode_generator(sc["episode_s"], transitions, tuple(sc["redraw_s"]), sc["low_cap_s"],
                                                   sc["self_loops"], initial_state_fn=init, max_episodes=3,
                                                   walk_rng=walk_rng, dl_factor=f)
                seq = []
                for ns in gen:
                    d = asdict(ns)
                    extra = {k: v for k, v in d.items() if k not in OLD_NETSTAT and v is not None}
                    seq.append({**{k: d[k] for k in OLD_NETSTAT}, "argv": make_argv(ns), **({"NEW": extra} if extra else {})})
                    if len(seq) >= 60:
                        break
                out[f"{platform}/{mid}/{key}"] = seq
    for st in ("1", "4", "10"):
        h = hold_schedule(st)
        random.seed(f"hold:{st}")
        walk_rng, rec = session_shaping(f"hold:{st}")
        gen = net_episode_generator(60, h["transition"], forced_initial_state=st, max_episodes=3,
                                    max_consecutive_low=h["max_consecutive_low"], walk_rng=walk_rng,
                                    dl_factor=rec["dl_factor"]["value"])
        out[f"hold/{st}"] = [{**{k: asdict(ns)[k] for k in OLD_NETSTAT}, "argv": make_argv(ns)} for ns in gen][:60]
    return out


def orchestrate_cmds() -> dict:
    import importlib.util
    import tempfile
    spec = importlib.util.spec_from_file_location("tts", NS / "test_trio_slots.py")
    TT = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(TT)
    import n_draw
    import record
    O, T = TT.O, TT.T
    d = next(x for x in n_draw.simulate(20, record.ERA) if x["n"] == 4)
    import datetime as _real

    class _Fixed(_real.datetime):                     # a fixed run id: the per-session draws are keyed by it
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 22, 12, 0, 0)
    saved_dt = O.dt
    O.dt = type("dtmod", (), {"datetime": _Fixed, "timedelta": _real.timedelta, "timezone": _real.timezone})
    try:
        with tempfile.TemporaryDirectory() as t:
            tmp, spawned = Path(t), []

            def spawn(cmd, env, log):
                spawned.append((cmd, env, log))
                return TT.FakeProc(1, 0, [])
            TT._trio(tmp, {"youtube": 5, "twitch": 5, "tiktok": 5}, spawn, slots=d["composition"], n_draw=d)
            return _collect(tmp, spawned, T)
    finally:
        O.dt = saved_dt


def _collect(tmp, spawned, T):
    if True:
        ev = [json.loads(x) for x in (tmp / T.TRIO_DIR / T.SLOTS_LOG).read_text().splitlines()]
        run = ev[0]["run"]
        assert run == "20261022_120000", run
        norm = lambda x: str(x).replace(run, "<RUN>").replace(str(tmp), "<TMP>")
        out = {}
        for cmd, env, _ in spawned:
            tag = cmd[cmd.index("--slot") + 1]
            import os
            out[tag] = {"argv": [norm(c) for c in cmd[1:]],     # the keys orchestrate sets (not the caller's own env)
                        "env_keys": sorted(k for k in env if k.startswith("LVDC_") and env[k] != os.environ.get(k)),
                        "session_length": json.loads(norm(env.get("LVDC_SESSION_LENGTH", "null")))}
        return out


def build() -> dict:
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):          # run_trio prints its progress
        return json.loads(json.dumps({"generators": generators(), "orchestrate": orchestrate_cmds()}, sort_keys=True,
                                     default=str))


if __name__ == "__main__":
    print(json.dumps(build(), sort_keys=True))
