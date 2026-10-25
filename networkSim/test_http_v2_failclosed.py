#!/usr/bin/env python3
"""levi 06:29 (WC2 tw_100001: unit_extractor's catch-all wrote "{}" with rc 0, and check_session flagged nothing):
the extractor fails closed (rc 3, no http_v2.json, an error marker with the traceback) and check_session names
http_v2_missing (the marker, or a session that played with no http_v2 / no media request), never in the verdict."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as CS  # noqa: E402


def test_unit_extractor_fails_closed():
    pytest.importorskip("network_core")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "h1_raw.json").write_text("{ not json")              # parse_tshark_http raises inside the try
        r = subprocess.run([sys.executable, str(HERE / "unit_extractor.py"), "--path", str(d),
                            "--save_path", str(d / "http_v2.json")], capture_output=True, text=True)
        assert r.returncode == 3, (r.returncode, r.stdout[-400:], r.stderr[-400:])
        assert not (d / "http_v2.json").exists()
        m = json.loads((d / CS.HTTP_V2_ERROR).read_text())
        assert m["error"] and "Traceback" in m["traceback"] and m["save_path"] == str(d / "http_v2.json")
        (d / "http_v2.json").write_text('{"old": []}')                 # a re-run over an older output (brad 06:56)
        r = subprocess.run([sys.executable, str(HERE / "unit_extractor.py"), "--path", str(d),
                            "--save_path", str(d / "http_v2.json")], capture_output=True, text=True)
        assert r.returncode == 3 and not (d / "http_v2.json").exists()
        stale = list(d.glob("http_v2.json.stale_*"))
        assert len(stale) == 1 and stale[0].read_text() == '{"old": []}'
        (d / "h1_raw.json").unlink()
        (d / CS.HTTP_V2_ERROR).unlink()
        r = subprocess.run([sys.executable, str(HERE / "unit_extractor.py"), "--path", str(d),
                            "--save_path", str(d / "http_v2.json")], capture_output=True, text=True)
        assert r.returncode == 0 and (d / "http_v2.json").exists() and not (d / CS.HTTP_V2_ERROR).exists()


def test_check_session_names_http_v2_missing():
    played, idle = {"n_res_meta": 1500}, {"n_res_meta": 0}
    rec = {"request_url": "https://video-weaver.example.hls.ttvnw.net/v1/segment/Cx.ts?x=1"}
    with tempfile.TemporaryDirectory() as d:
        sd = Path(d)
        assert CS.http_v2_missing(sd, played) == "http_v2_missing: no http_v2.json on a session that played"
        assert CS.http_v2_missing(sd, idle) is None                     # a start failure: nothing expected
        (sd / "http_v2.json").write_text("{}")                          # WC2 tw_100001's file
        assert CS.http_v2_missing(sd, played).startswith("http_v2_missing: 0 flows, 0 media requests")
        assert CS.http_v2_missing(sd, idle) is None
        (sd / "http_v2.json").write_text(json.dumps({"a->b": [{"request_url": "https://x/manifest.m3u8"}]}))
        assert "1 flows, 0 media requests" in CS.http_v2_missing(sd, played)
        (sd / "http_v2.json").write_text(json.dumps({"a->b": [rec]}))
        assert CS.http_v2_missing(sd, played) is None
        (sd / "http_v2.json").write_text(json.dumps({"a->b": [{"request_url": None, "response_ct": "video/mp4"}]}))
        assert CS.http_v2_missing(sd, played) is None                    # an h3 record without its URL (migration)
        (sd / "http_v2.json").write_text("{ broken")
        assert CS.http_v2_missing(sd, played) == "http_v2_missing: http_v2.json unreadable (JSONDecodeError)"
        (sd / CS.HTTP_V2_ERROR).write_text(json.dumps({"error": "RuntimeError('tshark died')"}))
        assert CS.http_v2_missing(sd, idle).startswith("http_v2_missing: unit_extractor failed (RuntimeError")


def main() -> int:
    test_unit_extractor_fails_closed()
    test_check_session_names_http_v2_missing()
    print("test_http_v2_failclosed: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
