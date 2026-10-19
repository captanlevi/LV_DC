#!/usr/bin/env python3
"""Post-ingest disk step (supervisor 2026-10-19: *_raw.pdml, tshark intermediates of several GB per top-state session, are
the bulk on chaos): compress a session's h2_raw.pdml / h3_raw.pdml in place to .zst once its derived outputs exist,
lossless and verified, never deleted. Ops only (not the collector): unit_extractor's file mode reads the .zst (LV_DC
c1f9181); nothing else reads the raw PDML.

Per session, only when http_v2.json exists and (unless --no-verify-pass) verify.json reads PASS:
  zstd -q -T0 -<level> <f> -o <f>.zst; the original's sha256 is recorded first, the .zst is decompressed and its
  sha256 (the round trip) must equal it, and only then does <f> leave (the .zst holds the same bytes); a failed round
  trip keeps the original and removes the .zst. Each file's record goes to <session>/compress_record.json (levi), a
  list: bytes, zst bytes, sha256 (the original), roundtrip_sha256 (the decompressed .zst), zst_sha256, zstd version,
  level.

    nice -n 19 python3 ops/compress_raw_pdml.py <session dir> ...      (or --root <data root>: every session under it)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

FILES = ("h2_raw.pdml", "h3_raw.pdml")
LEVEL = 10


def _sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha_decompressed(z: Path) -> str:
    h = hashlib.sha256()
    proc = subprocess.Popen(["zstd", "-dc", str(z)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    for chunk in iter(lambda: proc.stdout.read(1 << 20), b""):
        h.update(chunk)
    if proc.wait() != 0:
        raise RuntimeError(f"zstd -dc {z} failed")
    return h.hexdigest()


def eligible(sd: Path, verify_pass: bool = True) -> str | None:
    """None when the session's raw PDML may be compressed, else the reason it may not."""
    if not (sd / "http_v2.json").exists():
        return "no http_v2.json (derived output missing)"
    if verify_pass:
        try:
            if json.loads((sd / "verify.json").read_text()).get("verdict") != "PASS":
                return "verify not PASS"
        except (OSError, ValueError):
            return "no verify.json"
    return None


def compress_session(sd: Path, level: int = LEVEL, verify_pass: bool = True) -> list[dict]:
    sd = Path(sd)
    why = eligible(sd, verify_pass)
    if why:
        return [{"session": sd.name, "skipped": why}]
    out = []
    ver = subprocess.run(["zstd", "--version"], capture_output=True, text=True).stdout.strip()
    for name in FILES:
        f, z = sd / name, sd / (name + ".zst")
        if not f.exists():
            continue
        if z.exists():
            out.append({"session": sd.name, "file": name, "skipped": ".zst already present (left as is)"})
            continue
        sha = _sha_file(f)
        r = subprocess.run(["zstd", "-q", "-T0", f"-{level}", str(f), "-o", str(z)], capture_output=True, text=True)
        if r.returncode != 0:
            z.unlink(missing_ok=True)
            out.append({"session": sd.name, "file": name, "error": r.stderr.strip()[:200]})
            continue
        if _sha_decompressed(z) != sha:                    # the round trip: the original stays on a mismatch
            z.unlink(missing_ok=True)
            out.append({"session": sd.name, "file": name, "error": "decompressed sha256 differs: original kept"})
            continue
        rt = _sha_decompressed(z)
        rec = {"t": round(time.time(), 1), "file": name, "bytes": f.stat().st_size, "zst_bytes": z.stat().st_size,
               "sha256": sha, "roundtrip_sha256": rt, "zst_sha256": _sha_file(z), "zstd": ver, "level": level}
        cr = sd / "compress_record.json"                   # written before the original leaves the volume
        try:
            recs = json.loads(cr.read_text())
        except (OSError, ValueError):
            recs = []
        cr.write_text(json.dumps(recs + [rec], indent=1))
        f.unlink()
        out.append({"session": sd.name, **rec})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("sessions", nargs="*", type=Path)
    ap.add_argument("--root", type=Path, default=None, help="every <root>/**/<session> holding a raw PDML")
    ap.add_argument("--level", type=int, default=LEVEL)
    ap.add_argument("--no-verify-pass", dest="verify_pass", action="store_false",
                    help="compress even when verify.json is not PASS (http_v2.json is still required)")
    a = ap.parse_args(argv)
    dirs = list(a.sessions)
    if a.root:
        dirs += sorted({p.parent for name in FILES for p in a.root.rglob(name)})
    bad = 0
    for sd in dirs:
        for r in compress_session(sd, a.level, a.verify_pass):
            print(json.dumps(r), flush=True)
            bad += "error" in r
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
