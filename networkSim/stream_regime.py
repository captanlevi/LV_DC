"""stream_regime.py: per session, what the platform offered and played (ladder, codec, player size), and the suspicious
flags against the expected regime (supervisor 2026-10-18; rushi: a collection fault like tt's single tier is caught on
the first session, not after a day). check_session writes them to check.json stream_regime / suspicious and a check.md
row; orchestrate prints a FLAG line and appends to data/_summaries/flags.jsonl after each session. Flags are reported
only: they never change the verdict or the session class (prereg Addendum Q: no outcome-based exclusion).

Expected (EXPECTED):
  tiktok: played codec hevc, its ladder >= 3 rungs (Addendum Q amendment 2: tt tier floors count HEVC-ladder sessions);
  twitch: played codec h264 (the truth pipeline's TS path), master-playlist ladder >= 3 rungs;
  youtube: played codec av1 / vp9 / h264, offered ladder >= 3 rungs, and the player can reach the offered top.
    Reachable top = the smallest offered rung >= the player's device-px height. Basis (chaos, 2026-10-18 diagnosis):
    481- and 505-px players played 720p; 710-px players never played 1080p while it was on offer, at 6-9 Mb/s. That is
    a reading of 30 sessions, not a documented yt rule; the inputs are recorded beside the flag.
Flags: unexpected_codec, single_rung_ladder (1 rung), short_ladder (2 .. min - 1), ladder_unknown (no ladder captured),
  player_below_top (yt).
"""
from __future__ import annotations

import collections
import json
import re
from pathlib import Path

EXPECTED = {"tiktok": {"codecs": ["hevc"], "min_rungs": 3},
            "twitch": {"codecs": ["h264"], "min_rungs": 3},
            "youtube": {"codecs": ["av1", "h264", "vp9"], "min_rungs": 3, "player_reaches_top": True}}
STREAM_INF = re.compile(r"#EXT-X-STREAM-INF:([^\n]*)")


def codec_name(s: str | None) -> str:
    s = (s or "").strip().lower()
    for pre, name in (("avc1", "h264"), ("avc3", "h264"), ("h264", "h264"), ("hev1", "hevc"), ("hvc1", "hevc"),
                      ("hevc", "hevc"), ("h265", "hevc"), ("bytevc1", "hevc"), ("av01", "av1"), ("av1", "av1"),
                      ("vp09", "vp9"), ("vp9", "vp9")):
        if s.startswith(pre):
            return name
    return "unknown"


def _load(p: Path):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return None


def _bodies(http_v2: dict):
    for es in (http_v2 or {}).values():
        for e in es:
            rd = e.get("response_data")
            if isinstance(rd, str) and rd:
                yield e.get("request_url") or "", rd


def _stats(sd: Path) -> list[dict]:
    out = []
    p = Path(sd) / "player_stats.jsonl"
    if p.exists():
        with open(p) as fh:
            for line in fh:
                try:
                    out.append(json.loads(line).get("stats") or {})
                except ValueError:
                    pass
    return out


def _short(res: str | None) -> int | None:
    m = re.match(r"\s*(\d+)x(\d+)", res or "")
    return min(int(m.group(1)), int(m.group(2))) or None if m else None


def _mode(xs):
    xs = [x for x in xs if x is not None]
    return collections.Counter(xs).most_common(1)[0][0] if xs else None


def youtube(sd: Path, meta: dict, http_v2: dict) -> dict:
    offered = sorted({int(h) for _, b in _bodies(http_v2) for h in re.findall(r'"qualityLabel"\s*:\s*"(\d+)p', b)})
    st = [s.get("nerds") or {} for s in _stats(sd) if s.get("ad_state", -1) == -1]
    res = [str(n.get("resolution") or "").split(" / ") for n in st]
    played = [_short(r[0]) for r in res if r and r[0]]
    g, th = meta.get("geometry") or {}, (meta.get("player_pin") or {}).get("theater") or {}
    pc = g.get("player_client")
    player_h = th.get("player_device_px_h") or (round(pc[1] * (g.get("device_pixel_ratio") or 1)) if pc else None)
    reach = None
    if offered and player_h:
        reach = next((h for h in offered if h >= player_h), offered[-1])
    return {"played_codec": codec_name(_mode([str(n.get("codecs") or "").split(" / ")[0] for n in st])),
            "ladder": offered, "rungs": len(offered), "offered_top": offered[-1] if offered else None,
            "player_h_device_px": player_h, "reachable_top": reach, "played_top": max([p for p in played if p], default=None),
            "optimal_res_mode": _mode([r[1] for r in res if len(r) > 1 and _short(r[1])])}


def twitch(sd: Path, meta: dict, http_v2: dict) -> dict:
    ladder, codecs = set(), collections.Counter()
    for _, b in _bodies(http_v2):
        for attrs in STREAM_INF.findall(b):
            r = re.search(r"RESOLUTION=(\d+x\d+)", attrs)
            if r:
                ladder.add(_short(r.group(1)))
                c = re.search(r'CODECS="([^",]+)', attrs)
                codecs[codec_name(c.group(1) if c else None)] += 1
    played = _mode([str((s.get("twitch_video_stats") or {}).get("Codecs") or "").split(",")[0] or None for s in _stats(sd)])
    return {"played_codec": codec_name(played) if played else (_mode(list(codecs.elements())) or "unknown"),
            "played_codec_source": "video_stats" if played else "master_playlist",
            "ladder": sorted(ladder), "rungs": len(ladder), "ladder_codecs": dict(codecs)}


def tiktok(sd: Path, meta: dict, http_v2: dict | None) -> dict:
    ts = meta.get("tt_stream")
    if not ts:
        import tt_stream
        ts = tt_stream.regime(http_v2) if http_v2 is not None else {"codec": "unknown", "ladder": {}}
    codec = ts.get("codec") or "unknown"
    lad = (ts.get("ladder") or {}).get(codec) or []
    return {"played_codec": codec, "ladder": [r[0] for r in lad], "rungs": len(lad),
            "ladder_per_codec": {k: len(v) for k, v in (ts.get("ladder") or {}).items()},
            "codec_inferred_from_suffix": ts.get("codec_inferred_from_suffix")}


def suspicious(platform: str, reg: dict) -> list[dict]:
    exp = EXPECTED.get(platform)
    if not exp:
        return []
    out = []
    if reg["played_codec"] not in exp["codecs"]:
        out.append({"flag": "unexpected_codec", "played": reg["played_codec"], "expected": exp["codecs"]})
    n = reg["rungs"]
    if n == 0:
        out.append({"flag": "ladder_unknown"})
    elif n == 1:
        out.append({"flag": "single_rung_ladder", "ladder": reg["ladder"], "min_rungs": exp["min_rungs"]})
    elif n < exp["min_rungs"]:
        out.append({"flag": "short_ladder", "ladder": reg["ladder"], "min_rungs": exp["min_rungs"]})
    if exp.get("player_reaches_top") and reg.get("reachable_top") and reg["reachable_top"] < reg["offered_top"]:
        out.append({"flag": "player_below_top", "offered_top": reg["offered_top"], "reachable_top": reg["reachable_top"],
                    "player_h_device_px": reg["player_h_device_px"], "played_top": reg["played_top"]})
    return out


def session_regime(sd, platform: str, meta: dict | None = None) -> dict:
    """{regime, suspicious, expected} for the session (never raises: an error is recorded)."""
    sd = Path(sd)
    if platform not in EXPECTED:
        return {"regime": None, "suspicious": [], "expected": None}
    try:
        meta = meta if meta is not None else (_load(sd / "meta.json") or {})
        http_v2 = _load(sd / "http_v2.json")
        reg = {"youtube": youtube, "twitch": twitch, "tiktok": tiktok}[platform](sd, meta, http_v2 or {})
        reg["http_v2"] = http_v2 is not None
        return {"regime": reg, "suspicious": suspicious(platform, reg), "expected": EXPECTED[platform]}
    except Exception as e:  # a check never dies on this
        return {"regime": {"error": repr(e)[:200]}, "suspicious": [{"flag": "regime_error", "error": repr(e)[:200]}],
                "expected": EXPECTED[platform]}


def flag_keys(sus: list[dict]) -> list[str]:
    return [s["flag"] for s in sus or []]
