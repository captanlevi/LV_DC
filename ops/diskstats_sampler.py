#!/usr/bin/env python3
"""levi (t56, 2026-10-20): 1 s /proc/diskstats sampler beside the box record, per counted round (ops only). For each watched device
(the data volume and the system SSD) writes one jsonl row per interval with the deltas: reads / writes completed, sectors read /
written (-> MB/s), ms reading / writing, ios in flight, ms doing io (-> util %), and await = (ms reading + ms writing) / (ios completed).
A device is a /proc/diskstats name (sda, nvme0n1) or a mount point (an absolute path), resolved at start to the whole disk under it
(st_dev -> /sys/dev/block/MAJ:MIN; a partition -> its parent disk), so a disk swap behind the same mount path (the 4 TB migration,
shagun's runbook 00314) needs no edit; a path that is not a mount point stops the sampler (never a silent read of the root disk). The
header records each argument's resolution. Default: the data volume's mount (LVDC_MOUNT, as ops/env.sh) and /; after a
cutover that keeps the old disk mounted, pass both mounts. Moved into the repo from chaos current_data (sha256
b57ab7eddb89, devices by name only; rows unchanged).
Usage: diskstats_sampler.py <out.jsonl> [devices or mount points...]; stop with SIGTERM / SIGINT. Read only."""
import hashlib
import json
import os
import signal
import sys
import time

# the data volume's mount as ops/env.sh sets it (LVDC_MOUNT, default /media/<user>/memories), and the system disk
_USER = os.environ.get("SUDO_USER") or os.environ.get("USER") or "captanlevi"
DEFAULT_DEVICES = [os.environ.get("LVDC_MOUNT") or f"/media/{_USER}/memories", "/"]


def resolve(arg: str, sys_root: str = "/sys", ismount=os.path.ismount, stat=os.stat) -> str:
    """A device name as given; a mount point -> the whole disk holding it (its partition's parent)."""
    if not arg.startswith("/"):
        return arg
    if not ismount(arg):
        raise SystemExit(f"diskstats_sampler: {arg} is not a mount point")
    st = stat(arg).st_dev
    node = os.path.realpath(os.path.join(sys_root, "dev", "block", f"{os.major(st)}:{os.minor(st)}"))
    if os.path.exists(os.path.join(node, "partition")):
        node = os.path.dirname(node)
    return os.path.basename(node)


def read(devs, path="/proc/diskstats"):
    r = {}
    for line in open(path):
        f = line.split()
        if f[2] in devs:
            v = list(map(int, f[3:14]))
            r[f[2]] = dict(rd=v[0], rd_sec=v[2], rd_ms=v[3], wr=v[4], wr_sec=v[6], wr_ms=v[7], inflight=v[8], io_ms=v[9], wio_ms=v[10])
    return r


def row(prev, cur, t, dt):
    out = {"t": round(t, 3), "dt": round(dt, 3)}
    for d, c in cur.items():
        p = prev.get(d)
        if not p:
            continue
        n = (c["rd"] - p["rd"]) + (c["wr"] - p["wr"])
        out[d] = {"r_mbps": round((c["rd_sec"] - p["rd_sec"]) * 512 / 1e6 / dt, 3), "w_mbps": round((c["wr_sec"] - p["wr_sec"]) * 512 / 1e6 / dt, 3),
                  "r_iops": round((c["rd"] - p["rd"]) / dt, 1), "w_iops": round((c["wr"] - p["wr"]) / dt, 1),
                  "await_ms": round(((c["rd_ms"] - p["rd_ms"]) + (c["wr_ms"] - p["wr_ms"])) / n, 2) if n else None,
                  "util_pct": round(100 * (c["io_ms"] - p["io_ms"]) / (dt * 1000), 1), "inflight": c["inflight"]}
    return out


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out, args = argv[0], argv[1:] or DEFAULT_DEVICES
    resolved = {a: resolve(a) for a in args}
    devs = list(dict.fromkeys(resolved.values()))
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    with open(out, "a") as fh:
        fh.write(json.dumps({"header": True, "what": "diskstats 1 s deltas", "devices": devs, "resolved": resolved, "pid": os.getpid(),
                             "file_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest()}) + "\n")
        fh.flush()
        prev, pt = read(devs), time.time()
        while not stop:
            time.sleep(1.0)
            cur, t = read(devs), time.time()
            fh.write(json.dumps(row(prev, cur, t, t - pt)) + "\n")
            fh.flush()
            prev, pt = cur, t
    return 0


if __name__ == "__main__":
    sys.exit(main())
