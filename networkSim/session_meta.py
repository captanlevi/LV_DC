"""session_meta.py: <session_dir>/meta.json for collection v2 (LVQoE t35).

Records what produced a session: LV_DC commit (and whether the tree was dirty), Chrome and extension versions, the
shaper configuration (scenario table, the platform's transition matrix, episode length, interface), the channel URL
actually collected, the box hostname and its NTP offset, and start / end times. Written at start and updated at the end;
every field is best effort (a missing value is recorded as null with the reason, never fatal).
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], timeout: float = 10.0) -> str | None:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout or out.stderr).strip() if out.returncode == 0 else None
    except Exception:
        return None


def lvdc_commit() -> dict:
    sha = _run(["git", "-C", str(REPO), "rev-parse", "HEAD"])
    dirty = _run(["git", "-C", str(REPO), "status", "--porcelain"])
    return {"sha": sha, "dirty": bool(dirty) if dirty is not None else None}


def chrome_version(cdp_base: str = "http://localhost:9222") -> dict:
    try:
        with urllib.request.urlopen(f"{cdp_base}/json/version", timeout=5) as r:
            v = json.loads(r.read())
        return {"browser": v.get("Browser"), "user_agent": v.get("User-Agent"), "protocol": v.get("Protocol-Version")}
    except Exception as e:
        return {"browser": None, "error": repr(e)}


def extension_version() -> str | None:
    try:
        return json.loads((REPO / "labeling" / "manifest.json").read_text()).get("version")
    except Exception:
        return None


def ntp_offset() -> dict:
    """Clock offset vs NTP, from whichever tool the box has (chrony, ntpdate, sntp); raw output kept."""
    for cmd in (["chronyc", "tracking"], ["ntpdate", "-q", "pool.ntp.org"], ["sntp", "pool.ntp.org"]):
        out = _run(cmd, timeout=15)
        if out:
            return {"tool": cmd[0], "raw": out[-2000:]}
    return {"tool": None, "raw": None, "error": "no chronyc / ntpdate / sntp available"}


def ipv6_state() -> dict:
    """IPv6 sysctl state per interface (Linux /proc/sys/net/ipv6/conf/<if>/disable_ipv6; 1 = disabled) and whether
    any interface holds a global IPv6 address (t37: old data is IPv4-only; IPv6 is disabled on the box)."""
    root = Path("/proc/sys/net/ipv6/conf")
    if not root.exists():
        return {"available": False}
    conf = {}
    for d in sorted(root.iterdir()):
        try:
            conf[d.name] = int((d / "disable_ipv6").read_text().strip())
        except Exception:
            pass
    addrs = _run(["ip", "-6", "addr", "show", "scope", "global"])
    return {"available": True, "disable_ipv6": conf, "global_v6_addresses": bool(addrs and "inet6" in addrs)}


def shaper_config(platform: str, transitions: dict, episode_s: int, **extra) -> dict:
    from utils.shaping import SCENARIOS
    scen = {k: {kk: vv for kk, vv in vars(v).items()} for k, v in SCENARIOS.items()}
    iface = None
    try:
        for line in (REPO / "networkSim" / "Makefile").read_text().splitlines():
            if line.startswith("IFACE ?="):
                iface = line.split("=", 1)[1].strip()
    except Exception:
        pass
    return {"platform": platform, "scenarios": scen, "transitions": transitions, "episode_s": episode_s,
            "iface_default": iface, **extra}


def write(session_dir: Path, **fields) -> dict:
    """Merge `fields` into session_dir/meta.json (creating it)."""
    p = session_dir / "meta.json"
    meta = json.loads(p.read_text()) if p.exists() else {}
    meta.update(fields)
    p.write_text(json.dumps(meta, indent=1, default=str))
    return meta


def start(session_dir: Path, platform: str, transitions: dict, episode_s: int, argv: list[str],
          chrome_profile: Path, matrix_id: str | None = None, low_state_cap: int | None = None,
          initial_state_rule: str | None = None, forced_initial_state: str | None = None,
          channel_url_requested: str | None = None, channel_type: str | None = None,
          channel_handle: str | None = None, profile_template: dict | None = None) -> dict:
    return write(session_dir, schema="lvdc_session_meta_v2", platform=platform, argv=argv,
                 started_at=time.time(), hostname=socket.gethostname(), lvdc_commit=lvdc_commit(),
                 extension_version=extension_version(), chrome_profile=str(chrome_profile),
                 shaper=shaper_config(platform, transitions, episode_s, matrix_id=matrix_id,
                                      low_state_cap=low_state_cap, initial_state_rule=initial_state_rule,
                                      forced_initial_state=forced_initial_state),
                 channel_url_requested=channel_url_requested, channel_type=channel_type, channel_handle=channel_handle,
                 profile_template=profile_template,
                 ntp_offset_start=ntp_offset(), ipv6_sysctl=ipv6_state())


def finish(session_dir: Path, channel_url: str | None, **extra) -> dict:
    """extra: e.g. chat_action, geometry (window / viewport / player size at start), window_size_flag."""
    return write(session_dir, ended_at=time.time(), channel_url=channel_url, chrome=chrome_version(),
                 ntp_offset_end=ntp_offset(), **extra)
