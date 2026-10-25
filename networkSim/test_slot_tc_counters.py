#!/usr/bin/env python3
"""#15 (supervisor 14:40; per-hole shaper-drop attribution, W-B tt holes; levi's ruling after brad's review of
f35023d3): TcSampler (tc_stats.jsonl, 1 Hz) adds slot_tc, the session's OWN slot per direction (bytes, packets, drops,
overlimits, requeues, backlog, qlen) from `tc -s -j` JSON: in = the ingress IFB lvi_<tag> (root htb, class 1:10, the
netem under 1:10), out = the egress class 1:<mark> on the interface and its netem; tc_err names a failed read. JSON
feeds ONLY slot_tc: the legacy row (in / out / set_rates_kbit / iface / iw / flagged) stays on the text path and its
bytes equal 8af5400's (a golden made by that code, and that code itself when git can show it). tc_fast is untouched:
2 execs per tick, its class source identical. The JSON reader falls back to sudo -n, sticky both ways. A WC1 live dump
(networkSim/fixtures/tc_json_wc1_*.json) is tested when present. No tc, no sudo, no root.

    python3 test_slot_tc_counters.py        (exit 0 = pass)
"""
import importlib.util
import inspect
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trio_slots as T  # noqa: E402

TT = T.build_slots(T.TRIO_COMPOSITION)["tt1"].with_epoch(3)   # egress class 1:313 (mark 0x313): epoch in the id
IFACE = "wlp0s20f3"

# iproute2 6.1 `tc -s -j class show dev lvi_tt1` as printed (one line; rate / ceil in bytes per second, htb xstats at
# top level, "requeues" printed twice by tc: once in the queue stats, once on the backlog line)
IN_CLASSES_RAW = ('[{"class":"htb","handle":"1:10","root":true,"leaf":"8001:","prio":0,"rate":58125,"ceil":58125,'
                  '"burst":1600,"cburst":1600,"bytes":48298,"packets":39,"drops":4,"overlimits":35,"requeues":0,'
                  '"backlog":70979,"qlen":59,"requeues":0,"lended":39,"borrowed":0,"giants":0,"tokens":-1218,'
                  '"ctokens":-1218}]\n')


def _q(kind, handle, parent, by, pk, dr, ov, rq, bl, ql, **opt):
    d = {"kind": kind, "handle": handle, **({"root": True} if parent == "root" else {"parent": parent}),
         "refcnt": 2, "options": opt, "bytes": by, "packets": pk, "drops": dr, "overlimits": ov, "requeues": rq,
         "backlog": bl, "qlen": ql}
    return d


def _c(handle, rate_bps, by, pk, dr, ov, rq, bl, ql, leaf=None):
    return {"class": "htb", "handle": handle, "root": True, **({"leaf": leaf} if leaf else {}), "prio": 0,
            "rate": rate_bps, "ceil": rate_bps, "burst": 1600, "cburst": 1600, "bytes": by, "packets": pk, "drops": dr,
            "overlimits": ov, "requeues": rq, "backlog": bl, "qlen": ql, "lended": pk, "borrowed": 0, "giants": 0,
            "tokens": 100, "ctokens": 100}


IN_QDISCS = json.dumps([
    _q("htb", "1:", "root", 48298, 39, 4, 35, 0, 70979, 59, r2q=10, default="0x10", direct_packets_stat=0),
    _q("netem", "8001:", "1:10", 47000, 38, 4, 0, 0, 70979, 59, limit=1000,
       delay={"delay": 0.08, "jitter": 0, "correlation": 0}, **{"loss-random": {"loss": 0.01, "correlation": 0}})])
# the interface's shared root: management 1:20, default 1:30, three slots' classes (tt1's own 1:313 among them) and
# their netems; only tt1's are its own
OUT_CLASSES = json.dumps([_c("1:20", 125000000, 9000, 90, 0, 0, 0, 0, 0), _c("1:30", 125000000, 5000, 50, 0, 0, 0, 0, 0),
                          _c("1:111", 62500, 111, 1, 0, 0, 0, 0, 0, leaf="8011:"),
                          _c("1:313", 58125, 6862, 37, 1, 4, 2, 413, 5, leaf="8013:"),
                          _c("1:312", 99999, 312, 3, 0, 0, 0, 0, 0, leaf="8012:")])
OUT_QDISCS = json.dumps([_q("htb", "1:", "root", 99999, 999, 1, 9, 0, 413, 5, r2q=10, default="0x30"),
                         _q("netem", "8011:", "1:111", 111, 1, 0, 0, 0, 0, 0, limit=1000),
                         _q("netem", "8013:", "1:313", 6800, 36, 1, 0, 2, 413, 5, limit=1000),
                         _q("netem", "8012:", "1:312", 312, 3, 0, 0, 0, 0, 0, limit=1000)])
SHAPED = SHAPED_313 = {("class", TT.ifb_in): IN_CLASSES_RAW, ("qdisc", TT.ifb_in): IN_QDISCS,
          ("class", IFACE): OUT_CLASSES, ("qdisc", IFACE): OUT_QDISCS}


def _reader(table, calls=None):
    def read(kind, dev):
        if calls is not None:
            calls.append((kind, dev))
        return table.get((kind, dev), "[]"), 0.0, None
    return read


def test_parse_recorded_json():
    c = T.tc_classes(IN_CLASSES_RAW)["1:10"]
    assert c == {"kind": "htb", "parent": "root", "leaf": "8001:", "rate_kbit": 465.0, "bytes": 48298, "packets": 39,
                 "drops": 4, "overlimits": 35, "requeues": 0, "backlog": 70979, "qlen": 59}, c
    q = T.tc_qdiscs(IN_QDISCS)
    assert q["1:"]["kind"] == "htb" and q["1:"]["parent"] == "root"
    assert q["8001:"] == {"kind": "netem", "parent": "1:10", "bytes": 47000, "packets": 38, "drops": 4, "overlimits": 0,
                          "requeues": 0, "backlog": 70979, "qlen": 59}
    # a nested stats object (other iproute2 builds) reads the same; a missing counter is None, never a bool / string
    n = T.tc_qdiscs(json.dumps([{"kind": "netem", "handle": "8001:", "parent": "1:10",
                                 "stats": {"bytes": 5, "packets": 1, "drops": 0, "overlimits": True}}]))["8001:"]
    assert n["bytes"] == 5 and n["overlimits"] is None and n["backlog"] is None
    assert T.tc_classes("[]") == {} and T.tc_qdiscs("") == {} and T.tc_classes("[not json") == {}


def test_json_only():
    """slot_tc reads JSON only: text output parses to nothing (TcSampler's tc_err names it), never the legacy parser"""
    txt = ("class htb 1:10 root leaf 8001: prio 0 rate 465Kbit ceil 465Kbit burst 1600b cburst 1600b \n"
           " Sent 48298 bytes 39 pkt (dropped 4, overlimits 35 requeues 0) \n backlog 70979b 59p requeues 0\n")
    assert T.tc_classes(txt) == {} and T.tc_qdiscs(txt.replace("class", "qdisc")) == {}
    assert T.parse_classes(txt)["1:10"]["bytes"] == 48298          # the legacy text parser, untouched


def test_slot_record_is_the_slots_own_and_non_null():
    """a shaped slot: every part read and non-null, and out is ITS class 1:313 / netem 8013:, never a sibling's"""
    r = T.slot_tc_record(TT, IFACE, IN_CLASSES_RAW, OUT_CLASSES, IN_QDISCS, OUT_QDISCS)
    assert r["in"]["dev"] == "lvi_tt1" and r["out"]["dev"] == IFACE
    for d, p in T.SLOT_TC_PARTS:
        assert r[d][p] is not None and all(r[d][p][k] is not None for k in T.TC_FIELDS), (d, p, r[d][p])
    assert r["in"]["qdisc"]["kind"] == "htb" and r["in"]["netem"]["handle"] == "8001:"
    assert r["out"]["class"]["bytes"] == 6862 and r["out"]["class"]["requeues"] == 2
    assert r["out"]["netem"]["handle"] == "8013:" and r["out"]["netem"]["parent"] == "1:313"
    # before the first `make slow` (htb root only, no class / netem): those parts are None, the row still writes
    r0 = T.slot_tc_record(TT, IFACE, "[]", OUT_CLASSES.replace('"1:313"', '"1:999"'),
                          json.dumps([json.loads(IN_QDISCS)[0]]), "[]")
    assert r0["in"]["qdisc"] is not None and r0["in"]["class"] is None and r0["in"]["netem"] is None
    assert r0["out"]["class"] is None and r0["out"]["netem"] is None


# ---------------------------------------------------------------- the legacy row: byte-identical to 8af5400
OLD_SHA = "8af5400"
LEGACY_GOLDEN = HERE / "fixtures" / "tc_stats_legacy_rows_8af5400.jsonl"
LEGACY_TS0 = 1792599789.5
_LVI = ("class htb 1:10 root leaf 8001: prio 0 rate {r}Kbit ceil {r}Kbit burst 1599b cburst 1599b \n"
        " Sent {by} bytes {pk} pkt (dropped {dr}, overlimits {ov} requeues 0) \n backlog {bl}b {ql}p requeues 0 \n"
        " lended: {pk} borrowed: 0 giants: 0\n tokens: -1218 ctokens: -1218\n\n")
_EGR = ("class htb 1:20 root prio 0 rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b \n"
        " Sent 900120 bytes 9001 pkt (dropped 0, overlimits 0 requeues 0) \n backlog 0b 0p requeues 0 \n"
        " lended: 9001 borrowed: 0 giants: 0\n tokens: 172 ctokens: 172\n\n"
        "class htb 1:30 root prio 0 rate 1Gbit ceil 1Gbit burst 1375b cburst 1375b \n"
        " Sent 5000 bytes 50 pkt (dropped 0, overlimits 0 requeues 0) \n backlog 0b 0p requeues 0 \n"
        " lended: 50 borrowed: 0 giants: 0\n tokens: 172 ctokens: 172\n\n"
        "class htb 1:111 root leaf 8011: prio 0 rate 873Kbit ceil 873Kbit burst 1599b cburst 1599b \n"
        " Sent 111 bytes 1 pkt (dropped 0, overlimits 0 requeues 0) \n backlog 0b 0p requeues 0 \n"
        " lended: 1 borrowed: 0 giants: 0\n tokens: 228 ctokens: 228\n\n")
_OWN = ("class htb 1:313 root leaf 8013: prio 0 rate {r}Kbit ceil {r}Kbit burst 1599b cburst 1599b \n"
        " Sent {by} bytes {pk} pkt (dropped {dr}, overlimits {ov} requeues 0) \n backlog {bl}b {ql}p requeues 0 \n"
        " lended: {pk} borrowed: 0 giants: 0\n tokens: 1000 ctokens: 1000\n\n")
# per sample: own ingress, own egress (None = before the first `make slow`: no class), the sibling yt1's rate
def _k(r, by, pk, dr, ov, bl, ql):
    return dict(r=r, by=by, pk=pk, dr=dr, ov=ov, bl=bl, ql=ql)


_STEPS = [(None, None, 873),
          (_k(465, 48298, 39, 0, 35, 70979, 59), _k(465, 6862, 37, 0, 4, 413, 5), 873),
          (_k(465, 110350, 90, 2, 80, 0, 0), _k(465, 9000, 50, 0, 4, 0, 0), 196),
          (_k(1863, 402100, 300, 2, 81, 1514, 1), _k(1863, 12000, 70, 1, 5, 0, 0), 154),
          (_k(1863, 640000, 480, 7, 120, 0, 0), _k(1863, 15000, 90, 1, 5, 0, 0), 154),
          (_k(1863, 880000, 660, 7, 120, 0, 0), _k(1863, 17000, 99, 1, 5, 0, 0), 873)]
_IW = "Connected to aa:bb:cc:dd:ee:ff (on wlp0s20f3)\n\trx bitrate: 650.0 MBit/s\n\ttx bitrate: 780.0 MBit/s\n"


def legacy_lines(mod, d: Path, **kw) -> list[str]:
    """TcSampler.sample() x 6 on recorded-form `tc -s class show` text (iproute2 6.1's layout; a tt1 slot at epoch 3,
    egress 1:313, sibling yt1), a fixed clock and a fake /sys/class/net: the lines tc_stats.jsonl gets. `mod` is a
    trio_slots module (this one, or 8af5400's); kw goes to TcSampler (read= for this one)."""
    trio = mod.build_slots(mod.TRIO_COMPOSITION)        # fixed yt1 / tw1 / tt1, whatever LVDC_TRIO_SLOTS another test left
    slot, iface = trio["tt1"].with_epoch(3), IFACE
    net = d / "net"
    for dev in ("lvi_tt1", "lvi_yt1"):
        (net / dev).mkdir(parents=True, exist_ok=True)
    (net / iface / "statistics").mkdir(parents=True, exist_ok=True)
    k = {"i": 0}

    def sh(cmd):
        own_in, own_out, sib = _STEPS[k["i"]]
        if cmd == "tc -s class show dev lvi_tt1":
            return _LVI.format(**own_in) if own_in else ""
        if cmd == f"tc -s class show dev {iface}":
            return _EGR + (_OWN.format(**own_out) if own_out else "")
        if cmd == "tc class show dev lvi_yt1":
            return f"class htb 1:10 root leaf 8002: prio 0 rate {sib}Kbit ceil {sib}Kbit burst 1599b cburst 1599b \n"
        if cmd == f"iw dev {iface} link":
            return _IW
        return ""
    real, slots = time.time, mod.SLOTS
    mod.SLOTS = trio
    try:
        s = mod.TcSampler(d, slot, iface=iface, capacity_kbit=2500.0, sh=sh, net=net, **kw)
        for i in range(len(_STEPS)):
            k["i"] = i
            for name, v in (("rx_bytes", 4814530168 + 70000 * i), ("tx_bytes", 402142773 + 30000 * i)):
                (net / iface / "statistics" / name).write_text(f"{v}\n")
            time.time = lambda i=i: LEGACY_TS0 + 1.01 * i
            s.sample()
    finally:
        time.time, mod.SLOTS = real, slots
    return (d / "tc_stats.jsonl").read_text().splitlines()


def _old_module():
    """8af5400's trio_slots.py (git show), imported under another name; None when git cannot show it."""
    try:
        src = subprocess.run(["git", "show", f"{OLD_SHA}:networkSim/trio_slots.py"], cwd=HERE, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if src.returncode != 0 or not src.stdout:
        return None
    d = Path(tempfile.mkdtemp())
    (d / "trio_slots_8af5400.py").write_text(src.stdout)
    spec = importlib.util.spec_from_file_location("trio_slots_8af5400", d / "trio_slots_8af5400.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m                           # dataclasses look their module up while the class is built
    spec.loader.exec_module(m)
    return m


def _strip_new(line: str) -> str:
    """the line with #15's additive keys cut, as bytes: they come after every legacy key, so the legacy part is a
    prefix; the cut must leave exactly the old line"""
    i = line.find(', "slot_tc": ')
    assert i > 0 and line.endswith("}"), line[-200:]
    return line[:i] + "}"


def test_legacy_row_bytes_identical():
    with tempfile.TemporaryDirectory() as d:
        new = legacy_lines(T, Path(d), read=_reader(SHAPED_313))
    golden = LEGACY_GOLDEN.read_text().splitlines()
    assert len(new) == len(golden) == len(_STEPS)
    assert [_strip_new(x) for x in new] == golden                       # byte for byte, every legacy key
    assert json.loads(golden[0])["in"] is None and json.loads(golden[1])["in"]["bytes"] == 48298   # nulls and values
    rows = [json.loads(x) for x in new]
    assert rows[0]["slot_tc"]["in"]["class"] is not None and "tc_err" not in rows[0]   # slot_tc reads JSON on its own
    old = _old_module()
    if old is not None:                                                  # 8af5400's code itself, not only its golden
        with tempfile.TemporaryDirectory() as d:
            assert legacy_lines(old, Path(d)) == golden


def test_tc_sampler_slot_tc_row():
    calls = []
    with tempfile.TemporaryDirectory() as d:
        net = Path(d) / "net"
        (net / "lvi_tt1").mkdir(parents=True)
        s = T.TcSampler(Path(d), TT, iface=IFACE, sh=lambda c: "", read=_reader(SHAPED, calls), net=net)
        row = s.sample()
        on_disk = json.loads((Path(d) / "tc_stats.jsonl").read_text())
    # the JSON reads: the slot's own devices only, never single mode's ifb0 / ifb1, never a sibling's IFB
    assert calls == [("class", "lvi_tt1"), ("class", IFACE), ("qdisc", "lvi_tt1"), ("qdisc", IFACE)], calls
    assert list(row) == ["ts", "in", "out", "set_rates_kbit", "iface", "iw", "flagged", "slot_tc"]
    sc = row["slot_tc"]
    assert sc["in"]["dev"] == "lvi_tt1" and sc["out"]["dev"] == IFACE
    assert sc["out"]["netem"]["handle"] == "8013:" and sc["out"]["netem"]["drops"] == 1
    assert sc["in"]["netem"]["backlog"] == 70979 and sc["in"]["class"]["requeues"] == 0
    for d_, p in T.SLOT_TC_PARTS:                                        # a shaped slot: every part, every counter
        assert sc[d_][p] is not None and all(isinstance(sc[d_][p][k], int) for k in T.TC_FIELDS), (d_, p)
    assert on_disk["slot_tc"] == json.loads(json.dumps(sc))
    sm = s.summary()
    assert sm["slot_tc_unread"] == {f"{a}.{b}": 0 for a, b in T.SLOT_TC_PARTS}
    assert sm["samples"] == 0                                            # the legacy count: no text was given


def test_tc_sampler_records_why_a_json_read_failed():
    def read(kind, dev):
        return ("class htb 1:10 not json", 0.0, None) if kind == "class" and dev == "lvi_tt1" else \
            ("", 0.0, f"{kind} {dev}: rc 1: Cannot find device \"{dev}\"")
    with tempfile.TemporaryDirectory() as d:
        s = T.TcSampler(Path(d), TT, iface=IFACE, sh=lambda c: "", read=read, net=Path(d) / "none")
        row = s.sample()
    assert row["slot_tc"]["in"]["class"] is None and len(row["tc_err"]) == 4
    assert row["tc_err"][0].startswith("class lvi_tt1: not JSON")
    assert row["tc_err"][1].startswith(f"class {IFACE}: rc 1")
    assert s.summary()["slot_tc_unread"]["in.class"] == 1


def test_slot_tc_failure_never_costs_the_legacy_row():
    def boom(kind, dev):
        raise RuntimeError("reader broke")
    with tempfile.TemporaryDirectory() as d:
        new = legacy_lines(T, Path(d), read=boom)
    rows = [json.loads(x) for x in new]
    assert all("slot_tc" not in r and r["tc_err"] == ["slot_tc: RuntimeError: reader broke"] for r in rows)
    assert [x[:x.index(', "tc_err": ')] + "}" for x in new] == LEGACY_GOLDEN.read_text().splitlines()


# ---------------------------------------------------------------- tc_fast: untouched, 2 execs per tick
def test_fast_sampler_two_execs_per_tick_unchanged():
    calls = []

    def read(dev):                                       # A18's reader interface: one `tc -s class show` per call
        calls.append(dev)
        return _LVI.format(r=465, by=1, pk=1, dr=0, ov=0, bl=0, ql=0).replace("1:10", "1:10" if dev == TT.ifb_in
                                                                               else TT.egress_class), 0.0
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), TT, iface=IFACE, period_s=0.02, read=read).start()
        time.sleep(0.2)
        sm = s.stop()
        lines = [json.loads(x) for x in (Path(d) / "tc_fast.jsonl").read_text().splitlines()]
    assert sm["samples"] == len(lines) - 1 >= 3
    assert len(calls) == 2 * sm["samples"], (len(calls), sm["samples"])      # exactly 2 execs per tick
    assert calls[:2] == [TT.ifb_in, IFACE] and set(calls) == {TT.ifb_in, IFACE}
    assert set(lines[0]) == {"anchor"} and all(set(r) == {"t", "in", "t_in", "out", "t_out"} for r in lines[1:])
    assert "slot_tc" not in sm and "tc_read" not in sm
    old = _old_module()                                  # the class and its reader, source-identical to 8af5400
    if old is not None:
        for name in ("FastTcSampler", "_tc_classes", "parse_classes", "FAST_TC_FIELDS", "FAST_TC_S"):
            a, b = getattr(T, name), getattr(old, name)
            assert (inspect.getsource(a) == inspect.getsource(b)) if callable(a) else a == b, name


def test_reader_sudo_fallback_sticky():
    """unprivileged first; on failure sudo -n once; sticky when it works, never retried when it does not"""
    argvs = []

    def ex(rc_plain, rc_sudo):
        def exec_(argv, timeout_s):
            argvs.append(argv)
            rc = rc_sudo if argv[0] == "sudo" else rc_plain
            return rc, ("[]" if rc == 0 else ""), ("" if rc == 0 else "Operation not permitted"), 0.001
        return exec_
    r = T.TcReader(exec_=ex(1, 0), tc="/usr/sbin/tc")
    assert r("qdisc", "lvi_tt1") == ("[]", 0.002, None) and r.sudo is True
    assert argvs == [["/usr/sbin/tc", "-s", "-j", "qdisc", "show", "dev", "lvi_tt1"],
                     ["sudo", "-n", "/usr/sbin/tc", "-s", "-j", "qdisc", "show", "dev", "lvi_tt1"]]
    argvs.clear()
    r("class", IFACE)
    assert argvs == [["sudo", "-n", "/usr/sbin/tc", "-s", "-j", "class", "show", "dev", IFACE]]   # sticky: one exec
    argvs.clear()
    r2 = T.TcReader(exec_=ex(127, 1), tc="tc")
    out, _, err = r2("class", "lvi_tt1")
    assert out == "" and r2.sudo is False and "rc 127" in err and "sudo -n: rc 1" in err
    argvs.clear()
    r2("class", "lvi_tt1")
    assert len(argvs) == 1 and argvs[0][0] == "tc"                           # sudo never retried
    rec = r2.record()
    assert rec["failed"] == 2 and rec["reads"] == 2 and rec["first_error"].startswith("class lvi_tt1: rc 127")
    r3 = T.TcReader(exec_=ex(0, 0), tc="tc")
    r3("class", "lvi_tt1")
    assert r3.sudo is None and r3.failed == 0                                 # works unprivileged: sudo never run


def test_reader_real_exec_stub_tc():
    """the real exec path on a stub tc found on PATH (tc_bin), JSON flags passed, child CPU from wait4; sudo off"""
    with tempfile.TemporaryDirectory() as d:
        tc = Path(d) / "tc"
        tc.write_text("#!/bin/sh\n[ \"$1 $2\" = \"-s -j\" ] || exit 3\n[ \"$6\" = bad ] && { echo nope >&2; exit 2; }\n"
                      f"printf '%s' '{IN_CLASSES_RAW.strip()}'\n")
        tc.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{d}:{old}"
        try:
            r = T.TcReader(timeout_s=60.0)
            r.sudo = False                                                    # this test never runs sudo
            assert r.tc == str(tc)
            out, cpu, err = r("class", "lvi_tt1")
            bad = r("class", "bad")
        finally:
            os.environ["PATH"] = old
    assert err is None and T.tc_classes(out)["1:10"]["bytes"] == 48298 and cpu >= 0.0
    assert bad[0] == "" and bad[2] == "class bad: rc 2: nope", bad


def test_tc_bin_off_path():
    old = os.environ["PATH"]
    os.environ["PATH"] = "/nonexistent"
    try:
        b = T.tc_bin()
    finally:
        os.environ["PATH"] = old
    assert b in T.TC_BIN_FALLBACKS + ("tc",)


def test_single_mode_builds_neither_sampler():
    """single mode (SLOT None) constructs neither sampler; its Makefile targets are untouched (test_make_single_mode)"""
    src = (HERE / "exp.py").read_text()
    assert ("tc_sampler = (trio_slots.TcSampler(session_dir, SLOT, capacity_kbit=args.link_capacity_kbit).start()\n"
            "                  if SLOT is not None else None)") in src
    assert "tc_fast = trio_slots.FastTcSampler(session_dir, SLOT).start() if SLOT is not None else None" in src
    assert src.count("TcSampler(") == 2


# ---------------------------------------------------------------- WC1's live dump (skipped until it exists)
WC1_GLOB = "tc_json_wc1_*.json"
WC1_DIR = HERE / "fixtures"          # expected at networkSim/fixtures/tc_json_wc1_<tag>.json, one file per slot:
# {"slot": "<tag>", "mark": "0x313", "iface": "wlp0s20f3", "tc_version": "<tc -V>", "captured_at": "<iso>",
#  "dumps": {"class <ifb_in>": "<stdout of tc -s -j class show dev lvi_<tag>>", "qdisc <ifb_in>": "...",
#            "class <iface>": "...", "qdisc <iface>": "..."}}   (raw stdout text, captured while the slot is shaped)


def test_wc1_live_json_fixture():
    files = sorted(WC1_DIR.glob(WC1_GLOB))
    if not files:
        msg = f"no {WC1_GLOB} in {WC1_DIR} (WC1 captures one live tc -s -j class + qdisc dump per slot)"
        if "pytest" in sys.modules:
            import pytest
            pytest.skip(msg)
        print(f"test_wc1_live_json_fixture: skipped: {msg}")
        return
    for f in files:
        fx = json.loads(f.read_text())
        slot = T.Slot(platform="", tag=fx["slot"], mark=int(fx["mark"], 16), cdp_port=0, prio=0)   # names only
        dm = fx["dumps"]
        texts = [dm[f"class {slot.ifb_in}"], dm[f"class {fx['iface']}"], dm[f"qdisc {slot.ifb_in}"],
                 dm[f"qdisc {fx['iface']}"]]
        for t in texts:
            assert T._tc_json(t) is not None, (f.name, t[:120])
        sc = T.slot_tc_record(slot, fx["iface"], *texts)
        for d_, p in T.SLOT_TC_PARTS:
            assert sc[d_][p] is not None, (f.name, d_, p)
            assert all(isinstance(sc[d_][p][k], int) for k in T.TC_FIELDS), (f.name, d_, p, sc[d_][p])
        assert sc["in"]["class"]["rate_kbit"] and sc["out"]["class"]["rate_kbit"], f.name
        assert sc["in"]["qdisc"]["kind"] == "htb" and sc["in"]["netem"]["kind"] == sc["out"]["netem"]["kind"] == "netem"


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_slot_tc_counters: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
