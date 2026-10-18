#!/usr/bin/env python3
"""t53 gate 1: the trio dry run (design: LVQoE docs/analysis/design/concurrent_trio_design_peter_2026-10-18.md; collection
prereg Addendum Q am. 13). Standalone: no Chrome, no orchestrator, no capture of record. Run as root on chaos in a
boundary pause (no session running); it tears everything it built down at the end, also on error / Ctrl+C, and the
next session's `make enable` rebuilds single mode's tree as always.

What it does, in order:
  0. checks: root, cgroup v2, nft / tc / curl / ping present, no exp.py or capture running, the interface up;
  1. goodput, unshaped: K parallel bulk downloads in three cgroups for --goodput-s; the multi-stream goodput number
     (interface rx bytes / time) against the gate: >= 1.5 x the largest summed top-state rate (am. 13 gate 1);
  2. the trio tree:
     - three cgroups lvdc.slice/<p> (cgroup v2);
     - nft: `socket cgroupv2 level 2 "lvdc.slice/<p>"` -> meta mark, saved to conntrack (ct mark), every egress
       packet re-marked;
     - tc ingress on the interface: management bypass (as single mode), connmark restore, then per mark
       `fw` -> mirred redirect to the session's IFB (lvi_<p>: root htb default 10, class 1:10 rate, netem child, as
       single mode's ifb0), unmarked -> lvi_def (unshaped, counted);
     - tc egress on the interface: root htb default 30; management class 1:20 (as single mode); one class per mark
       (htb rate + netem child) mirrored to lvo_<p>; unmarked -> 1:30 unshaped, mirrored to lvo_def;
  3. three schedules (one per session, --states; each state held --state-s): a bulk download and a ping in each
     cgroup; per second, every class's bytes / packets / drops / backlog (tc -s -j) and the interface's rx bytes;
  4. the report: per session and state, the realised ingress rate vs the set rate, RTT and loss vs the set delay /
     loss, its curl bytes vs its class bytes, the bytes in the other sessions' classes and in the default class
     while it ran (cross-class), the shaper-bound seconds (backlog > 0 or drops); the goodput gate; versions.
Output: <out>/report.json, <out>/samples.jsonl, <out>/commands.log. --print-only lists the commands and exits.

    sudo python3 ops/diag/trio_dry_run.py --iface wlp0s20f3 --out /tmp/trio_dry_run_$(date +%s)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
try:
    from utils.shaping import SCENARIOS        # the collector's state semantics (rate / delay / loss ranges)
except Exception:                               # the container test copies only this script
    SCENARIOS = None

CG_ROOT = Path("/sys/fs/cgroup")
SLICE = "lvdc.slice"
SESSIONS = {"yt": 0x11, "tw": 0x12, "tt": 0x13}            # session -> fw mark (also its egress class minor)
NFT_TABLE = "lvdc_trio"
DEFAULT_STATES = {"yt": ["3", "7", "10"], "tw": ["5", "9", "2"], "tt": ["8", "4", "6"]}
MGMT_UDP_PORT, MGMT_TCP_PORT = 41641, 22
BULK_URL = "http://mirror.aarnet.edu.au/pub/ubuntu/releases/24.04/ubuntu-24.04.5-live-server-amd64.iso"   # ~3 GB
MARK_BY = "cgroup"  # "uid" only for a test kernel without nft socket cgroupv2 (each session a user lvdc_<p>)
TOP_STATE_KBIT = 9000.0                                      # SCENARIOS state 10's upper rate limit

LOG: list[str] = []


def sh(cmd: str, check: bool = True, capture: bool = False, print_only: bool = False) -> str:
    LOG.append(cmd)
    if print_only:
        print(cmd)
        return ""
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{cmd}\n -> rc {r.returncode}: {r.stderr.strip()[:400]}")
    return r.stdout if capture else ""


def state_params(state: str) -> dict:
    """A state's rate / delay / loss: the middle of its SCENARIOS ranges (deterministic for the dry run)."""
    if SCENARIOS is None:
        rate = {"1": 215, "2": 365, "3": 575, "4": 850, "5": 1200, "6": 1600, "7": 2150, "8": 3250, "9": 5000,
                "10": 7500}[state]
        return {"rate_kbit": rate, "delay_ms": 20, "loss_pct": 0.1}
    sc = SCENARIOS[state]
    return {"rate_kbit": int((sc.rate_min + sc.rate_max) / 2), "delay_ms": int((sc.delay_min + sc.delay_max) / 2),
            "loss_pct": round((sc.loss_pct_min + sc.loss_pct_max) / 2, 2)}


# ------------------------------------------------------------------------------------------------ the tree
def ifb(name: str) -> list[str]:
    return [f"ip link add {name} type ifb", f"ip link set {name} up"]


NO_IFB = False     # --no-ifb (a kernel without IFB, e.g. a test container): ingress filters count per mark, no redirect
NO_CONNMARK = False   # --no-connmark (a kernel without act_connmark, e.g. Docker Desktop's): no ingress mark filters


def tree_up_cmds(iface: str) -> list[str]:
    """The trio tree (module docstring, step 2), management bypass first."""
    c = ["modprobe ifb || true", "modprobe act_connmark || true", "modprobe cls_fw || true", "modprobe sch_netem || true"]
    if not NO_IFB:
        for s in SESSIONS:
            c += ifb(f"lvi_{s}") + ifb(f"lvo_{s}")
        c += ifb("lvi_def") + ifb("lvo_def")
    c += [f"mkdir -p {CG_ROOT / SLICE}"] + [f"mkdir -p {CG_ROOT / SLICE / s}" for s in SESSIONS]
    # nft: mark by socket cgroup, save to conntrack; re-mark on every egress packet
    c.append(f"nft add table inet {NFT_TABLE}")
    c.append(f"nft add chain inet {NFT_TABLE} out '{{ type route hook output priority mangle; policy accept; }}'")
    for s, m in SESSIONS.items():
        match = ('socket cgroupv2 level 2 "%s/%s"' % (SLICE, s)) if MARK_BY == "cgroup" else f"meta skuid lvdc_{s}"
        c.append(f"nft add rule inet {NFT_TABLE} out '{match} meta mark set {m:#x} ct mark set meta mark'")
    # ingress: bypass management, restore the mark, redirect by mark
    c += [f"tc qdisc del dev {iface} root 2>/dev/null || true", f"tc qdisc del dev {iface} ingress 2>/dev/null || true",
          f"tc qdisc add dev {iface} handle ffff: ingress"]
    for proto, port, side in ((17, MGMT_UDP_PORT, "sport"), (17, MGMT_UDP_PORT, "dport"), (6, MGMT_TCP_PORT, "sport"),
                              (6, MGMT_TCP_PORT, "dport")):
        c.append(f"tc filter add dev {iface} parent ffff: protocol ip prio 1 u32 match ip protocol {proto} 0xff "
                 f"match ip {side} {port} 0xffff action ok")
    if not NO_CONNMARK:
        c.append(f"tc filter add dev {iface} parent ffff: protocol all prio 5 matchall action connmark continue")
        for i, (s, m) in enumerate(SESSIONS.items()):
            c.append(f"tc filter add dev {iface} parent ffff: protocol all prio {10 + i} handle {m:#x} fw "
                     + ("action ok" if NO_IFB else f"action mirred egress redirect dev lvi_{s}"))
    c.append(f"tc filter add dev {iface} parent ffff: protocol all prio 50 matchall "
             + ("action ok" if NO_IFB else "action mirred egress redirect dev lvi_def"))
    if not NO_IFB:
        for s in SESSIONS:
            c.append(f"tc qdisc add dev lvi_{s} root handle 1: htb default 10")
    # egress: htb with management, per-mark classes and the unshaped default
    c += [f"tc qdisc add dev {iface} root handle 1: htb default 30",
          f"tc class add dev {iface} parent 1: classid 1:20 htb rate 1000mbit",
          f"tc class add dev {iface} parent 1: classid 1:30 htb rate 1000mbit"]
    for proto, port, side in ((17, MGMT_UDP_PORT, "sport"), (17, MGMT_UDP_PORT, "dport"), (6, MGMT_TCP_PORT, "sport"),
                              (6, MGMT_TCP_PORT, "dport")):
        c.append(f"tc filter add dev {iface} parent 1: protocol ip prio 1 u32 match ip protocol {proto} 0xff "
                 f"match ip {side} {port} 0xffff flowid 1:20")
    for i, (s, m) in enumerate(SESSIONS.items()):
        c.append(f"tc filter add dev {iface} parent 1: protocol all prio {10 + i} handle {m:#x} fw flowid 1:{m:x}"
                 + ("" if NO_IFB else f" action mirred egress mirror dev lvo_{s}"))
    c.append(f"tc filter add dev {iface} parent 1: protocol all prio 50 matchall flowid 1:30"
             + ("" if NO_IFB else " action mirred egress mirror dev lvo_def"))
    return c


def rate_cmds(iface: str, s: str, p: dict) -> list[str]:
    """One session's state: its ingress IFB class and its egress class, as single mode's `make slow` does."""
    m = SESSIONS[s]
    r, d, l = f"{p['rate_kbit']}kbit", f"{p['delay_ms']}ms", f"{p['loss_pct']}%"
    return ([] if NO_IFB else [f"tc class replace dev lvi_{s} parent 1: classid 1:10 htb rate {r}",
                               f"tc qdisc replace dev lvi_{s} parent 1:10 handle 10: netem delay {d} loss {l}"]) + [
            f"tc class replace dev {iface} parent 1: classid 1:{m:x} htb rate {r}",
            f"tc qdisc replace dev {iface} parent 1:{m:x} handle {m:x}0: netem delay {d} loss {l}"]


def tree_down_cmds(iface: str) -> list[str]:
    c = [f"tc qdisc del dev {iface} root", f"tc qdisc del dev {iface} ingress", f"nft delete table inet {NFT_TABLE}"]
    for s in ([] if NO_IFB else list(SESSIONS) + ["def"]):
        c += [f"ip link del lvi_{s}", f"ip link del lvo_{s}"]
    c += [f"rmdir {CG_ROOT / SLICE / s}" for s in SESSIONS] + [f"rmdir {CG_ROOT / SLICE}"]
    return c


# ------------------------------------------------------------------------------------------------ processes
def in_cgroup(s: str, cmd: str) -> str:
    """A shell that moves itself into the session's cgroup, then execs cmd (children inherit the cgroup); with
    MARK_BY uid it also runs as the session's user."""
    inner = f"echo $$ > {CG_ROOT / SLICE / s}/cgroup.procs && exec " + (cmd if MARK_BY == "cgroup"
                                                                      else f"runuser -u lvdc_{s} -- {cmd}")
    return f"sh -c {shlex.quote(inner)}"


def start(cmd: str, log: Path) -> subprocess.Popen:
    LOG.append(f"(bg) {cmd} > {log}")
    return subprocess.Popen(cmd, shell=True, stdout=open(log, "w"), stderr=subprocess.STDOUT, preexec_fn=os.setsid)


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)


def rx_bytes(iface: str) -> int:
    return int(Path(f"/sys/class/net/{iface}/statistics/rx_bytes").read_text())


_CLASS_RE = re.compile(r"^class \S+ (\S+) .*?\n\s*Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) "
                       r"requeues \d+\)\s*\n\s*backlog (\d+)b (\d+)p", re.M | re.S)


def parse_class_stats(text: str) -> dict:
    """{classid: {bytes, packets, drops, overlimits, backlog, qlen}} from `tc -s class show` text (iproute2 6.1 prints
    htb class stats as text even with -j)."""
    out = {}
    for blk in re.split(r"\n(?=class )", text):
        m = _CLASS_RE.search(blk)
        if m:
            out[m.group(1)] = {"bytes": int(m.group(2)), "packets": int(m.group(3)), "drops": int(m.group(4)),
                               "overlimits": int(m.group(5)), "backlog": int(m.group(6)), "qlen": int(m.group(7))}
    return out


def class_stats(dev: str) -> dict:
    try:
        return parse_class_stats(sh(f"tc -s class show dev {dev}", capture=True))
    except Exception:
        return {}


def parse_ingress_filter_stats(text: str) -> dict:
    """{fw handle (hex) or "default" (the pref-50 matchall): bytes} from `tc -s filter show ... parent ffff:` text."""
    out = {}
    for blk in re.split(r"\n(?=filter )", text):
        sent = re.search(r"Sent (\d+) bytes", blk)
        if not sent:
            continue
        h = re.search(r" fw chain \d+ handle (0x[0-9a-f]+)", blk)
        if h:
            out[h.group(1)] = out.get(h.group(1), 0) + int(sent.group(1))
        elif re.search(r"pref 50 matchall", blk):
            out["default"] = out.get("default", 0) + int(sent.group(1))
    return out


def ingress_filter_stats(iface: str) -> dict:
    try:
        return parse_ingress_filter_stats(sh(f"tc -s filter show dev {iface} parent ffff:", capture=True))
    except Exception:
        return {}


def link_stats(dev: str) -> dict:
    p = Path(f"/sys/class/net/{dev}/statistics")
    return {k: int((p / k).read_text()) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")} if p.exists() else {}


# ------------------------------------------------------------------------------------------------ the run
def preflight(iface: str) -> dict:
    out = {"root": os.geteuid() == 0, "cgroup2": (CG_ROOT / "cgroup.controllers").exists(),
           "iface_up": Path(f"/sys/class/net/{iface}/operstate").exists()
           and Path(f"/sys/class/net/{iface}/operstate").read_text().strip() in ("up", "unknown")}
    for tool in ("tc", "nft", "curl", "ping", "ip"):
        out[f"has_{tool}"] = subprocess.run(f"command -v {tool}", shell=True, capture_output=True).returncode == 0
    busy = subprocess.run("pgrep -af '[e]xp.py|[t]cpdump -i ifb' || true", shell=True, capture_output=True,
                          text=True).stdout.strip()
    out["busy"] = busy.splitlines()
    out["versions"] = {"uname": subprocess.run("uname -r", shell=True, capture_output=True, text=True).stdout.strip(),
                       "tc": subprocess.run("tc -V", shell=True, capture_output=True, text=True).stdout.strip(),
                       "nft": subprocess.run("nft -v", shell=True, capture_output=True, text=True).stdout.strip()}
    out["ok"] = all(out[k] for k in ("root", "cgroup2", "iface_up", "has_tc", "has_nft", "has_curl", "has_ping",
                                     "has_ip")) and not out["busy"]
    return out


def goodput(iface: str, url: str, seconds: float, streams: int, out: Path) -> dict:
    """Unshaped multi-stream goodput: `streams` parallel downloads (spread over the three cgroups, before any mark
    rule exists, so through single mode's absence of shaping) for `seconds`; interface rx bytes / time."""
    procs = []
    for k in range(streams):
        s = list(SESSIONS)[k % len(SESSIONS)]
        procs.append(start(in_cgroup(s, f"curl -s -o /dev/null --max-time {seconds + 5} {shlex.quote(url)}"),
                           out / f"goodput_{k}.log"))
    time.sleep(3)                                            # ramp-up excluded
    b0, t0 = rx_bytes(iface), time.time()
    time.sleep(seconds)
    b1, t1 = rx_bytes(iface), time.time()
    stop(procs)
    kbit = (b1 - b0) * 8 / 1000 / (t1 - t0)
    return {"streams": streams, "seconds": round(t1 - t0, 2), "goodput_kbit": round(kbit, 1)}


def run(a) -> dict:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    states = {s: (a.states.get(s) or DEFAULT_STATES[s]) for s in SESSIONS}
    rep: dict = {"what": "t53 trio dry run (gate 1)", "iface": a.iface, "states": states, "state_s": a.state_s,
                 "params": {s: [state_params(x) for x in v] for s, v in states.items()}, "url": a.url}
    rep["preflight"] = pf = preflight(a.iface)
    if not pf["ok"] and not a.force:
        rep["aborted"] = "preflight failed (root, cgroup v2, tools, interface, no session running)"
        return rep
    procs: list[subprocess.Popen] = []
    built = False
    try:
        for c in (f"tc qdisc del dev {a.iface} root", f"tc qdisc del dev {a.iface} ingress"):
            sh(c, check=False)                               # no single-mode tree left from the last session
        for c in tree_up_cmds(a.iface):
            if c.startswith("tc ") or c.startswith("nft "):
                continue                                     # the cgroups and IFBs only, for the goodput phase
            sh(c, check=not c.endswith("|| true") and "modprobe" not in c)
        rep["goodput"] = gp = goodput(a.iface, a.url, a.goodput_s, a.streams, out)
        need = 1.5 * TOP_STATE_KBIT * len(SESSIONS)
        rep["goodput_gate"] = {"needed_kbit": need, "measured_kbit": gp["goodput_kbit"],
                               "pass": gp["goodput_kbit"] >= need,
                               "rule": "measured multi-stream goodput >= 1.5 x the summed top-state rate (am. 13)"}
        for c in tree_up_cmds(a.iface):
            if c.startswith("tc ") or c.startswith("nft "):
                sh(c, check="|| true" not in c)
        built = True
        rep["tree"] = {"ingress_filters": sh(f"tc filter show dev {a.iface} parent ffff:", capture=True),
                       "egress_classes": sh(f"tc class show dev {a.iface}", capture=True),
                       "nft": sh(f"nft list table inet {NFT_TABLE}", capture=True)}
        for s in SESSIONS:
            for c in rate_cmds(a.iface, s, state_params(states[s][0])):
                sh(c)
        for s in SESSIONS:
            procs.append(start(in_cgroup(s, f"curl -s -o /dev/null -w '%{{size_download}}' --max-time "
                                            f"{a.state_s * len(states[s]) - 1} {shlex.quote(a.url)}"), out / f"curl_{s}.log"))
            procs.append(start(in_cgroup(s, f"ping -n -i {a.ping_interval} {a.ping_host}"), out / f"ping_{s}.log"))
        samples, timeline = [], []
        t0 = time.time()
        devs = ([] if NO_IFB else [f"lvi_{s}" for s in SESSIONS]) + [a.iface]
        for k in range(max(len(v) for v in states.values())):
            for s in SESSIONS:
                if k < len(states[s]):
                    p = state_params(states[s][k])
                    if k:
                        for c in rate_cmds(a.iface, s, p):
                            sh(c)
                    timeline.append({"t": round(time.time() - t0, 2), "session": s, "state": states[s][k], **p})
            t_end = time.time() + a.state_s
            while time.time() < t_end:
                samples.append({"t": round(time.time() - t0, 2), "classes": {d: class_stats(d) for d in devs},
                                "ingress_by_mark": ingress_filter_stats(a.iface),
                                "links": {d: link_stats(d) for d in [a.iface, "lvi_def", "lvo_def"]
                                          + [f"lvo_{s}" for s in SESSIONS]}})
                time.sleep(1.0)
        stop(procs)
        procs = []
        with open(out / "samples.jsonl", "w") as fh:
            for x in samples:
                fh.write(json.dumps(x) + "\n")
        rep["timeline"] = timeline
        rep["results"] = summarise(samples, timeline, states, a, out)
    except Exception as e:
        rep["error"] = repr(e)[:600]
    finally:
        stop(procs)
        for c in tree_down_cmds(a.iface):
            sh(c, check=False)
        rep["torn_down"] = True
        (out / "commands.log").write_text("\n".join(LOG) + "\n")
    return rep


def _delta(samples: list[dict], dev: str, cls: str, t0: float, t1: float, key: str = "bytes") -> int:
    w = [x for x in samples if t0 <= x["t"] <= t1 and cls in (x["classes"].get(dev) or {})]
    if len(w) < 2:
        return 0
    return int((w[-1]["classes"][dev][cls].get(key) or 0) - (w[0]["classes"][dev][cls].get(key) or 0))


def _link_delta(samples: list[dict], dev: str, t0: float, t1: float, key: str = "rx_bytes") -> int:
    w = [x for x in samples if t0 <= x["t"] <= t1 and dev in x["links"] and x["links"][dev]]
    return int(w[-1]["links"][dev][key] - w[0]["links"][dev][key]) if len(w) >= 2 else 0


def ping_stats(path: Path) -> list[tuple[float, float | None]]:
    """[(seconds since start, rtt ms or None for a lost reply)] from `ping -n` output (seq gaps = losses)."""
    rows, last = [], None
    for line in path.read_text(errors="ignore").splitlines():
        m = re.search(r"icmp_seq=(\d+).*time=([\d.]+) ms", line)
        if m:
            rows.append((int(m.group(1)), float(m.group(2))))
    return rows


def summarise(samples, timeline, states, a, out) -> dict:
    res = {}
    for s in SESSIONS:
        rows = []
        for k, st in enumerate(states[s]):
            t0 = k * a.state_s + 3.0                          # skip the first 3 s of a state (the change settles)
            t1 = (k + 1) * a.state_s - 0.5
            p = state_params(st)
            own = _delta(samples, f"lvi_{s}", "1:10", t0, t1)
            others = {o: _delta(samples, f"lvi_{o}", "1:10", t0, t1) for o in SESSIONS if o != s}
            default_in = _link_delta(samples, "lvi_def", t0, t1, "tx_bytes")
            drops = _delta(samples, f"lvi_{s}", "1:10", t0, t1, "drops")
            bound = sum(1 for x in samples if t0 <= x["t"] <= t1
                        and ((x["classes"].get(f"lvi_{s}") or {}).get("1:10") or {}).get("backlog"))
            n = sum(1 for x in samples if t0 <= x["t"] <= t1)
            rows.append({"state": st, "set": p, "seconds": round(t1 - t0, 1),
                         "realised_ingress_kbit": round(own * 8 / 1000 / (t1 - t0), 1),
                         "ratio_to_set": round(own * 8 / 1000 / (t1 - t0) / p["rate_kbit"], 3) if p["rate_kbit"] else None,
                         "class_drops": drops, "shaper_bound_share": round(bound / n, 3) if n else None,
                         "other_sessions_bytes_same_window": others, "default_class_bytes_same_window": default_in})
        pings = ping_stats(out / f"ping_{s}.log")
        curl = (out / f"curl_{s}.log").read_text().strip() if (out / f"curl_{s}.log").exists() else ""
        m = f"{SESSIONS[s]:#x}"
        ing = [x["ingress_by_mark"] for x in samples if "ingress_by_mark" in x]
        res[s] = {"per_state": rows, "curl_bytes": int(curl) if curl.isdigit() else None,
                  "ingress_mark_bytes_total": (ing[-1].get(m, 0) - ing[0].get(m, 0)) if len(ing) >= 2 else None,
                  "class_bytes_total": _delta(samples, f"lvi_{s}", "1:10", 0, 1e9),
                  "egress_class_bytes_total": _delta(samples, a.iface, f"1:{SESSIONS[s]:x}", 0, 1e9),
                  "ping": {"replies": len(pings), "rtt_ms_median": (sorted(r for _, r in pings)[len(pings) // 2]
                                                                    if pings else None)}}
    res["cross_class_note"] = ("each session's curl bytes should be its own class's bytes (within header overhead) and "
                               "no other class should rise from it; the default class carries only the OS remainder")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--iface", default="wlp0s20f3")
    ap.add_argument("--out", default=f"/tmp/trio_dry_run_{int(time.time())}")
    ap.add_argument("--state-s", type=float, default=30.0, help="seconds per state")
    ap.add_argument("--states", type=lambda x: dict(kv.split("=") for kv in x.split()), default={},
                    help="per session states, e.g. 'yt=3,7,10 tw=5,9,2 tt=8,4,6'")
    ap.add_argument("--goodput-s", type=float, default=20.0)
    ap.add_argument("--streams", type=int, default=6)
    ap.add_argument("--url", default=BULK_URL)
    ap.add_argument("--ping-host", default="1.1.1.1")
    ap.add_argument("--ping-interval", default="0.2")
    ap.add_argument("--force", action="store_true", help="run despite a failed preflight (never on chaos)")
    ap.add_argument("--no-ifb", action="store_true", help="a kernel without IFB (test container): count, no redirect")
    ap.add_argument("--no-connmark", action="store_true", help="a kernel without act_connmark (test container only)")
    ap.add_argument("--mark-by", choices=("cgroup", "uid"), default="cgroup",
                    help="uid only in a test container whose kernel lacks nft socket cgroupv2 (users lvdc_<p> made)")
    ap.add_argument("--print-only", action="store_true", help="print the tree / rate / teardown commands and exit")
    a = ap.parse_args(argv)
    a.states = {k: v.split(",") for k, v in a.states.items()}
    global NO_IFB, MARK_BY, NO_CONNMARK
    NO_IFB, MARK_BY, NO_CONNMARK = a.no_ifb, a.mark_by, a.no_connmark
    if MARK_BY == "uid" and not a.print_only:
        for s in SESSIONS:
            sh(f"id lvdc_{s} || useradd -M -s /usr/sbin/nologin lvdc_{s}", check=False)
    if a.print_only:
        for c in tree_up_cmds(a.iface) + [x for s in SESSIONS for x in rate_cmds(a.iface, s, state_params("5"))] \
                + tree_down_cmds(a.iface):
            print(c)
        return 0
    rep = run(a)
    Path(a.out, "report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: rep.get(k) for k in ("goodput", "goodput_gate", "error", "aborted", "torn_down")}, default=str))
    print(f"report: {a.out}/report.json")
    return 0 if not rep.get("error") and not rep.get("aborted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
