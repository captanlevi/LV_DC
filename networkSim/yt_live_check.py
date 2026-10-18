#!/usr/bin/env python3
"""yt_live_check.py: the logged live check for a spare's promotion (prereg Addendum P). Probes each handle's /live page
(yt_channels.yt_live_state: live / not_live / unknown), appends one row per handle to channel_checks/yt_live_checks.jsonl
(commit it), and prints each handle's promotion status (yt_channels.promotion_status).

    python3 yt_live_check.py @BeanBoltTV        (run >= 6 h apart; commit the log after each run)
    python3 yt_live_check.py --needed --leaves <data>/_summaries/yt_list_leaves.jsonl
                                                (the spares of every type left with < 2 owners)

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
    ap.add_argument("handles", nargs="*")
    ap.add_argument("--needed", action="store_true", help="also every spare needing promotion (Addendum P)")
    ap.add_argument("--leaves", type=Path, help="the collector's yt_list_leaves.jsonl (with --needed)")
    ap.add_argument("--log", type=Path, default=Y.LIVE_CHECKS_LOG)
    a = ap.parse_args()
    if a.needed:
        leaves = ([json.loads(l) for l in a.leaves.read_text().splitlines() if l.strip()]
                  if a.leaves and a.leaves.exists() else [])
        a.handles = list(dict.fromkeys(a.handles + Y.spares_needing_promotion(leaves)))
    if not a.handles:
        print("no handle to check")
        return 0
    a.log.parent.mkdir(parents=True, exist_ok=True)
    for h in a.handles:
        errors: list = []
        probe = Y.yt_live_probe(h, errors=errors)
        row = {"ts": time.time(), "handle": h, "state": probe["state"], "top_height": probe["top_height"],
               "host": socket.gethostname(), "url": Y.url_of(h)}
        if errors:
            row["error"] = errors[0]
        with open(a.log, "a") as fh:
            fh.write(json.dumps(row) + "\n")
        rows = [json.loads(l) for l in a.log.read_text().splitlines() if l.strip()]
        st = Y.promotion_status(rows, h)
        print(f"{h}: {row['state']} (top {row['top_height']}p) | promotion {'PASSED' if st['passed'] else 'not yet'} "
              f"({len(st['checks'])} live checks >= {Y.PROMOTE_MIN_GAP_S // 3600} h apart, "
              f"span {(st['checks'][-1] - st['checks'][0]) / 3600 if st['checks'] else 0:.1f} h of {Y.PROMOTE_SPAN_S // 3600} h)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
