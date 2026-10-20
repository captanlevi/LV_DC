#!/usr/bin/env python3
"""The trace regime's parsers and manifest builder (restart prereg §9, traces/): synthetic fixtures only (no trace data
in git). Riiser 3G and Ghent 4G logs to per-second kbit/s (bytes spread over each row's interval), the FCC curr_webget
series (per (unit_id, target), time order, 5 s holds, failed rows dropped), the FCC subset draw, the manifest's
determinism and the verify pass.

    python3 test_traces.py        (exit 0 = pass)
"""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "traces"))
import parsers as P  # noqa: E402
import build_manifest as B  # noqa: E402

RIISER = """1289406399 549692 59.851754 10.781778 248069 1008
1289406400 550772 59.851864 10.781833 191698 1080
1289406401 551773 59.851964 10.781901 280579 1001
1289406402 552893 59.85206 10.781969 248971 1120
garbage line
"""
GHENT = """1453121790686 39287 51.0386528885778 3.73220785642186 2493100 1000
1453121791687 40288 51.0386532820580 3.73222847166054 2763504 1001
1453121792686 41287 51.0386553301824 3.73225142858955 2728896 0999
1453121793686 42287 51.0386567711171 3.73227435543338 1954600 1000
"""


def test_interval_spreading():
    # two 1000 ms rows of 1000 and 3000 bytes: 8 and 24 kbit/s
    assert P.per_second_from_intervals([(1000.0, 1000.0, 1000.0), (2000.0, 3000.0, 1000.0)]) == [8.0, 24.0]
    # one 2000 ms row of 2000 bytes spreads 1000 bytes into each second
    assert P.per_second_from_intervals([(2000.0, 2000.0, 2000.0)]) == [8.0, 8.0]
    # a 500 ms row straddling: bytes split by time; a trailing partial second is dropped
    v = P.per_second_from_intervals([(1000.0, 1000.0, 1000.0), (1500.0, 500.0, 500.0), (2500.0, 1000.0, 1000.0)])
    assert v == [8.0, 8.0], v                   # [0,1000): 1000 B; [1000,2000): 500 + 500 B; [2000,2500) dropped
    # a gap between rows reads 0 there
    assert P.per_second_from_intervals([(1000.0, 1000.0, 1000.0), (3000.0, 1000.0, 1000.0)]) == [8.0, 0.0, 8.0]
    assert P.per_second_from_intervals([]) == []


def test_reverse_order_and_a_garbled_clock():
    """The timeline is column 6's running sum in time order: a log in reverse time order reads as its forward twin,
    and a garbled column 2 changes nothing."""
    fwd = P.parse_riiser(RIISER)["kbps"]
    rev = "\n".join(reversed([ln for ln in RIISER.splitlines() if len(ln.split()) == 6])) + "\n"
    assert P.parse_riiser(rev)["kbps"] == fwd
    garbled = "\n".join(" ".join(f[:1] + ["019948"] + f[2:]) for f in (ln.split() for ln in RIISER.splitlines())
                         if len(f) == 6) + "\n"
    assert P.parse_riiser(garbled)["kbps"] == fwd


def test_riiser_and_ghent():
    r = P.parse_riiser(RIISER)
    assert r["rows"] == 4 and r["rtt_ms"] is None and len(r["kbps"]) == 4
    tot_bytes = 248069 + 191698 + 280579 + 248971
    span_ms = 1008 + 1080 + 1001 + 1120
    assert abs(sum(r["kbps"]) * 1000 / 8 - tot_bytes * (len(r["kbps"]) * 1000.0) / span_ms) < tot_bytes * 0.08
    assert all(1300 < x < 2300 for x in r["kbps"]), r["kbps"]         # ~200-280 kB/s
    g = P.parse_ghent(GHENT)
    assert g["rows"] == 4 and len(g["kbps"]) == 4 and g["rtt_ms"] is None
    assert abs(g["kbps"][0] - 2493100 * 8 / 1000) < 1.0 and abs(g["kbps"][1] - 2763504 * 8 / 1000 * 1000 / 1001) < 30


def _webget_csv():
    head = "unit_id,dtime,target,address,fetch_time,bytes_total,bytes_sec,objects\n"
    rows = [("7", "2016-06-01 02:00:00", "http://a", "1.1.1.1", "900", "9000", "10000", "3"),
            ("7", "2016-06-01 01:00:00", "http://a", "1.1.1.1", "900", "9000", "5000", "3"),     # earlier: first
            ("7", "2016-06-01 03:00:00", "http://a", "1.1.1.1", "0", "0", "", "0"),              # failed: dropped
            ("7", "2016-06-01 01:30:00", "http://b", "2.2.2.2", "900", "9000", "2500", "3"),
            ("8", "2016-06-01 01:00:00", "http://a", "1.1.1.1", "900", "9000", "1000", "3")]
    return head + "".join(",".join(r) + "\n" for r in rows)


def test_fcc_series():
    s = P.parse_fcc_csv_text(_webget_csv())
    assert s == {("7", "http://a"): [5000.0, 10000.0], ("7", "http://b"): [2500.0], ("8", "http://a"): [1000.0]}
    assert P.fcc_webget_counts(_webget_csv().splitlines(True)) == {("7", "http://a"): 2, ("7", "http://b"): 1,
                                                                   ("8", "http://a"): 1}
    assert P.fcc_webget_series(_webget_csv().splitlines(True), keys={("8", "http://a")}) == {("8", "http://a"): [1000.0]}
    rec = P.fcc_per_second([5000.0, 10000.0])
    assert rec["kbps"] == [40.0] * 5 + [80.0] * 5 and rec["rows"] == 2 and rec["rtt_ms"] is None
    assert P.series_text([40.0, 80.125]) == "40.000\n80.125\n"


def _fake_cache(root: Path, n_units: int = 30):
    """A cache with two hsdpa logs, one ghent log and an FCC-shaped archive (curr_webget in a tar.gz)."""
    import io
    import tarfile
    for rel in ("hsdpa_3g_norway/bus.x/report.a.log", "hsdpa_3g_norway/car.y/report.b.log"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(RIISER)
    (root / "lte_4g_ghent").mkdir(parents=True)
    (root / "lte_4g_ghent/report_car_0001.log").write_text(GHENT)
    lines = ["unit_id,dtime,target,address,fetch_time,bytes_total,bytes_sec\n"]
    for u in range(n_units):
        for i in range(B.FCC_MIN_VALUES + (u % 3) - 1):         # u % 3 == 0: one short of the minimum
            lines.append(f"{u},2016-06-{1 + i // 24:02d} {i % 24:02d}:00:00,http://t,1.1.1.1,1,1,{1000 * (u + 1) + i}\n")
    data = "".join(lines).encode()
    (root / "fcc_mba_webget").mkdir(parents=True)
    with tarfile.open(root / "fcc_mba_webget/data-raw-2016-jun.tar.gz", "w:gz") as tf:
        ti = tarfile.TarInfo("201606/curr_webget.csv")
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
    return data


def test_manifest_build_is_deterministic_and_verifies():
    saved = B.FCC_SAMPLE
    try:
        B.FCC_SAMPLE = 5
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            data = _fake_cache(root)
            m1, m2 = B.dumps(B.build(root, fetch=False)), B.dumps(B.build(root, fetch=False))
            assert m1 == m2                                     # deterministic: no timestamps, sorted
            man = json.loads(m1)
            assert set(man["sources"]) == {"hsdpa_3g_norway", "lte_4g_ghent", "fcc_mba_webget"}
            hs = man["sources"]["hsdpa_3g_norway"]
            assert [f["name"] for f in hs["files"]] == ["bus.x/report.a.log", "car.y/report.b.log"]
            assert hs["files"][0]["sha256"] == hashlib.sha256(RIISER.encode()).hexdigest() and hs["files"][0]["rows"] == 4
            fcc = man["sources"]["fcc_mba_webget"]
            assert fcc["member"] == {"name": "201606/curr_webget.csv", "bytes": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest()}
            assert fcc["series_total"] == 30 and fcc["series_eligible"] == 20 and len(fcc["files"]) == 5
            units = [f["unit_id"] for f in fcc["files"]]
            assert all(int(u) % 3 != 0 for u in units) and units == sorted(units)
            cand = sorted((str(u), "http://t") for u in range(30) if u % 3 != 0)
            import random
            assert [(f["unit_id"], f["target"]) for f in fcc["files"]] == sorted(random.Random(B.FCC_SEED).sample(cand, 5))
            f0 = fcc["files"][0]
            assert f0["seconds"] == f0["rows"] * 5
            assert B.load_series(root, "fcc_mba_webget", f0)[:5] == [(1000 * (int(f0["unit_id"]) + 1)) * 8 / 1000] * 5
            assert B.verify(root, man) == []
            (root / "lte_4g_ghent/report_car_0001.log").write_text(GHENT + "1 2 3 4 5 6\n")
            assert B.verify(root, man) == ["lte_4g_ghent/report_car_0001.log: sha256 differs"]
            sm = B.summary(root, man)
            assert sm["fcc_mba_webget"]["traces"] == 5 and sm["hsdpa_3g_norway"]["traces"] == 2
    finally:
        B.FCC_SAMPLE = saved


def test_committed_manifest_if_present():
    """The committed manifest: its schema, the three sources, the FCC rule's constants as in the code."""
    p = HERE / "traces" / "manifest.json"
    if not p.exists():
        return
    man = json.loads(p.read_text())
    assert man["schema"] == B.SCHEMA and set(man["sources"]) == {"hsdpa_3g_norway", "lte_4g_ghent", "fcc_mba_webget"}
    r = man["sources"]["fcc_mba_webget"]["subset_rule"]
    assert (r["min_values"], r["sample"], r["seed"], r["hold_s"]) == (B.FCC_MIN_VALUES, B.FCC_SAMPLE, B.FCC_SEED, P.FCC_HOLD_S)
    assert man["parser_version"] == P.PARSER_VERSION
    assert B.dumps(man) == p.read_text()                         # the canonical form, byte for byte


def main() -> int:
    test_interval_spreading()
    test_reverse_order_and_a_garbled_clock()
    test_riiser_and_ghent()
    test_fcc_series()
    test_manifest_build_is_deterministic_and_verifies()
    test_committed_manifest_if_present()
    print("test_traces: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
