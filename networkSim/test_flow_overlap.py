#!/usr/bin/env python3
"""flow_overlap.py, the trio's ownership QA, read from the pcaps only: 5-tuple / QUIC CID uniqueness, sibling families,
default-class media, DNS presence (moved from test_trio_slots.py), link-local multicast reported apart and never a leak
(875925f, levi 02:07), the ICMP error read as the flow it quotes and kernel_reply split rows (levi's w10 ruling R2). One
file on the reader line and the collector line (levi 11:37: the same flow_overlap blob on both). No root, no network.

    python3 test_flow_overlap.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import flow_overlap as F                                 # noqa: E402
import trio_slots as T                                   # noqa: E402


def _pkt(t, src, dst, proto, sport, dport, n=100, dcid="", sni="", qname="", resp="", ans="", plen=None):
    r = dict.fromkeys(F.FIELDS, "")
    if plen is not None:
        r["tcp.len" if proto == "tcp" else "udp.length"] = str(plen if proto == "tcp" else plen + 8)
    r.update({"frame.time_epoch": str(t), "frame.len": str(n), "ip.src": src, "ip.dst": dst, "quic.dcid": dcid,
              "tls.handshake.extensions_server_name": sni, "dns.qry.name": qname, "dns.flags.response": resp,
              "dns.count.answers": ans})
    r["tcp.srcport" if proto == "tcp" else "udp.srcport"] = str(sport)
    r["tcp.dstport" if proto == "tcp" else "udp.dstport"] = str(dport)
    return r


def test_flow_overlap():
    me = "10.0.0.2"
    yt = [_pkt(100.0, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011", "rr1---sn-x.googlevideo.com"),
          _pkt(101.0, "142.250.1.1", me, "udp", 443, 50001, 1300, "1122334455667788"),
          _pkt(101.5, me, "172.217.1.1", "tcp", 50002, 443, 400, sni="www.youtube.com"),
          _pkt(99.0, me, "8.8.8.8", "udp", 40000, 53, 80, qname="rr1---sn-x.googlevideo.com", resp="1", ans="1")]
    tw = [_pkt(100.5, me, "151.101.1.1", "tcp", 50100, 443, 900, sni="video-weaver.fra02.hls.ttvnw.net")]
    dflt = [_pkt(98.0, "127.0.0.53", me, "udp", 53, 41000, 90, qname="video-weaver.fra02.hls.ttvnw.net", resp="1", ans="2")]
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt)},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw)}}
    rep = F.report(s, F.summarise(dflt, keep_udp=True))
    assert not rep["leak"], rep
    y = rep["sessions"]["youtube/yt_1"]
    assert y["dns"]["answered_own_pcap"] == 1 and rep["sessions"]["twitch/tw_1"]["dns"]["answered_default_capture"] == 1
    assert y["quic"] == {"bytes_share": round(2600 / 3080, 4), "flows_share": round(1 / 3, 4), "quic_flows": 1, "flows": 3}
    assert y["default_udp_from_media_addresses"]["packets"] == 0 and y["default_udp_from_media_addresses"]["media_addresses"] == 1
    assert F.family("rr1---sn-x.googlevideo.com") == "youtube" and F.family("www.google.com") is None
    # a leak: the tw session also holds a yt flow, a QUIC CID and a yt SNI
    s["twitch/tw_1"]["s"] = F.summarise(tw + [_pkt(100.5, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011",
                                                    "rr1---sn-x.googlevideo.com")])
    rep = F.report(s, F.summarise(dflt, keep_udp=True))
    assert rep["leak"] and rep["pairs"][0]["shared_flows"] == 1 and rep["pairs"][0]["shared_quic_cids"] == 1
    assert rep["sessions"]["twitch/tw_1"]["sibling_family_snis"] == {"youtube": ["rr1---sn-x.googlevideo.com"]}
    # L-A3: yt media (by SNI) in the default class during the yt session
    s["twitch/tw_1"]["s"] = F.summarise(tw)
    rep = F.report(s, F.summarise(dflt + [_pkt(120.0, me, "142.250.9.9", "tcp", 50200, 443, sni="rr5---sn-y.googlevideo.com")],
                                  keep_udp=True))
    assert rep["leak"] and rep["sessions"]["youtube/yt_1"]["default_media_snis_in_window"] == ["rr5---sn-y.googlevideo.com"]
    # the conntrack-timeout case: a short-header QUIC packet from the yt media address, unmarked, on a new client port
    rep = F.report(s, F.summarise(dflt + [_pkt(150.0, "142.250.1.1", me, "udp", 443, 50009, 1200, "99887766aabbccdd")],
                                  keep_udp=True))
    hit = rep["sessions"]["youtube/yt_1"]["default_udp_from_media_addresses"]
    assert rep["leak"] and hit["packets"] == 1 and hit["quic_packets"] == 1 and hit["bytes"] == 1200
    # levi's call on pilot-1, item 11: each Chrome's own mDNS on the fixed 5-tuple is reported when packet-disjoint
    mdns = lambda tt: _pkt(tt, me, "224.0.0.251", "udp", 5353, 5353, 120, plen=78)
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt + [mdns(110.0), mdns(130.0)])},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw + [mdns(115.0), mdns(125.0)])}}
    pr = F.report(s, None)["pairs"][0]
    assert (pr["shared_flows"], pr["shared_unicast_flows"]) == (1, 0), pr
    assert json.loads(json.dumps(pr["shared_link_local_multicast"])) == [{"flow": ["udp", ["10.0.0.2", 5353], ["224.0.0.251", 5353]],
                                                  "packets_a": 2, "packets_b": 2, "packets_in_both": 0}], pr
    s["twitch/tw_1"]["s"] = F.summarise(tw + [mdns(115.0), mdns(130.0)])    # one packet in both pcaps
    assert F.report(s, None)["pairs"][0]["shared_link_local_multicast"][0]["packets_in_both"] == 1
    # split flows: payload-carrying packets in the default class counted apart from control ones (RST / ACK)
    split = [_pkt(102.0, me, "172.217.1.1", "tcp", 50002, 443, 54, plen=0),
             _pkt(102.5, "172.217.1.1", me, "tcp", 443, 50002, 1400, plen=1346)]
    sy = F.summarise(yt, keep_pkts=True)
    sd = F.split_detail(sy, F.summarise(split, keep_pkts=True), "youtube")
    assert sd["flows"] == 1 and sd["rows"][0]["payload_packets"] == 1 and sd["rows"][0]["default_packets"] == 2, sd
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "nf_conntrack_udp_timeout").write_text("30\n")
        (Path(d) / "nf_conntrack_udp_timeout_stream").write_text("120\n")
        assert T.conntrack_timeouts(Path(d)) == {"nf_conntrack_udp_timeout": 30, "nf_conntrack_udp_timeout_stream": 120,
                                                 "nf_conntrack_tcp_timeout_established": None}


def test_split_detail():
    """Supervisor (i): a flow split between a session and the default class, by when its default packets came."""
    me, cdn = "10.0.0.2", "142.250.1.1"
    s_rows = [_pkt(100.0 + i, me, cdn, "udp", 50001, 443, 1200, sni="rr1---sn-x.googlevideo.com" if i == 0 else "")
              for i in range(10)]                                                    # flow A: 100..109
    s_rows += [_pkt(t, me, cdn, "udp", 50002, 443, 1200) for t in (100.0, 105.0, 200.0, 201.0)]   # flow B: a 95 s gap
    s_rows += [_pkt(t, me, cdn, "tcp", 50003, 443, 1500) for t in (100.0, 101.0, 102.0)]           # flow C
    s_rows += [_pkt(t, me, cdn, "tcp", 50004, 443, 1500) for t in (100.0, 110.0)]                  # flow D
    d_rows = [_pkt(115.0, me, cdn, "udp", 50001, 443, 66),                                         # A: after its end
              _pkt(180.0, cdn, me, "udp", 443, 50002, 1200),                                       # B: after the idle gap
              _pkt(101.5, cdn, me, "tcp", 443, 50003, 1500),                                       # C: mid-flow
              _pkt(90.0, me, cdn, "tcp", 50004, 443, 74)]                                          # D: before its first
    d_rows[0]["tcp.flags.str"] = ""
    d_rows[2]["tcp.flags.str"] = "·······A····"
    default = F.summarise(d_rows, keep_udp=True, keep_pkts=True)
    s = F.summarise(s_rows, keep_pkts=set(default["flows"]))
    det = F.split_detail(s, default, "youtube")
    assert det["by_class"] == {"after_last": 1, "after_idle": 1, "mid_flow": 1, "before_first": 1}, det["by_class"]
    assert det["media_flows_by_class"] == {"after_last": 1}
    assert det["default_packets_by_direction"] == {"up": 2, "down": 2}
    rows = {r["flow"][1][1] if r["flow"][1][0] == me else r["flow"][2][1]: r for r in det["rows"]}
    assert rows[50002]["max_gap_before_s"] == 75.0 and rows[50003]["tcp_flags"] == {"·······A····": 1}
    # levi (round 1): a link-local multicast flow (mDNS 5353 -> 224.0.0.251:5353, a host-wide 5-tuple) carries
    # packets_in_both: its default packets matching a session packet by (time, length); avahi's own query matches none
    mrow = [_pkt(t, me, "224.0.0.251", "udp", 5353, 5353, 82) for t in (100.0, 101.0)]
    dm = [_pkt(150.0, me, "224.0.0.251", "udp", 5353, 5353, 87), _pkt(101.0, me, "224.0.0.251", "udp", 5353, 5353, 82)]
    dflt = F.summarise(dm, keep_udp=True, keep_pkts=True)
    sm = F.summarise(mrow, keep_pkts=set(dflt["flows"]))
    r = F.split_detail(sm, dflt, "youtube")["rows"][0]
    assert r["link_local_multicast"] is True and r["packets_in_both"] == 1 and r["default_packets"] == 2, r
    assert "link_local_multicast" not in F.split_detail(s, default, "youtube")["rows"][0]


def test_flow_overlap_linklocal():
    """levi 02:07 (875925f, on the reader's fields): a link-local multicast flow (mDNS on 224.0.0.251 / ff02::fb) shared
    by two sessions or with the default capture is never a leak (rc 0); one shared unicast flow is (rc 1);
    flows_also_in_default_list names each flow with its kind; flows_also_in_default keeps counting both kinds."""
    me = "10.0.0.2"
    yt = [_pkt(100.0, me, "142.250.1.1", "udp", 50001, 443, 1300, "aabbccddeeff0011", "rr1---sn-x.googlevideo.com")]
    tw = [_pkt(100.5, me, "151.101.1.1", "tcp", 50100, 443, 900, sni="video-weaver.fra02.hls.ttvnw.net")]
    mdns = [_pkt(110.0, me, "224.0.0.251", "udp", 5353, 5353, 120)]
    mdns6 = [dict(_pkt(111.0, "", "", "udp", 5353, 5353, 140), **{"ipv6.src": "fe80::1", "ipv6.dst": "ff02::fb"})]
    assert F.is_linklocal(("udp", (me, 5353), ("224.0.0.251", 5353)))
    assert F.is_linklocal(("udp", ("fe80::1", 5353), ("FF02::FB", 5353)))
    assert not F.is_linklocal(("udp", (me, 5353), ("224.0.1.1", 5353))) and not F.is_linklocal(("udp", (me, 1), ("ff05::2", 1)))
    assert F.link_local_multicast is F.is_linklocal
    s = {"youtube/yt_1": {"platform": "youtube", "window": [95.0, 200.0], "s": F.summarise(yt + mdns + mdns6)},
         "twitch/tw_1": {"platform": "twitch", "window": [96.0, 200.0], "s": F.summarise(tw + mdns + mdns6)}}
    rep = F.report(s, F.summarise(mdns, keep_udp=True))
    p = rep["pairs"][0]
    assert not rep["leak"] and F.exit_code({**rep, "dns_fail": []}) == 0, rep
    assert (p["shared_flows"], p["shared_unicast_flows"], len(p["shared_link_local_multicast"])) == (2, 0, 2), p
    y = rep["sessions"]["youtube/yt_1"]
    assert (y["flows_also_in_default"], y["unicast_flows_also_in_default"], y["linklocal_flows_also_in_default"]) == (1, 0, 1)
    assert y["flows_also_in_default_list"] == [["udp", [me, 5353], ["224.0.0.251", 5353], "linklocal"]]
    s["twitch/tw_1"]["s"] = F.summarise(tw + mdns + yt)          # one unicast flow shared: a leak
    rep = F.report(s, F.summarise(mdns, keep_udp=True))
    assert rep["leak"] and rep["pairs"][0]["shared_unicast_flows"] == 1 and F.exit_code({**rep, "dns_fail": []}) == 1
    rep = F.report(s, F.summarise(mdns + tw, keep_udp=True))       # a unicast flow also in the default: a leak
    t = rep["sessions"]["twitch/tw_1"]
    assert rep["leak"] and t["unicast_flows_also_in_default"] == 1 and t["linklocal_flows_also_in_default"] == 1
    assert sorted(x[-1] for x in t["flows_also_in_default_list"]) == ["linklocal", "unicast"]
    assert F.exit_code({"leak": False, "dns_fail": ["x"]}) == 1


def _icmp(t, outer_src, outer_dst, inner_src, inner_dst, sport, dport, dcid="", v6=False, n=106):
    """An ICMP (v6: ICMPv6) error quoting a UDP packet: tshark's occurrence=a gives outer,inner for the address fields
    and the quoted transport fields only."""
    r = _pkt(t, "", "", "udp", sport, dport, n, dcid)
    if v6:
        r.update({"ipv6.src": f"{outer_src},{inner_src}", "ipv6.dst": f"{outer_dst},{inner_dst}", "ipv6.nxt": "58,17"})
    else:
        r.update({"ip.src": f"{outer_src},{inner_src}", "ip.dst": f"{outer_dst},{inner_dst}", "ip.proto": "1,17"})
    r["udp.length"] = "1208"
    return r


def test_icmp_quote_and_kernel_reply():
    """levi's w10 ruling R2 (ti_100091): the default capture's one 'shared' CID was an ICMP port-unreachable from the
    client quoting an inbound Google QUIC packet. An ICMP error is read as the flow it quotes (its inner header), its
    quoted CIDs / SNI / DNS are never the capture's, it is no UDP packet of the default class, and a split row whose
    default packets are all RST-only TCP or ICMP, with no payload, is kernel_reply; any payload packet makes it not."""
    me, g, cid = "192.168.4.50", "142.251.156.119", "f5:b9:b6:9a:10:7b:f1:a8"
    sess = [_pkt(100.0 + i, g, me, "udp", 443, 51000, 1250, cid, plen=1200) for i in range(17)]
    sess += [_pkt(100.0, me, "23.33.238.140", "tcp", 35520, 443, 400, plen=300)]     # the client: the commonest address
    icmp = _icmp(108.5, me, g, g, me, 443, 51000, cid)   # inside the flow's span: also in default
    k = F.flow_key(sess[0])
    assert F.icmp_outer(icmp) and not F.icmp_outer(sess[0]) and F.flow_key(icmp) == k, (F.flow_key(icmp), k)
    echo = dict(_pkt(150.0, me, g, "udp", 0, 0), **{"ip.proto": "1", "udp.srcport": "", "udp.dstport": ""})
    assert F.flow_key(echo) is None                                       # an ICMP message quoting nothing: no flow
    v6 = _icmp(144.0, "2001:db8::50", "2001:db8::77", "2001:db8::77", "2001:db8::50", 443, 51001, cid, v6=True)
    assert F.icmp_outer(v6) and F.flow_key(v6) == ("udp", ("2001:db8::50", 51001), ("2001:db8::77", 443))
    dflt = F.summarise([icmp, echo], keep_udp=True, keep_pkts=True)
    assert dflt["cids"] == {} and dflt["udp"] == [] and dflt["snis"] == {}, dflt
    s = {"tiktok/ti_1": {"platform": "tiktok", "window": [90.0, 200.0], "s": F.summarise(sess, keep_pkts=set(dflt["flows"]))}}
    rec = F.report(s, dflt)["sessions"]["tiktok/ti_1"]
    assert rec["quic_cids_also_in_default"] == 0 and rec["flows_also_in_default"] == 1, rec
    row = F.split_detail(s["tiktok/ti_1"]["s"], dflt, "tiktok")["rows"][0]
    assert (row["kernel_reply"], row["icmp_packets"], row["payload_packets"], row["up"]) == (True, 1, 0, 1), row
    assert row["tcp_flags"] == {} and row["quic_forms"] == {}
    # the RST-only rows of w10 (tw_100103's Fastly flows, ti_100091's Akamai flow): kernel_reply
    tcp = [_pkt(100.0 + i, me, "23.33.238.140", "tcp", 35520, 443, 1400, plen=1346) for i in range(5)]
    rst = [dict(_pkt(102.5 + i, me, "23.33.238.140", "tcp", 35520, 443, 54, plen=0), **{"tcp.flags.str": "·········R··"})
           for i in range(3)]
    d2 = F.summarise(rst, keep_pkts=True)
    r2 = F.split_detail(F.summarise(tcp, keep_pkts=set(d2["flows"])), d2, "tiktok")
    assert r2["rows"][0]["kernel_reply"] is True and r2["kernel_reply_flows"] == 1, r2
    # a FIN carrying the last unacked write (w10 tw_100101): not a kernel reply
    fin = [dict(_pkt(130.0, me, "23.33.238.140", "tcp", 35520, 443, 246, plen=180), **{"tcp.flags.str": "·······AP··F"})]
    d3 = F.summarise(rst + fin, keep_pkts=True)
    r3 = F.split_detail(F.summarise(tcp, keep_pkts=set(d3["flows"])), d3, "tiktok")["rows"][0]
    assert r3["kernel_reply"] is False and r3["payload_packets"] == 1, r3


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("test_flow_overlap: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
