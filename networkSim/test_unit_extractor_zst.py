#!/usr/bin/env python3
"""unit_extractor's file mode reads a zstd-compressed PDML (ops compresses h2/h3_raw.pdml in place after the derived
outputs exist): parse_pdml_zst gives what parse_pdml_http gives on the plain file; a corrupt .zst raises; and (levi on
c1f9181) end to end, unit_extractor's saved output from fixtures/pdml_h2_synthetic.pdml (two HTTP/2 streams: request /
response headers, data frames) and from its .zst are byte-identical."""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
pytest.importorskip("network_core")
pytestmark = pytest.mark.skipif(shutil.which("zstd") is None, reason="no zstd")

PDML = b'<?xml version="1.0"?>\n<pdml version="0" creator="wireshark">\n</pdml>\n'


def test_zst_equals_plain():
    import unit_extractor as U
    from network_core.http.httpExtract.pdml_parser import parse_pdml_http
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "h2_raw.pdml"
        p.write_bytes(PDML)
        subprocess.run(["zstd", "-q", str(p), "-o", str(p) + ".zst"], check=True)
        assert U.parse_pdml_zst(str(p) + ".zst", 2) == parse_pdml_http(str(p), http_version=2)
        bad = Path(d) / "h3_raw.pdml.zst"
        bad.write_bytes(b"not zstd")
        with pytest.raises(Exception):
            U.parse_pdml_zst(str(bad), 3)


def test_unit_extractor_output_byte_identical_from_zst():
    import unit_extractor as U  # noqa: F401  (network_core present)
    fx = HERE / "fixtures" / "pdml_h2_synthetic.pdml"
    outs = []
    with tempfile.TemporaryDirectory() as d:
        for mode in ("plain", "zst"):
            sd = Path(d) / mode
            sd.mkdir()
            if mode == "plain":
                shutil.copy(fx, sd / "h2_raw.pdml")
            else:
                subprocess.run(["zstd", "-q", str(fx), "-o", str(sd / "h2_raw.pdml.zst")], check=True)
            out = sd / "http_v2.json"
            r = subprocess.run([sys.executable, str(HERE / "unit_extractor.py"), "--path", str(sd), "--save_path", str(out)],
                               capture_output=True, text=True)
            assert r.returncode == 0 and out.exists(), r.stderr[-400:]
            outs.append(out.read_bytes())
        assert outs[0] == outs[1] and len(outs[0]) > 100 and b"/v1/segment/seg1.ts" in outs[0], outs[0][:300]
