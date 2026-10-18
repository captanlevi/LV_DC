"""entry_quality.py: the entry-quality exception (prereg Addendum Q amendment 1, the only exception to "no quality
forcing"; acceptance: Q am. 10, after peter's probe on chaos 2026-10-18). yt / twitch only (tt has no quality button).
Behind exp.py / orchestrate.py --entry-quality, default off.

At stream entry, unshaped, once: wait for content playback with no ad signal (content.js data-lvqoe-ad, at most
AD_WAIT_S, else no pick: reason "ad"); read the player's own quality menu; a ladder of fewer than MIN_OPTIONS resolution
options is a no-op (menu_available true, selected null, outside the rotation); else pick the entry tier by the rule
below, hold it until the <video> plays at that height (at most PICK_HOLD_MAX_S), restore Auto, and verify the restore
from state, never from the click (yt: the player API's preferred quality "auto", Auto checked in the menu, localStorage
yt-player-quality.quality 0; twitch: the Auto radio checked, localStorage video-quality.default "auto"; any miss ->
auto_restored false, which check_session QA-flags). After Auto, the <video> size and (yt) the API's playback quality
are sampled every second for SETTLE_RECORD_S, so t_settle can be derived (Q am. 10). A re-entry (Q am. 6) makes no new
pick: it re-reads the state on the new page and, if it is not Auto (a failed restore persists in the profile: twitch
fully, yt as the start quality), restores and verifies it, recorded in the re-entry.

The entry-tier rule (ENTRY_RULE, deterministic, on the same coverage snapshot as the shaper matrix, Q am. 1): of the
offered resolution options, the one whose tier class (coverage_report.quality_class on its height) has the largest
deficit to the class floor; ties to the higher option; no deficit -> the top option (so the top tier most often).
"""
from __future__ import annotations

import json
import re
import time

import coverage_report as CR

ENTRY_QUALITY_PLATFORMS = ("youtube", "twitch")
ENTRY_RULE = "entry-tier-v1 (Q am. 1 / am. 10): largest class deficit among the offered tiers, ties higher, else top"
AD_WAIT_S = 60.0          # the pick waits this long for content playback with no ad signal (Q am. 10)
PICK_HOLD_MAX_S = 15.0    # the pick is held until the <video> plays at its height, at most this long
SETTLE_RECORD_S = 120.0   # <video> size / yt API quality sampled each second after Auto (t_settle, Q am. 10)
MIN_OPTIONS = 2           # fewer resolution options: no-op, outside the rotation (Q am. 10)
MENU_STEP_S = 0.8         # the menus render between clicks
YT_QUALITY_HEIGHT = {"tiny": 144, "small": 240, "medium": 360, "large": 480, "hd720": 720, "hd1080": 1080,
                     "hd1440": 1440, "hd2160": 2160, "highres": 4320}


def option_height(label: str | None) -> int | None:
    """A menu label's resolution height ("1080p HD", "1440p60\\n2k\\n(Source)", "480p30") -> 1080 / 1440 / 480;
    Auto and anything else -> None."""
    m = re.match(r"\s*(\d{3,4})p", label or "")
    return int(m.group(1)) if m else None


def choose_tier(snapshot: dict | None, options: list[str]) -> dict:
    """ENTRY_RULE on the offered labels: {target_label, target_height, target_class, deficit, why, rule}; target None
    when fewer than MIN_OPTIONS resolution options are offered."""
    offered = sorted({(h, lab) for lab in options if (h := option_height(lab))}, reverse=True)
    heights = {}
    for h, lab in offered:                      # one label per height (the first, e.g. 720p60 before 720p)
        heights.setdefault(h, lab)
    rec = {"rule": ENTRY_RULE, "offered_heights": sorted(heights, reverse=True)}
    if len(heights) < MIN_OPTIONS:
        return {**rec, "target_label": None, "target_height": None, "target_class": None, "deficit": None,
                "why": f"ladder of {len(heights)} resolution option(s) < {MIN_OPTIONS}: no-op"}
    classes = (snapshot or {}).get("classes") or {}

    def deficit(h: int) -> float:
        c = CR.quality_class(h, h)
        v = classes.get(c)
        return float(CR.FLOOR_SESSIONS - v["sessions_ge_min"]) if v and v["sessions_ge_min"] < CR.FLOOR_SESSIONS else 0.0
    best = max(heights, key=lambda h: (deficit(h), h))
    why = "largest class deficit" if deficit(best) > 0 else "no deficit among the offered tiers: top"
    return {**rec, "target_label": heights[best], "target_height": best, "target_class": CR.quality_class(best, best),
            "deficit": deficit(best), "why": why}


def _ls_json(store: dict, key: str) -> dict | None:
    raw = (store or {}).get(key)
    try:
        v = json.loads(raw) if raw else None
        if isinstance(v, dict) and isinstance(v.get("data"), str):      # yt's {data: "<json>", expiration, creation}
            v = json.loads(v["data"])
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


def verify_auto(platform: str, state: dict | None) -> tuple[bool, dict]:
    """auto_restored from state (Q am. 10): every listed check must hold; the per-check record is returned."""
    st = state or {}
    items = st.get("items") or []
    menu = any((i.get("label") or "").lower().startswith("auto") and i.get("checked") in (True, "true") for i in items)
    if platform == "youtube":
        q = _ls_json(st.get("store"), "yt-player-quality")
        checks = {"menu_auto_checked": menu, "api_preferred_auto": (st.get("api") or {}).get("preferred") == "auto",
                  "ls_quality_0": q is not None and q.get("quality") == 0}
    elif platform == "twitch":
        q = _ls_json(st.get("store"), "video-quality")
        checks = {"menu_auto_checked": menu, "ls_default_auto": q is not None and q.get("default") == "auto"}
    else:
        return False, {"error": f"no entry quality on {platform}"}
    return all(checks.values()), checks


def t_settle(platform: str, trace: list[dict], t_auto: float | None, picked_height: int | None = None) -> float | None:
    """Seconds from t_auto until Auto's choice plays. yt: the API's playback quality first moves off the picked tier
    (Auto has chosen; until then the API still reports the pick), then the first sample whose <video> short side equals
    that target; twitch: the first <video> height change. None when no change within SETTLE_RECORD_S (Q am. 10: those
    sessions are counted on their own), including Auto choosing the picked tier itself."""
    if t_auto is None:
        return None
    pts = [p for p in trace if p.get("t") is not None and t_auto <= p["t"] <= t_auto + SETTLE_RECORD_S
           and p.get("w") and p.get("h")]
    if not pts:
        return None
    if platform == "youtube":
        moved = False
        for p in pts:
            want = YT_QUALITY_HEIGHT.get(p.get("q"))
            moved = moved or (want is not None and want != picked_height)
            if moved and want and min(p["w"], p["h"]) == want:
                return round(p["t"] - t_auto, 1)
        return None
    h0 = min(pts[0]["w"], pts[0]["h"])
    return next((round(p["t"] - t_auto, 1) for p in pts if min(p["w"], p["h"]) != h0), None)


# ------------------------------------------------------------------------------------------------ page JS (the probe's)
VIDEO_JS = """(() => { const vs = [...document.querySelectorAll('video')];
  const v = vs.filter(x => x.readyState > 0).sort((a, b) => (b.videoWidth * b.videoHeight) - (a.videoWidth * a.videoHeight))[0] || vs[0];
  return v ? {ct: v.currentTime, paused: v.paused, rs: v.readyState, w: v.videoWidth, h: v.videoHeight,
              ad: document.documentElement.dataset.lvqoeAd || null} : null; })()"""
STORE_JS = """(() => { const o = {}; for (const k of ['yt-player-quality', 'video-quality']) {
  const v = localStorage.getItem(k); if (v !== null) o[k] = v; } return o; })()"""
YT_API_JS = """(() => { const mp = document.getElementById('movie_player'); if (!mp || !mp.getPlaybackQuality) return null;
  const f = (n) => { try { return mp[n] ? mp[n]() : null; } catch (e) { return null; } };
  return {quality: f('getPlaybackQuality'), preferred: f('getPreferredQuality'), levels: f('getAvailableQualityLevels')}; })()"""
_YT_PANEL = "[...document.querySelectorAll('#movie_player .ytp-settings-menu .ytp-panel')].pop()"
OPEN_JS = {
    "youtube": """(() => { const mp = document.getElementById('movie_player'); mp && mp.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
      const b = document.querySelector('#movie_player .ytp-settings-button'); if (!b) return 'no settings button';
      const m = document.querySelector('#movie_player .ytp-settings-menu');
      if (!m || m.offsetParent === null) b.click();
      return 'ok'; })()""",
    "twitch": """(() => { const v = document.querySelector('video'); v && v.dispatchEvent(new MouseEvent('mousemove', {bubbles: true}));
      const b = [...document.querySelectorAll('button[data-a-target="player-settings-button"]')].find(e => e.offsetParent !== null)
                || document.querySelector('button[data-a-target="player-settings-button"]');
      if (!b) return 'no settings button';
      if (!document.querySelector('[data-a-target="player-settings-menu"]')) b.click();
      return 'ok'; })()""",
}
SUBMENU_JS = {   # into the quality list
    "youtube": "(() => { const p = " + _YT_PANEL + """; if (!p) return null;
      const it = [...p.querySelectorAll('.ytp-menuitem')].find(i => /^quality/i.test((i.querySelector('.ytp-menuitem-label') || i).innerText.trim()));
      if (!it) return null; const c = (it.querySelector('.ytp-menuitem-content') || {innerText: ''}).innerText.trim(); it.click(); return c; })()""",
    "twitch": """(() => { const q = document.querySelector('[data-a-target="player-settings-menu-item-quality"]'); if (!q) return null;
      const t = q.innerText.trim(); q.click(); return t; })()""",
}
ITEMS_JS = {
    "youtube": "(() => { const p = " + _YT_PANEL + """; if (!p) return [];
      return [...p.querySelectorAll('.ytp-menuitem')].map(i => ({label: (i.querySelector('.ytp-menuitem-label') || i).innerText.trim(),
        checked: i.getAttribute('aria-checked')})); })()""",
    "twitch": """(() => [...document.querySelectorAll('[data-a-target="player-settings-submenu-quality-option"]')].map(o => {
      const i = o.querySelector('input'); return {label: o.innerText.trim(), checked: i ? i.checked : null}; }))()""",
}
CLICK_JS = {    # (label regex) -> the clicked label or null
    "youtube": "((re) => { const p = " + _YT_PANEL + """; if (!p) return null;
      const it = [...p.querySelectorAll('.ytp-menuitem')].find(i => new RegExp(re, 'i').test((i.querySelector('.ytp-menuitem-label') || i).innerText.trim()));
      if (!it) return null; it.click(); return (it.querySelector('.ytp-menuitem-label') || it).innerText.trim(); })""",
    "twitch": """((re) => { const o = [...document.querySelectorAll('[data-a-target="player-settings-submenu-quality-option"]')]
      .find(o => new RegExp(re, 'i').test(o.innerText.trim())); if (!o) return null;
      (o.querySelector('input') || o).click(); return o.innerText.trim(); })""",
}
CLOSE_JS = {
    "youtube": """(() => { const m = document.querySelector('#movie_player .ytp-settings-menu');
      if (m && m.offsetParent !== null) document.querySelector('#movie_player .ytp-settings-button').click(); return 'ok'; })()""",
    "twitch": """(() => { if (document.querySelector('[data-a-target="player-settings-menu"]')) {
      const b = document.querySelector('button[data-a-target="player-settings-button"]'); b && b.click(); } return 'ok'; })()""",
}


class Driver:
    """The menu actions on one page (CDPPage-like: .eval(js)); `sleep` is injectable for tests."""

    def __init__(self, page, platform: str, sleep=time.sleep, clock=time.time):
        self.page, self.platform, self.sleep, self.clock = page, platform, sleep, clock

    def _menu(self) -> tuple[str | None, list[dict]]:
        self.page.eval(OPEN_JS[self.platform])
        self.sleep(MENU_STEP_S)
        row = self.page.eval(SUBMENU_JS[self.platform])
        self.sleep(MENU_STEP_S)
        return row, self.page.eval(ITEMS_JS[self.platform]) or []

    def state(self) -> dict:
        """The menu (its Quality row and options, then closed), localStorage and (yt) the player API."""
        row, items = self._menu()
        self.page.eval(CLOSE_JS[self.platform])
        return {"row": row, "items": items, "store": self.page.eval(STORE_JS) or {},
                "api": self.page.eval(YT_API_JS) if self.platform == "youtube" else None}

    def click(self, label_re: str) -> str | None:
        self._menu()
        got = self.page.eval(f"({CLICK_JS[self.platform]})({json.dumps(label_re)})")
        self.sleep(MENU_STEP_S)
        self.page.eval(CLOSE_JS[self.platform])
        return got

    def video(self) -> dict:
        return self.page.eval(VIDEO_JS) or {}

    def wait_content(self, timeout: float = AD_WAIT_S) -> bool:
        """Content playback with no ad signal: the playhead advancing over two samples while content.js reads no ad."""
        t0, last = self.clock(), None
        while self.clock() - t0 < timeout:
            v = self.video()
            if v.get("ad") == "0" and (v.get("rs") or 0) >= 3 and not v.get("paused"):
                if last is not None and (v.get("ct") or 0) > last + 0.3:
                    return True
                last = v.get("ct") or 0
            else:
                last = None
            self.sleep(1.0)
        return False

    def restore_auto(self) -> tuple[bool, dict, int]:
        """Auto clicked, then verified from state; one more click if the first did not verify. (ok, checks, clicks)"""
        for n in (1, 2):
            self.click("^auto")
            self.sleep(MENU_STEP_S)
            ok, checks = verify_auto(self.platform, self.state())
            if ok:
                return True, checks, n
        return False, checks, 2


def entry_pick(drv: Driver, snapshot: dict | None, t_entry: float | None = None) -> dict:
    """The once-per-session entry pick (module docstring); the record for meta.json entry_quality. The wait for content
    with no ad signal ends AD_WAIT_S after the stream entry `t_entry` (Q am. 10: "within 60 s of entry"; the call itself
    when unknown)."""
    rec: dict = {"rule": ENTRY_RULE, "t": drv.clock(), "t_entry": t_entry, "selected": None, "menu_available": None,
                 "auto_restored": None}
    left = AD_WAIT_S - (drv.clock() - t_entry) if t_entry is not None else AD_WAIT_S
    if left <= 0 or not drv.wait_content(left):
        return {**rec, "reason": "ad", "t_wait_s": round(drv.clock() - rec["t"], 1)}
    rec["t_content"] = drv.clock()
    before = drv.state()
    rec["entry_auto_state"] = {"row": before.get("row"), "auto_checked": verify_auto(drv.platform, before)[1].get("menu_auto_checked")}
    rec["options"] = [i.get("label") for i in before.get("items") or []]
    rec["menu_available"] = bool(rec["options"])
    if not rec["menu_available"]:
        return {**rec, "reason": "no menu"}
    choice = choose_tier(snapshot, rec["options"])
    rec["choice"] = choice
    if choice["target_label"] is None:
        return {**rec, "reason": choice["why"]}
    rec["t_pick"] = drv.clock()
    token = choice["target_label"].split("\n")[0].split(" ")[0]        # "1080p60" of "1080p60\n(Source)", "1080p" of "1080p HD"
    rec["selected"] = drv.click("^" + re.escape(token) + "(?![0-9])")
    rec["t_effect"] = None
    while drv.clock() - rec["t_pick"] < PICK_HOLD_MAX_S:
        v = drv.video()
        if v.get("w") and v.get("h") and min(v["w"], v["h"]) == choice["target_height"]:
            rec["t_effect"] = drv.clock()
            break
        drv.sleep(1.0)
    rec["t_auto"] = drv.clock()
    ok, checks, clicks = drv.restore_auto()
    rec.update(auto_restored=ok, verify=checks, auto_clicks=clicks, t_verified=drv.clock())
    return rec


def settle_trace(drv: Driver, stop=lambda: False) -> list[dict]:
    """The <video> size and (yt) the API's playback quality every second for SETTLE_RECORD_S."""
    out, t0 = [], drv.clock()
    while drv.clock() - t0 < SETTLE_RECORD_S and not stop():
        v = drv.video()
        q = (drv.page.eval(YT_API_JS) or {}).get("quality") if drv.platform == "youtube" else None
        out.append({"t": round(drv.clock(), 2), "w": v.get("w"), "h": v.get("h"), "q": q})
        drv.sleep(1.0)
    return out


def reentry_check(drv: Driver) -> dict:
    """A re-entry (Q am. 6): no new pick; the state is re-read and, if it is not Auto, restored and verified."""
    ok, checks = verify_auto(drv.platform, drv.state())
    if ok:
        return {"was_auto": True, "auto_restored": True, "verify": checks, "t": drv.clock()}
    ok2, checks2, clicks = drv.restore_auto()
    return {"was_auto": False, "was": checks, "auto_restored": ok2, "verify": checks2, "auto_clicks": clicks,
            "t": drv.clock()}
