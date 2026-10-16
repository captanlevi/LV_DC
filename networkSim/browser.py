"""
browser.py — CDP-based browser automation for live stream navigation and ad skipping.

Chrome must already be running with --remote-debugging-port=9222 (handled by
the Makefile's start_chrome target).  This module:
  1. Waits for the CDP endpoint to be ready
  2. Navigates to a live stream for the requested platform
  3. Waits until the video element is actually playing
  4. Runs a background thread that clicks "skip ad" whenever one appears

Usage from exp.py:
    session = BrowserSession("youtube")
    session.start()          # blocks until video is playing (or raises)
    ...run oscillate()...
    session.stop()           # stops ad-watcher, leaves browser open for PCAP
"""

import json
import os
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

import websocket  # websocket-client

from yt_channels import YT_CHANNELS, url_of

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CDP_BASE = "http://localhost:9222"

# Ordered priority list — first URL that delivers a playing video wins.
LIVE_URLS: dict[str, list[str]] = {
    # v2: the tagged channel list (yt_channels.py, verified 2026-10-16); orchestrate.py's rotation passes one channel
    # per session (first_url), so this list is only the manual-mode fallback
    "youtube": [url_of(c["handle"]) for c in YT_CHANNELS],
    "twitch": [
        # Just Chatting always has hundreds of live streams — click first card
        "https://www.twitch.tv/directory/game/Just%20Chatting",
        "https://www.twitch.tv/directory/game/League%20of%20Legends",
        "https://www.twitch.tv/directory/game/Valorant",
    ],
    "tiktok": [
        # TikTok's live explore page — Python clicks the first live room
        "https://www.tiktok.com/live",
    ],
    "bilibili": [
        # Bilibili's live recommendation page
        "https://live.bilibili.com/",
    ],
}

# JS that is true once the explore page's stream cards have rendered (polled before _PICK_STREAM_JS)
_CARD_READY_JS: dict[str, str] = {
    "twitch": "!!(document.querySelector('a[data-a-target=\"preview-card-image-link\"]'))",
    "tiktok": "!!([...document.querySelectorAll('a[href*=\"/@\"][href*=\"/live\"]')].some(a => a.offsetParent !== null))",
}

# JS that clicks into the first live stream card (platform-specific post-navigation)
_PICK_STREAM_JS: dict[str, str] = {
    "youtube": "null",  # /live URL already lands on the stream
    "twitch": """
        (function() {
            const sels = [
                'a[data-a-target="preview-card-image-link"]',
                'a[data-a-target="preview-card-title-link"]',
                '.tw-card a[href^="/"]',
                'article a[href^="/"]',
            ];
            for (const s of sels) {
                const a = document.querySelector(s);
                if (a && !a.href.includes('/directory') && !a.href.includes('/videos/') && !a.href.includes('/clip/'))
                    { a.click(); return a.href; }
            }
            return null;
        })()
    """,
    "tiktok": """
        (function() {
            // TikTok live explore — pick the first room in the discover list (old sessions are on /@user/live room
            // pages, LVQoE t35 check 2026-10-16); the side-nav "suggested creators" links are the last resort
            const sels = [
                '[data-e2e="discover-list-live-card"] a[href*="/live"]',
                '[data-e2e="discover_category-list-live-card"] a[href*="/live"]',
                '[data-e2e="live-room-info"] a',
                '[data-e2e="live-card-container"] a',
                'a[href*="/@"][href*="/live"]',
                'a[href*="/live"]',
                '[class*="LiveCard"] a',
                '[class*="liveCard"] a',
            ];
            for (const s of sels) {
                const el = document.querySelector(s);
                if (el && el.offsetParent !== null) {
                    const a = el.closest('a') || el;
                    a.click();
                    return a.href || 'clicked';
                }
            }
            return null;
        })()
    """,
    "bilibili": """
        (function() {
            const sels = [
                '.room-card-box a[href*="live.bilibili.com"]',
                '.card-wrapper a[href*="live.bilibili.com"]',
                '.live-item a',
                'a[href*="live.bilibili.com/"]',
            ];
            for (const s of sels) {
                const a = document.querySelector(s);
                if (a && a.offsetParent !== null) { a.click(); return a.href; }
            }
            return null;
        })()
    """,
}

# JS that returns 'skipped' when an ad skip button is found and clicked, else null.
_SKIP_AD_JS: dict[str, str] = {
    # v2 (LVQoE t35). The old version never skipped: steps 2 and 3 both required #movie_player.ad-showing, which yt
    # does not set reliably (SESSION_NOTES). Now: an ad is detected by ANY signal (finite short duration first, then
    # ad-showing, then ad DOM); then click a visible skip button, else jump to the end of a finite ad, else mute and
    # wait. The label's ad mark (content.js) never depends on this succeeding.
    "youtube": """
        (function() {
            const v = document.querySelector('#movie_player video') || document.querySelector('video');
            const mp = document.getElementById('movie_player');
            const sig = [];
            if (v && isFinite(v.duration) && v.duration > 0 && v.duration < %(max_dur)d) sig.push('duration_finite');
            if (mp && mp.classList.contains('ad-showing')) sig.push('yt_ad_showing');
            const vis = (el) => el && el.offsetParent !== null;
            const adDom = ['.ytp-ad-player-overlay', '.ytp-ad-player-overlay-layout', '.ytp-ad-preview-container',
                           '.ytp-ad-text', '.ytp-ad-simple-ad-badge', '.ytp-ad-badge', '.ytp-ad-duration-remaining'];
            if (adDom.some((s) => [...document.querySelectorAll(s)].some(vis))) sig.push('yt_ad_dom');
            if (sig.length === 0) {
                if (v && v.muted && v.dataset.lvqoeMutedForAd === '1') {
                    v.muted = false; delete v.dataset.lvqoeMutedForAd; return 'unmuted';
                }
                return null;
            }
            const tag = ' [' + sig.join('|') + ']';
            // a skip button can be visible while its countdown runs (clicks do nothing): after 2 clicks with the ad still
            // showing, fall through to fast-forward (laptop probe 2026-10-16)
            const tries = mp ? Number(mp.dataset.lvqoeSkipTries || 0) : 0;
            const canFF = v && isFinite(v.duration) && v.duration > 0 && v.duration < %(max_dur)d && v.currentTime < v.duration - 0.25;
            if (tries >= 2 && canFF) {
                v.currentTime = v.duration; v.play().catch(() => {});
                if (mp) mp.dataset.lvqoeSkipTries = '0';
                return 'fast-forwarded-after-skip' + tag;
            }
            // 1. a visible skip button: known classes, then any button whose text / aria-label says skip
            const skipSel = ['.ytp-ad-skip-button', '.ytp-skip-ad-button', '.ytp-ad-skip-button-modern',
                             '.ytp-ad-skip-button-container button', 'button.ytp-ad-skip-button-modern'];
            for (const s of skipSel) {
                const b = document.querySelector(s);
                if (vis(b) && !b.disabled) { b.click(); if (mp) mp.dataset.lvqoeSkipTries = String(tries + 1); return 'skipped' + tag; }
            }
            // only buttons inside the player: the page has a "Skip navigation" a11y button that matches by text and
            // does nothing to the ad (laptop test 2026-10-16: it was clicked every second during a 15 s ad)
            for (const b of (mp ? mp.querySelectorAll('button') : [])) {
                if (!vis(b) || b.disabled) continue;
                const label = (b.textContent + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
                if (label.includes('skip')) { b.click(); if (mp) mp.dataset.lvqoeSkipTries = String(tries + 1); return 'text-skipped' + tag; }
            }
            // 2. fast-forward a finite ad to its end
            if (v && isFinite(v.duration) && v.duration > 0 && v.duration < %(max_dur)d) {
                if (v.currentTime < v.duration - 0.25) {
                    v.currentTime = v.duration;
                    v.play().catch(() => {});
                    return 'fast-forwarded' + tag;
                }
            }
            // 3. mute and wait
            if (v && !v.muted) { v.muted = true; v.dataset.lvqoeMutedForAd = '1'; return 'muted-wait' + tag; }
            return 'waiting' + tag;
        })()
    """ % {"max_dur": 600},
    "twitch": """
        (function() {
            // Detect Twitch mid-roll ad
            const adEl = document.querySelector(
                '[data-a-target="ad-banner"], .ads-manager__container, ' +
                '[data-test-selector="ad-banner"], .tw-ad, ' +
                '[data-a-target="video-ad-label"], [data-a-target="video-ad-countdown"], ' +
                '[data-test-selector="ad-banner-default-text"]'
            );
            const v = document.querySelector('video');
            if (adEl) {
                // Mute while ad plays — do NOT reload (would lose stream + retrigger ad)
                if (v && !v.muted) { v.muted = true; return 'muted-ad'; }
                return 'waiting-ad';
            }
            // Ad is gone — restore sound
            if (v && v.muted) { v.muted = false; return 'unmuted'; }
            return null;
        })()
    """,
    "tiktok": "null",  # TikTok live has no pre-roll
    "bilibili": """
        (function() {
            const btn = document.querySelector(
                '.bilibili-player-ipad-ad-skip, ' +
                '[class*="adskip"], ' +
                '.skip-advertisement'
            );
            if (btn && btn.offsetParent !== null) {
                btn.click();
                return 'skipped';
            }
            return null;
        })()
    """,
}

# v2 (LVQoE t35, supervisor 2026-10-16): close the live-chat panel after navigation (a persistent WebSocket plus chat
# polling adds background bytes). Per platform, switchable: on twitch, collapsing chat WIDENS the player, which would
# change the player size vs the old collection; the default keeps twitch chat open until the window/player decision.
CLOSE_CHAT_DEFAULT: dict[str, bool] = {"youtube": True, "twitch": False, "tiktok": False, "bilibili": False}
_CLOSE_CHAT_JS: dict[str, str] = {
    # the chat's own close button sits in the same-origin live_chat iframe (laptop probe 2026-10-16); once closed, the
    # outer frame shows a visible "Show chat" button, which is the confirmation
    "youtube": """
        (function() {
            const f = document.querySelector('ytd-live-chat-frame');
            if (!f) return 'no-chat';
            const vis = (el) => el && el.offsetParent !== null;
            // closed = the chat frame is collapsed / not rendered, or the page offers to reopen it ("Show chat"; the
            // 2026-10 layout puts a "Live chat · Open panel" card under the player instead, laptop run 2026-10-16)
            const frameGone = () => f.hasAttribute('collapsed') || !vis(f) || f.getBoundingClientRect().height < 50;
            const label = (b) => b.getAttribute('aria-label') || b.innerText || '';
            const inChatCard = (b) => { let e = b; for (let i = 0; i < 6 && e; i++, e = e.parentElement)
                if (/live chat/i.test(e.innerText || '')) return true; return false; };
            const reopen = () => [...document.querySelectorAll('button')].some(b => vis(b) &&
                (/show chat/i.test(label(b)) || (/open panel/i.test(label(b)) && inChatCard(b))));
            const shown = () => frameGone() || reopen();
            if (shown()) return 'already-closed';
            let d = null;
            try { d = f.querySelector('iframe') && f.querySelector('iframe').contentDocument; } catch (e) {}
            const b = d && (d.querySelector('#close-button button') || d.querySelector('button[aria-label="Close"]'));
            if (b) { b.click(); return 'closed-in-iframe'; }
            const h = [...f.querySelectorAll('button')].find(x => vis(x) && /hide chat/i.test(x.getAttribute('aria-label') || x.innerText || ''));
            if (h) { h.click(); return 'closed-hide-button'; }
            return 'chat-present-no-button';
        })()
    """,
    "twitch": """
        (function() {
            const b = document.querySelector('[data-a-target="right-column__toggle-collapse-btn"]');
            if (b && b.offsetParent !== null) { b.click(); return 'collapsed'; }
            return 'no-chat-toggle';
        })()
    """,
}

# t37 (supervisor): turn on twitch's Video Stats overlay (settings > Advanced > Video Stats) so page_stats.js can log it
# per tick into player_stats.jsonl (G3: ground truth, never features); a CSS rule hides it (it keeps updating), so
# screenshots and the eye check are not covered by it. Read-only stats: no quality setting is touched.
_TWITCH_VIDEO_STATS_JS = """
(async function() {
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const vis = (el) => el && el.offsetParent !== null;
    if (!document.getElementById('lvqoe-hide-video-stats')) {
        const st = document.createElement('style');
        st.id = 'lvqoe-hide-video-stats';
        st.textContent = '[data-a-target="player-overlay-video-stats"]{opacity:0 !important;pointer-events:none !important}';
        document.head.appendChild(st);
    }
    if (document.querySelector('[data-a-target="player-overlay-video-stats-row"]')) return 'already-on';
    const btn = [...document.querySelectorAll('button[data-a-target="player-settings-button"]')].find(vis)
             || document.querySelector('button[data-a-target="player-settings-button"]');
    if (!btn) return 'no-settings-button';
    btn.click(); await sleep(800);
    const adv = document.querySelector('[data-a-target="player-settings-menu-item-advanced"]');
    if (!adv) { btn.click(); return 'no-advanced-item'; }
    adv.click(); await sleep(800);
    const box = document.querySelector('[data-a-target="player-settings-submenu-advanced-video-stats"] input');
    if (!box) { btn.click(); return 'no-video-stats-toggle'; }
    if (!box.checked) box.click();
    await sleep(500);
    btn.click();                                   // close the menu
    await sleep(800);
    return document.querySelector('[data-a-target="player-overlay-video-stats-row"]') ? 'on' : 'toggled-no-rows';
})()
"""

# v2 (supervisor 2026-10-16): pin the player to the old collection's modal player size over the CURRENT-era sessions
# (G6 compares to that era): yt 853x480 (30 of 47 sessions), twitch 1274x717 with chat open (tied with 794x447, 18 of
# 102 each; supervisor's choice), tt 783x604 landscape (23 of 67). The window is resized over CDP until the player
# matches; the target and the realised size go to meta.json and check.md. No theatre mode.
PLAYER_PIN: dict[str, tuple[int, int]] = {"youtube": (853, 480), "twitch": (1274, 717), "tiktok": (783, 604)}
PIN_TOLERANCE = 0.02

_PLAYER_SIZE_JS = """
(function() {
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    return v ? JSON.stringify([v.clientWidth, v.clientHeight]) : null;
})()
"""


def _browser_call(method: str, params: dict | None = None) -> dict:
    with urllib.request.urlopen(f"{CDP_BASE}/json/version", timeout=5) as r:
        b = CDPPage(json.loads(r.read())["webSocketDebuggerUrl"])
    try:
        return b.call(method, params)
    finally:
        b.close()


def pin_player(page: "CDPPage", target: tuple[int, int], target_id: str | None, iters: int = 5) -> dict:
    """Resize the browser window until the <video> client size is within PIN_TOLERANCE of `target` (proportional
    steps on the window's width and height). Returns the trace for meta.json; never raises."""
    trace = {"target": list(target), "steps": [], "ok": False}
    try:
        win = _browser_call("Browser.getWindowForTarget", {"targetId": target_id} if target_id else {})
        wid, bounds = win.get("windowId"), dict(win.get("bounds") or {})
        if wid is None:
            trace["error"] = "no window id"
            return trace
        _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        for _ in range(iters):
            ps = page.eval(_PLAYER_SIZE_JS)
            pw, ph = json.loads(ps) if ps else (0, 0)
            trace["steps"].append({"window": [bounds.get("width"), bounds.get("height")], "player": [pw, ph]})
            if pw and ph and abs(pw - target[0]) <= PIN_TOLERANCE * target[0] and abs(ph - target[1]) <= PIN_TOLERANCE * target[1]:
                trace["ok"] = True
                break
            if not (pw and ph and bounds.get("width") and bounds.get("height")):
                break
            if len(trace["steps"]) >= 2 and trace["steps"][-2]["player"] == [pw, ph]:
                trace["error"] = "player size did not change with the window (layout- or screen-bound)"
                break
            scr = json.loads(page.eval("JSON.stringify([screen.availWidth, screen.availHeight])") or "[0, 0]")
            nw = int(round(bounds["width"] + (target[0] - pw) * bounds["width"] / max(pw, 1) * 0.9))
            nh = int(round(bounds["height"] + (target[1] - ph) * bounds["height"] / max(ph, 1) * 0.9))
            if scr[0] and scr[1]:                 # a window larger than the screen is not honoured; do not chase it
                nw, nh = min(nw, scr[0]), min(nh, scr[1])
            bounds = {"width": max(400, nw), "height": max(300, nh)}
            _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
            time.sleep(1.5)                       # the page re-lays out the player
        trace["final_player"] = trace["steps"][-1]["player"] if trace["steps"] else None
    except Exception as exc:
        trace["error"] = repr(exc)
    return trace


# Window / viewport / player geometry (meta.json): the player size decides the top of yt's ladder.
_GEOMETRY_JS = """
(function() {
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    const flexy = document.querySelector('ytd-watch-flexy');
    return JSON.stringify({
        window_outer: [window.outerWidth, window.outerHeight], viewport: [window.innerWidth, window.innerHeight],
        device_pixel_ratio: window.devicePixelRatio, screen: [screen.width, screen.height],
        player_client: v ? [v.clientWidth, v.clientHeight] : null,
        yt_theater: flexy ? flexy.hasAttribute('theater') : null,
        yt_fullscreen: flexy ? flexy.hasAttribute('fullscreen') : null,
    });
})()
"""

# JS: click the video element to force playback (handles autoplay block).
# v2: never click a PLAYING video. On yt a click on <video> toggles play / pause, so the v1 "play(); click()" paused
# a playing stream on every call (laptop probe 2026-10-16: paused after each call). play() only when paused; a click
# only if play() is rejected (autoplay block).
_CLICK_PLAY_JS = """
(function() {
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    if (!v) return false;
    if (v.paused) {
        const pr = v.play();
        if (pr && pr.catch) pr.catch(() => { v.click(); });
    }
    return true;
})()
"""

# JS that returns the current video timestamp (used to verify it's advancing).
_CURRENT_TIME_JS = """
(function() {
    const v = document.querySelector('video');
    return v ? v.currentTime : -1;
})()
"""

# JS that returns true when the current page looks like a live video page.
_IS_LIVE_PAGE_JS: dict[str, str] = {
    "youtube": """
        !!(document.querySelector('.ytp-live-badge') ||
           document.querySelector('[href*="live_chat"]') ||
           document.querySelector('.ytp-live'))
    """,
    "twitch": """
        !!(document.querySelector('[data-a-target="player-overlay-click-handler"]') ||
           document.querySelector('.video-player__container'))
    """,
    "tiktok": "!!(document.querySelector('video'))",
    "bilibili": "!!(document.querySelector('video'))",
}


# ---------------------------------------------------------------------------
# CDPPage — thread-safe wrapper around a single CDP WebSocket connection
# ---------------------------------------------------------------------------

class CDPPage:
    """
    Wraps a CDP WebSocket target.  All calls are serialised by a lock so the
    instance can be shared safely between the main thread and the ad-watcher.
    """

    def __init__(self, ws_url: str):
        self._ws = websocket.WebSocket()
        self._ws.settimeout(20)
        self._ws.connect(ws_url)
        self._seq = 0
        self._lock = threading.Lock()

    def call(self, method: str, params: dict | None = None) -> dict:
        with self._lock:
            self._seq += 1
            mid = self._seq
            self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    msg = json.loads(self._ws.recv())
                except websocket.WebSocketTimeoutException:
                    break
                if msg.get("id") == mid:
                    return msg.get("result", {})
            return {}

    def navigate(self, url: str) -> None:
        self.call("Page.navigate", {"url": url})

    def eval(self, js: str, await_promise: bool = False):
        params: dict = {"expression": js.strip(), "returnByValue": True}
        if await_promise:
            params["awaitPromise"] = True
        res = self.call("Runtime.evaluate", params)
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _wait_for_cdp(timeout: float = 30.0) -> bool:
    """Poll until Chrome's CDP HTTP endpoint is responding."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"{CDP_BASE}/json", timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


def _get_page_ws_url() -> str | None:
    """Return the WebSocket debugger URL for the first non-DevTools page target."""
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
            targets = json.loads(r.read())
        for t in targets:
            if t.get("type") == "page" and "devtools" not in t.get("url", ""):
                return t.get("webSocketDebuggerUrl")
        # Fallback: any target
        if targets:
            return targets[0].get("webSocketDebuggerUrl")
    except Exception:
        pass
    return None


def _open_new_tab(url: str = "about:blank") -> str | None:
    """Ask Chrome to open a new tab and return its WebSocket debugger URL."""
    try:
        req = urllib.request.Request(
            f"{CDP_BASE}/json/new?{urllib.parse.quote(url, safe=':/?=&')}",
            method="PUT",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            target = json.loads(r.read())
        return target.get("webSocketDebuggerUrl")
    except Exception:
        return None


# realpath: Chrome derives an unpacked extension's id from the symlink-resolved absolute path
LABELING_DIR = os.path.realpath(Path(__file__).parent.parent / "labeling")
_OUR_SW_SUFFIX = "/background.js"     # our extension's MV3 service worker (manifest "background.service_worker")
OUR_EXT_NAME = json.loads((Path(LABELING_DIR) / "manifest.json").read_text())["name"]
EXT_ID: str | None = None              # set by ensure_extension_loaded()


def unpacked_extension_id(path: str) -> str:
    """Chrome's id for an unpacked extension: the first 32 hex digits of sha256(absolute path), mapped 0-f -> a-p."""
    import hashlib
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(path.encode()).hexdigest()[:32])


def _targets() -> list[dict]:
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []


def _our_sw_ws_url() -> str | None:
    """WebSocket URL of OUR extension's service worker, if it is running. Chrome also runs built-in extension
    service workers, so 'the first chrome-extension service worker' can be the wrong one. An MV3 worker stops when
    idle, so its absence does not mean the extension is not loaded (see _ext_page)."""
    ext_id = EXT_ID or unpacked_extension_id(LABELING_DIR)   # never "any background.js": other extensions have one
    return next((t.get("webSocketDebuggerUrl") for t in _targets()
                 if t.get("type") == "service_worker" and t.get("url", "").startswith(f"chrome-extension://{ext_id}/")
                 and t.get("url", "").endswith(_OUR_SW_SUFFIX) and t.get("webSocketDebuggerUrl")), None)


def _ext_page() -> tuple["CDPPage | None", str | None]:
    """A CDP handle with chrome.storage access to our extension: its service worker if running, else a tab on the
    extension's own popup.html (opened here; the caller closes it via the returned target id)."""
    ws = _our_sw_ws_url()
    if ws:
        return CDPPage(ws), None
    if not EXT_ID:
        return None, None
    try:
        req = urllib.request.Request(f"{CDP_BASE}/json/new?chrome-extension://{EXT_ID}/popup.html", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            t = json.loads(r.read())
        page = CDPPage(t["webSocketDebuggerUrl"])
        for _ in range(20):                       # wait until the extension context is live
            if page.eval("typeof chrome !== 'undefined' && !!chrome.storage") is True:
                break
            time.sleep(0.25)
        return page, t.get("id")
    except Exception:
        return None, None


def _close_target(target_id: str | None) -> None:
    if target_id:
        try:
            urllib.request.urlopen(urllib.request.Request(f"{CDP_BASE}/json/close/{target_id}", method="GET"), timeout=3)
        except Exception:
            pass


def ensure_extension_loaded() -> str | None:
    """Load the labeling extension and return its id, verified by its manifest name. Branded Chrome >= 137 ignores
    --load-extension, so it is loaded over CDP (Extensions.loadUnpacked; Chrome runs with
    --enable-unsafe-extension-debugging, which the Makefile passes); loading the same path again is harmless.
    Where CDP loading is unavailable (Chromium with --load-extension), the id is derived from the path.
    Never "the first extension service worker named background.js": a cloned profile carries Chrome's default
    extensions, and adopting one of their ids left ours unloaded and the export reading the wrong storage (chaos
    captest2, t37)."""
    global EXT_ID
    EXT_ID = None
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json/version", timeout=5) as r:
            browser_ws = json.loads(r.read())["webSocketDebuggerUrl"]
        b = CDPPage(browser_ws)
        res = b.call("Extensions.loadUnpacked", {"path": LABELING_DIR})
        b.close()
        print(f"[browser] Extensions.loadUnpacked → {res}", flush=True)
        EXT_ID = res.get("id")
    except Exception as exc:
        print(f"[browser] Extensions.loadUnpacked failed: {exc}", flush=True)
    if not EXT_ID:
        EXT_ID = unpacked_extension_id(LABELING_DIR)
    page, tid = _ext_page()
    name = None
    try:
        name = page.eval("chrome.runtime && chrome.runtime.getManifest().name") if page else None
    except Exception:
        pass
    if page:
        page.close()
    _close_target(tid)
    if name != OUR_EXT_NAME:
        print(f"[browser] extension {EXT_ID} is not ours (manifest name {name!r}, expected {OUR_EXT_NAME!r})", flush=True)
        return None
    return EXT_ID


def _set_download_dir(page: CDPPage, download_path: str) -> None:
    """Tell Chrome to save all downloads to download_path without prompting."""
    page.call("Browser.setDownloadBehavior", {
        "behavior": "allow",
        "downloadPath": download_path,
        "eventsEnabled": False,
    })


def _export_via_service_worker(session_dir: Path) -> bool:
    """
    Read QoE events from the extension's chrome.storage.local via CDP,
    write labels.csv, and clear the storage for the next run.
    Returns True on success.
    """
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
            targets = json.loads(r.read())

        sw, tmp_tab = _ext_page()
        if sw is None:
            print("[browser] No extension context found (service worker idle and no extension page)", flush=True)
            return False

        events_json = sw.eval(
            "new Promise(r => chrome.storage.local.get({events:[]}, d => r(JSON.stringify(d.events))))",
            await_promise=True,
        )
        # yt player stats (G3 ground truth): a separate file, never merged into labels.csv
        stats_json = sw.eval(
            "new Promise(r => chrome.storage.local.get({player_stats:[]}, d => r(JSON.stringify(d.player_stats))))",
            await_promise=True,
        )
        # Clear storage so the next run starts fresh
        sw.eval("new Promise(r => chrome.storage.local.remove(['events', 'player_stats'], r))", await_promise=True)
        sw.close()
        _close_target(tmp_tab)
        stats = json.loads(stats_json) if stats_json else []
        if stats:
            with open(session_dir / "player_stats.jsonl", "w") as fh:
                for rec in stats:
                    fh.write(json.dumps(rec) + "\n")
            print(f"[browser] Player stats ({len(stats)} records) → {session_dir / 'player_stats.jsonl'}", flush=True)

        events: list[dict] = json.loads(events_json) if events_json else []
        if not events:
            print("[browser] Extension storage empty — no QoE events collected", flush=True)
            return False

        # v2 (LVQoE t35): new columns appended after the v1 ones (additive; v1 readers keep working)
        header = ["ts", "type", "video_w", "video_h", "client_w", "client_h",
                  "dropped", "current_time", "tabId", "url", "event_id",
                  "buffered_ahead_s", "playback_rate", "duration_s", "is_ad", "ad_signals", "src_changed",
                  "login_wall_text", "login_wall", "quality_countdown"]
        rows = [",".join(
            '"{}"'.format(str(e.get(k, "")).replace('"', '""')) for k in header
        ) for e in events]
        csv_text = "\n".join([",".join(header)] + rows)

        dest = session_dir / "labels.csv"
        dest.write_text(csv_text)
        print(f"[browser] Extension labels ({len(events)} events) → {dest}", flush=True)
        return True

    except Exception as exc:
        print(f"[browser] Extension CSV export error: {exc}", flush=True)
        return False


try:
    import urllib.parse  # already in stdlib, just ensuring import
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Navigation logic
# ---------------------------------------------------------------------------

def _video_is_advancing(page: CDPPage) -> bool:
    """Return True if the video currentTime increases over a 1.5s window."""
    t0 = page.eval(_CURRENT_TIME_JS)
    if t0 is None or t0 < 0:
        return False
    time.sleep(1.5)
    t1 = page.eval(_CURRENT_TIME_JS)
    return t1 is not None and t1 > t0


def _navigate_to_live(page: CDPPage, platform: str, timeout: float = 45.0, first_url: str | None = None) -> str | None:
    """Returns the list URL that delivered a playing live video (None if none did)."""
    """
    Try each URL in LIVE_URLS[platform].  For directory/discovery pages,
    execute platform-specific JS to click into a live room.  Then kick play
    via JS and verify the video timestamp is actually advancing.
    """
    urls = list(LIVE_URLS[platform])
    if first_url:
        # the rotation's channel only: falling back to another channel would break the rotation's accounting
        urls = [first_url]
    for url in urls:
        print(f"[browser] Navigating to {url}", flush=True)
        page.navigate(url)
        # Wait until location.href actually changes — _wait_for_load alone is not
        # enough because the old page may already be in readyState=complete.
        nav_deadline = time.time() + 25
        while time.time() < nav_deadline:
            current = page.eval("location.href") or ""
            if current.startswith(url[:30]):
                break
            time.sleep(0.5)
        _wait_for_load(page, timeout=15)

        # For directory/discovery pages, click into the first live stream.
        # Twitch is a heavy SPA — wait for stream cards to render after load.
        pick_js = _PICK_STREAM_JS.get(platform, "null")
        if pick_js.strip() != "null":
            card_js = _CARD_READY_JS.get(platform)
            if card_js:
                # Poll until at least one card link is visible (max 15s); the pick otherwise runs before the list renders
                card_deadline = time.time() + 15
                while time.time() < card_deadline:
                    if page.eval(card_js):
                        break
                    time.sleep(1)
            clicked = page.eval(pick_js)
            if clicked:
                print(f"[browser] Clicked into stream: {clicked}", flush=True)
                _wait_for_load(page, timeout=15)

        # Kick playback (handles Chrome autoplay block)
        page.eval(_CLICK_PLAY_JS)
        time.sleep(2)
        page.eval(_CLICK_PLAY_JS)  # second nudge in case first hit an ad overlay

        # Verify the video is actually advancing (not just paused at t=0). Pre-roll ads are handled here too: the
        # ad watcher only starts after navigation, and a pre-roll would otherwise keep the content from advancing.
        deadline = time.time() + timeout
        skip_js = _SKIP_AD_JS.get(platform, "null")
        while time.time() < deadline:
            if skip_js.strip() != "null":
                act = page.eval(skip_js)
                if act:
                    print(f"[browser] Pre-roll ad action: {act}", flush=True)
            page.eval(_CLICK_PLAY_JS)
            if _video_is_advancing(page):
                # YouTube: confirm live badge (YouTube DVR uses finite duration, not Infinity)
                if platform == "youtube":
                    has_badge = page.eval("!!(document.querySelector('.ytp-live-badge,.ytp-live'))")
                    if not has_badge:
                        print(f"[browser] YouTube: no live badge yet — waiting", flush=True)
                        time.sleep(3)
                        has_badge = page.eval("!!(document.querySelector('.ytp-live-badge,.ytp-live'))")
                        if not has_badge:
                            break
                # Twitch: confirm live indicator, reject VODs
                if platform == "twitch":
                    url_now = page.eval("location.href") or ""
                    if "/videos/" in url_now or "/clip/" in url_now:
                        print(f"[browser] Twitch VOD detected — skipping", flush=True)
                        break
                    is_live = page.eval(
                        "(function(){return !!(document.querySelector('.channel-root--live,.live-time')"
                        "||document.querySelector('p[data-a-target=\"stream-title\"]'));})()"
                    )
                    if not is_live:
                        # Give the page a few more seconds to render the live indicator
                        time.sleep(4)
                        is_live = page.eval(
                            "(function(){return !!(document.querySelector('.channel-root--live,.live-time')"
                            "||document.querySelector('p[data-a-target=\"stream-title\"]'));})()"
                        )
                        if not is_live:
                            print(f"[browser] Twitch: no live indicator — skipping", flush=True)
                            break
                print(f"[browser] Video is playing on {platform}", flush=True)
                return url
            time.sleep(2)

    print(f"[browser] Could not find a playing live stream for {platform}", flush=True)
    return None


def _wait_for_load(page: CDPPage, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = page.eval("document.readyState")
        if state in ("complete", "interactive"):
            return
        time.sleep(0.5)


# ---------------------------------------------------------------------------
# Ad watcher
# ---------------------------------------------------------------------------

_WALL_JS = "document.documentElement.dataset.lvqoeWall === '1'"
TICK_STARTUP_TIMEOUT_S = 15


def _ad_watcher_loop(page: CDPPage, platform: str, stop: threading.Event, log_path: Path | None = None,
                     on_wall=None) -> None:
    """Run the platform's ad action once a second; every action is logged with a timestamp (ad_actions.jsonl).
    Also watches for the login wall content.js marks (t37) and calls on_wall() once when it appears."""
    skip_js = _SKIP_AD_JS.get(platform, "null")
    walled = False
    while not stop.wait(timeout=1):
        try:
            result = page.eval(skip_js)
            if result:
                print(f"[browser] Ad action: {result}", flush=True)
                if log_path is not None:
                    with open(log_path, "a") as fh:
                        fh.write(json.dumps({"ts": time.time(), "platform": platform, "action": result}) + "\n")
        except Exception:
            pass
        if on_wall is not None and not walled:
            try:
                if page.eval(_WALL_JS):
                    walled = True
                    on_wall()
            except Exception:
                pass


SHOT_INTERVAL_S = 10.0
SHOT_WIDTH_PX = 640
SHOT_JPEG_QUALITY = 40


def _screenshot_loop(page: CDPPage, shots_dir: Path, stop: threading.Event) -> None:
    """A small JPEG of the tab every SHOT_INTERVAL_S ('cadence'), plus one at every tier change and every ad start
    (t37: content.js raises <html data-lvqoe-shot-req="<n>|<reason>|<event_id>">, polled each second). Each shot is
    stamped with the extension's latest tick (<html data-lvqoe-tick>) and its reason, for the human label-vs-screen
    check. Nothing here feeds labels. The loop starts after navigation: a pre-roll that ends during navigation has no
    event shot (laptop run8, yt), and two events within one poll second give one shot (the later reason)."""
    import base64
    shots_dir.mkdir(parents=True, exist_ok=True)
    index = shots_dir / "index.jsonl"
    last_cadence, last_req = time.time(), None
    while not stop.wait(timeout=1.0):
        try:
            st = page.eval("JSON.stringify([document.documentElement.dataset.lvqoeTick || null, "
                           "document.documentElement.dataset.lvqoeTickTs || null, "
                           "document.documentElement.dataset.lvqoeShotReq || null])")
            tick_id, tick_ts, req = json.loads(st) if st else (None, None, None)
            reason = None
            if req and req != last_req:
                reason = req.split("|")[1] if "|" in req else "event"
                last_req = req
            if reason is None and time.time() - last_cadence >= SHOT_INTERVAL_S:
                reason = "cadence"
            if reason is None:
                continue
            if reason == "cadence":
                last_cadence = time.time()
            m = page.call("Page.getLayoutMetrics")
            vp = m.get("cssLayoutViewport") or m.get("layoutViewport") or {}
            w, h = float(vp.get("clientWidth") or 1280), float(vp.get("clientHeight") or 720)
            res = page.call("Page.captureScreenshot", {
                "format": "jpeg", "quality": SHOT_JPEG_QUALITY,
                "clip": {"x": 0, "y": 0, "width": w, "height": h, "scale": SHOT_WIDTH_PX / w}})
            data = res.get("data")
            if not data:
                continue
            ts = time.time()
            tag = "" if reason == "cadence" else f"_{reason}"
            name = f"{int(ts * 1000)}_tick{tick_id if tick_id is not None else 'na'}{tag}.jpg"
            (shots_dir / name).write_bytes(base64.b64decode(data))
            with open(index, "a") as fh:
                fh.write(json.dumps({"ts": ts, "file": name, "tick": tick_id, "tick_ts": tick_ts, "reason": reason,
                                     "request": req if reason != "cadence" else None}) + "\n")
        except Exception as exc:  # a missed shot must never stop collection
            print(f"[browser] screenshot failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class BrowserSession:
    """
    Manages a Chrome tab pointed at a live stream.

    Usage:
        session = BrowserSession("youtube")
        session.start()   # raises RuntimeError on failure
        # ... run oscillate() ...
        session.stop()    # stops ad-watcher, browser stays open for PCAP
    """

    def __init__(self, platform: str, session_dir: Path | None = None, close_chat: bool | None = None,
                 first_url: str | None = None):
        self.platform = platform
        self.first_url = first_url          # yt channel rotation: try this URL first (orchestrate.py)
        self.close_chat = CLOSE_CHAT_DEFAULT.get(platform, False) if close_chat is None else close_chat
        self._session_dir = session_dir
        self._start_time: float = 0.0
        self._page: CDPPage | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._shooter: threading.Thread | None = None
        self.channel_url: str | None = None      # the live page actually collected (meta.json)
        self.list_url: str | None = None         # the LIVE_URLS / first_url entry that led to it
        self.chat_action: str | None = None      # what the chat-close step did (meta.json)
        self.geometry: dict | None = None        # window / player geometry after navigation (meta.json)
        self.player_pin: dict | None = None      # pin_player trace (meta.json, check.md)
        self.abort_reason: str | None = None     # set by a watcher (t37: "login_wall"); exp.py stops the session
        self.video_stats: str | None = None      # twitch Video Stats overlay result (meta.json)
        self.abort_at: float | None = None

    def start(self, cdp_ready_timeout: float = 30.0) -> None:
        print("[browser] Waiting for Chrome CDP endpoint...", flush=True)
        if not _wait_for_cdp(cdp_ready_timeout):
            raise RuntimeError("Chrome CDP not available after %ds" % cdp_ready_timeout)

        # Give Chrome a moment to finish initialising all internal targets
        time.sleep(3)
        ext = ensure_extension_loaded()
        if not ext:
            raise RuntimeError("labeling extension not loaded; see chrome.log")
        print(f"[browser] Labeling extension {ext}", flush=True)

        # Close extra tabs (session-restored ones) but KEEP one alive so Chrome
        # doesn't exit.  Reuse the survivor tab rather than opening a new one.
        try:
            with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
                all_tabs = json.loads(r.read())
            page_tabs = [t for t in all_tabs if t.get("type") == "page"]
            # Keep the first tab; close the rest
            for tab in page_tabs[1:]:
                try:
                    tid = tab["id"]
                    req = urllib.request.Request(f"{CDP_BASE}/json/close/{tid}", method="GET")
                    urllib.request.urlopen(req, timeout=3)
                except Exception:
                    pass
        except Exception:
            page_tabs = []
        time.sleep(1)
        # Reuse the surviving tab (or open one if Chrome had no tabs somehow)
        ws_url = page_tabs[0].get("webSocketDebuggerUrl") if page_tabs else None
        if not ws_url:
            ws_url = _open_new_tab()
        if not ws_url:
            ws_url = _get_page_ws_url()
        if not ws_url:
            raise RuntimeError("Could not get a CDP page target")

        print(f"[browser] Connected to CDP: {ws_url[:60]}...", flush=True)
        self._ws_url = ws_url
        self._page = CDPPage(ws_url)
        # A hidden / occluded page does not start yt playback and throttles the extension's 1 Hz timer (laptop test
        # 2026-10-16: visibilityState hidden, player unstarted until brought to front). Keep the tab visible and focused.
        self._page.call("Page.bringToFront")
        self._page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        self._start_time = time.time()

        # Direct extension CSV downloads into the session directory
        download_dir = self._session_dir or (Path(__file__).parent.parent / "current_data")
        download_dir.mkdir(parents=True, exist_ok=True)
        _set_download_dir(self._page, str(download_dir))
        print(f"[browser] Downloads → {download_dir}", flush=True)

        # Clear any stale events from a previous killed/crashed session
        try:
            with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
                targets = json.loads(r.read())
            sw, tmp_tab = _ext_page()
            if sw is not None:
                sw.eval("new Promise(r => chrome.storage.local.remove(['events', 'player_stats'], r))", await_promise=True)
                sw.close()
                _close_target(tmp_tab)
                print("[browser] Extension storage cleared", flush=True)
        except Exception as e:
            print(f"[browser] Could not pre-clear extension storage: {e}", flush=True)

        self.list_url = _navigate_to_live(self._page, self.platform, first_url=self.first_url)
        if not self.list_url:
            raise RuntimeError(f"Failed to navigate to a live {self.platform} stream")

        try:
            self.channel_url = self._page.eval("location.href")
        except Exception:
            self.channel_url = None

        # fail fast if the labeling content script is not ticking (else a whole session collects with no labels:
        # chaos captest2, t37); content.js mirrors each res_meta tick onto <html data-lvqoe-tick>
        tick = None
        for _ in range(TICK_STARTUP_TIMEOUT_S):
            tick = self._page.eval("document.documentElement.dataset.lvqoeTick || null")
            if tick:
                break
            time.sleep(1)
        if not tick:
            raise RuntimeError(f"labeling content script not ticking {TICK_STARTUP_TIMEOUT_S} s after playback started "
                               f"(extension {EXT_ID})")
        print(f"[browser] Content script ticking (tick {tick})", flush=True)
        if self.close_chat and self.platform in _CLOSE_CHAT_JS:
            try:
                time.sleep(2)                       # the chat frame renders after the player
                self.chat_action = self._page.eval(_CLOSE_CHAT_JS[self.platform])
                time.sleep(1.5)
                if self.platform == "youtube" and str(self.chat_action).startswith("closed"):
                    again = self._page.eval(_CLOSE_CHAT_JS["youtube"])
                    self.chat_action += " (confirmed)" if again == "already-closed" else f" (not confirmed: {again})"
            except Exception as exc:
                self.chat_action = f"error: {exc}"
            print(f"[browser] Chat: {self.chat_action}", flush=True)
        else:
            self.chat_action = "left-open (close_chat off)"
        if self.platform == "twitch":
            try:
                self.video_stats = self._page.eval(_TWITCH_VIDEO_STATS_JS, await_promise=True)
            except Exception as exc:
                self.video_stats = f"error: {exc}"
            print(f"[browser] Twitch Video Stats: {self.video_stats}", flush=True)
        if self.platform in PLAYER_PIN:
            tid = None
            try:
                tid = next((t.get("id") for t in _targets() if t.get("webSocketDebuggerUrl") == self._ws_url), None)
            except Exception:
                pass
            self.player_pin = pin_player(self._page, PLAYER_PIN[self.platform], tid)
            print(f"[browser] Player pin: {self.player_pin}", flush=True)
        try:
            self.geometry = json.loads(self._page.eval(_GEOMETRY_JS) or "null")
        except Exception:
            self.geometry = None
        print(f"[browser] Geometry: {self.geometry}", flush=True)

        # Start ad-skip background thread (actions logged to ad_actions.jsonl)
        log_path = (self._session_dir / "ad_actions.jsonl") if self._session_dir else None
        self._watcher = threading.Thread(
            target=_ad_watcher_loop,
            args=(self._page, self.platform, self._stop, log_path, self._on_wall),
            daemon=True,
            name="ad-watcher",
        )
        self._watcher.start()
        print("[browser] Ad-watcher running", flush=True)

        if self._session_dir:
            self._shooter = threading.Thread(
                target=_screenshot_loop,
                args=(self._page, self._session_dir / "shots", self._stop),
                daemon=True,
                name="screenshots",
            )
            self._shooter.start()
            print(f"[browser] Screenshots every {SHOT_INTERVAL_S:.0f} s → {self._session_dir / 'shots'}", flush=True)

    def _on_wall(self) -> None:
        self.abort_reason, self.abort_at = "login_wall", time.time()
        print("[browser] LOGIN WALL detected (content.js login_wall) — aborting the session", flush=True)

    def stop(self) -> None:
        # Read QoE events from the extension service worker before closing the tab
        if self._session_dir:
            _export_via_service_worker(self._session_dir)

        # Close the tab — triggers chrome.tabs.onRemoved in the extension
        if self._page:
            try:
                self._page.call("Page.close")
            except Exception:
                pass

        self._stop.set()
        if self._watcher:
            self._watcher.join(timeout=5)
        if self._shooter:
            self._shooter.join(timeout=5)
        if self._page:
            self._page.close()

        print("[browser] Session stopped", flush=True)
