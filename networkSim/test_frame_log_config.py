#!/usr/bin/env python3
"""t91 (levi 17:4x, boundary 3): browser.frame_log_config, the per-frame rVFC log's switch (LVQOE_FRAME_LOG):
  - unset / "0": None (no extension config, no meta key: the session as without it);
  - "1" on, "alt<N>" OFF / ON blocks of N s from t0; anything else recorded as unparsed, off;
  - levi 18:11: a counted run or a session under a marked record root takes "1" only (recorded record: true); alt<N>
    there, the instruments off or an unparsed value are refused (recorded, off);
  - browser.py writes the extension config only when enabled, and exp.py the meta key only when asked.

    python3 test_frame_log_config.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    import websocket  # noqa: F401
except ImportError:
    sys.modules["websocket"] = types.ModuleType("websocket")
import browser as B  # noqa: E402
import data_root  # noqa: E402


def test_frame_log_config():
    with tempfile.TemporaryDirectory() as t:
        dry = Path(t) / "dry" / "s1"
        rec = Path(t) / "rec" / "_trio" / "s1"
        dry.mkdir(parents=True)
        rec.mkdir(parents=True)
        (Path(t) / "rec" / data_root.RECORD_MARKER).write_text(json.dumps({"uuid": "x"}))
        cfg = lambda raw, d=dry, rec_=False, ins=True: B.frame_log_config(raw, d, rec_, ins)
        assert cfg(None) is None and cfg("") is None and cfg("0") is None
        assert cfg("1") == {"requested": "1", "enabled": True, "block_s": 0, "t0": 0, "clock": "epoch"}
        assert cfg(" alt60 ") == {"requested": "alt60", "enabled": True, "block_s": 60, "t0": 0, "clock": "epoch"}
        for bad in ("alt0", "alt", "on", "2"):
            assert cfg(bad) == {"requested": bad, "enabled": False, "reason": "unparsed (1 or alt<N>)"}, bad
        on_rec = {"requested": "1", "enabled": True, "record": True, "block_s": 0, "t0": 0, "clock": "epoch"}
        assert cfg("1", rec_=True) == on_rec and cfg("1", d=rec) == on_rec      # the counted mode: "1" only
        assert cfg("alt60", rec_=True) == {"requested": "alt60", "enabled": False,
                                           "reason": "of record: alt<N> is dry only"}
        assert cfg("alt60", d=rec) == {"requested": "alt60", "enabled": False,
                                       "reason": "under a record root: alt<N> is dry only"}
        assert cfg("alt60", d=rec, rec_=True)["reason"] == "of record: alt<N> is dry only"
        assert cfg("1", ins=False) == {"requested": "1", "enabled": False, "reason": "instruments off"}
        assert cfg("1", d=rec, rec_=True, ins=False) == {"requested": "1", "enabled": False, "record": True,
                                                         "reason": "instruments off"}
        assert cfg("x", rec_=True)["reason"] == "unparsed (1 or alt<N>)"
        assert cfg("1", d=None)["enabled"]


def test_frame_log_wiring():
    src = (HERE / "browser.py").read_text()
    assert "frame_log_config(os.environ.get(FRAME_LOG_ENV), session_dir," in src
    assert 'if self.frame_log and self.frame_log["enabled"] else {}' in src     # the extension config only when on
    assert "if browser.frame_log:" in (HERE / "exp.py").read_text()             # the meta key only when asked


def main() -> int:
    test_frame_log_config()
    test_frame_log_wiring()
    print("test_frame_log_config: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
