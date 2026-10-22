#!/usr/bin/env python3
"""The trace regime's committed manifest (LVQoE restart_2026-10-21_prereg_levi.md §9, the pre-commitment: the file list
and sha256 fixed before the first trace session; the regime itself lands at the next boundary). Three public, citable
sources, equal weight by source:

 - hsdpa_3g_norway: Riiser et al., MMSys 2013 (every log of the 11 routes, as published);
 - lte_4g_ghent: van der Hooft et al., IEEE Comm. Letters 2016 (every report_*.log, as published);
 - fcc_mba_webget: the FCC Measuring Broadband America raw release of June 2016, table curr_webget (web browsing), one
   series per (unit_id, target) in time order, each fetch's bytes_sec held 5 s (Pensieve's construction, which used
   this release); the subset: the series with >= FCC_MIN_VALUES fetches (>= the shortest session, 10 min), a seeded
   sample of FCC_SAMPLE of them (random.Random(FCC_SEED) over the sorted keys); each derived series' sha256 is pinned.

The data never enter git: they live in the cache (--cache, default $LVDC_TRACE_CACHE or ~/Desktop/LV_DC_traces_cache).
The manifest is deterministic (sorted, no timestamps); its sha256 is the committed file's.

    python3 build_manifest.py --fetch          download what the cache lacks, hash, write traces/manifest.json
    python3 build_manifest.py --verify         re-hash the cache against the committed manifest (exit 1 on mismatch)
    python3 build_manifest.py --summary        per source: traces, hours, per-second kbit/s p5 / p50 / p95
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
import subprocess
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import parsers as P  # noqa: E402

MANIFEST = HERE / "manifest.json"
SCHEMA = "lvdc_trace_manifest_v1"
UA = "Mozilla/5.0 (lvdc trace manifest)"

HSDPA = {
    "id": "hsdpa_3g_norway",
    "citation": "H. Riiser, P. Vigmostad, C. Griwodz, P. Halvorsen. Commute Path Bandwidth Traces from 3G Networks: "
                "Analysis and Applications. ACM MMSys 2013. doi:10.1145/2483977.2483991",
    "url": "https://datasets.simula.no/downloads/hsdpa-tcp-logs/",
    "mirror": "https://skulddata.cs.umass.edu/traces/mmsys/2013/pathbandwidth/",
    "routes": ["bus.ljansbakken-oslo", "car.aarnes-elverum", "car.oslo-grimstad", "car.snaroya-smestad",
               "ferry.nesoddtangen-oslo", "metro.kalbakken-jernbanetorget", "train.oslo-vestby", "train.vestby-oslo",
               "tram.jernbanetorget-ljabru", "tram.jernbanetorget-universitetssykehuset", "tram.ljabru-jernbanetorget"],
    "format": "whitespace: unix s, monotonic ms, lat, lon, bytes since the previous row, ms since the previous row "
              "(~1 s rows); per-second kbit/s by spreading each row's bytes over its interval",
    "parser": "parse_riiser",
    "rtt": False,
}
GHENT = {
    "id": "lte_4g_ghent",
    "citation": "J. van der Hooft, S. Petrangeli, T. Wauters, R. Huysegems, P. Rondao Alface, T. Bostoen, F. De Turck. "
                "HTTP/2-Based Adaptive Streaming of HEVC Video over 4G/LTE Networks. IEEE Communications Letters "
                "20(11):2177-2180, 2016",
    "url": "https://users.ugent.be/~jvdrhoof/dataset-4g/logs/",
    "format": "whitespace: epoch ms, ms since start, lat, lon, bytes since the previous row, ms since the previous row "
              "(~1 s rows); per-second kbit/s as for hsdpa",
    "parser": "parse_ghent",
    "rtt": False,
}
FCC_MIN_VALUES = 120          # >= 600 s at 5 s per value: the shortest session (§4, 10 min)
FCC_SAMPLE = 200
FCC_SEED = 0
FCC = {
    "id": "fcc_mba_webget",
    "citation": "FCC Measuring Broadband America, raw data release June 2016 (data-raw-2016-jun.tar.gz, raw unaudited). "
                "Series construction after H. Mao, R. Netravali, M. Alizadeh. Neural Adaptive Video Streaming with "
                "Pensieve. ACM SIGCOMM 2017 (github.com/hongzimao/pensieve traces/fcc/load_webget_data.py)",
    "url": "https://data.fcc.gov/download/measuring-broadband-america/2017/data-raw-2016-jun.tar.gz",
    "listing": "https://www.fcc.gov/reports-research/reports/measuring-broadband-america/raw-data-measuring-broadband-america-2016",
    "format": "curr_webget CSV: unit_id, dtime, target, address, fetch_time, bytes_total, bytes_sec, ...: one row per "
              "web page fetch (about hourly per unit and target)",
    "parser": "fcc_webget_series + fcc_per_second",
    "rtt": False,
    "subset_rule": {"table": "curr_webget", "series": "one per (unit_id, target), its rows sorted by dtime",
                    "value": f"bytes_sec (column {P.FCC_BYTES_SEC_COL}); rows with no numeric bytes_sec dropped",
                    "hold_s": P.FCC_HOLD_S, "min_values": FCC_MIN_VALUES, "sample": FCC_SAMPLE, "seed": FCC_SEED,
                    "draw": "random.Random(seed).sample(sorted(keys with >= min_values), min(sample, n)), sorted",
                    "caveat": "consecutive values are fetches about an hour apart, each held 5 s (Pensieve's "
                              "construction): a composed series, not a measured per-second trace"},
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _get(url: str) -> bytes:
    """curl (the system's CA store; a python.org build may have none): the content is pinned by sha256 anyway."""
    return subprocess.run(["curl", "-fsSL", "-A", UA, url], check=True, capture_output=True, timeout=600).stdout


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    subprocess.run(["curl", "-fsSL", "-A", UA, "-o", str(part), url], check=True, timeout=7200)
    part.rename(dest)


def _listing(url: str, pattern: str) -> list[str]:
    """The file names an Apache index lists (href matching pattern), sorted."""
    return sorted(set(re.findall(rf'href="({pattern})"', _get(url).decode("utf-8", "replace"))))


def _file_rec(cache: Path, rel: str, parse) -> dict:
    p = cache / rel
    rec = parse(p.read_text(errors="replace"))
    return {"name": rel, "bytes": p.stat().st_size, "sha256": sha256_file(p), "rows": rec["rows"],
            "seconds": len(rec["kbps"])}


def fetch_logs(cache: Path, src: dict, names: list[str]) -> None:
    for rel in names:
        dest = cache / src["id"] / rel
        if not dest.exists():
            _download(src["url"] + rel, dest)


def hsdpa_names() -> list[str]:
    return [f"{r}/{n}" for r in HSDPA["routes"] for n in _listing(HSDPA["url"] + r + "/", r"report[^\"/]*\.log")]


def ghent_names() -> list[str]:
    return _listing(GHENT["url"], r"report_[^\"/]*\.log")


def _webget_member(tf: tarfile.TarFile):
    for m in tf:
        if m.isfile() and re.search(r"(^|/)curr_webget[^/]*\.csv$", m.name):
            return m
    raise SystemExit("no curr_webget*.csv in the archive")


def _member_lines(tf: tarfile.TarFile, m, h=None):
    fh = tf.extractfile(m)
    for raw in iter(fh.readline, b""):
        if h is not None:
            h.update(raw)
        yield raw.decode("utf-8", "replace")


def fcc_build(cache: Path) -> dict:
    """Two streaming passes over the archive's curr_webget: (1) the per-series counts and the member's sha256, (2) the
    chosen series' values; each derived per-second series is written to the cache and its sha256 pinned."""
    arc = cache / FCC["id"] / "data-raw-2016-jun.tar.gz"
    h = hashlib.sha256()
    with tarfile.open(arc, "r|gz") as tf:
        m = _webget_member(tf)
        counts = P.fcc_webget_counts(_member_lines(tf, m, h))
        member = {"name": m.name, "bytes": m.size, "sha256": h.hexdigest()}
    cand = sorted(k for k, n in counts.items() if n >= FCC_MIN_VALUES)
    chosen = sorted(random.Random(FCC_SEED).sample(cand, min(FCC_SAMPLE, len(cand))))
    with tarfile.open(arc, "r|gz") as tf:
        m = _webget_member(tf)
        series = P.fcc_webget_series(_member_lines(tf, m), keys=set(chosen))
    out = cache / FCC["id"] / "series"
    out.mkdir(parents=True, exist_ok=True)
    recs = []
    for k in chosen:
        rec = P.fcc_per_second(series[k])
        name = "trace_" + re.sub(r"[^A-Za-z0-9._-]", "-", f"{k[0]}_{k[1]}") + ".txt"
        text = P.series_text(rec["kbps"])
        (out / name).write_text(text)
        recs.append({"name": f"series/{name}", "unit_id": k[0], "target": k[1], "rows": rec["rows"],
                     "seconds": len(rec["kbps"]), "sha256": hashlib.sha256(text.encode()).hexdigest()})
    return {"archive": {"name": arc.name, "bytes": arc.stat().st_size, "sha256": sha256_file(arc)}, "member": member,
            "series_total": len(counts), "series_eligible": len(cand), "files": recs}


def build(cache: Path, fetch: bool) -> dict:
    srcs = {}
    for src, names_fn, parse in ((HSDPA, hsdpa_names, P.parse_riiser), (GHENT, ghent_names, P.parse_ghent)):
        root = cache / src["id"]
        if fetch:
            names = names_fn()
            fetch_logs(cache, src, names)
        else:
            names = sorted(str(p.relative_to(root)) for p in root.rglob("*.log"))
        srcs[src["id"]] = {**{k: v for k, v in src.items() if k != "id"},
                           "files": [_file_rec(root, n, parse) for n in names]}
    arc = cache / FCC["id"] / "data-raw-2016-jun.tar.gz"
    if fetch and not arc.exists():
        _download(FCC["url"], arc)
    srcs[FCC["id"]] = {**{k: v for k, v in FCC.items() if k != "id"}, **fcc_build(cache)}
    return {"schema": SCHEMA, "prereg": "LVQoE docs/analysis/design/restart_2026-10-21_prereg_levi.md §9",
            "parser_version": P.PARSER_VERSION, "weights": "equal by source", "sources": srcs}


def dumps(man: dict) -> str:
    return json.dumps(man, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def verify(cache: Path, man: dict, series_only: bool = False) -> list[str]:
    """Every manifest file's sha256 in the cache; series_only (chaos: the derived series and logs are copied there, the
    FCC archive never is, rushi 11:45) skips the archives."""
    bad = []
    for sid, s in man["sources"].items():
        for f in s["files"]:
            p = cache / sid / f["name"]
            if not p.exists():
                bad.append(f"{sid}/{f['name']}: missing")
            elif sha256_file(p) != f["sha256"]:
                bad.append(f"{sid}/{f['name']}: sha256 differs")
        if "archive" in s and not series_only:
            p = cache / sid / s["archive"]["name"]
            if not p.exists() or sha256_file(p) != s["archive"]["sha256"]:
                bad.append(f"{sid}/{s['archive']['name']}: missing or sha256 differs")
    return bad


def load_series(cache: Path, sid: str, f: dict) -> list[float]:
    """A manifest entry's per-second kbit/s series from the cache."""
    p = cache / sid / f["name"]
    if sid == FCC["id"]:
        return [float(x) for x in p.read_text().split()]
    return (P.parse_riiser if sid == HSDPA["id"] else P.parse_ghent)(p.read_text(errors="replace"))["kbps"]


def _pct(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))] if s else float("nan")


def summary(cache: Path, man: dict) -> dict:
    out = {}
    for sid, s in sorted(man["sources"].items()):
        allv, secs = [], 0
        for f in s["files"]:
            v = load_series(cache, sid, f)
            allv += v
            secs += len(v)
        out[sid] = {"traces": len(s["files"]), "hours": round(secs / 3600.0, 2),
                    "kbps_p5_p50_p95": [round(_pct(allv, q), 1) for q in (0.05, 0.5, 0.95)],
                    "trace_seconds_min_max": [min(f["seconds"] for f in s["files"]),
                                              max(f["seconds"] for f in s["files"])]}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cache", type=Path, default=Path(os.environ.get("LVDC_TRACE_CACHE")
                                                        or Path.home() / "Desktop" / "LV_DC_traces_cache"))
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--fetch", action="store_true", help="download what the cache lacks, then build the manifest")
    g.add_argument("--build", action="store_true", help="build the manifest from the cache only")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--summary", action="store_true")
    ap.add_argument("--series-only", action="store_true", help="--verify: the series and logs only, no archive (chaos)")
    a = ap.parse_args(argv)
    if a.fetch or a.build:
        text = dumps(build(a.cache, a.fetch))
        MANIFEST.write_text(text)
        print(f"wrote {MANIFEST} sha256 {hashlib.sha256(text.encode()).hexdigest()}")
        return 0
    man = json.loads(MANIFEST.read_text())
    if a.verify:
        bad = verify(a.cache, man, a.series_only)
        print("\n".join(bad) if bad else f"verified: manifest sha256 {sha256_file(MANIFEST)}")
        return 1 if bad else 0
    print(json.dumps(summary(a.cache, man), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
