#!/usr/bin/env python3
"""t56, levi's pooling ruling: the N-slot code at `--slots youtube=1,twitch=1,tiktok=1` reproduces the trio's per-slot
commands (LV_DC aaba6ae, the counted rounds' collector, as of 74ea819) exactly, up to the tag renaming (yt1 -> yt, tw1 ->
tw, tt1 -> tt: lvi_yt1 -> lvi_yt, lvdc.slice/yt1 -> lvdc.slice/yt, chrome_yt1.pid -> chrome_yt.pid, ...). Compared:
each slot's record (mark, CDP port, prio, IFBs, cgroup, egress class) at epochs 0 and 3, its tc / nft / ip commands up,
detach and down, its make variables, and `make -n` of the slot's targets (the shaper at two states, capture, merge,
Chrome) through those variables; plus the shared tree. fixtures/trio_slot_commands_aaba6ae.json was written from the
trio code (python3 test_nslot_equivalence.py --write at 74ea819) before the N refactor; never rewrite it after.

    python3 test_nslot_equivalence.py        (exit 0 = pass)
"""
import json
import re
import shutil
import subprocess
import sys
import os
from pathlib import Path

os.environ.setdefault("LVDC_MGMT_UDP_PORT", "41641")   # t58 L-6: the goldens hold the default port

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_make_single_mode as MS  # noqa: E402

FIXTURE = HERE / "fixtures" / "trio_slot_commands_aaba6ae.json"
IFACE = "wlp0s20f3"
STATES = (["BANDWIDTH=179kbit", "DELAY=45ms", "PLR=0.5%"], ["BANDWIDTH=5200kbit", "DELAY=10ms", "PLR=0.0%"])
SLOT_TARGETS = ("start-capture", "stop-capture", "merge-capture", "start_chrome", "kill_chrome")


def _make_n(target: str, extra) -> str:
    r = subprocess.run(["make", "-s", "-n", target, *MS.VARS, *extra], cwd=HERE, capture_output=True, text=True)
    return MS.normalise((r.stdout + r.stderr).replace(str(HERE.parent), "<REPO>"))


def slot_commands(T, s) -> dict:
    e3 = s.with_epoch(3)
    mv = T.make_vars(e3, Path("/T"))
    out = {"record_e0": s.with_epoch(0).record(), "record_e3": e3.record(),
           "up_e0": T.slot_up_cmds(s.with_epoch(0), IFACE), "up_e3": T.slot_up_cmds(e3, IFACE),
           "detach": T.slot_detach_cmds(e3, IFACE), "down": T.slot_down_cmds(e3, IFACE, nft_handle=7),
           "make_vars": mv}
    if shutil.which("make"):
        out["make_n"] = {**{f"slow_{i}": _make_n("slow", [*st, *mv]) for i, st in enumerate(STATES)},
                         **{t: _make_n(t, mv) for t in SLOT_TARGETS}}
    return out


def commands(T, slots) -> dict:
    return {"shared_up": T.shared_up_cmds(IFACE), "shared_down": T.shared_down_cmds(IFACE),
            "slots": {s.tag: slot_commands(T, s) for s in slots}}


def rename(obj, tags: dict):
    """The N code's tags back to the trio's (yt1 -> yt, ...) in every string, so the two diff on behaviour only."""
    if isinstance(obj, dict):
        return {rename(k, tags): rename(v, tags) for k, v in obj.items()}
    if isinstance(obj, list):
        return [rename(x, tags) for x in obj]
    if isinstance(obj, str):
        for new, old in tags.items():
            obj = re.sub(rf"{re.escape(new)}(?!\d)", old, obj)
        return obj
    return obj


def repo_paths(obj):
    """Any checkout's path to <REPO> (make_vars' CHROME_PREFIX carries in_cgroup.sh's absolute path; the fixture was
    written in /private/tmp/wt-lvdc-nslot), so the comparison holds in every checkout."""
    if isinstance(obj, dict):
        return {k: repo_paths(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [repo_paths(x) for x in obj]
    if isinstance(obj, str):
        return re.sub(r"/[^\s'\"]*?(?=/networkSim/)", "<REPO>", obj)
    return obj


def current() -> dict:
    """The N code at the trio's composition, renamed to the trio's tags."""
    import trio_slots as T
    slots = T.build_slots({"youtube": 1, "twitch": 1, "tiktok": 1})
    tags = {s.tag: {"youtube": "yt", "twitch": "tw", "tiktok": "tt"}[s.platform] for s in slots.values()}
    saved = dict(T.SLOTS)
    T.SLOTS.clear()
    T.SLOTS.update(slots)
    try:
        return rename(commands(T, list(slots.values())), tags)
    finally:
        T.SLOTS.clear()
        T.SLOTS.update(saved)


def main() -> int:
    if "--write" in sys.argv:                          # once, from the trio code (74ea819), before the N refactor
        import trio_slots as T
        FIXTURE.write_text(json.dumps(commands(T, list(T.SLOTS.values())), indent=1, sort_keys=True))
        print(f"wrote {FIXTURE.name}")
        return 0
    want = repo_paths(json.loads(FIXTURE.read_text()))
    got = repo_paths(json.loads(json.dumps(current(), sort_keys=True)))
    if not shutil.which("make"):
        for s in want["slots"].values():
            s.pop("make_n", None)
    assert got == want, "the N code at youtube=1,twitch=1,tiktok=1 differs from the trio's per-slot commands"
    print("test_nslot_equivalence: ok")
    return 0


def test_nslot_equivalence():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
