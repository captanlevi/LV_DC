"""The file names the collector writes into a session dir, kept in ONE place (brad / levi 23:10: A1's rVFC stream and
LVQoE's frame extractor both wrote <session>/frames.jsonl, a collision found before any v3 counted window ran).

- SIDE_STREAM_FILES: content.js's side-stream buffer key -> its session file (browser.py writes through it). The A1
  rVFC stream keeps its buffer key "frames" and is written as rvfc_frames.jsonl: frames.jsonl is LVQoE's extractor's.
- COLLECTOR_FILES: every name (fnmatch pattern) the collector writes into a session dir; test_session_files checks the
  collector's code names nothing outside it and that it is disjoint from READER_FILES.
- READER_FILES: the names LVQoE's readers / extractors WRITE into a session dir (brad 23:25, from LVQoE code; brad
  keeps the reader side's authoritative list). A name both sides write is a collision, except SHARED_BY_DESIGN: the
  pcap decode products the collector makes and LVQoE's ingest regenerates from the same pcap.
"""
from __future__ import annotations

from fnmatch import fnmatch

SIDE_STREAM_FILES: dict[str, str] = {
    "player_events": "player_events.jsonl",      # v3 A2
    "frames": "rvfc_frames.jsonl",               # v3 A1 (buffer key "frames"; the file is NOT frames.jsonl)
    "ad_edges": "ad_edges.jsonl",                # v3 A3
}

COLLECTOR_FILES: frozenset[str] = frozenset({
    # labels, shaper, meta, QA
    "labels.csv", "net_labels.txt", "meta.json", "check.json", "check.md", "player_stats.jsonl",
    *SIDE_STREAM_FILES.values(),
    # recorders
    "media_events.jsonl", "ad_actions.jsonl", "tc_stats.jsonl", "tc_fast.jsonl", "compute_load.jsonl",
    "box_record.jsonl", "tt_room_dom.jsonl", "motion.jsonl", "dns_answers.json",
    # capture and its post-processing (process_http_logs: the collector's own; LVQoE reads them)
    "merged.pcap", "early.pcap", "dns_default.pcap", "dns_default.err", "default_tcp_*.pcap", "tcpdump_*.err",
    "ssl_keys.log", "mergedPackets.csv", "mergedFlows.csv", "h2_raw.pdml", "h2_raw.pdml.zst", "http_v2.json",
    "http.json", "ping_*.log",
    # screenshots (a dir; its index)
    "shots", "index.jsonl",
})

READER_FILES: frozenset[str] = frozenset({
    # brad 23:25 (reader_session_outputs_2325.md, from LVQoE code at 7520a28c): names LVQoE writes into <session>/
    "verify.json", "verify.log", "frames.jsonl", "frames_summary.json", "frames.log",
    "mergedFlows.csv", "mergedPackets.csv", "mergedFlows.csv.dns_obs.csv", "mergedFlows.csv.diagnostics.json",
    "http_v2.json", "h1_raw.json", "h2_raw.pdml", "h3_raw.pdml", "h1_raw.json.zst", "h2_raw.pdml.zst", "h3_raw.pdml.zst",
    "motion_gt.jsonl", "motion_gt_summary.json", "linear.pcap",
    # patterns: P5 caches and reader artifacts
    "selection_gen*", "selection_*.json", "fld_*.npz", "burst_*.npz", "requet_*.npz", "vicrypt*_*.npz", "bufgru*",
    "*_buffer_est*", "*_fps_delivered*", "motion_gt*",
    # reserved for the tt readers (levi 23:08 / 23:10)
    "a5tt*", "a6tt*",
})

# Both sides write these BY DESIGN: the collector's post-session decode of its own pcap and LVQoE's ingest, which
# regenerates the same products from the same pcap. Any OTHER name both sides write is a collision.
SHARED_BY_DESIGN: frozenset[str] = frozenset({
    "mergedPackets.csv", "mergedFlows.csv", "http_v2.json", "h2_raw.pdml", "h2_raw.pdml.zst",
})

def collides(a: str, b: str) -> bool:
    """Two names / patterns can name the same file."""
    return a == b or fnmatch(a, b) or fnmatch(b, a)


def collisions(collector=COLLECTOR_FILES, reader=READER_FILES, shared=SHARED_BY_DESIGN) -> list[tuple[str, str]]:
    """(collector name, reader name) pairs that can name the same file, outside the shared-by-design products."""
    return sorted((c, r) for c in collector for r in reader if collides(c, r) and not (c == r and c in shared))


def side_file(stream: str) -> str:
    """The session file of a side stream; an unmapped stream raises (it must be named here first)."""
    if stream not in SIDE_STREAM_FILES:
        raise KeyError(f"side stream {stream!r} has no file in session_files.SIDE_STREAM_FILES")
    return SIDE_STREAM_FILES[stream]
