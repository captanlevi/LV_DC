"""tw_latency.py: the twitch viewer-side low-latency setting, drawn per session (restart §8, levi's prereg 2026-10-21).

The setting is drawn per session (seeded, P_ON / 1 - P_ON) by orchestrate (the trio) and applied before playback
starts: browser.py registers set_js for every new twitch document (CDP Page.addScriptToEvaluateOnNewDocument) before
the first navigation, so the player reads the drawn value when it initialises. The script runs only while the Page
domain is enabled on the registering connection (laptop: without Page.enable the set never ran; Page.disable after
registering dropped it), so apply enables it and read_back disables it again after removing the script: the collector's
page connection is back to its usual state (Page never enabled) from then on. Twitch keeps the setting in the page's
localStorage, key KEY ("true" / "false"; absent = low latency on, the player's default); the player's Advanced menu
toggle "Low Latency" writes the same key (laptop probe 2026-10-20, a throwaway headless Chrome, logged out: the toggle
turned off wrote KEY = "false" and the Video Stats row Latency Mode read "Normal Latency"; the set before load gave
"Low Latency" / "Normal Latency" in both directions, from a fresh profile too).

Read-back, once, after the player plays and the Video Stats overlay is on (browser.py turns it on for page_stats.js):
the overlay's Latency Mode row (the player's own report), KEY, and the overlay's Buffer Size and Latency To Broadcaster
(the player exposes no buffer target; these are the observed buffer and latency at the read). Then the hook is removed
(start-only: no later document is touched; the value stays in the profile's localStorage). A failed set or an
unreadable read is recorded (ok false / None) and the session continues in its actual state: never an abort, so
membership never selects on the toggle's success. No in-stall or mid-session interaction: no menu click, no reload.
Per tick, page_stats.js logs every overlay row (Latency Mode included) to player_stats.jsonl, so an unread start (e.g.
an ad on screen at the read) is recoverable from the session's own record.
"""
from __future__ import annotations

import json
import random
import time

KEY = "lowLatencyModeEnabled"
P_ON = 0.5
RULE = ("restart §8: per session, seeded, low latency on with p 0.5; set before playback (localStorage "
        f"{KEY}, a new-document script), read back once from the Video Stats overlay; a failed set is recorded, "
        "never aborted")
MODE_ON, MODE_OFF = "Low Latency", "Normal Latency"     # the overlay's Latency Mode values
READ_TRIES, READ_GAP_S = 6, 1.5                          # the read-back waits up to ~8 s for the Latency Mode row


def draw(seed: str) -> dict:
    """The session's setting from its seed (orchestrate's per-session seed, salted with this draw's name)."""
    on = random.Random(f"tw_low_latency:{seed}").random() < P_ON
    return {"rule": RULE, "seed": seed, "p_on": P_ON, "drawn": "on" if on else "off"}


def set_js(on: bool) -> str:
    """The new-document script: the drawn value into twitch's localStorage before the page's own scripts run."""
    return ("(() => { try { if (/(^|\\.)twitch\\.tv$/.test(location.hostname)) "
            f"localStorage.setItem({json.dumps(KEY)}, {json.dumps('true' if on else 'false')}); }} catch (e) {{}} }})();")


READ_JS = """
(function() {
    const vs = {};
    document.querySelectorAll('[data-a-target="player-overlay-video-stats-row"]').forEach((r) => {
        const c = r.querySelectorAll('td');
        if (c.length >= 2) vs[(c[0].innerText || '').trim()] = (c[1].innerText || '').trim();
    });
    let ls = null;
    try { ls = localStorage.getItem(%s); } catch (e) { ls = 'error: ' + e; }
    return JSON.stringify({mode: vs['Latency Mode'] || null, buffer: vs['Buffer Size'] || null,
                           latency: vs['Latency To Broadcaster'] || null, local_storage: ls, rows: Object.keys(vs).length});
})()
""" % json.dumps(KEY)


def _secs(v) -> float | None:
    """'2.75 sec.' -> 2.75; None when absent or unparsable."""
    try:
        return float(str(v).split()[0])
    except (TypeError, ValueError, IndexError):
        return None


def verdict(drawn: str, read: dict | None) -> bool | None:
    """True: the player reports the drawn mode; False: it reports the other one; None: no Latency Mode row was read."""
    mode = (read or {}).get("mode")
    if mode not in (MODE_ON, MODE_OFF):
        return None
    return mode == (MODE_ON if drawn == "on" else MODE_OFF)


def apply(page, drawn: str) -> dict:
    """Before the first navigation: register the set for every new document (the Page domain on). Never raises."""
    rec: dict = {"drawn": drawn, "t_applied": time.time()}
    try:
        page.call("Page.enable")
        res = page.call("Page.addScriptToEvaluateOnNewDocument", {"source": set_js(drawn == "on")}) or {}
        rec["hook"] = res.get("identifier")
        rec["applied"] = bool(res.get("identifier"))
    except Exception as exc:
        rec.update(applied=False, error=repr(exc)[:200])
    return rec


def read_back(page, rec: dict, tries: int = READ_TRIES, gap_s: float = READ_GAP_S, sleep=time.sleep) -> dict:
    """Once the player plays and the overlay is on: read the player's mode (up to `tries` reads until the Latency Mode
    row appears), then remove the hook and disable the Page domain again. Returns rec updated (read_back, ok, buffer_s,
    latency_s, hook_removed); never raises, never aborts."""
    read = None
    for i in range(tries):
        try:
            read = json.loads(page.eval(READ_JS) or "null")
        except Exception as exc:
            read = {"error": repr(exc)[:200]}
        if (read or {}).get("mode"):
            break
        if i < tries - 1:
            sleep(gap_s)
    rec = {**rec, "read_back": read, "t_read": time.time(), "ok": verdict(rec.get("drawn"), read),
           "local_storage_ok": (read or {}).get("local_storage") == ("true" if rec.get("drawn") == "on" else "false"),
           "buffer_s": _secs((read or {}).get("buffer")), "latency_s": _secs((read or {}).get("latency"))}
    if rec.get("hook"):
        try:
            page.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": rec["hook"]})
            page.call("Page.disable")
            rec["hook_removed"] = True
        except Exception as exc:
            rec["hook_removed"] = f"error: {exc!r}"[:200]
    return rec
