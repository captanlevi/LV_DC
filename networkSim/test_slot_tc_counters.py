#!/usr/bin/env python3
"""#15 (supervisor 14:40; per-hole shaper-drop attribution, W-B tt holes): in trio mode both tc samplers (tc_stats.jsonl
1 s, tc_fast.jsonl 100 ms) read the session's OWN slot: per direction the qdisc and class counters htb and netem expose
(bytes, packets, drops, overlimits, requeues, backlog, qlen), from `tc -s -j` JSON: in = the ingress IFB lvi_<tag>
(root htb, class 1:10, the netem under 1:10), out = the egress class 1:<mark> on the interface and its netem. Rows keep
their keys (in / out / t_in / t_out ...) and add slot_tc (+ t_in_q / t_out_q, tc_err). The reader finds tc off PATH and
falls back to sudo -n once. Single mode builds neither sampler. No tc, no sudo, no root: recorded-form JSON text and a
stub tc.

    python3 test_slot_tc_counters.py        (exit 0 = pass)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trio_slots as T  # noqa: E402

TT = T.SLOTS["tt1"].with_epoch(3)          # egress class 1:313 (mark 0x313): the epoch is in the class id
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
SHAPED = {("class", TT.ifb_in): IN_CLASSES_RAW, ("qdisc", TT.ifb_in): IN_QDISCS,
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


def test_parse_text_fallback():
    """not JSON (an older tc): the text parsers, with the same keys"""
    txt = ("qdisc htb 1: root refcnt 2 r2q 10 default 0x10 direct_packets_stat 0\n"
           " Sent 48298 bytes 39 pkt (dropped 4, overlimits 35 requeues 1) \n backlog 70979b 59p requeues 1\n"
           "qdisc netem 8001: parent 1:10 limit 1000 delay 80ms loss 1%\n"
           " Sent 47000 bytes 38 pkt (dropped 4, overlimits 0 requeues 0) \n backlog 70979b 59p requeues 0\n")
    q = T.tc_qdiscs(txt)
    assert q["1:"]["parent"] == "root" and q["1:"]["requeues"] == 1
    assert q["8001:"] == {"kind": "netem", "parent": "1:10", "bytes": 47000, "packets": 38, "drops": 4, "overlimits": 0,
                          "requeues": 0, "backlog": 70979, "qlen": 59}
    c = T.tc_classes("class htb 1:10 root leaf 8001: prio 0 rate 465Kbit ceil 465Kbit burst 1600b cburst 1600b \n"
                     " Sent 48298 bytes 39 pkt (dropped 4, overlimits 35 requeues 0) \n backlog 70979b 59p requeues 0\n")
    assert c["1:10"]["rate_kbit"] == 465.0 and c["1:10"]["bytes"] == 48298 and c["1:10"]["requeues"] is None


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


def test_tc_sampler_row():
    calls = []
    with tempfile.TemporaryDirectory() as d:
        net = Path(d) / "net"
        (net / "lvi_tt1").mkdir(parents=True)
        s = T.TcSampler(Path(d), TT, iface=IFACE, sh=lambda c: "", read=_reader(SHAPED, calls), net=net)
        row = s.sample()
        on_disk = json.loads((Path(d) / "tc_stats.jsonl").read_text())
    # the slot's own devices only: never single mode's ifb0 / ifb1, never another slot's IFB for the counters
    assert calls[:4] == [("class", "lvi_tt1"), ("class", IFACE), ("qdisc", "lvi_tt1"), ("qdisc", IFACE)], calls
    assert not any(dev in ("ifb0", "ifb1") for _, dev in calls)
    # additive: the old keys in their old order, then slot_tc; the legacy in / out keep their keys and are non-null
    assert list(row)[:5] == ["ts", "in", "out", "set_rates_kbit", "iface"] and "slot_tc" in row and "tc_err" not in row
    assert row["in"] == {"rate_kbit": 465.0, "bytes": 48298, "packets": 39, "drops": 4, "overlimits": 35,
                         "backlog": 70979, "qlen": 59}
    assert row["out"]["bytes"] == 6862 and row["set_rates_kbit"] == {"tt1": 465.0}
    assert row["slot_tc"]["out"]["netem"]["drops"] == 1 and row["slot_tc"]["in"]["netem"]["backlog"] == 70979
    assert on_disk["slot_tc"] == json.loads(json.dumps(row["slot_tc"]))
    sm = s.summary()
    assert sm["samples"] == 1 and sm["slot_tc_unread"] == {f"{d}.{p}": 0 for d, p in T.SLOT_TC_PARTS}


def test_tc_sampler_records_why_a_read_failed():
    def read(kind, dev):
        return "", 0.0, f"{kind} {dev}: rc 1: Cannot find device \"{dev}\""
    with tempfile.TemporaryDirectory() as d:
        s = T.TcSampler(Path(d), TT, iface=IFACE, sh=lambda c: "", read=read, net=Path(d) / "none")
        row = s.sample()
    assert row["in"] is None and row["out"] is None and len(row["tc_err"]) == 4
    assert row["tc_err"][0].startswith("class lvi_tt1: rc 1")
    assert s.summary()["slot_tc_unread"]["in.class"] == 1


def test_fast_sampler_row_and_layout():
    calls = []
    with tempfile.TemporaryDirectory() as d:
        s = T.FastTcSampler(Path(d), TT, iface=IFACE, period_s=0.02, read=_reader(SHAPED, calls)).start()
        time.sleep(0.2)
        sm = s.stop()
        lines = [json.loads(x) for x in (Path(d) / "tc_fast.jsonl").read_text().splitlines()]
    head, rows = lines[0], lines[1:]
    assert head["slot_tc"] == {"fields": list(T.TC_FIELDS),
                               "in": {"dev": "lvi_tt1", "qdisc": "root", "class": "1:10", "netem": "1:10"},
                               "out": {"dev": IFACE, "class": "1:313", "netem": "1:313"}} == sm["slot_tc"]
    assert set(calls) == {("class", "lvi_tt1"), ("class", IFACE), ("qdisc", "lvi_tt1"), ("qdisc", IFACE)}
    r = rows[0]
    assert r["in"] == [48298, 39, 4, 35, 70979, 59] and r["out"] == [6862, 37, 1, 4, 413, 5]      # A18's lists, as before
    assert r["slot_tc"] == {"in": {"qdisc": [48298, 39, 4, 35, 0, 70979, 59], "class": [48298, 39, 4, 35, 0, 70979, 59],
                                   "netem": [47000, 38, 4, 0, 0, 70979, 59]},
                            "out": {"class": [6862, 37, 1, 4, 2, 413, 5], "netem": [6800, 36, 1, 0, 2, 413, 5]}}
    assert all(x is not None for row in rows for d in ("in", "out") for x in row["slot_tc"][d].values())
    assert sm["unread_in"] == sm["unread_out"] == 0 and set(sm["slot_tc_unread"].values()) == {0}


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


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_slot_tc_counters: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
