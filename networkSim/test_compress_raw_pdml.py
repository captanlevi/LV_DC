#!/usr/bin/env python3
"""ops/compress_raw_pdml.py: a session's raw PDML is compressed in place only once http_v2.json exists and verify
PASSed; the .zst decompresses to the same sha256 (recorded) and only then does the plain file go; ineligible sessions
are left untouched."""
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("compress_raw_pdml", HERE.parent / "ops" / "compress_raw_pdml.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="no zstd")


def _sess(root: Path, name: str, verify="PASS", http=True) -> Path:
    sd = root / name
    sd.mkdir()
    (sd / "h2_raw.pdml").write_bytes(b"<pdml>" + b"x" * 5000 + b"</pdml>")
    (sd / "h3_raw.pdml").write_bytes(b"<pdml></pdml>")
    if http:
        (sd / "http_v2.json").write_text("{}")
    if verify:
        (sd / "verify.json").write_text(json.dumps({"verdict": verify}))
    return sd


def test_compress_in_place_verified():
    with tempfile.TemporaryDirectory() as d:
        sd = _sess(Path(d), "tw_1")
        orig = (sd / "h2_raw.pdml").read_bytes()
        res = C.compress_session(sd)
        assert [r["file"] for r in res] == ["h2_raw.pdml", "h3_raw.pdml"] and all("error" not in r for r in res), res
        assert not (sd / "h2_raw.pdml").exists() and (sd / "h2_raw.pdml.zst").exists()
        assert subprocess.run(["zstd", "-dc", str(sd / "h2_raw.pdml.zst")], capture_output=True).stdout == orig
        recs = json.loads((sd / "compress_record.json").read_text())
        assert recs[0]["bytes"] == len(orig) and recs[0]["zst_bytes"] < len(orig) and len(recs[0]["sha256"]) == 64
        assert recs[0]["roundtrip_sha256"] == recs[0]["sha256"] and len(recs) == 2
        assert C.compress_session(sd) == []                         # nothing left to do; the .zst stays


def test_failed_roundtrip_keeps_the_original(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        sd = _sess(Path(d), "tw_2")
        monkeypatch.setattr(C, "_sha_decompressed", lambda z: "0" * 64)          # a round trip that does not match
        r = C.compress_session(sd)
        assert all("error" in x for x in r) and (sd / "h2_raw.pdml").exists() and not (sd / "h2_raw.pdml.zst").exists(), r
        assert not (sd / "compress_record.json").exists()


def test_ineligible_sessions_untouched():
    with tempfile.TemporaryDirectory() as d:
        for name, kw, why in (("a", {"http": False}, "no http_v2.json"), ("b", {"verify": "FAIL"}, "verify not PASS"),
                              ("c", {"verify": None}, "no verify.json")):
            sd = _sess(Path(d), name, **kw)
            r = C.compress_session(sd)
            assert r[0]["skipped"].startswith(why) and (sd / "h2_raw.pdml").exists(), r
        assert C.compress_session(Path(d) / "b", verify_pass=False)[0]["file"] == "h2_raw.pdml"
