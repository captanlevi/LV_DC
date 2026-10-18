#!/usr/bin/env python3
"""meta.chrome (tw_43, shagun 2026-10-19: the end read was refused once and check_session failed the whole session on
chrome.browser): the version is read once the browser attached (chrome_start, 3 tries); at finish the end read gets
CHROME_END_TRIES attempts and, when Chrome no longer answers, the start read stands with the end error beside it.

    python3 test_chrome_version.py        (exit 0 = pass)
"""
import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_meta as S  # noqa: E402

V = {"Browser": "Chrome/141.0", "User-Agent": "UA", "Protocol-Version": "1.3"}


def _urlopen(plan):
    calls = []

    def f(url, timeout=None):
        calls.append(url)
        r = plan[min(len(calls), len(plan)) - 1]
        if r == "refused":
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return io.BytesIO(json.dumps(V).encode())
    return f, calls


def main() -> int:
    real_open, real_sleep = S.urllib.request.urlopen, S.time.sleep
    S.time.sleep = lambda s: None
    try:
        with tempfile.TemporaryDirectory() as d:
            sd = Path(d)
            S.urllib.request.urlopen, calls = _urlopen(["refused", "ok"])
            v = S.chrome_version(tries=3)
            assert v["browser"] == "Chrome/141.0" and v["tries"] == 2 and len(calls) == 2, v
            S.write(sd, chrome_start=v)
            S.urllib.request.urlopen, calls = _urlopen(["refused"])           # tw_43: Chrome gone at finish
            c = S.chrome_at_end(sd)
            assert c["browser"] == "Chrome/141.0" and c["source"].startswith("chrome_start"), c
            assert "Connection refused" in c["end_error"] and c["end_tries"] == S.CHROME_END_TRIES == len(calls), c
            S.urllib.request.urlopen, _ = _urlopen(["ok"])                    # the usual case: the end read itself
            assert S.chrome_at_end(sd) == {"browser": "Chrome/141.0", "user_agent": "UA", "protocol": "1.3"}
        with tempfile.TemporaryDirectory() as d:                             # no start read either: the error stays
            S.urllib.request.urlopen, _ = _urlopen(["refused"])
            c = S.chrome_at_end(Path(d))
            assert c["browser"] is None and "refused" in c["error"], c
    finally:
        S.urllib.request.urlopen, S.time.sleep = real_open, real_sleep
    print("test_chrome_version: ok")
    return 0


def test_chrome_version():
    assert main() == 0


if __name__ == "__main__":
    sys.exit(main())
