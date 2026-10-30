#!/usr/bin/env python3
"""t102 #3 (levi 00140 / 00141 / 00254): no decrypting HTTP pass on chaos.

- exp.py: CHAOS_HTTP_LOGS False skips make_http_logs / process_http_logs and tt_stream, and writes meta.http_logs =
  check_session.HTTP_LOGS_DEFERRED; True keeps 7809b21c's calls (source pins);
- check_session: a deferred session's join_order is a named deferral (never a violation, never "absent or
  unreadable"), http_v2_missing is not raised, check.json carries deferred_to_halo4; a session without the marker reads
  http_v2 as before (an absent http_v2 stays "absent or unreadable");
- the field-only tshark reads stay (dns_answers): no keylog, no decode.

    python3 test_http_deferred.py        (exit 0 = pass)
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_session as C  # noqa: E402

T0 = 1_800_000_000.0
NET = [{"timestamp": T0 + 30, "apply_start": T0 + 30}]


def _meta(deferred: bool) -> dict:
    m = {"started_at": T0, "join": {"discovery": {"t_end": T0 + 20, "kind": "list"}}}
    return {**m, "http_logs": C.HTTP_LOGS_DEFERRED} if deferred else m


def test_join_order_deferred(tmp_path: Path):
    tmp = tmp_path
    jo = C.join_order(tmp, _meta(True), NET)
    assert jo == {"discovery_end": T0 + 20, "first_apply": T0 + 30, "http_v2": "deferred to halo4 (t102 #3)"}
    assert C.join_order_violation(jo) is None
    # a deferred session ignores a stale http_v2 that would violate: halo4's own http_v2 decides, not a leftover
    (tmp / "http_v2.json").write_text(json.dumps({"k": [{"request_url": "https://x/videoplayback?a", "request_start": T0 + 25}]}))
    assert C.join_order_violation(C.join_order(tmp, _meta(True), NET)) is None
    # without the marker: as before (the same file is a violation; no file is "absent or unreadable")
    assert C.join_order_violation(C.join_order(tmp, _meta(False), NET))
    (tmp / "http_v2.json").unlink()
    assert C.join_order(tmp, _meta(False), NET)["http_v2"] == "absent or unreadable"


def test_markers():
    assert C.http_logs_deferred({"http_logs": C.HTTP_LOGS_DEFERRED})
    assert not C.http_logs_deferred({}) and not C.http_logs_deferred({"http_logs": {"deferred": "chaos"}})
    assert C.HTTP_LOGS_DEFERRED["skipped"] == ["make_http_logs", "process_http_logs", "tt_stream"]
    assert {"http_v2", "join_order", "tt_stream"} <= set(C.DEFERRED_TO_HALO4)


def test_check_session_source():
    src = (HERE / "check_session.py").read_text()
    assert "h2m = None if deferred else http_v2_missing(sd, lab)" in src
    assert 'out["deferred_to_halo4"] = DEFERRED_TO_HALO4' in src


def test_exp_gating():
    src = (HERE / "exp.py").read_text()
    assert "CHAOS_HTTP_LOGS = False" in src and "from check_session import HTTP_LOGS_DEFERRED" in src
    i = src.index("        if CHAOS_HTTP_LOGS:\n            http_ok = make_http_logs(session_dir)")
    j = src.index("session_meta.write(session_dir, http_logs=HTTP_LOGS_DEFERRED)")
    assert i < j < i + 600                                                        # the else of the same block
    assert 'if platform == "tiktok" and CHAOS_HTTP_LOGS:' in src                    # tt_stream gated with it
    assert src.count("make_http_logs(session_dir)") == 1 and src.count("process_http_logs(session_dir)") == 1
    assert "trio_slots.dns_answers_for(" in src                                     # the field-only read stays


def main() -> int:
    test_markers()
    test_check_session_source()
    test_exp_gating()
    with tempfile.TemporaryDirectory() as t:
        test_join_order_deferred(Path(t))
    print("test_http_deferred: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
