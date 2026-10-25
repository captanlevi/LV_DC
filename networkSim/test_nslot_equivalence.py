#!/usr/bin/env python3
"""t56, levi's pooling ruling: the N-slot code at `--slots youtube=1,twitch=1,tiktok=1` reproduces the trio's per-slot
commands (LV_DC c1f9181, the line the N = 3 sessions of record ran: levi's pooling condition iii, 2026-10-19; it was
aaba6ae as of 74ea819, kept as fixtures/trio_slot_commands_aaba6ae.json, identical content) exactly, up to the tag
renaming (yt1 -> yt, tw1 ->
tw, tt1 -> tt: lvi_yt1 -> lvi_yt, lvdc.slice/yt1 -> lvdc.slice/yt, chrome_yt1.pid -> chrome_yt.pid, ...). Compared:
each slot's record (mark, CDP port, prio, IFBs, cgroup, egress class) at epochs 0 and 3, its tc / nft / ip commands up,
detach and down, its make variables, and `make -n` of the slot's targets (the shaper at two states, capture, merge,
Chrome) through those variables; plus the shared tree. fixtures/trio_slot_commands_c1f9181.json was written from
c1f9181's trio code (its trio_slots.py and Makefile, this file's commands(); never rewrite it). The N line's intended
differences are outside these commands (FROZEN_COLLECTOR, the c1f9181 row's known deltas): tt's own-IFB room rule,
the sibling claim, the steering / membership / ERA, the live management port (equal here at its default). One known
delta lies inside them and is asserted, then undone, by name (KNOWN_DELTAS): the capture buffer's -B 65536 (t57), the
staged tcpdump's -Z root (t58), Cast off (levi 13:41) and the ingress htb's direct_qlen 1000 (levi 8a81bd5d).

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

FIXTURE = HERE / "fixtures" / "trio_slot_commands_c1f9181.json"
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


def staged_tcpdump_as_root(got: dict) -> list[str]:
    """Known delta (t58, levi / supervisor 2026-10-19: capture staging moved to the exFAT data volume): every slot's
    staged tcpdump runs with -Z root (make variable TCPDUMP_Z). Asserted present in the N code, then taken out, so the
    rest compares exactly; returns what it found."""
    seen = []
    for tag, s in got["slots"].items():
        assert "TCPDUMP_Z=-Z root" in s["make_vars"], (tag, s["make_vars"])
        s["make_vars"] = [v for v in s["make_vars"] if v != "TCPDUMP_Z=-Z root"]
        seen.append(f"{tag}: make_vars TCPDUMP_Z=-Z root")
        if "make_n" in s:
            sc = s["make_n"]["start-capture"]
            assert sc.count("tcpdump -Z root -i") == 2, (tag, sc)
            s["make_n"]["start-capture"] = sc.replace("tcpdump -Z root -i", "tcpdump -i")
            seen.append(f"{tag}: start-capture tcpdump -Z root (2)")
    return seen


def capture_buffer_64mib(got: dict) -> list[str]:
    """Known delta (t57 tw_100023, levi 2026-10-20): every session capture runs with a 64 MiB kernel buffer (-B 65536,
    the Makefile's TCPDUMP_B; no captured byte changes). Asserted on both start-capture tcpdumps, then taken out."""
    seen = []
    for tag, s in got["slots"].items():
        if "make_n" in s:
            sc = s["make_n"]["start-capture"]
            assert sc.count(" -B 65536 -i ") == 2, (tag, sc)
            s["make_n"]["start-capture"] = sc.replace(" -B 65536 -i ", " -i ")
            seen.append(f"{tag}: start-capture tcpdump -B 65536 (2)")
    return seen or ["no make here"]


CAST_OFF = ",MediaRouter,GlobalMediaControlsCastStartStop"


def cast_disabled(got: dict) -> list[str]:
    """Known delta (levi 13:41, the Cast LAN defect): every slot's Chrome launches with Media Router and the Cast
    start/stop control disabled, appended to the one --disable-features list. Asserted on start_chrome, then taken out."""
    seen = []
    for tag, s in got["slots"].items():
        if "make_n" in s:
            sc = s["make_n"]["start_chrome"]
            assert sc.count("OptimizationGuideOnDeviceModel" + CAST_OFF + " ") == 1, (tag, sc)
            s["make_n"]["start_chrome"] = sc.replace(CAST_OFF + " ", " ")
            seen.append(f"{tag}: start_chrome --disable-features ...{CAST_OFF}")
    return seen or ["no make here"]


def ifb_direct_qlen(got: dict) -> list[str]:
    """Known delta (levi 8a81bd5d, WC1 r1: the pre-apply direct path tail-dropped at the IFB's 32-packet queue): every
    slot's ingress htb gets direct_qlen 1000. Asserted on both epochs' slot_up, then taken out."""
    seen = []
    for tag, s in got["slots"].items():
        for k in ("up_e0", "up_e3"):
            hit = [i for i, c in enumerate(s[k]) if c.endswith(" htb default 10 direct_qlen 1000")]
            assert len(hit) == 1, (tag, k, s[k])
            s[k][hit[0]] = s[k][hit[0]][:-len(" direct_qlen 1000")]
            seen.append(f"{tag}: {k} htb direct_qlen 1000")
    return seen


KNOWN_DELTAS = (("the ingress htb direct queue, 1000 packets (levi 8a81bd5d)", ifb_direct_qlen),
                ("Cast discovery off (levi 13:41)", cast_disabled),
                ("the capture buffer, 64 MiB (t57 tw_100023)", capture_buffer_64mib),
                ("staged tcpdump runs as root (t58: staging on the data volume)", staged_tcpdump_as_root))


def main() -> int:
    if "--write" in sys.argv:                          # once, from the trio code (written at c1f9181), never after
        import trio_slots as T
        FIXTURE.write_text(json.dumps(commands(T, list(T.SLOTS.values())), indent=1, sort_keys=True))
        print(f"wrote {FIXTURE.name}")
        return 0
    want = repo_paths(json.loads(FIXTURE.read_text()))
    got = repo_paths(json.loads(json.dumps(current(), sort_keys=True)))
    if not shutil.which("make"):
        for s in want["slots"].values():
            s.pop("make_n", None)
    for name, undo in KNOWN_DELTAS:                    # each intended difference asserted present, then undone
        assert undo(got), name
    assert got == want, "the N code at youtube=1,twitch=1,tiktok=1 differs from the trio's per-slot commands"
    print("test_nslot_equivalence: ok")
    return 0


def test_nslot_equivalence():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
