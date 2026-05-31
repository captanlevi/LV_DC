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
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

import websocket  # websocket-client

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CDP_BASE = "http://localhost:9222"

# Ordered priority list — first URL that delivers a playing video wins.
LIVE_URLS: dict[str, list[str]] = {
    "youtube": [
        # 24/7 news — most reliable, support 1080p
        "https://www.youtube.com/@AlJazeeraEnglish/live",
        "https://www.youtube.com/@dwnews/live",
        "https://www.youtube.com/@euronews/live",
        "https://www.youtube.com/@SkyNews/live",
        "https://www.youtube.com/@ABCNews/live",
        "https://www.youtube.com/@BBCNews/live",
        "https://www.youtube.com/@BloombergLive/live",
    ],
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
            // TikTok live explore — pick first live room tile
            const sels = [
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
    "youtube": """
        (function() {
            // 1. Class-based skip buttons (YouTube rotates these)
            const cssSel = [
                '.ytp-ad-skip-button',
                '.ytp-skip-ad-button',
                '.ytp-ad-skip-button-modern',
                '.ytp-ad-skip-button-modern .ytp-button',
                'button.ytp-ad-skip-button-modern',
                '.ytp-ad-skip-button-container button',
            ];
            for (const s of cssSel) {
                const btn = document.querySelector(s);
                if (btn && btn.offsetParent !== null) { btn.click(); return 'skipped'; }
            }
            // 2. Text / aria-label fallback — only when an ad is actually showing
            const playerEl = document.querySelector('#movie_player');
            if (playerEl && playerEl.classList.contains('ad-showing')) {
                for (const btn of document.querySelectorAll('button')) {
                    if (!btn.offsetParent) continue;
                    const label = (btn.textContent + ' ' + (btn.getAttribute('aria-label') || '')).toLowerCase();
                    if (label.includes('skip')) { btn.click(); return 'text-skipped'; }
                }
            }
            // 3. Fast-forward non-skippable ads; nudge player afterwards
            const player = playerEl || document.querySelector('#movie_player');
            if (player && player.classList.contains('ad-showing')) {
                const vid = document.querySelector('video');
                if (vid) {
                    if (isFinite(vid.duration) && vid.duration > 0) {
                        vid.currentTime = vid.duration;
                    }
                    // Nudge to dismiss any end-of-ad overlay
                    vid.play().catch(() => {});
                    player.click();
                    return 'fast-forwarded';
                }
            }
            return null;
        })()
    """,
    "twitch": """
        (function() {
            // Detect Twitch mid-roll ad
            const adEl = document.querySelector(
                '[data-a-target="ad-banner"], .ads-manager__container, ' +
                '[data-test-selector="ad-banner"], .tw-ad'
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

# JS: click the video element to force playback (handles autoplay block).
_CLICK_PLAY_JS = """
(function() {
    const v = document.querySelector('video');
    if (!v) return false;
    if (v.paused) { v.play(); }
    v.click();
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

        sw_ws_url = next(
            (t.get("webSocketDebuggerUrl") for t in targets
             if t.get("type") == "service_worker"
             and "chrome-extension" in t.get("url", "")
             and t.get("webSocketDebuggerUrl")),
            None,
        )
        if not sw_ws_url:
            print("[browser] No extension service worker target found", flush=True)
            return False

        sw = CDPPage(sw_ws_url)

        events_json = sw.eval(
            "new Promise(r => chrome.storage.local.get({events:[]}, d => r(JSON.stringify(d.events))))",
            await_promise=True,
        )
        # Clear storage so the next run starts fresh
        sw.eval("new Promise(r => chrome.storage.local.remove('events', r))", await_promise=True)
        sw.close()

        events: list[dict] = json.loads(events_json) if events_json else []
        if not events:
            print("[browser] Extension storage empty — no QoE events collected", flush=True)
            return False

        header = ["ts", "type", "video_w", "video_h", "client_w", "client_h",
                  "dropped", "tabId", "url", "event_id"]
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


def _navigate_to_live(page: CDPPage, platform: str, timeout: float = 45.0) -> bool:
    """
    Try each URL in LIVE_URLS[platform].  For directory/discovery pages,
    execute platform-specific JS to click into a live room.  Then kick play
    via JS and verify the video timestamp is actually advancing.
    """
    urls = LIVE_URLS[platform]
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
            if platform == "twitch":
                # Poll until at least one card link is visible (max 10s)
                card_deadline = time.time() + 10
                while time.time() < card_deadline:
                    found = page.eval("!!(document.querySelector('a[data-a-target=\"preview-card-image-link\"]'))")
                    if found:
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

        # Verify the video is actually advancing (not just paused at t=0)
        deadline = time.time() + timeout
        while time.time() < deadline:
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
                return True
            time.sleep(2)

    print(f"[browser] Could not find a playing live stream for {platform}", flush=True)
    return False


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

def _ad_watcher_loop(page: CDPPage, platform: str, stop: threading.Event) -> None:
    skip_js = _SKIP_AD_JS.get(platform, "null")
    while not stop.wait(timeout=1):
        try:
            result = page.eval(skip_js)
            if result:
                print(f"[browser] Ad action: {result}", flush=True)
        except Exception:
            pass


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

    def __init__(self, platform: str, session_dir: Path | None = None):
        self.platform = platform
        self._session_dir = session_dir
        self._start_time: float = 0.0
        self._page: CDPPage | None = None
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def start(self, cdp_ready_timeout: float = 30.0) -> None:
        print("[browser] Waiting for Chrome CDP endpoint...", flush=True)
        if not _wait_for_cdp(cdp_ready_timeout):
            raise RuntimeError("Chrome CDP not available after %ds" % cdp_ready_timeout)

        # Give Chrome a moment to finish initialising all internal targets
        time.sleep(3)

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
        self._page = CDPPage(ws_url)
        self._start_time = time.time()

        # Direct extension CSV downloads into the session directory (or a fallback)
        labels_dir = self._session_dir or (Path(__file__).parent.parent / "current_data" / "labels")
        labels_dir.mkdir(parents=True, exist_ok=True)
        _set_download_dir(self._page, str(labels_dir))
        print(f"[browser] Downloads → {labels_dir}", flush=True)

        # Clear any stale events from a previous killed/crashed session
        try:
            with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
                targets = json.loads(r.read())
            sw_ws = next((t.get("webSocketDebuggerUrl") for t in targets
                          if t.get("type") == "service_worker"
                          and "chrome-extension" in t.get("url", "")
                          and t.get("webSocketDebuggerUrl")), None)
            if sw_ws:
                sw = CDPPage(sw_ws)
                sw.eval("new Promise(r => chrome.storage.local.remove('events', r))", await_promise=True)
                sw.close()
                print("[browser] Extension storage cleared", flush=True)
        except Exception as e:
            print(f"[browser] Could not pre-clear extension storage: {e}", flush=True)

        ok = _navigate_to_live(self._page, self.platform)
        if not ok:
            raise RuntimeError(f"Failed to navigate to a live {self.platform} stream")

        # Start ad-skip background thread
        self._watcher = threading.Thread(
            target=_ad_watcher_loop,
            args=(self._page, self.platform, self._stop),
            daemon=True,
            name="ad-watcher",
        )
        self._watcher.start()
        print("[browser] Ad-watcher running", flush=True)

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
        if self._page:
            self._page.close()

        print("[browser] Session stopped", flush=True)
