"""preflight.py: the collection box's state, checked before every --batch session (supervisor 2026-10-18).

Why: a Wi-Fi reconnect at 02:20 on chaos made NetworkManager re-enable IPv6; three sessions were captured on the IPv6
path (ipv6_media FAIL) before anyone looked. The README's "before every start" list is now enforced per session:

  ipv6     re-apply the IPv6-off sysctl file and the interface key, then require 0 global IPv6 addresses
  shaper   tc is installed and the ifb module is loaded (exp.py builds the qdiscs per session after preclean)
  volume   the data root resolves to a mounted volume (not the root filesystem when data/ is a symlink) with at least
           MIN_FREE_GB free
  holds    HELD_PACKAGES (Chrome, the VA-API driver) are apt-held and the OS download timers are inactive (Linux / apt only)
  rootfs   the root filesystem (Chrome's per-session profile clones, logs, tmp) has at least ROOT_MIN_FREE_GB free
           (supervisor 2026-10-18: chaos's root fell to 3.3 GB while the data volume had hundreds)

run() returns {"ok", "problems", "checks", "at"}; orchestrate refuses to start a session when ok is False (exit 8),
writes the result to data/_summaries/preflight_last.json and hands it to exp.py, which records it in meta.json. A
between-sessions watchdog can call the same function: `python preflight.py [data_dir]` prints the JSON, exit 0 / 1.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SYSCTL_FILE = Path(os.environ.get("LVDC_NOIPV6_SYSCTL", "/etc/sysctl.d/99-lvqoe-collection-noipv6.conf"))
IFACE = os.environ.get("LVDC_IFACE", "wlp0s20f3")
ROOT_MIN_FREE_GB = float(os.environ.get("LVDC_ROOT_MIN_FREE_GB", "5"))
# the collection Chrome, and (2026-10-18, shagun's driver upgrade for tt's HEVC decode) the VA-API driver it decodes with
HELD_PACKAGES = ("google-chrome-stable", "intel-media-va-driver-non-free", "libva2")
OS_TIMERS = ("unattended-upgrades", "apt-daily.timer", "apt-daily-upgrade.timer", "fwupd-refresh.timer")


def _sh(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 127, repr(e)


def check_ipv6(sh=_sh, sysctl_file: Path = SYSCTL_FILE, iface: str = IFACE) -> dict:
    out: dict = {"reapplied": False}
    if sysctl_file.exists():
        rc1, _ = sh(["sudo", "-n", "sysctl", "-q", "-p", str(sysctl_file)])
        rc2, _ = sh(["sudo", "-n", "sysctl", "-q", "-w", f"net.ipv6.conf.{iface}.disable_ipv6=1"])
        out["reapplied"] = rc1 == 0 and rc2 == 0
    rc, txt = sh(["ip", "-6", "addr", "show", "scope", "global"])
    out["global_v6"] = txt.count("inet6") if rc == 0 else None
    out["ok"] = out["global_v6"] == 0
    return out


def check_shaper(sh=_sh) -> dict:
    tc = shutil.which("tc") or (Path("/usr/sbin/tc").exists() and "/usr/sbin/tc")
    ifb = Path("/sys/module/ifb").exists()
    if not ifb:
        sh(["sudo", "-n", "modprobe", "ifb"])
        ifb = Path("/sys/module/ifb").exists()
    return {"tc": bool(tc), "ifb_module": ifb, "ok": bool(tc) and ifb}


def check_volume(data_dir: Path, min_free_gb: float, sh=_sh) -> dict:
    data_dir = Path(data_dir)
    rp = data_dir.resolve()
    out: dict = {"data_dir": str(data_dir), "realpath": str(rp), "exists": rp.exists()}
    rc, txt = sh(["findmnt", "-T", str(rp), "-n", "-o", "TARGET"])
    target = txt.strip().splitlines()[0] if rc == 0 and txt.strip() else None
    out["mount"] = target
    # a data/ symlink that resolves onto the root filesystem means the external volume is not mounted (the
    # mount-path trap: udisks mounted it elsewhere and data/ dangles or points at an empty dir)
    out["on_external"] = bool(target) and (target != "/" or not data_dir.is_symlink())
    try:
        out["free_gb"] = round(shutil.disk_usage(rp).free / 1e9, 1)
    except Exception:
        out["free_gb"] = None
    out["min_free_gb"] = min_free_gb
    out["ok"] = out["exists"] and out["on_external"] and out["free_gb"] is not None and out["free_gb"] >= min_free_gb
    return out


def check_holds(sh=_sh) -> dict:
    if not shutil.which("apt-mark") and not Path("/usr/bin/apt-mark").exists():
        return {"applicable": False, "ok": True}
    rc, held = sh(["apt-mark", "showhold"])
    held_set = set(held.split()) if rc == 0 else set()
    rc2, act = sh(["systemctl", "is-active", *OS_TIMERS])
    states = act.split() if act else []
    active = [t for t, s in zip(OS_TIMERS, states) if s == "active"]
    missing = [p for p in HELD_PACKAGES if p not in held_set]
    return {"applicable": True, "held_missing": missing, "timers_active": active, "ok": not missing and not active}


def check_rootfs(min_free_gb: float = ROOT_MIN_FREE_GB, path: str = "/") -> dict:
    try:
        free = round(shutil.disk_usage(path).free / 1e9, 1)
    except Exception:
        free = None
    return {"path": path, "free_gb": free, "min_free_gb": min_free_gb, "ok": free is not None and free >= min_free_gb}


def run(data_dir: Path, min_free_gb: float, sh=_sh, root_min_free_gb: float | None = None) -> dict:
    """root_min_free_gb: the trio's raised rootfs floor (t53 A2: its pcaps are staged on the root filesystem)."""
    checks = {"ipv6": check_ipv6(sh), "shaper": check_shaper(sh), "volume": check_volume(data_dir, min_free_gb, sh),
              "holds": check_holds(sh),
              "rootfs": check_rootfs() if root_min_free_gb is None else check_rootfs(root_min_free_gb)}
    problems = [k for k, v in checks.items() if not v.get("ok")]
    return {"ok": not problems, "problems": problems, "checks": checks, "at": time.time()}


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "data"
    res = run(d, float(os.environ.get("LVDC_MIN_FREE_GB", "25")))
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["ok"] else 1)
