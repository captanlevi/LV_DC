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
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

CG_ROOT = Path("/sys/fs/cgroup")
SLICE = "lvdc.slice"
NFT_TABLE = "lvdc_trio"
IFACE_DEFAULT = "wlp0s20f3"
MGMT_UDP_PORT, MGMT_TCP_PORT = 41641, 22             # MGMT_UDP_PORT: the fallback; mgmt_udp_port() is what the tree uses


def mgmt_udp_port_record() -> dict:
    """t58 L-6 (brad's audit): tailscaled's live UDP port, read as the Makefile reads it (`sudo -n ss -ulpnH`, the
    tailscaled line's local port), so the trio's management bypass, single mode's BPF and meta.json mgmt_exclusion name
    one port; LVDC_MGMT_UDP_PORT overrides (tests); else MGMT_UDP_PORT. {port, source}."""
    v = os.environ.get("LVDC_MGMT_UDP_PORT")
    if v:
        return {"port": int(v), "source": "LVDC_MGMT_UDP_PORT"}
    try:
        out = subprocess.run(["sudo", "-n", "ss", "-ulpnH"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if "tailscaled" in line:
                return {"port": int(line.split()[3].rsplit(":", 1)[1]), "source": "tailscaled (ss -ulpnH)"}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return {"port": MGMT_UDP_PORT, "source": "default (no tailscaled port read)"}


def mgmt_udp_port() -> int:
    return mgmt_udp_port_record()["port"]
HERE = Path(__file__).resolve().parent
CURRENT_DATA = Path(__file__).resolve().parent.parent / "current_data"
# t58 (supervisor / levi 2026-10-19, after the N = 5 decode_stress hit the rootfs floor): a trio session's pcaps are
# staged on the data volume, data/_trio_staging (A2's rootfs staging retired: the rootfs cannot hold N slots' captures);
# merge-capture writes merged.pcap into the session dir at the session's own end. The volume is exFAT: every staged
# tcpdump runs with -Z root (TCPDUMP_Z; the Makefile's root-shell stdout redirect already avoids the chown, t37) and a
# slot checks its capture grows (capture_growing). LVDC_TRIO_STAGING overrides the place (recorded per session).
from data_root import DATA_ROOT as DATA  # noqa: E402  (one setting: data_root.py)
STAGING = Path(os.environ.get("LVDC_TRIO_STAGING") or (DATA / "_trio_staging"))
TCPDUMP_Z = "-Z root"
TCPDUMP_B_KIB = 65536      # t57 tw_100023 (levi): every session capture's kernel buffer, = the Makefile's TCPDUMP_B
DATASET_TRIO_DIR = "_trio"                            # the counted collection's root (decode_stress's is STRESS_DIR)
# supervisor 2026-10-19: trio sessions have their own data root, data/_trio/<platform>/ (single mode's accepted counts,
# coverage snapshot, checkpoint list and verify loop never see them: am. 13's "never pooled silently", mechanically)
# t56 decode_stress (design v4 §9 (5)): uncounted real sessions under their own root, data/_trio_stress, never data/_trio
# (orchestrate --trio-stress sets LVDC_TRIO_DIR for the run and every slot's exp.py); nothing else is accepted
STRESS_DIR = "_trio_stress"
TRIO_DIR = os.environ.get("LVDC_TRIO_DIR") or "_trio"   # under the data dir
if TRIO_DIR not in ("_trio", STRESS_DIR):
    raise SystemExit(f"LVDC_TRIO_DIR={TRIO_DIR!r}: only _trio or {STRESS_DIR}")
SLOTS_LOG = "_summaries/trio_slots.jsonl"           # under the trio root
# a trio session's own extra files (its default-class DNS capture, tcpdump's counters) sit in <session>/trio/, so the
# session dir holds one pcap, in single mode's form (pilot-0, levi 3: LVQoE's pcap finder ignores a dir with two)
SESSION_TRIO_DIR = "trio"
DEFAULT_CAPTURE = "_default"                        # under the trio root: the unmarked remainder, one per trio run
# every trio write goes under the trio root, its session counter too (supervisor 2026-10-19): trio names start above
# TRIO_NAME_FLOOR (yt_100001, ...), so they never meet single mode's, whose counter the trio never reads or writes
TRIO_NAME_FLOOR = 100000 if TRIO_DIR == "_trio" else 900000      # stress names (yt_900001 ...) never meet the trio's
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
                "ifb_out": self.ifb_out, "egress_class": self.egress_class, "epoch": self.mark >> 8}

    def with_epoch(self, k: int) -> "Slot":
        """The slot's mark for one session: its base (low byte) with the epoch k mod MARK_EPOCHS above it. A session's
        mark differs from the slot's previous session's, so conntrack entries a finished session left (the old
        connections' late packets, restored by connmark) match no filter of the next one and stay out of its class and
        capture (pilot-0 read, levi 1b)."""
        return replace(self, mark=(self.mark & 0xFF) | ((k % MARK_EPOCHS) << 8))


MARK_EPOCHS = 16
ORPHAN_PRIO = 40          # the shared drop for a mark of ours with no slot filter (after the slots' 10-12, before 50)
# conntrack's UDP stream timeout while a trio runs (pilot-0 read, levi 1b: at the box's 120 s an idle QUIC flow lost its
# entry and so its mark, and the server's next packets reached the unshaped default class); set by run_trio, restored
TRIO_UDP_STREAM_TIMEOUT_S = 3600
# t56 (design v4 §9): N slots from a composition {platform: count}, platform order youtube, twitch, tiktok; slot k (in that
# order) is <abbrev><i> (yt1, yt2, tw1 ...) with mark base 0x11 + k, CDP port 9223 + k, filter prio 10 + k. The marks'
# low byte 0x1X is ours (0x10 stays the orphan drop's pattern), so at most MAX_SLOTS; the trio (one each) is yt1 / tw1 / tt1
# on the trio's marks, ports and prios (test_nslot_equivalence: aaba6ae's commands up to the tag renaming).
PLATFORM_ORDER = ("youtube", "twitch", "tiktok")
ABBREV = {"youtube": "yt", "twitch": "tw", "tiktok": "tt"}
MAX_SLOTS = 15
TRIO_COMPOSITION = {"youtube": 1, "twitch": 1, "tiktok": 1}


def parse_composition(spec: str | None) -> dict[str, int]:
    """'youtube=2,twitch=2,tiktok=2' -> {platform: count}; None / empty: the trio's."""
    if not spec:
        return dict(TRIO_COMPOSITION)
    comp = {}
    for kv in spec.split(","):
        k, _, v = kv.strip().partition("=")
        if k not in ABBREV or not v.isdigit() or int(v) < 0:
            raise ValueError(f"--slots {spec!r}: {kv!r} (platform=count, platforms {list(ABBREV)})")
        comp[k] = int(v)
    return {p: comp[p] for p in PLATFORM_ORDER if comp.get(p)}


def build_slots(composition: dict[str, int]) -> dict[str, "Slot"]:
    """{tag: Slot} for a composition (module comment above)."""
    out, k = {}, 0
    for p in PLATFORM_ORDER:
        for i in range(1, int(composition.get(p, 0)) + 1):
            tag = f"{ABBREV[p]}{i}"
            out[tag] = Slot(p, tag, 0x11 + k, 9223 + k, 10 + k)
            k += 1
    if not out or len(out) > MAX_SLOTS:
        raise ValueError(f"composition {composition}: 1..{MAX_SLOTS} slots (the mark space)")
    return out


def slot_arg_error(tag: str, platform: str, auto: bool) -> str | None:
    """exp.py --slot (t56: shagun's N = 4 gate, 1941f80: the parser still offered the trio's tags, so every slot exited
    rc 2): a tag of the run's composition (LVDC_TRIO_SLOTS), on its own platform, with --auto; else the error text."""
    if tag not in SLOTS:
        return f"--slot {tag!r}: not a slot of the composition ({', '.join(SLOTS)})"
    if SLOTS[tag].platform != platform or not auto:
        return f"--slot {tag} is the {SLOTS[tag].platform} slot and needs --auto"
    return None


def composition_of(slots: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in slots.values():
        out[s.platform] = out.get(s.platform, 0) + 1
    return out


# the run's slots: orchestrate --slots, passed to every slot's exp.py as LVDC_TRIO_SLOTS (the composition)
SLOTS: dict[str, "Slot"] = build_slots(parse_composition(os.environ.get("LVDC_TRIO_SLOTS")))


def layout_of(s: Slot, env) -> dict:
    """The slot's window layout from run_trio's environment: "cdp" (pilot-0: windows at the origin, CDP shots) or
    "fb" (design §4: the X framebuffer enlarged to len(SLOTS) monitor widths, slot k's window at k x the width)."""
    lay = env.get("LVDC_TRIO_LAYOUT") or "cdp"
    if lay != "fb":
        return {"layout": lay}
    w, h = (int(x) for x in env["LVDC_TRIO_SCREEN"].split("x"))
    k = list(SLOTS).index(s.tag)
    return {"layout": "fb", "screen": [w, h], "x_screen_px": [w * len(SLOTS), h], "origin": [k * w, 0], "index": k}


def slot_of(tag_or_platform: str) -> Slot:
    """A slot by its tag; a platform names its first slot (the trio's one)."""
    if tag_or_platform in SLOTS:
        return SLOTS[tag_or_platform]
    for s in SLOTS.values():
        if tag_or_platform == s.platform:
            return s
    raise KeyError(f"no trio slot {tag_or_platform!r}")


# ------------------------------------------------------------------------------------------------ the tree
def _mgmt_filters(iface: str, parent: str, verdict: str) -> list[str]:
    udp = mgmt_udp_port()                              # t58 L-6: the live port, as the Makefile's BPF and meta
    return [f"tc filter add dev {iface} parent {parent} protocol ip prio 1 u32 match ip protocol {proto} 0xff "
            f"match ip {side} {port} 0xffff {verdict}"
            for proto, port, side in ((17, udp, "sport"), (17, udp, "dport"),
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
          # a packet with no full socket (a TIME_WAIT ACK, a kernel RST) carries no cgroup, so the slot rules miss it:
          # it takes its connection's mark from conntrack (our marks only: low byte 0x1X), as the ingress connmark does
          f"nft add rule inet {NFT_TABLE} out 'meta mark 0 ct mark & 0xf0 == 0x10 meta mark set ct mark "
          f"comment \"lvdc restore\"'",
          f"tc qdisc add dev {iface} handle ffff: ingress"]
    c += _mgmt_filters(iface, "ffff:", "action ok")
    c += [f"tc filter add dev {iface} parent ffff: protocol all prio 5 matchall action connmark continue",
          # a mark of ours that no slot filter takes (a finished session's late packets: its epoch's filters are gone)
          # is dropped, never sent to the unshaped default class (pilot-0, levi 1 / 7)
          f"tc filter add dev {iface} parent ffff: protocol all prio {ORPHAN_PRIO} handle 0x10/0xf0 fw action drop",
          f"tc filter add dev {iface} parent ffff: protocol all prio 50 matchall action mirred egress redirect dev lvi_def",
          f"tc qdisc add dev {iface} root handle 1: htb default 30",
          f"tc class add dev {iface} parent 1: classid 1:20 htb rate 1000mbit",
          f"tc class add dev {iface} parent 1: classid 1:30 htb rate 1000mbit"]
    c += _mgmt_filters(iface, "1:", "flowid 1:20")
    # egress too restores the mark from conntrack (pilot-0b: tt's kernel RSTs answering a closed connection's late
    # packets reached the default class: conntrack left them without an entry, so nft's ct mark restore could not see
    # it; tc's connmark looks the connection up by its tuple itself, as at ingress)
    c.append(f"tc filter add dev {iface} parent 1: protocol all prio 5 matchall action connmark continue")
    c.append(f"tc filter add dev {iface} parent 1: protocol all prio {ORPHAN_PRIO} handle 0x10/0xf0 fw action drop")
    c.append(f"tc filter add dev {iface} parent 1: protocol all prio 50 matchall flowid 1:30 "
             f"action mirred egress mirror dev lvo_def")
    return c


UPLINK_CAP_PRIO = 6          # after the management bypass (1) and the connmark restore (5), before the slots (10 + k)
UPLINK_CAP_BURST_S = 0.05    # the policer's bucket: 50 ms at the cap (>= 64 KiB), a short drop-tail "link buffer"


def uplink_cap_cmds(iface: str, kbit: int) -> list[str]:
    """levi 02:48 (the positive control for brad's uplink-bound detector; diag only, --trio-stress): every download on the
    interface but management passes one policer at kbit before any slot or the default class sees it, as a slower
    house link would. A policer, not a queue: excess is dropped, never delayed beyond the bucket (disclosed). The
    ingress qdisc's removal (shared_down_cmds) removes it."""
    burst = max(65536, int(kbit * 1000 / 8 * UPLINK_CAP_BURST_S))
    return [f"tc filter add dev {iface} parent ffff: protocol all prio {UPLINK_CAP_PRIO} matchall action police "
            f"rate {int(kbit)}kbit burst {burst} mtu 64kb conform-exceed drop/continue"]


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


def slot_detach_cmds(s: Slot, iface: str = IFACE_DEFAULT) -> list[str]:
    """The slot's two classifying filters only: from then its packets (the dead Chrome's late ones) match the orphan
    drop, so nothing more reaches its IFBs and its capture stops with received == captured (pilot-0, levi 7)."""
    return [f"tc filter del dev {iface} parent ffff: prio {s.prio}", f"tc filter del dev {iface} parent 1: prio {s.prio}"]


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
            f"CAPTURE_ERR_IN={staging / 'tcpdump_in.err'}", f"CAPTURE_ERR_OUT={staging / 'tcpdump_out.err'}",
            f"TCPDUMP_Z={TCPDUMP_Z}"]


PCAP_HEADER_BYTES = 24


def capture_growing(staging: Path, files=("incoming.pcap", "outgoing.pcap")) -> dict:
    """t58 (levi): a staged capture is writing: each file exists and is past the pcap header (`24` B; the t55 exFAT
    incident left header-only pcaps that passed as sessions). {sizes, ok}."""
    sizes = {}
    for f in files:
        try:
            sizes[f] = (Path(staging) / f).stat().st_size
        except OSError:
            sizes[f] = None
    return {"sizes": sizes, "ok": all(v is not None and v > PCAP_HEADER_BYTES for v in sizes.values())}


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


def chrome_tree(chrome_pid: int, table: dict) -> list[int]:
    """The Chrome instance's processes (its pid and every descendant) from _proc_table."""
    tree, todo = [chrome_pid], [chrome_pid]
    while todo:
        p = todo.pop()
        kids = sorted(k for k, (pp, _) in table.items() if pp == p and k not in tree)
        tree += kids
        todo += kids
    return tree


class CgroupSweeper:
    """Keeps every process of the session's Chrome in the slot's cgroup (pilot-0 read, levi 1a: Chrome moved its own
    browser process into a systemd scope, app.slice/app-com.google.Chrome-<pid>.scope, and what it forked afterwards
    followed; the network service, forked before, stayed). Each sweep moves any process of the tree outside the slot
    back into it (root writes cgroup.procs); every move is recorded. Fast sweeps over the first FAST_S, then SLOW_S."""
    FAST_S, FAST_EVERY_S, SLOW_EVERY_S = 120.0, 0.5, 5.0

    def __init__(self, slot: Slot, pid_fn, proc: Path = Path("/proc"), mover=None, clock=time.time):
        self.slot, self.pid_fn, self.proc, self.clock = slot, pid_fn, proc, clock
        self.mover = mover or self._move
        self.moves: list[dict] = []
        self.sweeps = 0
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._t0 = clock()

    def _move(self, pid: int) -> bool:
        r = subprocess.run(["sudo", "-n", "tee", str(CG_ROOT / self.slot.cgroup / "cgroup.procs")], input=f"{pid}\n",
                           text=True, capture_output=True)
        return r.returncode == 0

    def sweep(self) -> int:
        pid = self.pid_fn()
        if not pid:
            return 0
        table = _proc_table(self.proc)
        if pid not in table:
            return 0
        self.sweeps += 1
        want, n = f"0::/{self.slot.cgroup}", 0
        for p in chrome_tree(pid, table):
            cg = _cgroup_of(p, self.proc)
            if cg is not None and cg != want:
                ok = self.mover(p)
                n += 1
                self.moves.append({"t": round(self.clock(), 3), "pid": p, "from": cg, "ok": ok, "cmd": table[p][1][:60]})
        return n

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                pass
            fast = self.clock() - self._t0 < self.FAST_S
            self._stop.wait(self.FAST_EVERY_S if fast else self.SLOW_EVERY_S)

    def start(self) -> "CgroupSweeper":
        self._t0 = self.clock()
        self._t = threading.Thread(target=self._run, name="cgroup_sweeper", daemon=True)
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=10)
        return {"sweeps": self.sweeps, "n_moves": len(self.moves), "moves": self.moves[:50],
                "first_move_s": round(self.moves[0]["t"] - self._t0, 2) if self.moves else None}


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
    tree = chrome_tree(chrome_pid, table)
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


# #15 (supervisor 14:40; per-hole shaper-drop attribution, W-B tt holes): v3 trio sessions were reported whose
# tc_stats / tc_fast rows all read "in": null, "out": null. Both samplers now read the slot's own tree as JSON
# (`tc -s -j`), qdiscs as well as classes, through one reader that finds tc off PATH (/usr/sbin, /sbin) and falls back
# to `sudo -n` (apply()'s runner) when the unprivileged read fails, and that keeps why a read failed (meta, tc_err).
TC_FIELDS = ("bytes", "packets", "drops", "overlimits", "requeues", "backlog", "qlen")   # slot_tc's counters, in order
TC_BIN_FALLBACKS = ("/usr/sbin/tc", "/sbin/tc")


def tc_bin() -> str:
    """tc's path: PATH first, then /usr/sbin, /sbin (preflight.check_shaper's rule: tc can be off a minimal PATH)."""
    return shutil.which("tc") or next((p for p in TC_BIN_FALLBACKS if Path(p).exists()), "tc")


def _tc_exec(argv: list[str], timeout_s: float = 2.0) -> tuple[int, str, str, float]:
    """argv exec'd (no shell): (rc, stdout, stderr, that child's own CPU seconds from os.wait4; brad / levi 21:27-21:28:
    the exact cost of the reads, never every child of exp.py; under sudo the rusage includes the tc sudo reaped). The
    child is reaped here by wait4, not by Popen; a read over timeout_s (2 s) is killed. rc 127 = could not exec."""
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
    except Exception as e:
        return 127, "", f"exec: {type(e).__name__}: {e}"[:200], 0.0
    killer = threading.Timer(timeout_s, p.kill)
    killer.start()
    out = err = ""
    try:
        out = p.stdout.read()
        err = p.stderr.read()
    except Exception:
        pass
    finally:
        killer.cancel()
        for fh in (p.stdout, p.stderr):
            try:
                fh.close()
            except Exception:
                pass
    try:
        _, status, ru = os.wait4(p.pid, 0)
        p.returncode = os.waitstatus_to_exitcode(status)
        return p.returncode, out or "", (err or "").strip()[:200], ru.ru_utime + ru.ru_stime
    except ChildProcessError:
        return 0, out or "", (err or "").strip()[:200], 0.0


class TcReader:
    """`tc -s -j <kind> show dev <dev>` -> (stdout, child CPU s, error or None). Unprivileged first (a dump needs no
    root); when that fails (rc != 0 or no exec) the same under `sudo -n` once; if sudo works it is used from then on,
    if it fails too it is never tried again. Counts reads and failures and keeps the last error (meta). One per sampler
    thread."""

    def __init__(self, timeout_s: float = 2.0, exec_=_tc_exec, tc: str | None = None):
        self.tc, self.timeout, self.exec_ = tc or tc_bin(), timeout_s, exec_
        self.sudo: bool | None = None          # None: untried; True: in use; False: tried and failed
        self.reads = self.failed = 0
        self.last_error: str | None = None
        self.first_error: str | None = None

    def _argv(self, kind: str, dev: str, sudo: bool) -> list[str]:
        return (["sudo", "-n"] if sudo else []) + [self.tc, "-s", "-j", kind, "show", "dev", dev]

    def __call__(self, kind: str, dev: str) -> tuple[str, float, str | None]:
        self.reads += 1
        rc, out, err, cpu = self.exec_(self._argv(kind, dev, bool(self.sudo)), self.timeout)
        err = f"rc {rc}: {err}" if rc else ""
        if rc and self.sudo is None:                  # the unprivileged read failed: sudo -n once, sticky either way
            rc2, out2, err2, cpu2 = self.exec_(self._argv(kind, dev, True), self.timeout)
            cpu += cpu2
            self.sudo = rc2 == 0
            rc, out, err = (0, out2, "") if rc2 == 0 else (rc2, "", f"{err}; sudo -n: rc {rc2}: {err2}")
        if rc:
            self.failed += 1
            self.last_error = f"{kind} {dev}: {err}"[:300]
            self.first_error = self.first_error or self.last_error
            return "", cpu, self.last_error
        return out, cpu, None

    def record(self) -> dict:
        return {"tc": self.tc, "json": True, "sudo": self.sudo, "reads": self.reads, "failed": self.failed,
                "first_error": self.first_error, "last_error": self.last_error}


def _tc_json(text: str) -> list | None:
    s = (text or "").lstrip()
    if not s.startswith("["):
        return None
    try:
        v = json.loads(s)
    except ValueError:
        return None
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else None


def _counters(obj: dict) -> dict:
    flat = {**obj, **(obj.get("stats") if isinstance(obj.get("stats"), dict) else {})}
    return {k: (flat.get(k) if isinstance(flat.get(k), int) and not isinstance(flat.get(k), bool) else None)
            for k in TC_FIELDS}


def _parent(obj: dict) -> str | None:
    return "root" if obj.get("root") else obj.get("parent")


def tc_classes(text: str) -> dict:
    """{classid: {kind, parent, leaf, rate_kbit, bytes, packets, drops, overlimits, requeues, backlog, qlen}} from
    `tc -s -j class show` (JSON prints a rate in bytes / s: x 8 / 1000 = kbit); text output falls back to
    parse_classes (no requeues there)."""
    j = _tc_json(text)
    if j is None:
        return {cid: {"kind": None, "parent": None, "leaf": None, "requeues": None, **rec}
                for cid, rec in parse_classes(text).items()}
    out = {}
    for c in j:
        if not c.get("handle"):
            continue
        r = c.get("rate")
        out[c["handle"]] = {"kind": c.get("class"), "parent": _parent(c), "leaf": c.get("leaf"),
                            "rate_kbit": round(r * 8 / 1000, 3) if isinstance(r, (int, float)) else None,
                            **_counters(c)}
    return out


def parse_qdiscs_text(text: str) -> dict:
    """{handle: {kind, parent, <TC_FIELDS>}} from `tc -s qdisc show` text (the fallback when the output is not JSON)."""
    out = {}
    for blk in re.split(r"\n(?=qdisc )", text or ""):
        head = re.match(r"qdisc (\S+) (\S+) (root|parent (\S+))", blk)
        if not head:
            continue
        rec = {"kind": head.group(1), "parent": "root" if head.group(3) == "root" else head.group(4),
               **{k: None for k in TC_FIELDS}}
        st = re.search(r"Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues (\d+)\)\s*\n\s*"
                       r"backlog (\d+)b (\d+)p", blk)
        if st:
            rec.update(zip(TC_FIELDS, map(int, st.groups())))
        out[head.group(2)] = rec
    return out


def tc_qdiscs(text: str) -> dict:
    """{handle: {kind, parent, <TC_FIELDS>}} from `tc -s -j qdisc show` (text output: parse_qdiscs_text)."""
    j = _tc_json(text)
    if j is None:
        return parse_qdiscs_text(text)
    return {q["handle"]: {"kind": q.get("kind"), "parent": _parent(q), **_counters(q)} for q in j if q.get("handle")}


def _child_of(qdiscs: dict, parent: str, kind: str = "netem") -> dict | None:
    return next((dict(q, handle=h) for h, q in qdiscs.items() if q.get("parent") == parent and q.get("kind") == kind),
                None)


def _root_of(qdiscs: dict) -> dict | None:
    return next((dict(q, handle=h) for h, q in qdiscs.items() if q.get("parent") == "root"), None)


def slot_tc_record(slot: Slot, iface: str, in_classes: str, out_classes: str, in_qdiscs: str, out_qdiscs: str) -> dict:
    """The slot's OWN tree from the four reads, per direction (the module docstring; SlotSpec ifb_in / egress_class):
    in = its ingress IFB lvi_<tag>: the root qdisc (htb 1:, default 10), class 1:10 and the netem under 1:10; out = its
    egress class 1:<mark> on the interface's shared root and the netem under it (the shared root is no slot's own).
    Each record is a tc_classes / tc_qdiscs row, None when absent (before the first `make slow`) or unread."""
    ci, qi = tc_classes(in_classes), tc_qdiscs(in_qdiscs)
    co, qo = tc_classes(out_classes), tc_qdiscs(out_qdiscs)
    return {"in": {"dev": slot.ifb_in, "qdisc": _root_of(qi), "class": ci.get("1:10"), "netem": _child_of(qi, "1:10")},
            "out": {"dev": iface, "class": co.get(slot.egress_class), "netem": _child_of(qo, slot.egress_class)}}


SLOT_TC_PARTS = (("in", "qdisc"), ("in", "class"), ("in", "netem"), ("out", "class"), ("out", "netem"))
_LEGACY_KEYS = ("rate_kbit", "bytes", "packets", "drops", "overlimits", "backlog", "qlen")


def _legacy(rec: dict | None) -> dict | None:
    """The pre-#15 "in" / "out" dict (parse_classes' keys) from a tc_classes row: rows keep their old keys."""
    if not rec:
        return None
    if rec.get("bytes") is None:
        return {"rate_kbit": rec.get("rate_kbit")}
    return {k: rec.get(k) for k in _LEGACY_KEYS}


class TcSampler:
    """L-A2, per second while the session runs: its own classes' bytes / drops / backlog (tc -s class: the
    shaper-bound indicator), every slot's set ingress rate, the interface's bytes; `iw` PHY rates every IW_EVERY
    samples (not goodput). A second whose summed set rates exceed the measured link capacity is flagged; the samples
    go to <session>/tc_stats.jsonl, the summary to meta.json concurrency.tc. #15: each row also carries slot_tc (the
    slot's own qdisc and class counters per direction, slot_tc_record) and, when a read failed, tc_err; "in" / "out"
    keep their keys."""

    def __init__(self, session_dir: Path, slot: Slot, iface: str = IFACE_DEFAULT, capacity_kbit: float | None = None,
                 sample_s: float = TC_SAMPLE_S, sh=_sh_out, net: Path = Path("/sys/class/net"), read=None):
        self.path = Path(session_dir) / "tc_stats.jsonl"
        self.slot, self.iface, self.capacity, self.sample_s, self.sh, self.net = slot, iface, capacity_kbit, sample_s, sh, net
        self.read = read if read is not None else TcReader()
        self.rows: list[dict] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def _link(self, dev: str) -> dict:
        p = self.net / dev / "statistics"
        try:
            return {k: int((p / k).read_text()) for k in ("rx_bytes", "tx_bytes")}
        except OSError:
            return {}

    def _read(self, kind: str, dev: str, errs: list) -> str:
        text, _cpu, err = self.read(kind, dev)
        if err:
            errs.append(err)
        return text

    def sample(self) -> dict:
        s, errs = self.slot, []
        reads = [self._read(k, d, errs) for k, d in (("class", s.ifb_in), ("class", self.iface),
                                                      ("qdisc", s.ifb_in), ("qdisc", self.iface))]
        sc = slot_tc_record(s, self.iface, *reads)
        own_in = _legacy(sc["in"]["class"])
        set_rates = {s.tag: (own_in or {}).get("rate_kbit")}
        for o in SLOTS.values():
            if o.tag != s.tag and (self.net / o.ifb_in).exists():
                set_rates[o.tag] = (tc_classes(self._read("class", o.ifb_in, errs)).get("1:10") or {}).get("rate_kbit")
        row = {"ts": time.time(), "in": own_in, "out": _legacy(sc["out"]["class"]), "set_rates_kbit": set_rates,
               "iface": self._link(self.iface)}
        if len(self.rows) % IW_EVERY == 0:
            row["iw"] = iw_bitrate(self.sh(f"iw dev {self.iface} link"))
        row["flagged"] = bool(self.capacity) and sum(v for v in set_rates.values() if v) > self.capacity
        row["slot_tc"] = sc
        if errs:
            row["tc_err"] = errs
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

    def _unread(self) -> dict:
        return {f"{d}.{p}": sum(1 for x in self.rows if not ((x.get("slot_tc") or {}).get(d) or {}).get(p))
                for d, p in SLOT_TC_PARTS}

    def summary(self) -> dict:
        extra = {"rows": len(self.rows), "slot_tc_unread": self._unread(),
                 **({"tc_read": self.read.record()} if hasattr(self.read, "record") else {})}
        r = [x for x in self.rows if (x.get("in") or {}).get("bytes") is not None]
        if not r:
            return {"samples": 0, "file": self.path.name, **extra}
        bound = sum(shaper_bound(a["in"], b["in"]) for a, b in zip(r, r[1:]))
        flagged = sum(1 for x in r if x.get("flagged"))
        return {"samples": len(r), "sample_s": self.sample_s, "capacity_kbit": self.capacity,
                "shaper_bound_share": round(bound / max(1, len(r) - 1), 3),
                "ingress_drops": r[-1]["in"]["drops"] - r[0]["in"]["drops"],
                "flagged_seconds": flagged, "flagged_share": round(flagged / len(r), 3),
                "qa_flag": flagged / len(r) > FLAGGED_SHARE_QA, "file": self.path.name, **extra}


FAST_TC_S = 0.1                                      # v3 A18: the shaped classes every 100 ms (base truth, both A/B arms)
FAST_TC_FIELDS = ("bytes", "packets", "drops", "overlimits", "backlog", "qlen")


def _tc_classes(dev: str, timeout_s: float = 2.0) -> tuple[str, float]:
    """`tc -s class show dev <dev>` text (exec, no shell): (stdout, child CPU s); empty on failure. Kept for callers of
    the A18 reader; the samplers read through TcReader (#15)."""
    rc, out, _err, cpu = _tc_exec([tc_bin(), "-s", "class", "show", "dev", dev], timeout_s)
    return (out if rc == 0 else ""), cpu


def _fast_list(rec: dict | None, fields=TC_FIELDS) -> list | None:
    return [rec.get(k) for k in fields] if rec and rec.get("bytes") is not None else None


class FastTcSampler:
    """v3 A18 (levi / supervisor 19:33-19:41): the session's own shaped classes (its IFB ingress 1:10 and its egress class
    on the interface) every FAST_TC_S, so 'backlogged' is read at 100 ms (the tw W4 rate FAIL is a 1 s classifier
    artifact by the 100 ms bin diagnostic). One row per tick to <session>/tc_fast.jsonl, compact: t (the tick), then per
    class its FAST_TC_FIELDS as a list (null when unread) and its read instants [start, end] (t_in / t_out; brad / levi
    21:27-21:28: each exec takes milliseconds, so readers bin on the class's own instants, never on t). Every instant is
    time.monotonic(); the session's wall anchor {wall0, mono0} (taken together once) is the file's first line and in
    meta, so wall = wall0 + (m - mono0) and an NTP step cannot move or gap them. Ticks run on a
    fixed grid (mono0 + k x FAST_TC_S); a tick that cannot keep the grid is skipped and counted, never bunched. Cost,
    measured: this thread's CPU (time.thread_time) plus each tc child's own CPU (os.wait4), over the wall time.
    Bound (levi 19:41 / 21:27): the SUM over a window's concurrent samplers <= 5 % of one core on chaos, else the
    netlink fallback before a counted window. Runs in both arms of the instrumentation A/B (base truth).
    #15: each tick also reads both devices' qdiscs (t_in_q / t_out_q, after the class reads) and the row carries slot_tc
    {in: {qdisc, class, netem}, out: {class, netem}}, each a TC_FIELDS list or null; the first line's slot_tc names the
    fields and devices. Four execs per tick (two before #15): the measured cost is in meta as before."""

    def __init__(self, session_dir: Path, slot: Slot, iface: str = IFACE_DEFAULT, period_s: float = FAST_TC_S,
                 read=None):
        self.path = Path(session_dir) / "tc_fast.jsonl"
        self.slot, self.iface, self.period = slot, iface, period_s
        self.read = read if read is not None else TcReader()
        self.n = self.skipped = self.unread_in = self.unread_out = 0
        self.unread = {f"{d}.{p}": 0 for d, p in SLOT_TC_PARTS}
        self.child_cpu = 0.0
        self.gaps: list[float] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._cost: dict = {}
        self.mono0 = time.monotonic()
        self.wall0 = time.time()

    @staticmethod
    def _m() -> float:
        return round(time.monotonic(), 4)

    def _read(self, kind: str, dev: str) -> tuple[str, list]:
        t0 = self._m()
        r = self.read(kind, dev)
        t1 = self._m()
        text, cpu = (r[0], r[1]) if isinstance(r, tuple) else (r, 0.0)
        self.child_cpu += cpu
        return text, [t0, t1]

    def row(self) -> dict:
        s = self.slot
        t = self._m()
        ci, t_in = self._read("class", s.ifb_in)
        co, t_out = self._read("class", self.iface)
        qi, t_in_q = self._read("qdisc", s.ifb_in)
        qo, t_out_q = self._read("qdisc", self.iface)
        sc = slot_tc_record(s, self.iface, ci, co, qi, qo)
        return {"t": t, "in": _fast_list(sc["in"]["class"], FAST_TC_FIELDS), "t_in": t_in,
                "out": _fast_list(sc["out"]["class"], FAST_TC_FIELDS), "t_out": t_out, "t_in_q": t_in_q,
                "t_out_q": t_out_q,
                "slot_tc": {d: {p: _fast_list(v) for p, v in sc[d].items() if p != "dev"} for d in ("in", "out")}}

    def _run(self):
        w0, th0 = time.monotonic(), time.thread_time()
        nxt, last = time.monotonic(), None
        try:
            with open(self.path, "a", buffering=1 << 16) as fh:
                fh.write(json.dumps({"anchor": self.anchor(), "slot_tc": self.layout()}) + "\n")
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - nxt >= self.period:             # behind the grid: skip to the next slot, counted
                        k = int((now - nxt) // self.period)
                        self.skipped += k
                        nxt += k * self.period
                    m = time.monotonic()
                    try:
                        r = self.row()
                    except Exception:
                        r = {"t": self._m(), "in": None, "t_in": None, "out": None, "t_out": None, "t_in_q": None,
                             "t_out_q": None, "slot_tc": None}
                    self.unread_in += r["in"] is None
                    self.unread_out += r["out"] is None
                    for d, p in SLOT_TC_PARTS:
                        self.unread[f"{d}.{p}"] += not ((r.get("slot_tc") or {}).get(d) or {}).get(p)
                    if last is not None:
                        self.gaps.append(m - last)
                    last = m
                    fh.write(json.dumps(r, separators=(",", ":")) + "\n")
                    self.n += 1
                    nxt += self.period
                    self._stop.wait(max(0.0, nxt - time.monotonic()))
        except OSError:
            pass
        wall = max(1e-9, time.monotonic() - w0)
        th = time.thread_time() - th0
        self._cost = {"wall_s": round(wall, 1), "thread_cpu_s": round(th, 2), "tc_child_cpu_s": round(self.child_cpu, 2),
                      "cpu_share_of_core": round((th + self.child_cpu) / wall, 4)}

    def anchor(self) -> dict:
        return {"wall0": self.wall0, "mono0": self.mono0}

    def layout(self) -> dict:
        """What slot_tc's lists are: field order and the devices / class each part was read from."""
        s = self.slot
        return {"fields": list(TC_FIELDS), "in": {"dev": s.ifb_in, "qdisc": "root", "class": "1:10", "netem": "1:10"},
                "out": {"dev": self.iface, "class": s.egress_class, "netem": s.egress_class}}

    def start(self) -> "FastTcSampler":
        self._t = threading.Thread(target=self._run, name="tc_fast", daemon=True)
        self._t.start()
        return self

    def stop(self) -> dict:
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=5)
        g = sorted(self.gaps)
        q = lambda p: round(g[min(len(g) - 1, int(p * len(g)))], 4) if g else None
        return {"file": self.path.name, "period_s": self.period, "samples": self.n, "skipped_ticks": self.skipped,
                "unread_in": self.unread_in, "unread_out": self.unread_out, "gap_p50_s": q(0.5), "gap_p99_s": q(0.99),
                "gap_max_s": round(g[-1], 4) if g else None, "anchor": self.anchor(),
                "clock": "instants monotonic; wall = wall0 + (m - mono0)",
                **self._cost, "slot_tc": self.layout(), "slot_tc_unread": dict(self.unread),
                **({"tc_read": self.read.record()} if hasattr(self.read, "record") else {})}


def shaper_bound(prev: dict | None, cur: dict | None) -> bool:
    """One interval shaper-bound, between two samples of a class's tc counters (prev, cur: parse_classes rows): its
    backlog > 0 at cur, or its drops grew since prev. The one definition (levi t58, brad's B-8): TcSampler's summary and
    ops/diag/trio_dry_run's shaper_bound_share import it. Report-only (levi on cc62d89): the class's drops include
    netem's emulated loss, so a lossy state reads bound with no backlog; it never feeds a gate or a qualifying rule (the
    player load qualifies by the tokens rule)."""
    if not cur:
        return False
    return bool(cur.get("backlog")) or (prev is not None and (cur.get("drops") or 0) > (prev.get("drops") or 0))


# ------------------------------------------------------------------------------------------------ records
def concurrency_record(s: Slot | None, **extra) -> dict:
    """meta.json concurrency (design §6, Q am. 13); single mode writes {mode: single}."""
    if s is None:
        return {"mode": "single"}
    import os
    import record
    # of record (levi): a counted run (orchestrate --trio-record) under the current era; steering counts only these
    import session_meta
    # t56 (levi's pooling ruling): n, the composition and the collector's sha per session
    return {"mode": "trio", "pool": "per_session", **s.record(), "record": os.environ.get("LVDC_TRIO_RECORD") == "1",
            "era": record.ERA, "n": len(SLOTS), "composition": composition_of(SLOTS),
            "lvdc_commit": session_meta.lvdc_commit(), "mgmt_udp_port": mgmt_udp_port_record(),
            **({"n_draw": json.loads(os.environ["LVDC_N_DRAW"])} if os.environ.get("LVDC_N_DRAW") else {}),
            **({"window_stratum": json.loads(os.environ["LVDC_WINDOW_STRATUM"])} if os.environ.get("LVDC_WINDOW_STRATUM") else {}),
            **({"window": json.loads(os.environ["LVDC_TRIO_WINDOW"])} if os.environ.get("LVDC_TRIO_WINDOW") else {}),
            **extra}


def slots_log_append(path: Path, rec: dict) -> None:
    """The orchestrator's slot timeline (data/_trio/_summaries/trio_slots.jsonl): every session's start / end per slot, from
    which each session's siblings and their overlap are read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _spans(path: Path) -> dict[str, dict] | None:
    """Every session's span in the slots log ({session: {session, slot, start, end, ...}}); None: no log."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
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
    return spans


def siblings(path: Path, session: str, t0: float, t1: float | None) -> list[dict]:
    """The other sessions whose slot window overlaps [t0, t1] (t1 None: now), from the slots log, with the overlap in
    seconds (a sibling still open counts up to t1)."""
    spans = _spans(path)
    if spans is None:
        return []
    hi = t1 if t1 is not None else time.time()
    out = []
    for k, d in sorted(spans.items()):
        if k == session or d.get("start") is None:
            continue
        lo2, hi2 = max(t0, d["start"]), min(hi, d["end"] if d.get("end") is not None else hi)
        if hi2 > lo2:
            out.append({**d, "overlap_s": round(hi2 - lo2, 1)})
    return out


def concurrency_over(path: Path, session: str, t0: float, t1: float | None) -> dict | None:
    """The restart's §10 covariate: the number of sessions running (this one included) over [t0, t1] (t1 None: now),
    from the slots log, as a step series ([[seconds from t0, n], ...] at every change; per second by construction) and
    its time-weighted mean, min and max. None: no log."""
    spans = _spans(path)
    if spans is None:
        return None
    hi = t1 if t1 is not None else time.time()
    if hi <= t0:
        return {"mean": 1.0, "min": 1, "max": 1, "steps": [[0.0, 1]], "span_s": 0.0}
    delta: dict[float, int] = {}
    for k, d in spans.items():
        if k == session or d.get("start") is None:
            continue
        lo2, hi2 = max(t0, d["start"]), min(hi, d["end"] if d.get("end") is not None else hi)
        if hi2 > lo2:
            delta[lo2] = delta.get(lo2, 0) + 1
            delta[hi2] = delta.get(hi2, 0) - 1
    times = [t for t in sorted(set(delta) | {t0}) if t < hi]
    n, steps, area = 1, [], 0.0
    for i, t in enumerate(times):
        n += delta.get(t, 0)
        area += n * ((times[i + 1] if i + 1 < len(times) else hi) - t)
        if not steps or steps[-1][1] != n:
            steps.append([round(t - t0, 1), n])
    ns = [x[1] for x in steps]
    return {"mean": round(area / (hi - t0), 4), "min": min(ns), "max": max(ns), "steps": steps,
            "span_s": round(hi - t0, 1)}


# ------------------------------------------------------------------------------------------------ DNS per session
# design v3 §8 / L-C6: the system resolver's upstream DNS is unmarked (it is systemd-resolved's, not the session's), so
# it lands in the default class, not in the session's pcap. Each slot captures the default class's DNS during its
# session (dns_default.pcap) and writes dns_answers.json in LVQoE's form (watcher/lv/dns_answers: the same tshark query,
# {address: [names]}, first seen first): its own pcap's answers, then the default capture's for addresses its own pcap
# contacted. LVQoE's ingest reads the file instead of scanning the pcap (load_or_build_dns_answers). The resolver
# caches: a name resolved by an earlier session of the run (same CDN node) is not asked again, so the earlier sessions'
# DNS captures of the same run are read too, and run_trio flushes the resolver's cache at trio start (levi: a contacted
# media address with no name in dns_answers.json is a gate-2 FAIL).
DNS_FILTER = "port 53"
DNS_TSHARK_FILTER = "dns.qry.type == 1 or dns.qry.type == 28"      # LVQoE scan_pcap_for_dns_answers


def dns_capture_start(staging: Path) -> subprocess.Popen:
    cmd = (f'exec tcpdump {TCPDUMP_Z} -B {TCPDUMP_B_KIB} -i lvi_def -U -w - "{DNS_FILTER}" > "{staging / "dns_default.pcap"}" '
           f'2> "{staging / "dns_default.err"}"')
    return subprocess.Popen(["sudo", "-n", "sh", "-c", cmd])


# v3 A17 (t67; levi / supervisor 17:58, brad): the default class's TCP headers, beside its DNS, every session, so the
# preregistered shared-path detector has its anchor (a default-class RTT sample) on v3 data; on t58 (c) of d2883c3c was
# uncomputable for want of it. Headers only (snaplen DEFAULT_TCP_SNAPLEN), both mirrors of the unshaped default class.
DEFAULT_TCP_SNAPLEN = 128
DEFAULT_TCP_FILTER = "tcp"


def default_tcp_capture_start(staging: Path) -> list[subprocess.Popen]:
    out = []
    for dev, side in (("lvi_def", "in"), ("lvo_def", "out")):
        cmd = (f'exec tcpdump {TCPDUMP_Z} -B {TCPDUMP_B_KIB} -s {DEFAULT_TCP_SNAPLEN} -i {dev} -U -w - "{DEFAULT_TCP_FILTER}" '
               f'> "{staging / f"default_tcp_{side}.pcap"}" 2> "{staging / f"default_tcp_{side}.err"}"')
        out.append(subprocess.Popen(["sudo", "-n", "sh", "-c", cmd]))
    return out


def client_addr(iface: str, run=subprocess.run) -> dict:
    """v3 A16 (t67; the W4 loss reader's client bug): the collection interface's IPv4 address(es) at session start
    (`ip -4 -o addr show dev <iface>`), so readers take the client from the record, never infer it. All slots share it."""
    import re
    try:
        out = run(["ip", "-4", "-o", "addr", "show", "dev", iface], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return {"iface": iface, "addr": None, "addrs": [], "error": repr(exc)[:200]}
    addrs = re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", out or "")
    return {"iface": iface, "addr": addrs[0] if addrs else None, "addrs": addrs}


def dns_capture_stop(p: subprocess.Popen | None) -> None:
    if p is None:
        return
    subprocess.run(["sudo", "-n", "kill", "-INT", str(p.pid)], capture_output=True)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "-n", "kill", "-KILL", str(p.pid)], capture_output=True)


def parse_dns(text: str) -> dict[str, list[str]]:
    """LVQoE watcher/lv/dns_answers.parse_tshark_dns_output: lines `name<TAB>a,a<TAB>aaaa` -> {ip: [names]}."""
    out: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        parts = line.rstrip("\n").split("\t")
        if not parts or not parts[0]:
            continue
        host = parts[0].strip()
        for field in parts[1:]:
            for ip in field.split(","):
                ip = ip.strip()
                if ip:
                    arr = out.setdefault(ip, [])
                    if host not in arr:
                        arr.append(host)
    return out


def tshark_dns(pcap: Path, tshark: str = "tshark") -> str:
    try:
        r = subprocess.run([tshark, "-r", str(pcap), "-n", "-Y", DNS_TSHARK_FILTER, "-T", "fields", "-e", "dns.qry.name",
                            "-e", "dns.a", "-e", "dns.aaaa"], capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


_TUPLE4 = re.compile(r" IP (\d+\.\d+\.\d+\.\d+)\.\d+ > (\d+\.\d+\.\d+\.\d+)\.\d+:")


def contacted_addresses(text: str) -> set:
    """Every IPv4 address on a TCP / UDP packet in `tcpdump -nn -q -r` output (both ends)."""
    return {ip for m in _TUPLE4.finditer(text or "") for ip in m.groups()}


def run_dns_pcaps(slots_log: Path, troot: Path, run: str | None, session: str) -> list[Path]:
    """The DNS captures of the sessions of trio run `run` that ended before now (their session dirs, accepted or
    _failed), from the slots log."""
    out = []
    try:
        lines = slots_log.read_text().splitlines()
    except OSError:
        return out
    started, ended = {}, set()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("event") == "start" and r.get("run") == run and r.get("session") != session:
            started[r["session"]] = r.get("platform")
        elif r.get("event") == "end":
            ended.add(r.get("session"))
    for s, plat in started.items():
        if s in ended and plat:
            for d in (troot / plat / s, troot / "_failed" / plat / s):
                if (d / SESSION_TRIO_DIR / "dns_default.pcap").exists():
                    out.append(d / SESSION_TRIO_DIR / "dns_default.pcap")
                    break
    return sorted(out)


def dns_answers_for(own_pcap: Path, default_dns_pcaps, tshark: str = "tshark") -> tuple[dict, dict]:
    """default_dns_pcaps: the session's own default-class DNS capture first, then the run's earlier ones."""
    if isinstance(default_dns_pcaps, (str, Path)) or default_dns_pcaps is None:
        default_dns_pcaps = [default_dns_pcaps] if default_dns_pcaps else []
    own = parse_dns(tshark_dns(own_pcap, tshark))
    dflt: dict = {}
    for pc in default_dns_pcaps:
        if pc and Path(pc).exists():
            for ip, names in parse_dns(tshark_dns(Path(pc), tshark)).items():
                arr = dflt.setdefault(ip, [])
                arr += [n for n in names if n not in arr]
    addrs: set = set()
    if dflt:
        try:
            addrs = contacted_addresses(subprocess.run(["tcpdump", "-nn", "-q", "-r", str(own_pcap)], capture_output=True,
                                                       text=True, timeout=600).stdout)
        except (OSError, subprocess.SubprocessError):
            addrs = set()
    out = {ip: list(v) for ip, v in own.items()}
    kept = 0
    for ip, names in dflt.items():
        if ip in addrs:
            kept += 1
            arr = out.setdefault(ip, [])
            arr += [n for n in names if n not in arr]
    return out, {"own_pcap_addresses": len(own), "default_addresses": len(dflt), "default_kept": kept,
                 "contacted_addresses": len(addrs), "addresses": len(out),
                 "default_pcaps": [str(p) for p in default_dns_pcaps]}


def trio_preflight(platforms, repo: Path, templates: Path, data: Path, tools=("tc", "nft", "tcpdump", "mergecap",
                                                                               "tshark", "make", "xrandr")) -> dict:
    """Before any slot starts (supervisor 2026-10-19, after pilot-0's missing profiles/ link): what exp.py needs from
    ignored or local paths and the box's tools. {ok, problems: [..], checks}."""
    import shutil as _sh
    checks = {"venv_python": (repo / ".venv" / "bin" / "python3").exists(),
              "current_data": (repo / "current_data").is_dir(),
              "data_dir": data.is_dir(),
              "labeling_extension": (repo / "labeling" / "manifest.json").exists()}
    for p in platforms:
        checks[f"profile_template_{p}"] = (templates / p).is_dir()
    for t in tools:
        checks[f"tool_{t}"] = bool(_sh.which(t)) or Path(f"/usr/sbin/{t}").exists()
    problems = [k for k, v in checks.items() if not v]
    return {"ok": not problems, "problems": problems, "checks": checks, "templates": str(templates), "at": time.time()}


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


def staging_floor_gb(runs_dir: Path, base_gb: float, platforms=("youtube", "twitch", "tiktok"),
                     n_slots: int | None = None) -> dict:
    """The data volume's floor while the trio stages there (t58): the volume's own floor + the slot count (N-5) times
    the largest per-session pcap of the dataset roots (merged.pcap of accepted and _failed sessions, single mode's root
    and the counted trio root data/_trio; never decode_stress's _trio_stress, whose held top-state pcaps inflated it:
    supervisor 2026-10-19, the N = 5 run)."""
    n = n_slots if n_slots is not None else len(SLOTS)
    largest = 0
    for root in (runs_dir, runs_dir / DATASET_TRIO_DIR):
        for p in platforms:
            for f in list((root / p).glob("*/merged.pcap")) + list((root / "_failed" / p).glob("*/merged.pcap")):
                try:
                    largest = max(largest, f.stat().st_size)
                except OSError:
                    pass
    return {"base_gb": base_gb, "largest_pcap_gb": round(largest / 1e9, 2), "n_slots": n,
            "floor_gb": round(base_gb + n * largest / 1e9, 1), "applies_to": "data volume (trio staging)",
            "staging": str(STAGING)}
