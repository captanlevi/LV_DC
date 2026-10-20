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
Levi's gate-1 conditions (2026-10-19), each a phase of its own:
  - idle delay / loss per class: before the bulk, every class at each of its states with NO bulk in it, a ping from
    each cgroup (RTT minus the unshaped idle RTT vs 2 x the set delay: netem on both directions), and the netem
    qdiscs' own drop counters under the bulk (the realised loss per direction);
  - cross-class bytes: one page fetch inside each cgroup in turn, every class's counters before and after (its own
    class rises by the page, no other session's class rises);
  - the single-mode null for the same states: single mode's own tree (the Makefile's enable / slow / disable) under
    the same bulk and idle ping, one state at a time; and, with --data, the accepted single-mode sessions'
    check.json shaper ratios per state ([min, max], n);
  - every state and the goodput run >= 60 s.
Output: <out>/report.json, <out>/samples.jsonl, <out>/commands.log. --print-only lists the commands and exits.

    sudo .venv/bin/python ops/diag/trio_dry_run.py --iface wlp0s20f3 --out /tmp/trio_dry_run_$(date +%s)

The collector's venv Python (numpy): the script checks its imports before any phase and records the interpreter.
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
import threading
import time
from pathlib import Path

try:
    import numpy as np            # checked in main() before any phase (pilot-0b: system Python, no numpy, after goodput)
except ImportError:
    np = None

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
try:
    from utils.shaping import SCENARIOS        # the collector's state semantics (rate / delay / loss ranges)
except Exception:                               # the container test copies only this script
    SCENARIOS = None

CG_ROOT = Path("/sys/fs/cgroup")
SLICE = "lvdc.slice"
NFT_TABLE = "lvdc_trio"
# tw holds states 1 and 2 (pilot-0: twitch's per-second rate p95 at its two lowest states, against single mode's)
PLATFORM_STATES = {"youtube": ["3", "7", "10"], "twitch": ["1", "2", "9"], "tiktok": ["8", "4", "6"]}
_ABBREV = {"youtube": "yt", "twitch": "tw", "tiktok": "tt"}


def sessions_of(composition: dict) -> dict:
    """t56: tag -> fw mark (also its egress class minor) for a composition, by trio_slots.build_slots' rule (platform order
    youtube, twitch, tiktok; slot k: <abbrev><i>, mark 0x11 + k). Inline: the container test copies only this script
    (test_trio_dry_run checks it against build_slots)."""
    out, k = {}, 0
    for p in ("youtube", "twitch", "tiktok"):
        for i in range(1, int(composition.get(p, 0)) + 1):
            out[f"{_ABBREV[p]}{i}"] = 0x11 + k
            k += 1
    return out


def platform_of(tag: str) -> str:
    return {v: k for k, v in _ABBREV.items()}[re.sub(r"\d+$", "", tag)]


SESSIONS = sessions_of({"youtube": 1, "twitch": 1, "tiktok": 1})    # --slots changes it (main)
DEFAULT_STATES = {s: PLATFORM_STATES[platform_of(s)] for s in SESSIONS}
MGMT_UDP_PORT, MGMT_TCP_PORT = 41641, 22             # the fallback; the tree uses _mgmt_udp() (t58 L-6)


def _mgmt_udp() -> int:
    """t58 L-6: the live tailscaled port, the same lookup as production's tree (trio_slots.mgmt_udp_port)."""
    import trio_slots
    return trio_slots.mgmt_udp_port()
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
    for proto, port, side in ((17, _mgmt_udp(), "sport"), (17, _mgmt_udp(), "dport"), (6, MGMT_TCP_PORT, "sport"),
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
    for proto, port, side in ((17, _mgmt_udp(), "sport"), (17, _mgmt_udp(), "dport"), (6, MGMT_TCP_PORT, "sport"),
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
                 "interpreter": {"executable": sys.executable, "python": sys.version.split()[0],
                                 "numpy": getattr(np, "__version__", None)},
                 "params": {s: [state_params(x) for x in v] for s, v in states.items()}, "url": a.url}
    rep["preflight"] = pf = preflight(a.iface)
    if not pf["ok"] and not a.force:
        rep["aborted"] = "preflight failed (root, cgroup v2, tools, interface, no session running)"
        return rep
    procs: list[subprocess.Popen] = []
    built = False
    # t56 §9: the box record (CPU, swap, OOM, i915 engine busy per second; the PMU, as this runs as root) for the run
    import gpu_record
    box_stop = threading.Event()
    box = threading.Thread(target=gpu_record.record, args=(out / "box_record.jsonl", 1.0, None, box_stop), daemon=True)
    box.start()
    try:
        for c in (f"tc qdisc del dev {a.iface} root", f"tc qdisc del dev {a.iface} ingress"):
            sh(c, check=False)                               # no single-mode tree left from the last session
        for c in tree_up_cmds(a.iface):
            if c.startswith("tc ") or c.startswith("nft "):
                continue                                     # the cgroups and IFBs only, for the goodput phase
            sh(c, check=not c.endswith("|| true") and "modprobe" not in c)
        # P3 (levi 00120): the IFBs with their segmentation / coalescing offloads off, as the collector sets them
        import offloads as _off
        devs = [d for s in SESSIONS for d in (f"lvi_{s}", f"lvo_{s}")] + ["lvi_def", "lvo_def"]
        rep["ifb_offloads"] = {d: _off.ensure_off(d, features=_off.IFB_OFF) for d in devs
                               if sh(f"ip link show {d}", check=False, capture=True)}
        if not all(r["ok"] for r in rep["ifb_offloads"].values()):
            raise RuntimeError(f"P3: IFB offloads not off: {rep['ifb_offloads']}")
        rep["idle_ping_unshaped"] = ping_many([None], a, out, "unshaped")["root"]      # the RTT baseline
        rep["goodput"] = gp = goodput(a.iface, a.url, a.goodput_s, a.streams, out)
        need = 1.5 * TOP_STATE_KBIT * len(SESSIONS)
        rep["goodput_gate"] = {"needed_kbit": need, "measured_kbit": gp["goodput_kbit"],
                               "pass": gp["goodput_kbit"] >= need,
                               "rule": "measured multi-stream goodput >= 1.5 x the summed top-state rate (am. 13)"}
        if not a.no_single_reference:
            rep["single_mode_reference"] = single_mode_reference(a, states, out)
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
        if not NO_IFB:      # levi item 2: the per-slot ingress HTB classes as the kernel holds them (every state: timeline)
            rep["tree"]["ingress_classes"] = {s: tc_detail(f"lvi_{s}") for s in SESSIONS}
        rep["cross_class_fetch"] = cross_class_fetch(a, out)
        idle = []                                     # levi (1): every class at each of its states, no bulk anywhere
        for k in range(max(len(v) for v in states.values())):
            for s in SESSIONS:
                if k < len(states[s]):
                    for c in rate_cmds(a.iface, s, state_params(states[s][k])):
                        sh(c)
            time.sleep(2)
            res = ping_many([s for s in SESSIONS if k < len(states[s])], a, out, f"idle_{k}")
            base = (rep.get("idle_ping_unshaped") or {}).get("rtt_ms_median")
            for s, r in res.items():
                p = state_params(states[s][k])
                idle.append({"session": s, "state": states[s][k], "set": p, **r,
                             "rtt_minus_unshaped_ms": round(r["rtt_ms_median"] - base, 2) if r["rtt_ms_median"] and base else None,
                             "expected_added_ms": 2 * p["delay_ms"],
                             "expected_ping_loss": round(1 - (1 - p["loss_pct"] / 100) ** 2, 5)})
        rep["idle_ping_per_class"] = idle
        for s in SESSIONS:
            for c in rate_cmds(a.iface, s, state_params(states[s][0])):
                sh(c)
        up = None
        if a.upload and not NO_IFB:   # t55 B2 / P3: an upstream load per slot, and each lvo_* captured (snaplen 96)
            upf = out / "upload_4MiB.bin"
            upf.write_bytes(os.urandom(UPLOAD_FILE_BYTES))
            sink = a.udp_sink or default_gateway()
            up = {"url": a.upload_url, "udp_sink": sink, "captures": {}}
            for s in SESSIONS:
                cap = out / f"lvo_{s}.pcap"
                procs.append(start(f"tcpdump -i lvo_{s} -s 96 -nn -U -w {cap}", out / f"tcpdump_lvo_{s}.err"))
                up["captures"][s] = str(cap)
        for s in SESSIONS:
            secs = a.state_s * len(states[s]) - 1
            for i in range(a.bulk_streams):
                procs.append(start(in_cgroup(s, bulk_cmd(a.url, secs)), out / f"curl_{s}_{i}.log"))
            procs.append(start(in_cgroup(s, f"ping -n -i {a.ping_interval} {a.ping_host}"), out / f"ping_{s}.log"))
        up_procs: dict = {s: [] for s in SESSIONS}         # the upload load, restarted at each state with its limits
        samples, timeline = [], []
        t0 = time.time()
        rep["t0_epoch"] = t0          # the bulk schedule's start (brad / levi: C4 / C6 align on it, not by correlation)
        devs = ([] if NO_IFB else [f"lvi_{s}" for s in SESSIONS]) + [a.iface]
        for k in range(max(len(v) for v in states.values())):
            for s in SESSIONS:
                if k < len(states[s]):
                    p = state_params(states[s][k])
                    if k:
                        for c in rate_cmds(a.iface, s, p):
                            sh(c)
                    timeline.append({"t": round(time.time() - t0, 2), "session": s, "state": states[s][k], **p,
                                     "tc_detail": None if NO_IFB else tc_detail(f"lvi_{s}")})
                    if up is not None:
                        stop(up_procs[s])
                        lim = upload_limits(p["rate_kbit"])
                        timeline[-1]["upload"] = lim
                        up_procs[s] = [start(in_cgroup(s, upload_cmd(a.upload_url, out / "upload_4MiB.bin", a.state_s - 1,
                                                                     lim["tcp_limit_Bps"])), out / f"upload_{s}_{k}.log")]
                        if up["udp_sink"]:
                            up_procs[s].append(start(in_cgroup(s, udpgso_cmd(up["udp_sink"], a.state_s - 1, lim["udp_gap_s"])),
                                                     out / f"udpgso_{s}_{k}.log"))
                        procs += up_procs[s]
            t_end = time.time() + a.state_s
            while time.time() < t_end:
                samples.append({"t": round(time.time() - t0, 2), "classes": {d: class_stats(d) for d in devs},
                                "netem": {d: netem_stats(d) for d in devs},
                                "ingress_by_mark": ingress_filter_stats(a.iface),
                                "links": {d: link_stats(d) for d in [a.iface, "lvi_def", "lvo_def"]
                                          + [f"lvo_{s}" for s in SESSIONS]}})
                time.sleep(1.0)
        stop(procs)
        procs = []
        if up is not None:          # C2's oversize line per slot (0 = the capture IFB showed wire packets only)
            time.sleep(1.0)
            up["oversize"] = {s: oversize(Path(c)) for s, c in up["captures"].items()}
            up["kernel_drops"] = {s: (re.findall(r"(\d+) packets? dropped by kernel",
                                                 (out / f"tcpdump_lvo_{s}.err").read_text()) or [None])[-1]
                                  for s in SESSIONS if (out / f"tcpdump_lvo_{s}.err").exists()}
            up["pass"] = all(v["ip_over_1500"] == 0 and v["frames"] > 0 for v in up["oversize"].values())
            rep["upstream_load"] = up
        with open(out / "samples.jsonl", "w") as fh:
            for x in samples:
                fh.write(json.dumps(x) + "\n")
        rep["timeline"] = timeline
        rep["results"] = summarise(samples, timeline, states, a, out)
        if a.fast_segment_s and not NO_IFB:   # Q am. 17: the @fast segment under the bulk load, its own phase
            ph = fast_segment_phase(a, out)
            with open(out / "fast_segment.json", "w") as fh:
                json.dump(ph, fh)
            rep["fast_segment"] = read_fast_segment(ph)
        if a.walk_segment_s and not NO_IFB:   # restart §2: the base schedule with the walk under the bulk load
            ph = fast_segment_phase(a, out, family="base")
            with open(out / "walk_segment.json", "w") as fh:
                json.dump(ph, fh)
            rep["walk_segment"] = read_fast_segment(ph)
        if a.player_load:               # Q am. 16: the player-shaped load, its own phase on the same states
            ph = player_phase(a, states, out)
            rep["player_load"] = {"timeline": ph["timeline"], "t0_epoch": ph["t0_epoch"],
                                  **read_player_phase(ph, a.state_s)}
    except Exception as e:
        rep["error"] = repr(e)[:600]
    finally:
        stop(procs)
        rep["cgroups_left"] = empty_cgroups(SESSIONS)       # every slot cgroup empty before its rmdir
        for c in tree_down_cmds(a.iface):
            sh(c, check=False)
        rep["torn_down"] = True
        box_stop.set()
        box.join(timeout=5)
        try:
            rep["box_record"] = gpu_record.summary(out / "box_record.jsonl")
        except Exception as e:
            rep["box_record"] = {"error": repr(e)[:300]}
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


_NETEM_RE = re.compile(r"qdisc netem (\S+) parent (\S+).*?\n\s*Sent (\d+) bytes (\d+) pkt \(dropped (\d+),", re.S)


def parse_netem_stats(text: str) -> dict:
    """{parent classid: {bytes, packets, drops}} of every netem qdisc in `tc -s qdisc show` text."""
    out = {}
    for blk in re.split(r"\n(?=qdisc )", text or ""):
        m = _NETEM_RE.search(blk)
        if m:
            out[m.group(2)] = {"bytes": int(m.group(3)), "packets": int(m.group(4)), "drops": int(m.group(5))}
    return out


def netem_stats(dev: str) -> dict:
    try:
        return parse_netem_stats(sh(f"tc -s qdisc show dev {dev}", capture=True))
    except Exception:
        return {}


def parse_ping(text: str) -> dict:
    """`ping -n -c N` output: transmitted, received, loss share, the replies' RTT median."""
    rtts = sorted(float(m.group(1)) for m in re.finditer(r"time=([\d.]+) ms", text or ""))
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text or "")
    tx, rx = (int(m.group(1)), int(m.group(2))) if m else (None, len(rtts))
    return {"transmitted": tx, "received": rx, "loss_share": round(1 - rx / tx, 4) if tx else None,
            "rtt_ms_median": rtts[len(rtts) // 2] if rtts else None, "rtt_ms_p90": rtts[int(len(rtts) * 0.9)] if rtts else None}


def ping_cmd(host: str, seconds: float, interval: str) -> str:
    n = max(1, int(seconds / float(interval)))
    return f"ping -n -c {n} -i {interval} -w {int(seconds) + 5} {host}"


def ping_many(sessions: list[str | None], a, out: Path, tag: str) -> dict:
    """Pings from each cgroup (None: no cgroup) at once, for a.ping_s; {session: parse_ping}."""
    procs = {}
    for s in sessions:
        log = out / f"ping_{tag}_{s or 'root'}.log"
        cmd = ping_cmd(a.ping_host, a.ping_s, a.ping_interval)
        procs[s] = (start(in_cgroup(s, cmd) if s else cmd, log), log)
    res = {}
    for s, (p, log) in procs.items():
        try:
            p.wait(timeout=a.ping_s + 15)
        except subprocess.TimeoutExpired:
            stop([p])
        res[s or "root"] = parse_ping(log.read_text(errors="ignore"))
    return res


def cross_class_fetch(a, out: Path) -> dict:
    """Levi (2): one page fetch inside each cgroup in turn; every class's bytes before and after."""
    devs = ([] if NO_IFB else [f"lvi_{s}" for s in SESSIONS])

    def snap():
        c = {d: (class_stats(d).get("1:10") or {}).get("bytes", 0) for d in devs}
        c.update({f"{a.iface} 1:{m:x}": (class_stats(a.iface).get(f"1:{m:x}") or {}).get("bytes", 0)
                  for m in SESSIONS.values()})
        c["lvi_def tx"] = (link_stats("lvi_def") or {}).get("tx_bytes", 0)
        return c
    res = {}
    for s in SESSIONS:
        b0 = snap()
        r = subprocess.run(in_cgroup(s, f"curl -s -o /dev/null -w '%{{size_download}}' --max-time 30 "
                                        f"{shlex.quote(a.page_url)}"), shell=True, text=True, capture_output=True)
        LOG.append(f"(fetch {s}) {a.page_url}")
        time.sleep(1.0)
        b1 = snap()
        d = {k: b1[k] - b0[k] for k in b0}
        own = d.get(f"lvi_{s}", 0)
        others = {k: v for k, v in d.items() if k.startswith("lvi_") and k not in (f"lvi_{s}", "lvi_def tx")}
        res[s] = {"page_bytes": int(r.stdout) if r.stdout.strip().isdigit() else None, "deltas": d,
                  "own_class_bytes": own, "other_session_classes_bytes": others,
                  "pass": bool(own) and all(v == 0 for v in others.values())}
    return res


def single_mode_reference(a, states: dict, out: Path) -> dict:
    """Levi (3): single mode's own tree (the Makefile's enable / slow / disable, unchanged) under the same bulk and the
    same idle ping, one state at a time (every state the sessions visit, in order)."""
    nsim = Path(__file__).resolve().parents[2] / "networkSim"
    order = []
    for s in SESSIONS:
        for x in states[s]:
            if x not in order:
                order.append(x)
    rows = []
    sh(f"make -s -C {nsim} enable IFACE={a.iface}", check=False)
    try:
        for st in order:
            p = state_params(st)
            sh(f"make -s -C {nsim} slow IFACE={a.iface} BANDWIDTH={p['rate_kbit']}kbit DELAY={p['delay_ms']}ms "
               f"PLR={p['loss_pct']}%", check=False)
            time.sleep(2)
            idle = ping_many([None], a, out, f"single_{st}")["root"]
            prs = [start(bulk_cmd(a.url, a.ref_state_s), out / f"curl_single_{st}_{i}.log") for i in range(a.bulk_streams)]
            time.sleep(3.0)
            c0, n0, t0 = (class_stats("ifb0").get("1:10") or {}), netem_stats("ifb0").get("1:10") or {}, time.time()
            bound, k, series, prev = 0, 0, [(t0, c0.get("bytes"))], c0
            while time.time() < t0 + a.ref_state_s - 3:
                cs = class_stats("ifb0").get("1:10") or {}
                series.append((time.time(), cs.get("bytes")))
                bound += _shaper_bound(prev, cs)              # backlog or drops grew (trio_slots' one definition)
                prev, k = cs, k + 1
                time.sleep(1.0)
            detail = tc_detail("ifb0")
            c1, n1, t1 = (class_stats("ifb0").get("1:10") or {}), netem_stats("ifb0").get("1:10") or {}, time.time()
            stop(prs)
            ok = [x for x in series if x[1] is not None]             # the first and last good counter reads
            kbit = ((ok[-1][1] - ok[0][1]) * 8 / 1000 / (ok[-1][0] - ok[0][0])) if len(ok) >= 2 and ok[-1][0] > ok[0][0] else 0.0
            dp, dd = (n1.get("packets", 0) - n0.get("packets", 0)), (n1.get("drops", 0) - n0.get("drops", 0))
            rows.append({"state": st, "set": p, "realised_ingress_kbit": round(kbit, 1),
                         "ratio_to_set": round(kbit / p["rate_kbit"], 3), "shaper_bound_share": round(bound / k, 3) if k else None,
                         "netem_drop_share_under_bulk": round(dd / (dp + dd), 5) if dp + dd else None, "idle_ping": idle,
                         "per_second_ratio": per_second_ratio(series, p["rate_kbit"]), "tc_detail": detail})
    finally:
        sh(f"make -s -C {nsim} disable IFACE={a.iface}", check=False)
    return {"tree": "single mode's Makefile enable / slow / disable", "states": rows}


PLAYER = Path(__file__).resolve().parent / "player_load.py"
PLAYER_VENV_PY = Path(__file__).resolve().parent / ".venv-load" / "bin" / "python"   # aioquic (PyPI), diag only


def player_proto(k: int) -> str:
    """Q am. 16 / levi's read: each slot alternates HTTP/3 and TCP by state, h3 on even state indexes."""
    return "h3" if k % 2 == 0 else "tcp"


def player_cmd(a, s: str, k: int, p: dict, log: Path) -> str:
    return (f"{shlex.quote(str(a.player_python))} {shlex.quote(str(PLAYER))} --platform {platform_of(s)} "
            f"--rate-kbit {p['rate_kbit']} --proto {player_proto(k)} --seconds {a.state_s - 1} "
            f"--url {shlex.quote(a.player_url)} --fallback-url {shlex.quote(a.player_fallback_url)} --log {log}")


def player_phase(a, states: dict, out: Path) -> dict:
    """Q am. 16's player-shaped load (peter's spec, levi's read 2026-10-20): its own phase on the bulk run's states and
    timeline (state k for every slot at once, each state a.state_s), the player alone in each slot's class (a ratio read
    beside the bulk would be the bulk's); each slot's ingress and the default class captured at snaplen 96 for the
    5-tuple leak check; per-second class samples as the bulk phase's."""
    procs, caps, pl = [], {}, {s: [] for s in SESSIONS}
    for d in [f"lvi_{s}" for s in SESSIONS] + ["lvi_def"]:
        cap = out / f"player_{d}.pcap"
        procs.append(start(f"tcpdump -i {d} -s 96 -nn -U -w {cap}", out / f"tcpdump_player_{d}.err"))
        caps[d] = str(cap)
    samples, timeline = [], []
    t0 = time.time()
    devs = [f"lvi_{s}" for s in SESSIONS] + [a.iface]
    for k in range(max(len(v) for v in states.values())):
        for s in SESSIONS:
            stop(pl[s])
            pl[s] = []
            if k < len(states[s]):
                p = state_params(states[s][k])
                for c in rate_cmds(a.iface, s, p):
                    sh(c)
                log = out / f"player_{s}_{k}.jsonl"
                pl[s] = [start(in_cgroup(s, player_cmd(a, s, k, p, log)), out / f"player_{s}_{k}.err")]
                timeline.append({"t": round(time.time() - t0, 2), "k": k, "session": s, "state": states[s][k],
                                 "proto": player_proto(k), **p, "log": str(log)})
        t_end = time.time() + a.state_s
        while time.time() < t_end:
            samples.append({"t": round(time.time() - t0, 2), "classes": {d: class_stats(d) for d in devs},
                            "links": {d: link_stats(d) for d in [a.iface, "lvi_def"]}})
            time.sleep(1.0)
    for s in SESSIONS:
        stop(pl[s])
    stop(procs)
    time.sleep(1.0)
    drops = {d: (re.findall(r"(\d+) packets? dropped by kernel", (out / f"tcpdump_player_{d}.err").read_text())
                 or [None])[-1] for d in caps if (out / f"tcpdump_player_{d}.err").exists()}
    with open(out / "player_samples.jsonl", "w") as fh:
        for x in samples:
            fh.write(json.dumps(x) + "\n")
    return {"t0_epoch": t0, "samples": samples, "timeline": timeline, "captures": caps, "kernel_drops": drops}


def read_player_phase(ph: dict, state_s: float) -> dict:
    """Q am. 16's gated and reported readings of the player phase (ops/diag/player_reading.py)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("player_reading", Path(__file__).resolve().parent / "player_reading.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    segs_of = {}
    for e in ph["timeline"]:
        try:
            segs_of[(e["session"], e["k"])] = [json.loads(x) for x in Path(e["log"]).read_text().splitlines() if x.strip()]
        except OSError:
            segs_of[(e["session"], e["k"])] = []
    rows = {s: [R.read_state(ph["samples"], s, e["k"], e, segs_of[(s, e["k"])], state_s, ph["t0_epoch"])
                for e in ph["timeline"] if e["session"] == s] for s in SESSIONS}
    tuples = {s: set().union(*[R.five_tuples(segs_of[(s, e["k"])]) for e in ph["timeline"] if e["session"] == s] or [set()])
              for s in SESSIONS}
    pkts = {}
    for d, cap in ph["captures"].items():
        try:
            pkts[d] = R.parse_tcpdump(sh(f"tcpdump -nn -q -r {cap}", capture=True, check=False))
        except Exception as e:  # noqa: BLE001
            pkts[d] = []
    leak = R.leaks(tuples, pkts)
    by_proto = {}
    for s, rs in rows.items():
        for r in rs:
            if r["ratio"] is not None and r["gate"] != "insufficient":
                by_proto.setdefault(r["proto"], []).append(r["ratio"])
    gated = {"ratio": all(r["gate"] in ("pass", "insufficient") for rs in rows.values() for r in rs),
             "h3_exercised": all((r["h3"] or {}).get("gate", "pass") == "pass" for rs in rows.values() for r in rs),
             "no_leak": all(n == 0 for row in leak.values() for n in row.values()),
             "zero_capture_drops": all(v in (None, "0") for v in ph["kernel_drops"].values())}
    return {"per_slot": rows, "leaks": leak, "player_tuples": {s: len(t) for s, t in tuples.items()},
            "capture_kernel_drops": ph["kernel_drops"], "quic_vs_tcp_ratio": {p: R.q(v) for p, v in by_proto.items()},
            "gated": gated, "pass": all(gated.values()), "notes": R.NOTES,
            "insufficient_states": [(s, r["state"]) for s, rs in rows.items() for r in rs if r["gate"] == "insufficient"]}


def _fast():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fast_segment", Path(__file__).resolve().parent / "fast_segment.py")
    F = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(F)
    return F


def _fast_snap(s: str, t0: float, clock=time.time) -> dict:
    return {"t": round(clock() - t0, 3), "class": class_stats(f"lvi_{s}").get("1:10"),
            "netem": netem_stats(f"lvi_{s}").get("1:10")}


def fast_segment_phase(a, out: Path, clock=time.time, sleep=time.sleep, family: str = "fast") -> dict:
    """Q am. 17's landing condition: every slot's class driven by its platform's @fast plan (fast_segment.schedule,
    seeded by --fast-seed) for --fast-segment-s under the bulk load; each change applied with rate_cmds at its time,
    the slot's class / netem snapshotted just before and after the tc commands and fast_segment.SETTLE_S later; every
    device sampled once a second, as the bulk phase does. family "base": restart §2's walk segment, the base matrix's
    plan for --walk-segment-s (the same seed), read by the same gates."""
    F = _fast()
    seconds = a.fast_segment_s if family == "fast" else a.walk_segment_s
    plan = {s: F.schedule(platform_of(s), s, seconds, a.fast_seed, family) for s in SESSIONS}
    for s in SESSIONS:
        for c in rate_cmds(a.iface, s, plan[s][0]):
            sh(c)
    procs = [start(in_cgroup(s, bulk_cmd(a.url, seconds + 5)), out / f"curl_{family}_{s}_{i}.log")
             for s in SESSIONS for i in range(a.bulk_streams)]
    devs = [f"lvi_{s}" for s in SESSIONS] + [a.iface]
    # the first plan step is in force from before t0 (applied above): an initial entry, no snapshots (never a change read)
    changes: dict = {s: [{"pre": {"t": -3.0}, "post": {"t": -3.0}, "settle": None, "params": plan[s][0], "initial": True}]
                     for s in SESSIONS}
    samples = []
    try:
        sleep(3.0)                                   # the load fills the classes before the first change
        t0 = clock()
        ev = sorted([(e["t"], 1, s, i) for s in SESSIONS for i, e in enumerate(plan[s]) if i]
                    + [(float(k), 0, None, None) for k in range(int(seconds) + 1)])
        pending: list = []                           # settle snapshots due: (t, slot, change index)
        for t_ev, kind, s, i in ev:
            while True:
                due = [x for x in pending if x[0] <= t_ev]
                if not due:
                    break
                x = min(due)
                pending.remove(x)
                sleep(max(0.0, t0 + x[0] - clock()))
                changes[x[1]][x[2]]["settle"] = _fast_snap(x[1], t0, clock)
            sleep(max(0.0, t0 + t_ev - clock()))
            if kind == 0:
                samples.append({"t": round(clock() - t0, 3), "classes": {d: class_stats(d) for d in devs},
                                "netem": {d: netem_stats(d) for d in devs}})
                continue
            e = plan[s][i]
            prev = changes[s][-1] if changes[s] else None
            pre = _fast_snap(s, t0, clock)
            if prev is not None and prev.get("settle") is None:
                pending = [x for x in pending if not (x[1] == s and x[2] == len(changes[s]) - 1)]
            for c in rate_cmds(a.iface, s, e):
                sh(c)
            changes[s].append({"pre": pre, "post": _fast_snap(s, t0, clock), "settle": None, "params": e})
            pending.append((t_ev + F.SETTLE_S, s, len(changes[s]) - 1))
    finally:
        stop(procs)
    return {"plan": plan, "changes": changes, "samples": samples, "seconds": seconds, "seed": a.fast_seed,
            "family": family}


def read_fast_segment(ph: dict) -> dict:
    """The @fast segment's gates per slot (fast_segment's docstring) and the overall pass."""
    F = _fast()
    per = {}
    for s, ch in ph["changes"].items():
        per[s] = {"matrix_id": ph["plan"][s][0]["matrix_id"], "changes": sum(1 for c in ch if not c.get("initial")),
                  "change_drops": {k: v for k, v in F.change_drops(ch).items() if k != "per_change"},
                  "ratio_non_straddling": F.ratio_non_straddling(ph["samples"], f"lvi_{s}", "1:10", ch, 0.0,
                                                                 float(ph["seconds"])),
                  **F.whole_drop_share(ch)}
    ok = bool(per) and all(v["change_drops"]["gate"] == "pass" and v["ratio_non_straddling"]["gate"] == "pass"
                           for v in per.values())
    return {"per_slot": per, "pass": ok, "seconds": ph["seconds"], "seed": ph["seed"], "notes": F.NOTES,
            "family": ph.get("family", "fast"),
            "rule": "Q am. 17: no class drops at change instants beyond the set loss; the ratio in "
                    f"[{F.RATIO_LO}, {F.RATIO_HI}] on backlogged seconds not straddling a change"}


def bulk_cmd(url: str, seconds: float) -> str:
    """One bulk stream for `seconds`: curl again whenever a transfer ends (pilot-0b: a single download stalled at
    state 1 in both trees and the class sat idle); each transfer's bytes on its own line."""
    loop = (f'end=$(( $(date +%s) + {int(seconds)} )); while :; do rem=$(( end - $(date +%s) )); '
            f'[ "$rem" -gt 0 ] || break; curl -s -o /dev/null -w "%{{size_download}}\\n" --max-time "$rem" '
            f'{shlex.quote(url)}; done')
    return "sh -c " + shlex.quote(loop)


UPLOAD_URL = "https://speed.cloudflare.com/__up"     # accepts large POSTs (t55 B2 upstream load)
UPLOAD_FILE_BYTES = 4 * 2 ** 20
UPLOAD_SHARE = 0.3          # levi (B2): the upload load at <= 0.3 x the state's egress set rate, half TCP, half UDP
_UDPGSO_PY = ("import socket, sys, time\n"
              "end, sink, gap = time.time() + float(sys.argv[1]), sys.argv[2], float(sys.argv[3])\n"
              "u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.setsockopt(17, 103, 1200)\n"
              "n = e = 0\n"
              "while time.time() < end:\n"
              "    try:\n        u.sendto(b'q' * 4800, (sink, 9)); n += 1\n"
              "    except OSError:\n        e += 1\n"
              "    time.sleep(gap)\n"
              "print('udp_gso sends', n, 'errors', e, flush=True)\n")


def upload_limits(rate_kbit: float) -> dict:
    """levi (B2: an unlimited upload at the low states saturated the slot's uplink class and starved the bulk's ACKs):
    the state's upload budget, UPLOAD_SHARE x its egress set rate, half to the TCP POSTs (curl --limit-rate, B/s), half
    to the UDP GSO sends (4800 B each, the gap between sends)."""
    half = UPLOAD_SHARE * rate_kbit * 125.0 / 2            # B/s
    return {"tcp_limit_Bps": max(int(half), 1000), "udp_gap_s": round(4800.0 / max(half, 1.0), 3)}


def upload_cmd(url: str, path: Path, seconds: float, limit_bps: int | None = None) -> str:
    """t55 B2 / P3 (levi 00120): Chrome-like upstream, 4 MiB HTTPS POSTs back to back for `seconds` (a bulk download
    writes one MSS upstream at most, so it cannot show a sender's > MSS skbs), rate-limited to limit_bps; each POST's
    bytes on its own line."""
    lim = f"--limit-rate {int(limit_bps)} " if limit_bps else ""
    loop = (f'end=$(( $(date +%s) + {int(seconds)} )); while :; do rem=$(( end - $(date +%s) )); '
            f'[ "$rem" -gt 0 ] || break; curl -s -o /dev/null -w "%{{size_upload}} %{{http_code}}\\n" --max-time "$rem" '
            f'{lim}--data-binary @{shlex.quote(str(path))} {shlex.quote(url)}; done')
    return "sh -c " + shlex.quote(loop)


def udpgso_cmd(sink: str, seconds: float, gap_s: float = 0.05) -> str:
    """QUIC-like UDP GSO upstream: one UDP_SEGMENT 1200 send of 4800 B (4 datagrams) every gap_s to sink:9 (discard)."""
    return "python3 -c " + shlex.quote(_UDPGSO_PY) + f" {int(seconds)} {shlex.quote(sink)} {gap_s}"


def empty_cgroups(slots, root: Path = CG_ROOT / SLICE, wait_s: float = 5.0) -> dict:
    """Before the teardown's rmdir (shagun: B2 left empty slot cgroups while the load's children were still exiting):
    cgroup.kill each slot cgroup, then wait until its cgroup.procs is empty. {slot: procs left}."""
    left = {}
    for s in slots:
        cg = Path(root) / s
        try:
            (cg / "cgroup.kill").write_text("1")
        except OSError:
            pass
        end = time.time() + wait_s
        while time.time() < end:
            try:
                if not (cg / "cgroup.procs").read_text().split():
                    break
            except OSError:
                break
            time.sleep(0.1)
        try:
            left[s] = len((cg / "cgroup.procs").read_text().split())
        except OSError:
            left[s] = 0
    return left


def default_gateway() -> str | None:
    m = re.search(r"default via (\S+)", sh("ip route show default", check=False, capture=True) or "")
    return m.group(1) if m else None


def oversize(pcap: Path) -> dict:
    """An upstream capture's frames and those whose IP packet exceeds 1500 B (C2's oversize line; tshark)."""
    def count(flt):
        out = sh(f"tshark -r {shlex.quote(str(pcap))} -Y {shlex.quote(flt)} -T fields -e frame.number 2>/dev/null",
                 check=False, capture=True)
        return len([x for x in (out or "").splitlines() if x.strip()])
    return {"frames": count("ip || ipv6"), "ip_over_1500": count("ip.len > 1500 || ipv6.plen > 1460"),
            "udp_over_1500": count("udp && ip.len > 1500")}


def logged_bytes(paths) -> int | None:
    """The bytes the bulk streams' logs record (one integer per finished transfer)."""
    tot, seen = 0, False
    for p in paths:
        try:
            for line in Path(p).read_text().split():
                if line.isdigit():
                    tot, seen = tot + int(line), True
        except OSError:
            continue
    return tot if seen else None


def per_second_ratio(series: list[tuple[float, int]], rate_kbit: float) -> dict:
    """Per second (consecutive samples), the class's delivered kbit / its set rate: n, p50, p95, max, mean (pilot-0,
    levi 4: the per-second p95 at twitch's low states against single mode's, the mean the long-run delivery)."""
    series = [(t, b) for t, b in series if b is not None]           # a missed counter read is skipped, never 0
    r = [((b1 - b0) * 8 / 1000 / (t1 - t0)) / rate_kbit for (t0, b0), (t1, b1) in zip(series, series[1:])
         if t1 > t0 and rate_kbit and b1 >= b0]
    if not r:
        return {"n": 0}
    v = np.array(r)
    return {"n": len(r), "p50": round(float(np.median(v)), 3), "p95": round(float(np.percentile(v, 95)), 3),
            "max": round(float(v.max()), 3), "mean": round(float(v.mean()), 3)}


def tc_detail(dev: str) -> str:
    """`tc -s -d` class and qdisc of a device (L-A1: the realised HTB rate / ceil / burst / cburst / quantum and netem
    limit, as the kernel holds them)."""
    return (sh(f"tc -s -d class show dev {dev}", capture=True, check=False) + "\n"
            + sh(f"tc -s -d qdisc show dev {dev}", capture=True, check=False))


def check_session_reference(data: Path, states: dict) -> dict:
    """Levi (3), from the record: per platform and state, the accepted single-mode sessions' check.json episode shaper
    ratios (measured / set downstream): n sessions, n episodes, [min, max], median. Demand-limited (players do not
    saturate), so a lower bound only (the gate-2 checklist's one-sided reading)."""
    plat = {s: platform_of(s) for s in SESSIONS}
    out = {}
    for s, pl in plat.items():
        by: dict = {}
        for c in sorted((data / pl).glob("*/check.json")):
            try:
                chk = json.loads(c.read_text())
            except (OSError, ValueError):
                continue
            if chk.get("verdict") != "PASS":
                continue
            for e in (chk.get("traffic") or {}).get("episodes") or []:
                if e.get("ratio") is not None and str(e.get("state")) in states.get(s, []):
                    by.setdefault(str(e["state"]), {}).setdefault(c.parent.name, []).append(e["ratio"])
        out[s] = {}
        for st in states.get(s, []):
            per = by.get(st) or {}
            vals = sorted(v for x in per.values() for v in x)
            out[s][st] = {"n_sessions": len(per), "n_episodes": len(vals),
                          "min": vals[0] if vals else None, "max": vals[-1] if vals else None,
                          "median": vals[len(vals) // 2] if vals else None,
                          "enough": len(per) >= 5}
    return {"platforms": plat, "source": str(data), "per_session_state": out}


def ping_stats(path: Path) -> list[tuple[float, float | None]]:
    """[(seconds since start, rtt ms or None for a lost reply)] from `ping -n` output (seq gaps = losses)."""
    rows, last = [], None
    for line in path.read_text(errors="ignore").splitlines():
        m = re.search(r"icmp_seq=(\d+).*time=([\d.]+) ms", line)
        if m:
            rows.append((int(m.group(1)), float(m.group(2))))
    return rows


def _reading():
    import importlib.util
    spec = importlib.util.spec_from_file_location("player_reading", Path(__file__).resolve().parent / "player_reading.py")
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    return R


def _qualifying(*a):
    return _reading().qualifying(*a)


def _ratio_gate(*a):
    return _reading().ratio_gate(*a)


def _shaper_bound(prev: dict | None, cur: dict | None) -> bool:
    """levi t58 (brad's B-8): the shaper-bound interval is trio_slots.shaper_bound, imported (it was backlog only here)."""
    import trio_slots
    return trio_slots.shaper_bound(prev, cur)


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
            nw = [x for x in samples if t0 <= x["t"] <= t1 and ((x.get("netem") or {}).get(f"lvi_{s}") or {}).get("1:10")]
            if len(nw) >= 2:
                n0, n1 = nw[0]["netem"][f"lvi_{s}"]["1:10"], nw[-1]["netem"][f"lvi_{s}"]["1:10"]
                dp, dd = n1["packets"] - n0["packets"], n1["drops"] - n0["drops"]
                netem_loss = round(dd / (dp + dd), 5) if dp + dd else None
            else:
                netem_loss = None
            win = [((x["classes"].get(f"lvi_{s}") or {}).get("1:10") or {}) for x in samples if t0 <= x["t"] <= t1]
            bound = sum(_shaper_bound(c0, c1) for c0, c1 in zip(win, win[1:]))   # backlog or drops grew, per interval
            n = max(0, len(win) - 1)
            ser = [(x["t"], ((x["classes"].get(f"lvi_{s}") or {}).get("1:10") or {}).get("bytes"))
                   for x in samples if t0 <= x["t"] <= t1]
            qs, _thr = _qualifying(samples, f"lvi_{s}", "1:10", t0, t1, p["rate_kbit"], p["delay_ms"])
            rows.append({"state": st, "set": p, "seconds": round(t1 - t0, 1),
                         "ratio_backlogged": _ratio_gate(qs, p["rate_kbit"]),
                         "per_second_ratio": per_second_ratio(ser, p["rate_kbit"]),
                         "realised_ingress_kbit": round(own * 8 / 1000 / (t1 - t0), 1),
                         "ratio_to_set": round(own * 8 / 1000 / (t1 - t0) / p["rate_kbit"], 3) if p["rate_kbit"] else None,
                         "class_drops": drops, "shaper_bound_share": round(bound / n, 3) if n else None,
                         "netem_drop_share_under_bulk": netem_loss, "set_loss_share": round(p["loss_pct"] / 100, 5),
                         "other_sessions_bytes_same_window": others, "default_class_bytes_same_window": default_in})
        pings = ping_stats(out / f"ping_{s}.log")
        curl_b = logged_bytes(sorted(out.glob(f"curl_{s}_*.log")))
        m = f"{SESSIONS[s]:#x}"
        ing = [x["ingress_by_mark"] for x in samples if "ingress_by_mark" in x]
        res[s] = {"per_state": rows, "curl_bytes": curl_b,
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
    ap.add_argument("--state-s", type=float, default=60.0, help="seconds per state (levi: >= 60)")
    ap.add_argument("--slots", default=None, help="t56: the composition, e.g. youtube=2,twitch=2,tiktok=2 (default one each)")
    ap.add_argument("--states", type=lambda x: dict(kv.split("=") for kv in x.split()), default={},
                    help="per session states, e.g. 'yt=3,7,10 tw=5,9,2 tt=8,4,6'")
    ap.add_argument("--goodput-s", type=float, default=60.0)
    ap.add_argument("--ping-s", type=float, default=20.0, help="seconds per idle ping (no bulk)")
    ap.add_argument("--ref-state-s", type=float, default=60.0, help="seconds per state in the single-mode reference")
    ap.add_argument("--no-single-reference", action="store_true", help="skip the single-mode tree reference phase")
    ap.add_argument("--no-upload", dest="upload", action="store_false",
                    help="skip the per-slot upstream load (4 MiB HTTPS POSTs + a UDP_SEGMENT sender) and the lvo_* captures")
    ap.add_argument("--upload-url", default=UPLOAD_URL)
    ap.add_argument("--udp-sink", default=None, help="the UDP GSO sender's sink (default: the default gateway, port 9)")
    ap.add_argument("--data", type=Path, default=None, help="the data dir: single-mode check.json ratios per state")
    ap.add_argument("--page-url", default="https://www.wikipedia.org/", help="the cross-class page fetch")
    ap.add_argument("--streams", type=int, default=6)
    ap.add_argument("--bulk-streams", type=int, default=3, help="parallel, self-restarting bulk downloads per class")
    ap.add_argument("--url", default=BULK_URL)
    ap.add_argument("--ping-host", default="1.1.1.1")
    ap.add_argument("--ping-interval", default="0.2")
    ap.add_argument("--force", action="store_true", help="run despite a failed preflight (never on chaos)")
    ap.add_argument("--no-ifb", action="store_true", help="a kernel without IFB (test container): count, no redirect")
    ap.add_argument("--no-connmark", action="store_true", help="a kernel without act_connmark (test container only)")
    ap.add_argument("--mark-by", choices=("cgroup", "uid"), default="cgroup",
                    help="uid only in a test container whose kernel lacks nft socket cgroupv2 (users lvdc_<p> made)")
    ap.add_argument("--print-only", action="store_true", help="print the tree / rate / teardown commands and exit")
    ap.add_argument("--player-load", action="store_true",
                    help="Q am. 16: the player-shaped load phase (h3 / TCP alternating by state) after the bulk one")
    ap.add_argument("--player-python", type=Path, default=PLAYER_VENV_PY,
                    help="the diag venv's Python with aioquic (PyPI): python3 -m venv ops/diag/.venv-load && "
                         "ops/diag/.venv-load/bin/pip install aioquic")
    ap.add_argument("--player-url", default="https://speed.cloudflare.com/__down")
    ap.add_argument("--player-fallback-url", default=BULK_URL, help="TCP only: the HTTP Range fallback")
    ap.add_argument("--fast-segment-s", type=float, default=0.0,
                    help="Q am. 17: seconds of each slot's @fast plan under the bulk load after the bulk phase (the "
                         "schedule families' landing dry run: 180); 0 = off")
    ap.add_argument("--fast-seed", type=int, default=0, help="the @fast and walk plans' seed (recorded)")
    ap.add_argument("--walk-segment-s", type=float, default=0.0,
                    help="restart §2: seconds of each slot's base-matrix plan with the walk (2 s steps between 12-30 s "
                         "re-draws) and a delay / loss factor under the bulk load, after the @fast segment, read by the "
                         "@fast gates; 0 = off")
    a = ap.parse_args(argv)
    a.states = {k: v.split(",") for k, v in a.states.items()}
    global SESSIONS, DEFAULT_STATES
    if a.slots:                                       # t56: the step's composition (--slots youtube=2,twitch=2,tiktok=2)
        comp = {k: int(v) for k, v in (kv.split("=") for kv in a.slots.split(","))}
        SESSIONS = sessions_of(comp)
        DEFAULT_STATES = {s: PLATFORM_STATES[platform_of(s)] for s in SESSIONS}
    global NO_IFB, MARK_BY, NO_CONNMARK
    NO_IFB, MARK_BY, NO_CONNMARK = a.no_ifb, a.mark_by, a.no_connmark
    if MARK_BY == "uid" and not a.print_only:
        for s in SESSIONS:
            sh(f"id lvdc_{s} || useradd -M -s /usr/sbin/nologin lvdc_{s}", check=False)
    if np is None and not a.print_only:           # fail before any phase, not after the goodput run
        print(f"trio_dry_run needs numpy; {sys.executable} has none. Run it with the collector's venv: "
              f"sudo .venv/bin/python ops/diag/trio_dry_run.py ...", file=sys.stderr)
        return 2
    if a.print_only:
        for c in tree_up_cmds(a.iface) + [x for s in SESSIONS for x in rate_cmds(a.iface, s, state_params("5"))] \
                + tree_down_cmds(a.iface):
            print(c)
        return 0
    if a.player_load:                # h3 needs aioquic in the player's Python: fail before any phase
        r = subprocess.run([str(a.player_python), "-c", "import aioquic"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"--player-load needs aioquic in {a.player_python} (python3 -m venv ops/diag/.venv-load && "
                  f"ops/diag/.venv-load/bin/pip install aioquic): {r.stderr.strip()[-200:]}", file=sys.stderr)
            return 4
    if a.state_s < 60 or a.goodput_s < 60:
        print("note: levi's gate-1 condition is >= 60 s per state and for the goodput run", file=sys.stderr)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
    import offloads                                  # t55: the dry runs measure under the collection's offload setting
    off = offloads.ensure_off(a.iface)
    if not off["ok"]:
        print(f"t55: the capture offloads could not be switched off on {a.iface}: {json.dumps(off)}", file=sys.stderr)
        return 3
    rep = run(a)
    rep["capture_offloads"] = off
    if a.data:
        rep["check_session_reference"] = check_session_reference(a.data, rep.get("states") or {})
    Path(a.out, "report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: rep.get(k) for k in ("goodput", "goodput_gate", "error", "aborted", "torn_down")}, default=str))
    print(f"report: {a.out}/report.json")
    return 0 if not rep.get("error") and not rep.get("aborted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
