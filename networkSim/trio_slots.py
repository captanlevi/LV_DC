"""trio_slots.py: trio collection (t53; design LVQoE docs/analysis/design/concurrent_trio_design_peter_2026-10-18.md,
collection prereg Addendum Q am. 13). Behind orchestrate.py --concurrent; single mode never imports this. (Not named
concurrent.py: that shadows the standard library's `concurrent` package, which asyncio imports.)

One slot per platform (A1: rolling, refilled when its session ends). Each session runs in its own cgroup
(lvdc.slice/<tag>), its own Chrome (CDP port), its own IFBs and shaper classes. The tree is the one gate 1 validated on
chaos (ops/diag/trio_dry_run.py, 2026-10-19: a session's class bytes = its own bytes; the unmarked default class only
kB per window):
  - nft: `socket cgroupv2 level 2 "lvdc.slice/<tag>"` -> meta mark, saved to conntrack, every egress packet re-marked;
  - ingress (shared): management bypass (as single mode), connmark restore, then per mark `fw` -> redirect to the
    slot's IFB lvi_<tag> (root htb default 10, class 1:10 + netem: single mode's ifb0 tree), unmarked -> lvi_def;
  - egress (shared root on the interface): htb default 30; management 1:20 (as single mode); per mark a class
    1:<mark> + netem, mirrored to lvo_<tag> for the capture; unmarked -> 1:30 unshaped, mirrored to lvo_def.
The orchestrator brings the shared part up once (shared_up_cmds) and down at the end; each exp.py --slot brings its
slot up and down (slot_up / slot_down: only its own pieces, design §5 "torn down alone") and drives it with single
mode's own make targets through make_vars (slow, start-capture / stop-capture / merge-capture, start_chrome /
kill_chrome on the slot's IFBs, egress class, CDP port, pid files and cgroup). Single mode's ifb0 / ifb1, port 9222 and
pid files are never used by a slot. Commands are strings run as root (`sudo -n` prefixed by apply()).
"""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CG_ROOT = Path("/sys/fs/cgroup")
SLICE = "lvdc.slice"
NFT_TABLE = "lvdc_trio"
IFACE_DEFAULT = "wlp0s20f3"
MGMT_UDP_PORT, MGMT_TCP_PORT = 41641, 22
HERE = Path(__file__).resolve().parent
CURRENT_DATA = Path(__file__).resolve().parent.parent / "current_data"
# A2 (supervisor): during a trio session its pcaps are written on the root filesystem (SSD), not the data volume (USB
# exFAT, three writers); merge-capture writes merged.pcap into the session dir at the session's own end
STAGING = CURRENT_DATA / "trio_staging"
# supervisor 2026-10-19: trio sessions have their own data root, data/_trio/<platform>/ (single mode's accepted counts,
# coverage snapshot, checkpoint list and verify loop never see them: am. 13's "never pooled silently", mechanically)
TRIO_DIR = "_trio"                                  # under the data dir
SLOTS_LOG = "_summaries/trio_slots.jsonl"           # under the trio root
DEFAULT_CAPTURE = "_default"                        # under the trio root: the unmarked remainder, one per trio run
# trio v1 (supervisor 2026-10-19): no fullscreen geometry (one screen, three windows); yt Q_1080p is then unreachable
EXCLUDED_GEOMETRIES = ("fullscreen",)
TC_SAMPLE_S = 1.0
IW_EVERY = 5                                        # `iw link` every IW_EVERY tc samples
FLAGGED_SHARE_QA = 0.05                             # L-A2: > 5 % flagged seconds is a QA flag


@dataclass(frozen=True)
class Slot:
    platform: str
    tag: str
    mark: int
    cdp_port: int
    prio: int            # its tc filter preference on the interface (unique: slot_down deletes its filters by it)

    @property
    def cgroup(self) -> str:
        return f"{SLICE}/{self.tag}"

    @property
    def ifb_in(self) -> str:
        return f"lvi_{self.tag}"

    @property
    def ifb_out(self) -> str:
        return f"lvo_{self.tag}"

    @property
    def egress_class(self) -> str:
        return f"1:{self.mark:x}"

    def record(self) -> dict:
        return {**asdict(self), "mark": f"{self.mark:#x}", "cgroup": self.cgroup, "ifb_in": self.ifb_in,
                "ifb_out": self.ifb_out, "egress_class": self.egress_class}


# marks and preferences as gate 1 (trio_dry_run.SESSIONS); CDP ports beside single mode's 9222
SLOTS = {"youtube": Slot("youtube", "yt", 0x11, 9223, 10),
         "twitch": Slot("twitch", "tw", 0x12, 9224, 11),
         "tiktok": Slot("tiktok", "tt", 0x13, 9225, 12)}


def slot_of(tag_or_platform: str) -> Slot:
    for s in SLOTS.values():
        if tag_or_platform in (s.tag, s.platform):
            return s
    raise KeyError(f"no trio slot {tag_or_platform!r}")


# ------------------------------------------------------------------------------------------------ the tree
def _mgmt_filters(iface: str, parent: str, verdict: str) -> list[str]:
    return [f"tc filter add dev {iface} parent {parent} protocol ip prio 1 u32 match ip protocol {proto} 0xff "
            f"match ip {side} {port} 0xffff {verdict}"
            for proto, port, side in ((17, MGMT_UDP_PORT, "sport"), (17, MGMT_UDP_PORT, "dport"),
                                      (6, MGMT_TCP_PORT, "sport"), (6, MGMT_TCP_PORT, "dport"))]


def shared_up_cmds(iface: str = IFACE_DEFAULT) -> list[str]:
    """What every slot shares: modules, the default IFBs, the slice, the nft table, the interface's ingress
    (management bypass, connmark restore, the unmarked default) and egress root (management, the unshaped default).
    It replaces single mode's tree on the interface (orchestrate brings it up only in a pause, no session running)."""
    c = [f"modprobe {m}" for m in ("ifb", "act_connmark", "cls_fw", "sch_netem", "nft_socket")]
    for d in ("lvi_def", "lvo_def"):
        c += [f"ip link add {d} type ifb", f"ip link set {d} up"]
    c += [f"mkdir -p {CG_ROOT / SLICE}", f"nft add table inet {NFT_TABLE}",
          f"nft add chain inet {NFT_TABLE} out '{{ type route hook output priority mangle; policy accept; }}'",
          f"tc qdisc add dev {iface} handle ffff: ingress"]
    c += _mgmt_filters(iface, "ffff:", "action ok")
    c += [f"tc filter add dev {iface} parent ffff: protocol all prio 5 matchall action connmark continue",
          f"tc filter add dev {iface} parent ffff: protocol all prio 50 matchall action mirred egress redirect dev lvi_def",
          f"tc qdisc add dev {iface} root handle 1: htb default 30",
          f"tc class add dev {iface} parent 1: classid 1:20 htb rate 1000mbit",
          f"tc class add dev {iface} parent 1: classid 1:30 htb rate 1000mbit"]
    c += _mgmt_filters(iface, "1:", "flowid 1:20")
    c.append(f"tc filter add dev {iface} parent 1: protocol all prio 50 matchall flowid 1:30 "
             f"action mirred egress mirror dev lvo_def")
    return c


def shared_down_cmds(iface: str = IFACE_DEFAULT) -> list[str]:
    """Also the trio's preclean (before shared_up): each is idempotent when run with check off."""
    return [f"tc qdisc del dev {iface} root", f"tc qdisc del dev {iface} ingress", f"nft delete table inet {NFT_TABLE}",
            "ip link del lvi_def", "ip link del lvo_def", f"rmdir {CG_ROOT / SLICE}"]


def _nft_rule(s: Slot) -> str:
    return (f'socket cgroupv2 level 2 "{s.cgroup}" meta mark set {s.mark:#x} ct mark set meta mark '
            f'comment "lvdc slot {s.tag}"')


def slot_up_cmds(s: Slot, iface: str = IFACE_DEFAULT) -> list[str]:
    """A slot's own part: its cgroup (before its nft rule: nft resolves the path to the cgroup's id when the rule is
    added), IFBs, the ingress redirect and the egress classification + mirror. Its rate / delay / loss are single mode's
    `make slow` through make_vars; until the first one the slot is unshaped, as single mode's entry is."""
    return [f"mkdir -p {CG_ROOT / s.cgroup}",
            f"nft add rule inet {NFT_TABLE} out '{_nft_rule(s)}'",
            f"ip link add {s.ifb_in} type ifb", f"ip link set {s.ifb_in} up",
            f"ip link add {s.ifb_out} type ifb", f"ip link set {s.ifb_out} up",
            f"tc qdisc add dev {s.ifb_in} root handle 1: htb default 10",
            f"tc filter add dev {iface} parent ffff: protocol all prio {s.prio} handle {s.mark:#x} fw "
            f"action mirred egress redirect dev {s.ifb_in}",
            f"tc filter add dev {iface} parent 1: protocol all prio {s.prio} handle {s.mark:#x} fw "
            f"flowid {s.egress_class} action mirred egress mirror dev {s.ifb_out}"]


def slot_down_cmds(s: Slot, iface: str = IFACE_DEFAULT, nft_handle: int | None = None) -> list[str]:
    """Only this slot's pieces. Any process still in its cgroup is killed first (cgroup.kill: a Chrome helper left
    behind would otherwise carry the next session's mark), then its filters (by their preference), egress class, IFBs,
    nft rule and cgroup. Also the slot's own preclean before slot_up (check off)."""
    cg = CG_ROOT / s.cgroup
    c = [f"sh -c 'test -d {cg} && echo 1 > {cg}/cgroup.kill || true'",
         f"tc filter del dev {iface} parent ffff: prio {s.prio}", f"tc filter del dev {iface} parent 1: prio {s.prio}",
         f"tc qdisc del dev {iface} parent {s.egress_class}", f"tc class del dev {iface} classid {s.egress_class}",
         f"ip link del {s.ifb_in}", f"ip link del {s.ifb_out}"]
    if nft_handle is not None:
        c.append(f"nft delete rule inet {NFT_TABLE} out handle {nft_handle}")
    c.append(f"sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do rmdir {cg} 2>/dev/null && exit 0; test -d {cg} || exit 0; "
             f"sleep 0.5; done; exit 1'")
    return c


def nft_handle_of(s: Slot, listing: str | None = None) -> int | None:
    """The slot's nft rule handle (found by its comment) in `nft -a list chain inet lvdc_trio out`."""
    if listing is None:
        listing = subprocess.run(f"sudo -n nft -a list chain inet {NFT_TABLE} out", shell=True, text=True,
                                 capture_output=True).stdout
    m = re.search(rf'comment "lvdc slot {s.tag}" # handle (\d+)', listing or "")
    return int(m.group(1)) if m else None


def apply(cmds: list[str], check: bool = True) -> list[dict]:
    """Run each command as root; the record of what ran and how it ended (meta.json concurrency)."""
    out = []
    for c in cmds:
        r = subprocess.run("sudo -n " + c, shell=True, text=True, capture_output=True)
        out.append({"cmd": c, "rc": r.returncode, **({"err": r.stderr.strip()[:200]} if r.returncode else {})})
        if check and r.returncode != 0:
            raise RuntimeError(f"{c}: rc {r.returncode}: {r.stderr.strip()[:300]}")
    return out


def slot_up(s: Slot, iface: str = IFACE_DEFAULT) -> dict:
    """The slot's preclean (its own leftovers only), then its pieces; raises when a piece fails."""
    pre = apply(slot_down_cmds(s, iface, nft_handle_of(s)), check=False)
    up = apply(slot_up_cmds(s, iface))
    return {"preclean_failed": [x["cmd"] for x in pre if x["rc"]], "up": [x["cmd"] for x in up],
            "procs_at_up": cgroup_procs(s), "at": time.time()}


def slot_down(s: Slot, iface: str = IFACE_DEFAULT) -> dict:
    down = apply(slot_down_cmds(s, iface, nft_handle_of(s)), check=False)
    return {"failed": [{"cmd": x["cmd"], "err": x.get("err")} for x in down if x["rc"]], "at": time.time()}


def cgroup_procs(s: Slot, root: Path = CG_ROOT) -> list[int] | None:
    try:
        return [int(x) for x in (root / s.cgroup / "cgroup.procs").read_text().split()]
    except OSError:
        return None


# ------------------------------------------------------------------------------------------------ make
def staging_dir(session_dir: Path) -> Path:
    return STAGING / f"{session_dir.parent.name}_{session_dir.name}"


def make_vars(s: Slot, staging: Path | None = None) -> list[str]:
    """The make variables that put single mode's targets on this slot (Makefile defaults = single mode:
    test_make_single_mode.py). Chrome starts through in_cgroup.sh as the user exp.py runs as (the shell joins the
    cgroup, then execs Chrome; every child, the network service included, inherits it). With `staging`, the capture
    files and tcpdump's stderr (its kernel drop counters) go there (A2)."""
    v = [f"IFB_INGRESS={s.ifb_in}", f"IFB_EGRESS={s.ifb_out}", f"EGRESS_CLASS={s.egress_class}",
         f"CDP_PORT={s.cdp_port}", f"SLOT_SUFFIX=_{s.tag}", f"CHROME_PREFIX={HERE / 'in_cgroup.sh'} {s.cgroup}"]
    if staging is not None:
        v += capture_vars(staging)
    return v


def capture_vars(staging: Path) -> list[str]:
    return [f"IN_FILE={staging / 'incoming.pcap'}", f"OUT_FILE={staging / 'outgoing.pcap'}",
            f"CAPTURE_ERR_IN={staging / 'tcpdump_in.err'}", f"CAPTURE_ERR_OUT={staging / 'tcpdump_out.err'}"]


def default_capture_vars(staging: Path) -> list[str]:
    """The unmarked remainder's capture (design §3: written once per trio): single mode's capture targets on the
    default IFBs."""
    return ["IFB_INGRESS=lvi_def", "IFB_EGRESS=lvo_def"] + capture_vars(staging)


def capture_drops(err_text: str) -> dict:
    """tcpdump's exit counters (captured / received by filter / dropped by kernel) from its stderr."""
    out = {}
    for key, pat in (("captured", r"(\d+) packets? captured"), ("received", r"(\d+) packets? received by filter"),
                     ("dropped_kernel", r"(\d+) packets? dropped by kernel")):
        m = re.search(pat, err_text or "")
        out[key] = int(m.group(1)) if m else None
    return out


def capture_drops_of(staging: Path) -> dict:
    rec = {}
    for side in ("in", "out"):
        p = staging / f"tcpdump_{side}.err"
        try:
            rec[side] = capture_drops(p.read_text(errors="ignore"))
        except OSError:
            rec[side] = {"captured": None, "received": None, "dropped_kernel": None, "missing": True}
    vals = [rec[k]["dropped_kernel"] for k in ("in", "out")]
    rec["dropped_kernel_total"] = None if None in vals else sum(vals)
    return rec


# ------------------------------------------------------------------------------------------------ socket owner
def _proc_table(proc: Path) -> dict[int, tuple[int, str]]:
    """{pid: (ppid, cmdline)} from /proc (stat's ppid after the last ')': comm may hold spaces or parentheses)."""
    out = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
            cmd = (d / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
        except (OSError, ValueError, IndexError):
            continue
        out[int(d.name)] = (ppid, cmd)
    return out


def _cgroup_of(pid: int, proc: Path) -> str | None:
    try:
        return (proc / str(pid) / "cgroup").read_text().strip()
    except OSError:
        return None


def socket_owner(chrome_pid: int | None, slot: Slot, proc: Path = Path("/proc")) -> dict:
    """Supervisor v3, the socket-owner statement: the Chrome instance's process tree, its network service process
    (it opens the sockets the nft rule matches) and every process's cgroup; `all_in_slot` when each is in the slot's
    cgroup (cgroup v2 line `0::/lvdc.slice/<tag>`)."""
    if not chrome_pid:
        return {"chrome_pid": None, "why": "no chrome pid"}
    try:
        table = _proc_table(proc)
    except OSError as e:
        return {"chrome_pid": chrome_pid, "why": repr(e)}
    if chrome_pid not in table:
        return {"chrome_pid": chrome_pid, "why": "chrome pid not running"}
    tree, todo = [chrome_pid], [chrome_pid]
    while todo:
        p = todo.pop()
        kids = sorted(k for k, (pp, _) in table.items() if pp == p and k not in tree)
        tree += kids
        todo += kids
    want = f"0::/{slot.cgroup}"
    cg = {p: _cgroup_of(p, proc) for p in tree}
    ns = [p for p in tree if "network.mojom.NetworkService" in table[p][1]]
    return {"chrome_pid": chrome_pid, "processes": len(tree), "network_service_pids": ns,
            "network_service_cgroups": sorted({cg[p] or "?" for p in ns}),
            "outside_slot": [{"pid": p, "cgroup": cg[p], "cmd": table[p][1][:80]} for p in tree if cg[p] != want],
            "all_in_slot": all(cg[p] == want for p in tree), "at": time.time()}


def chrome_pid_of(slot: Slot, current_data: Path = CURRENT_DATA) -> int | None:
    try:
        return int((current_data / f"chrome_{slot.tag}.pid").read_text().strip())
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------------------------------------ tc samples
_RATE_UNITS = {"bit": 1e-3, "kbit": 1.0, "mbit": 1e3, "gbit": 1e6}


def rate_kbit(text: str) -> float | None:
    m = re.fullmatch(r"([\d.]+)([KMG]?bit)", text.strip(), re.I)
    return float(m.group(1)) * _RATE_UNITS[m.group(2).lower()] if m else None


def parse_classes(text: str) -> dict:
    """{classid: {rate_kbit, bytes, packets, drops, overlimits, backlog, qlen}} from `tc -s class show` text (the
    stats regex of gate 1's trio_dry_run.parse_class_stats, plus the class rate); without -s, the rate only."""
    out = {}
    for blk in re.split(r"\n(?=class )", text or ""):
        head = re.match(r"class \S+ (\S+) .*?rate (\S+)", blk)
        if not head:
            continue
        rec = {"rate_kbit": rate_kbit(head.group(2))}
        st = re.search(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues \d+\)\s*\n\s*"
                       r"backlog (\d+)b (\d+)p", blk)
        if st:
            rec.update(bytes=int(st.group(1)), packets=int(st.group(2)), drops=int(st.group(3)),
                       overlimits=int(st.group(4)), backlog=int(st.group(5)), qlen=int(st.group(6)))
        out[head.group(1)] = rec
    return out


def iw_bitrate(text: str) -> dict:
    out = {}
    for k in ("rx", "tx"):
        m = re.search(rf"{k} bitrate: ([\d.]+) MBit/s", text or "")
        out[f"{k}_mbit"] = float(m.group(1)) if m else None
    return out


def _sh_out(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=5).stdout
    except Exception:
        return ""


class TcSampler:
    """L-A2, per second while the session runs: its own classes' bytes / drops / backlog (tc -s class: the
    shaper-bound indicator), every slot's set ingress rate, the interface's bytes; `iw` PHY rates every IW_EVERY
    samples (not goodput). A second whose summed set rates exceed the measured link capacity is flagged; the samples
    go to <session>/tc_stats.jsonl, the summary to meta.json concurrency.tc."""

    def __init__(self, session_dir: Path, slot: Slot, iface: str = IFACE_DEFAULT, capacity_kbit: float | None = None,
                 sample_s: float = TC_SAMPLE_S, sh=_sh_out, net: Path = Path("/sys/class/net")):
        self.path = Path(session_dir) / "tc_stats.jsonl"
        self.slot, self.iface, self.capacity, self.sample_s, self.sh, self.net = slot, iface, capacity_kbit, sample_s, sh, net
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _link(self, dev: str) -> dict:
        p = self.net / dev / "statistics"
        try:
            return {k: int((p / k).read_text()) for k in ("rx_bytes", "tx_bytes")}
        except OSError:
            return {}

    def sample(self) -> dict:
        s = self.slot
        own_in = parse_classes(self.sh(f"tc -s class show dev {s.ifb_in}")).get("1:10")
        egress = parse_classes(self.sh(f"tc -s class show dev {self.iface}"))
        set_rates = {s.tag: (own_in or {}).get("rate_kbit")}
        for o in SLOTS.values():
            if o.tag != s.tag and (self.net / o.ifb_in).exists():
                set_rates[o.tag] = (parse_classes(self.sh(f"tc class show dev {o.ifb_in}")).get("1:10") or {}).get("rate_kbit")
        row = {"ts": time.time(), "in": own_in, "out": egress.get(s.egress_class), "set_rates_kbit": set_rates,
               "iface": self._link(self.iface)}
        if len(self.rows) % IW_EVERY == 0:
            row["iw"] = iw_bitrate(self.sh(f"iw dev {self.iface} link"))
        row["flagged"] = bool(self.capacity) and sum(v for v in set_rates.values() if v) > self.capacity
        self.rows.append(row)
        try:
            with open(self.path, "a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            pass
        return row

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:
                pass
            self._stop.wait(self.sample_s)

    def start(self) -> "TcSampler":
        self._t = threading.Thread(target=self._run, name="tc_sampler", daemon=True)
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=5)
        return self.summary()

    def summary(self) -> dict:
        r = [x for x in self.rows if (x.get("in") or {}).get("bytes") is not None]
        if not r:
            return {"samples": 0, "file": self.path.name}
        bound = sum(bool(b["in"]["backlog"]) or b["in"]["drops"] > a["in"]["drops"] for a, b in zip(r, r[1:]))
        flagged = sum(1 for x in r if x.get("flagged"))
        return {"samples": len(r), "sample_s": self.sample_s, "capacity_kbit": self.capacity,
                "shaper_bound_share": round(bound / max(1, len(r) - 1), 3),
                "ingress_drops": r[-1]["in"]["drops"] - r[0]["in"]["drops"],
                "flagged_seconds": flagged, "flagged_share": round(flagged / len(r), 3),
                "qa_flag": flagged / len(r) > FLAGGED_SHARE_QA, "file": self.path.name}


# ------------------------------------------------------------------------------------------------ records
def concurrency_record(s: Slot | None, **extra) -> dict:
    """meta.json concurrency (design §6, Q am. 13); single mode writes {mode: single}."""
    if s is None:
        return {"mode": "single"}
    return {"mode": "trio", "pool": "per_session", **s.record(), **extra}


def slots_log_append(path: Path, rec: dict) -> None:
    """The orchestrator's slot timeline (data/_trio/_summaries/trio_slots.jsonl): every session's start / end per slot, from
    which each session's siblings and their overlap are read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def siblings(path: Path, session: str, t0: float, t1: float | None) -> list[dict]:
    """The other sessions whose slot window overlaps [t0, t1] (t1 None: now), from the slots log, with the overlap in
    seconds (a sibling still open counts up to t1)."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    spans: dict[str, dict] = {}
    last_of_slot: dict[str, str] = {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        ev = r.get("event")
        if ev == "exit":       # the orchestrator's record of exp.py's exit: closes a session that wrote no end
            d = spans.get(last_of_slot.get(r.get("slot"), ""))
            if d is not None and d.get("end") is None:
                d["end"], d["closed_by"] = r["t"], "exit"
            if d is not None:
                d["rc"] = r.get("rc")
            continue
        if ev not in ("start", "end"):
            continue
        d = spans.setdefault(str(r.get("session")), {"session": r.get("session"), "slot": r.get("slot")})
        if ev == "start":
            d["start"] = r["t"]
            last_of_slot[r.get("slot")] = str(r.get("session"))
        elif d.get("end") is None:
            d["end"] = r["t"]
    hi = t1 if t1 is not None else time.time()
    out = []
    for k, d in sorted(spans.items()):
        if k == session or d.get("start") is None:
            continue
        lo2, hi2 = max(t0, d["start"]), min(hi, d["end"] if d.get("end") is not None else hi)
        if hi2 > lo2:
            out.append({**d, "overlap_s": round(hi2 - lo2, 1)})
    return out


def conntrack_timeouts(proc: Path = Path("/proc/sys/net/netfilter")) -> dict:
    """The conntrack timeouts in force (a QUIC flow idle past the UDP stream timeout loses its ct mark, and its next
    downstream packets reach the unmarked default class until an egress packet re-marks it)."""
    out = {}
    for k in ("nf_conntrack_udp_timeout", "nf_conntrack_udp_timeout_stream", "nf_conntrack_tcp_timeout_established"):
        try:
            out[k] = int((proc / k).read_text().strip())
        except (OSError, ValueError):
            out[k] = None
    return out


def trio_root_floor_gb(runs_dir: Path, single_floor_gb: float, platforms=("youtube", "twitch", "tiktok")) -> dict:
    """A2: in trio mode the root filesystem floor = single mode's + three times the largest per-session pcap observed
    (merged.pcap of accepted and _failed sessions)."""
    largest = 0
    for p in platforms:
        for f in list((runs_dir / p).glob("*/merged.pcap")) + list((runs_dir / "_failed" / p).glob("*/merged.pcap")):
            try:
                largest = max(largest, f.stat().st_size)
            except OSError:
                pass
    return {"single_floor_gb": single_floor_gb, "largest_pcap_gb": round(largest / 1e9, 2),
            "floor_gb": round(single_floor_gb + 3 * largest / 1e9, 1)}
