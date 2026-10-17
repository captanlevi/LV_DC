#!/usr/bin/env python3
"""yt_live_check.py: the logged live check for a spare's promotion (prereg Addendum P). Probes each handle's /live page
(yt_channels.yt_live_state: live / not_live / unknown), appends one row per handle to channel_checks/yt_live_checks.jsonl
(commit it), and prints each handle's promotion status (yt_channels.promotion_status).

    python3 yt_live_check.py @BeanBoltTV        (run >= 6 h apart; commit the log after each run)

An unknown row (e.g. a Python without a CA bundle: SSL_CERT_FILE=$(python -m certifi)) is logged with its error and
ignored by the promotion rule.
"""
import argparse
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yt_channels as Y  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("handles", nargs="+")
    ap.add_argument("--log", type=Path, default=Y.LIVE_CHECKS_LOG)
    a = ap.parse_args()
    a.log.parent.mkdir(parents=True, exist_ok=True)
    for h in a.handles:
        errors: list = []
        row = {"ts": time.time(), "handle": h, "state": Y.yt_live_state(h, errors=errors), "host": socket.gethostname(),
               "url": Y.url_of(h)}
        if errors:
            row["error"] = errors[0]
        with open(a.log, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        rows = [json.loads(l) for l in a.log.read_text().splitlines() if l.strip()]
        st = Y.promotion_status(rows, h)
        print(f"{h}: {row['state']} | promotion {'PASSED' if st['passed'] else 'not yet'} "
              f"({len(st['checks'])} live checks >= {Y.PROMOTE_MIN_GAP_S // 3600} h apart, "
              f"span {(st['checks'][-1] - st['checks'][0]) / 3600 if st['checks'] else 0:.1f} h of {Y.PROMOTE_SPAN_S // 3600} h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
