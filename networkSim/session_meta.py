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


CDP_BASE = "http://localhost:9222"      # a trio slot's exp.py sets its own port (trio_slots.Slot.cdp_port)


def chrome_version(cdp_base: str | None = None, tries: int = 1, wait_s: float = 2.0) -> dict:
    """Chrome's version from /json/version; up to `tries` attempts `wait_s` apart (the last error kept)."""
    cdp_base = cdp_base or CDP_BASE
    err = None
    for i in range(max(1, tries)):
        try:
            with urllib.request.urlopen(f"{cdp_base}/json/version", timeout=5) as r:
                v = json.loads(r.read())
            return {"browser": v.get("Browser"), "user_agent": v.get("User-Agent"), "protocol": v.get("Protocol-Version"),
                    **({"tries": i + 1} if i else {})}
        except Exception as e:
            err = repr(e)
            if i + 1 < tries:
                time.sleep(wait_s)
    return {"browser": None, "error": err, "tries": max(1, tries)}


CHROME_END_TRIES = 5       # tw_43 (shagun 2026-10-19): the end read was refused once; then the start read stands


def chrome_at_end(session_dir: Path) -> dict:
    """meta.chrome at finish: the end read (CHROME_END_TRIES attempts); when Chrome no longer answers there (tw_43:
    refused, the session otherwise complete), the start read (meta.chrome_start, taken once the browser attached) with
    the end error beside it, so a version string never fails a session that ran."""
    end = chrome_version(tries=CHROME_END_TRIES)
    if end.get("browser"):
        return end
    p = Path(session_dir) / "meta.json"
    start = (json.loads(p.read_text()) if p.exists() else {}).get("chrome_start") or {}
    if start.get("browser"):
        return {**start, "source": "chrome_start (the end read failed)", "end_error": end.get("error"),
                "end_tries": end.get("tries")}
    return end


def browser_pid_cdp(cdp_base: str | None = None) -> int | None:
    """The browser process's pid over CDP (SystemInfo.getProcessInfo, type "browser"); None when it cannot be read."""
    try:
        import websocket
        with urllib.request.urlopen(f"{cdp_base or CDP_BASE}/json/version", timeout=5) as r:
            ws_url = json.loads(r.read())["webSocketDebuggerUrl"]
        ws = websocket.create_connection(ws_url, timeout=5)
        try:
            ws.send(json.dumps({"id": 1, "method": "SystemInfo.getProcessInfo"}))
            for _ in range(100):
                m = json.loads(ws.recv())
                if m.get("id") == 1:
                    procs = (m.get("result") or {}).get("processInfo") or []
                    return next((int(x["id"]) for x in procs if x.get("type") == "browser"), None)
        finally:
            ws.close()
    except Exception:
        return None
    return None


def proc_argv(raw: bytes) -> tuple[list[str], bool]:
    """(argv, title_rewritten) from /proc/<pid>/cmdline. Chrome rewrites its process title (shagun on chaos: the
    browser's cmdline is one space-joined string with a single trailing NUL), so a single part holding spaces is split
    on whitespace (lossy only for an argument with a space in it)."""
    parts = [a.decode(errors="replace") for a in raw.split(b"\0") if a]
    if len(parts) == 1 and " " in parts[0]:
        return parts[0].split(), True
    return parts, False


def chrome_cmdline(cdp_base: str | None = None, proc_root: str = "/proc") -> dict:
    """The launched browser process's command line (t53, levi: the anti-throttling flags are in both modes; recorded so
    each session shows them). The pid from CDP (browser_pid_cdp), else the /proc process with --remote-debugging-port=<the
    CDP port> and no --type= (not a renderer / GPU child); its argv by proc_argv (Chrome's rewritten title handled;
    composite_check on chaos, 179c870: a NUL split never found it). Linux only; never raises."""
    import re
    port = re.search(r":(\d+)", cdp_base or CDP_BASE)
    want = f"--remote-debugging-port={port.group(1) if port else 9222}"
    root = Path(proc_root)

    def argv_of(pid: int):
        return proc_argv((root / str(pid) / "cmdline").read_bytes())
    try:
        pid = browser_pid_cdp(cdp_base) if proc_root == "/proc" else None
        if pid is not None:
            try:
                args, rewritten = argv_of(pid)
            except OSError:
                args, rewritten = None, None
            return {"pid": pid, "argv": args, "source": "cdp SystemInfo.getProcessInfo", "title_rewritten": rewritten}
        for p in root.iterdir():
            if not p.name.isdigit():
                continue
            try:
                args, rewritten = argv_of(int(p.name))
            except OSError:
                continue
            if want in args and not any(a.startswith("--type=") for a in args):
                return {"pid": int(p.name), "argv": args, "source": "proc scan", "title_rewritten": rewritten}
        return {"pid": None, "argv": None, "error": f"no browser process with {want}"}
    except Exception as e:
        return {"pid": None, "argv": None, "error": repr(e)}


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


def data_volume(path: Path) -> dict:
    """Where a session's data physically lands (t37, rushi / supervisor): the realpath, the mount point / source /
    fstype holding it (findmnt -T; Linux) and the free / total GB. Best effort, never fatal."""
    import shutil
    rp = Path(path).resolve()
    out: dict = {"path": str(path), "realpath": str(rp)}
    try:
        du = shutil.disk_usage(rp)
        out.update(free_gb=round(du.free / 1e9, 1), total_gb=round(du.total / 1e9, 1), free_share=round(du.free / du.total, 3))
    except Exception as e:
        out["disk_error"] = repr(e)
    raw = _run(["findmnt", "-T", str(rp), "-J", "-o", "TARGET,SOURCE,FSTYPE,OPTIONS"])
    try:
        fs = json.loads(raw)["filesystems"][0] if raw else None
    except Exception:
        fs = None
    out["mount"] = ({k: fs.get(k) for k in ("target", "source", "fstype", "options")} if fs
                    else {"error": "findmnt unavailable or failed"})
    return out


def mgmt_exclusion() -> dict:
    """t37: the management traffic excluded from shaping and capture (Makefile mgmt-exclusion) and the tailscale link
    state: a direct tunnel is the excluded UDP port; a DERP-relayed one (TCP 443) is not excludable by port."""
    out: dict = {}
    raw = _run(["make", "-s", "-C", str(REPO / "networkSim"), "mgmt-exclusion"])
    try:
        out["exclusion"] = json.loads(raw) if raw else None
    except Exception:
        out["exclusion"] = {"raw": raw}
    ts = _run(["tailscale", "status", "--json"], timeout=10)
    if ts:
        try:
            st = json.loads(ts)
            out["tailscale"] = {
                "self": (st.get("Self") or {}).get("HostName"),
                "peers": [{"host": p.get("HostName"), "online": p.get("Online"), "cur_addr": p.get("CurAddr"),
                           "relay": p.get("Relay"), "direct": bool(p.get("CurAddr"))}
                          for p in (st.get("Peer") or {}).values() if p.get("Online")]}
        except Exception:
            out["tailscale"] = {"raw": ts[:500]}
    else:
        out["tailscale"] = None
    return out


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


def display_state(drm_root: str = "/sys/class/drm") -> dict:
    """Decision A (levi 00271 item 4, supervisor 2026-10-19), read only: the monitor's DPMS state (`xset q`: DPMS
    enabled, "Monitor is On / Off / in Standby / in Suspend") and the display topology (`xrandr --listproviders`; each
    connected DRM connector and its card's driver). Both change GPU load: on chaos X renders on the Intel GPU while the
    monitor hangs off the NVIDIA (reverse PRIME), so every displayed frame is copied across, and DPMS off may drop it."""
    import re
    out: dict = {"at": time.time()}
    q = _run(["xset", "q"])
    out["dpms_enabled"] = None if q is None else (True if "DPMS is Enabled" in q else
                                                 False if "DPMS is Disabled" in q else None)
    m = re.search(r"Monitor is (On|Off|in Standby|in Suspend)", q or "")
    out["monitor"] = m.group(1) if m else None
    prov = _run(["xrandr", "--listproviders"])
    out["providers"] = [line.strip() for line in prov.splitlines()[1:] if line.strip()] if prov else None
    connected = []
    for c in sorted(Path(drm_root).glob("card*-*")):
        try:
            if (c / "status").read_text().strip() != "connected":
                continue
            card = c.name.split("-", 1)[0]
            connected.append({"connector": c.name, "driver": (Path(drm_root) / card / "device" / "driver").resolve().name})
        except OSError:
            continue
    out["connected"] = connected
    return out


def display_change(start: dict | None, end: dict | None) -> dict:
    """Hardening stop: the monitor's state at the session's start and end (display_state) side by side, and woke = it was
    not On at the start (--dpms-off forced it off) and is On at the end. A wake the keeper re-blanked before the end is
    not visible here (keep_display_off.sh's log is that record); an unreadable state is None, never a wake."""
    a, b = (start or {}).get("monitor"), (end or {}).get("monitor")
    return {"monitor": [a, b], "woke": a is not None and b is not None and a != "On" and b == "On",
            "dpms_enabled": [(start or {}).get("dpms_enabled"), (end or {}).get("dpms_enabled")]}


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


VA_PACKAGES = ("intel-media-va-driver-non-free", "intel-media-va-driver", "libva2", "libigdgmm12")


def platform_stack(run=None) -> dict:
    """levi 15:42 (record only): the kernel (uname -r) and the VA driver stack's package versions at session start
    (dpkg-query, once per session), so the 'unchanged across the era' claim rests on each session's record, not on apt
    history. A package not installed reads None; never raises."""
    import platform as _platform
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=10))
    out: dict = {"kernel_release": _platform.release(), "packages": {}}
    for pkg in VA_PACKAGES:
        try:
            r = run(["dpkg-query", "-W", "-f=${Version}", pkg])
            out["packages"][pkg] = r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
        except Exception as e:  # noqa: BLE001 -- recorded, never fatal
            out["packages"][pkg] = None
            out.setdefault("errors", {})[pkg] = repr(e)[:120]
    return out


def start(session_dir: Path, platform: str, transitions: dict, episode_s: int, argv: list[str],
          chrome_profile: Path, matrix_id: str | None = None, low_state_cap: int | None = None,
          initial_state_rule: str | None = None, forced_initial_state: str | None = None,
          channel_url_requested: str | None = None, channel_type: str | None = None,
          channel_handle: str | None = None, profile_template: dict | None = None, schedule: dict | None = None,
          shaping: dict | None = None) -> dict:
    """schedule (Q am. 17): utils/shaping.schedule_of's record plus the session's episodes, merged into shaper (episode_s
    is the schedule's; low_state_cap stays in base 60 s episodes, low_cap_s is the cap in seconds). shaping (restart
    §2): utils/netStat.session_shaping's record (walk, dl_factor), merged into shaper."""
    sched = {k: v for k, v in (schedule or {}).items() if k != "episode_s"}
    return write(session_dir, schema="lvdc_session_meta_v2", platform=platform, argv=argv,
                 started_at=time.time(), hostname=socket.gethostname(), lvdc_commit=lvdc_commit(),
                 extension_version=extension_version(), chrome_profile=str(chrome_profile),
                 shaper=shaper_config(platform, transitions, episode_s, matrix_id=matrix_id,
                                      low_state_cap=low_state_cap, initial_state_rule=initial_state_rule,
                                      forced_initial_state=forced_initial_state, **sched, **(shaping or {})),
                 channel_url_requested=channel_url_requested, channel_type=channel_type, channel_handle=channel_handle,
                 profile_template=profile_template,
                 ntp_offset_start=ntp_offset(), ipv6_sysctl=ipv6_state(), data_volume_start=data_volume(session_dir),
                 mgmt_exclusion=mgmt_exclusion(), platform_stack=platform_stack())


def finish(session_dir: Path, channel_url: str | None, **extra) -> dict:
    """extra: e.g. chat_action, geometry (window / viewport / player size at start), window_size_flag."""
    return write(session_dir, ended_at=time.time(), channel_url=channel_url, chrome=chrome_at_end(session_dir),
                 chrome_cmdline=chrome_cmdline(), ntp_offset_end=ntp_offset(), data_volume_end=data_volume(session_dir),
                 **extra)
