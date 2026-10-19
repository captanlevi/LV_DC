#!/usr/bin/env python3
"""unit_extractor's file mode reads a zstd-compressed PDML (ops compresses h2/h3_raw.pdml in place after the derived
outputs exist): parse_pdml_zst gives what parse_pdml_http gives on the plain file; a corrupt .zst raises."""
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
