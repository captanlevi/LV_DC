"""The file names the collector writes into a session dir, kept in ONE place (brad / levi 23:10: A1's rVFC stream and
LVQoE's frame extractor both wrote <session>/frames.jsonl, a collision found before any v3 counted window ran).

- SIDE_STREAM_FILES: content.js's side-stream buffer key -> its session file (browser.py writes through it). The A1
  rVFC stream keeps its buffer key "frames" and is written as rvfc_frames.jsonl: frames.jsonl is LVQoE's extractor's.
- COLLECTOR_FILES: every name (fnmatch pattern) the collector writes into a session dir; test_session_files checks the
  collector's code names nothing outside it and that it is disjoint from READER_FILES.
- READER_FILES: the names LVQoE's readers / extractors WRITE into a session dir (brad 23:21, from LVQoE code; brad
  keeps the reader side's authoritative list). One writer per name (levi 23:21): SHARED_BY_DESIGN is empty on both
  sides, so any name both sides can write is a collision.
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
    # capture and its post-processing (Makefile process_http_logs -> unit_extractor.py: the collector's own; LVQoE only
    # reads them). The collector's network_core run writes merged*.csv under check_nc/ only (NESTED_FILES); the
    # top-level merged*.csv are LVQoE's (ingest.ensure_csvs; brad 23:21)
    "discovery_empty.png", "chrome_stderr.log",      # levi 07:48: the empty list page; the slot's Chrome stderr
    "merged.pcap", "early.pcap", "dns_default.pcap", "dns_default.err", "default_tcp_*.pcap", "tcpdump_*.err",
    "ssl_keys.log", "http_v2.json", "http_v2_error.json", "http.json", "h1_raw.json", "h2_raw.pdml", "h3_raw.pdml", "ping_*.log",
    # the compressed PDMLs (brad 23:29; ops/compress_raw_pdml.py is on the gate2-* line only, not v3): unit_extractor
    # reads a .pdml.zst; the names stay reserved collector-side
    "h2_raw.pdml.zst", "h3_raw.pdml.zst", "compress_record.json",
    # an unmapped side stream's fallback file (flagged in meta side_streams_unmapped; never expected)
    "side_*.jsonl",
    # session subdirs the collector owns
    "shots", "check_nc",
})

# files inside the collector's own subdirs (the literal scan sees their basenames)
NESTED_FILES: frozenset[str] = frozenset({"shots/index.jsonl", "check_nc/mergedFlows.csv", "check_nc/mergedPackets.csv"})

READER_FILES: frozenset[str] = frozenset({
    # brad 23:21 (LVQoE brad/session-files-list 5dfc0e41, READER_OUTPUTS / READER_PATTERNS, from code): names LVQoE writes
    # into <session>/ top level
    "verify.json", "verify.log", "frames.jsonl", "frames_summary.json", "frames.log",
    "mergedFlows.csv", "mergedPackets.csv", "mergedFlows.csv.dns_obs.csv", "mergedFlows.csv.diagnostics.json",
    "motion_gt.jsonl", "motion_gt_summary.json", "linear.pcap",
    "selection_gen*", "selection_*.json", "fld_*.npz", "burst_*.npz", "requet_*.npz", "vicrypt*_*.npz", "bufgru*",
    "*_buffer_est*", "*_fps_delivered*", "motion_gt*",
    "a5tt*", "a6tt*",                              # reserved for the tt readers (levi 23:08 / 23:10)
})

LVQOE_PIN = "18d19654"   # levi 17:31: LVQoE's watcher/lv/session_files.py at the land of brad's list (eee07d36); re-pin
                         # when either list changes
# levi 23:21: one writer per name; LVQoE declares the same (empty) set and the two must be equal
SHARED_BY_DESIGN: frozenset[str] = frozenset()

def collides(a: str, b: str) -> bool:
    """Two names / patterns can name the same file."""
    return a == b or fnmatch(a, b) or fnmatch(b, a)


def collisions(collector=COLLECTOR_FILES, reader=READER_FILES, shared=SHARED_BY_DESIGN) -> list[tuple[str, str]]:
    """(collector name, reader name) pairs that can name the same file, outside the shared-by-design products."""
    return sorted((c, r) for c in collector for r in reader if collides(c, r) and not (c == r and c in shared))


def side_file(stream: str) -> tuple[str, bool]:
    """(the session file of a side stream, mapped?). An unmapped stream is written as side_<stream>.jsonl and flagged
    (levi 23:21: never lose records, never collide); the export resolves every name before it clears storage."""
    if stream in SIDE_STREAM_FILES:
        return SIDE_STREAM_FILES[stream], True
    safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in str(stream))[:64] or "unnamed"
    name = f"side_{safe}.jsonl"
    if any(collides(name, r) for r in READER_FILES):      # brad / levi 17:36: never a reader-owned name (e.g. a stream
        import hashlib                                    # "buffer_est" would match *_buffer_est*): a hashed name
        name = f"side_unmapped_{hashlib.sha256(str(stream).encode()).hexdigest()[:12]}.jsonl"
        if any(collides(name, r) for r in READER_FILES):
            raise RuntimeError(f"side stream {stream!r}: no non-reader file name")   # unreachable with today's lists
    return name, False
