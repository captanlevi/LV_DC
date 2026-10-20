#!/usr/bin/env python3
"""ops/diskstats_sampler.py: a device argument is a /proc/diskstats name or a mount point resolved to the whole disk under it (a
partition's parent; a device-mapper node kept), a path that is not a mount point stops it, and the rows' deltas are the chaos
original's."""
import importlib.util
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("diskstats_sampler", HERE.parent / "ops" / "diskstats_sampler.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def _sys(root: Path) -> None:
    """A fake /sys: 8:1 is sda1 (a partition of sda), 65:5 nvme0n1p5, 9:0 dm-0 (no partition file); majors below 128, as
    macOS's dev_t holds a signed 8-bit major."""
    for maj_min, path, part in (("8:1", "devices/usb/block/sda/sda1", True), ("65:5", "devices/pci/block/nvme0n1/nvme0n1p5", True),
                                ("9:0", "devices/virtual/block/dm-0", False)):
        d = root / path
        d.mkdir(parents=True)
        if part:
            (d / "partition").write_text("1\n")
        (root / "dev" / "block").mkdir(parents=True, exist_ok=True)
        os.symlink(d, root / "dev" / "block" / maj_min)


def test_resolve():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _sys(root)
        devs = {"/media/x": os.makedev(8, 1), "/": os.makedev(65, 5), "/crypt": os.makedev(9, 0)}
        stat = lambda p: SimpleNamespace(st_dev=devs[p])
        mounted = lambda p: p in devs
        assert S.resolve("sda", str(root), mounted, stat) == "sda"                         # a name as given
        assert S.resolve("/media/x", str(root), mounted, stat) == "sda"                    # the partition's disk
        assert S.resolve("/", str(root), mounted, stat) == "nvme0n1"
        assert S.resolve("/crypt", str(root), mounted, stat) == "dm-0"                     # no partition: kept
        try:
            S.resolve("/media/not_mounted", str(root), mounted, stat)
            raise AssertionError("an unmounted path must stop the sampler")
        except SystemExit as e:
            assert "not a mount point" in str(e)


def test_row():
    with tempfile.NamedTemporaryFile("w", delete=False) as fh:
        fh.write("   8       0 sda 100 0 2000 50 200 0 4000 150 0 300 400 0 0 0 0\n"
                 " 259       0 nvme0n1 10 0 20 5 20 0 40 15 0 30 40 0 0 0 0\n")
    try:
        prev = S.read(["sda"], fh.name)
        assert set(prev) == {"sda"} and prev["sda"]["rd_sec"] == 2000
        cur = {"sda": dict(prev["sda"], rd=110, rd_sec=4048, rd_ms=70, wr=210, wr_ms=170, io_ms=800, inflight=2)}
        r = S.row(prev, cur, 1000.0, 1.0)
        assert r["sda"] == {"r_mbps": 1.049, "w_mbps": 0.0, "r_iops": 10.0, "w_iops": 10.0, "await_ms": 2.0,
                            "util_pct": 50.0, "inflight": 2}, r
    finally:
        os.unlink(fh.name)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_diskstats_sampler: ok")
