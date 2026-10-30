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

import collections
import json
import os
import math
import re
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

import websocket  # websocket-client

from yt_channels import YT_CHANNELS, url_of
from check_session import FROZEN_PLAYHEAD_MAX_S, PLAYER_DEAD_MAX_S, CHECKS as _CS_CHECKS
import stuck

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
            // levi 00176 (2c): a live page's short DVR window is a finite duration too; duration_finite alone is no ad
            // (content.js isAdFrom), so nothing is skipped, fast-forwarded or muted on it
            if (sig.length === 0 || sig.every((s) => s === 'duration_finite')) {
                if (v && v.muted && v.dataset.lvqoeMutedForAd === '1') {
                    v.muted = false; delete v.dataset.lvqoeMutedForAd; return 'unmuted';
                }
                return null;
            }
            const tag = ' [' + sig.join('|') + ']';
            // levi 00176 (2b) / 14:35: SKIP_FUTILE_CLICKS skip clicks with the ad state and its countdown text unchanged
            // stop the clicking (skip_futile, logged every second); the re-entry is the existing ad_frozen clock's
            const cdEl = document.querySelector('.ytp-ad-duration-remaining, .ytp-ad-preview-text, .ytp-ad-text');
            const adKey = sig.join('|') + '#' + (cdEl ? cdEl.textContent : '');
            if (mp && mp.dataset.lvqoeAdKey !== adKey) { mp.dataset.lvqoeAdKey = adKey; mp.dataset.lvqoeFutile = '0'; }
            const futile = mp ? Number(mp.dataset.lvqoeFutile || 0) : 0;
            if (futile >= %(futile)d) return 'skip_futile' + tag;
            // a skip button can be visible while its countdown runs (clicks do nothing): after 2 clicks with the ad still
            // showing, fall through to fast-forward (laptop probe 2026-10-16)
            const tries = mp ? Number(mp.dataset.lvqoeSkipTries || 0) : 0;
            // t83 (11), W7 yt_100138 (levi 03:14): a fast-forward at readyState 1, 0.8 s after the ad's loadedmetadata,
            // left the element paused at its own end with yt never leaving the ad; never below readyState 2, and each
            // fast-forward's element state stashed for the log (dataset.lvqoeFf)
            const ffStash = () => { document.documentElement.dataset.lvqoeFf = JSON.stringify(
                {rs: v.readyState, ct: v.currentTime, dur: v.duration, paused: v.paused}); };
            const canFF = v && isFinite(v.duration) && v.duration > 0 && v.duration < %(max_dur)d && v.currentTime < v.duration - 0.25
                          && v.readyState >= 2;
            if (tries >= 2 && canFF) {
                ffStash();
                v.currentTime = v.duration; v.play().catch(() => {});
                if (mp) mp.dataset.lvqoeSkipTries = '0';
                return 'fast-forwarded-after-skip' + tag;
            }
            // 1. a visible skip button: known classes, then any button whose text / aria-label says skip
            const skipSel = ['.ytp-ad-skip-button', '.ytp-skip-ad-button', '.ytp-ad-skip-button-modern',
                             '.ytp-ad-skip-button-container button', 'button.ytp-ad-skip-button-modern'];
            for (const s of skipSel) {
                const b = document.querySelector(s);
                if (vis(b) && !b.disabled) { b.click(); if (mp) { mp.dataset.lvqoeSkipTries = String(tries + 1); mp.dataset.lvqoeFutile = String(futile + 1); } return 'skipped' + tag; }
            }
            // only buttons inside the player: the page has a "Skip navigation" a11y button that matches by text and
            // does nothing to the ad (laptop test 2026-10-16: it was clicked every second during a 15 s ad)
            for (const b of (mp ? mp.querySelectorAll('button') : [])) {
                if (!vis(b) || b.disabled) continue;
                const label = (b.textContent + ' ' + (b.getAttribute('aria-label') || '')).toLowerCase();
                if (label.includes('skip')) { b.click(); if (mp) { mp.dataset.lvqoeSkipTries = String(tries + 1); mp.dataset.lvqoeFutile = String(futile + 1); } return 'text-skipped' + tag; }
            }
            // 2. fast-forward a finite ad to its end
            if (v && isFinite(v.duration) && v.duration > 0 && v.duration < %(max_dur)d && v.readyState >= 2) {
                if (v.currentTime < v.duration - 0.25) {
                    ffStash();
                    v.currentTime = v.duration;
                    v.play().catch(() => {});
                    return 'fast-forwarded' + tag;
                }
            }
            // 3. mute and wait
            if (v && !v.muted) { v.muted = true; v.dataset.lvqoeMutedForAd = '1'; return 'muted-wait' + tag; }
            return 'waiting' + tag;
        })()
    """ % {"max_dur": 600, "futile": 10},
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
    # TikTok live has no pre-roll. levi 03:29 / batch #10: TikTok's own feed-navigation guide ("Scroll, use the up / down
    # keys, or click the arrows on the right to browse your feed." + a "Got it" button) covers the video in a session's
    # first shots (WA3 pinned rooms); dismissed by its own button, only when the guide's text is that guide's, at most
    # TT_GUIDE_MAX_TRIES clicks (then tt_onboarding_stuck, no more clicks); every action recorded (meta.json tt_onboarding)
    "tiktok": """
        (function() {
            const vis = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
            const btn = [...document.querySelectorAll('button, [role="button"]')]
                .find((b) => vis(b) && /^\\s*got it\\s*$/i.test(b.textContent || ''));
            if (!btn) return null;
            let host = btn;
            for (let i = 0; i < 6 && host.parentElement; i++) {
                host = host.parentElement;
                if (/browse your feed/i.test(host.textContent || '')) break;
            }
            const txt = (host.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
            if (!/browse your feed/i.test(txt)) return null;          // only the feed-navigation guide, never another dialog
            const d = document.documentElement.dataset;
            const tries = Number(d.lvqoeTtGuideTries || 0);
            if (tries >= %(max_tries)d) return 'tt_onboarding_stuck [' + txt + ']';
            d.lvqoeTtGuideTries = String(tries + 1);
            btn.click();
            return 'tt_onboarding_dismissed [' + txt + ']';
        })()
    """ % {"max_tries": 3},
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
    // the overlay is hidden by the CSS rule; when the container does not carry that data-a-target, the rows' box (two
    // levels up) is hidden inline. The result says whether it is hidden: 'on hidden' / 'on VISIBLE'
    const hidden = (row) => {
        if (!row) return 'toggled-no-rows';
        let box = row.closest('[data-a-target="player-overlay-video-stats"]');
        if (!box) { box = row.parentElement && row.parentElement.parentElement || row; box.style.opacity = '0'; }
        return getComputedStyle(box).opacity === '0' ? 'on hidden' : 'on VISIBLE';
    };
    const pre = document.querySelector('[data-a-target="player-overlay-video-stats-row"]');
    if (pre) return 'already-' + hidden(pre);
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
    return hidden(document.querySelector('[data-a-target="player-overlay-video-stats-row"]'));
})()
"""

# addendum M (a): the session's login state, from the page itself: a visible "Log in" / "Sign in" control against a
# profile / avatar marker (tt profile icon, yt avatar button, twitch user menu). Raw markers are kept in meta.json.
_LOGIN_STATE_JS = """
(function() {
    const vis = (el) => el && el.offsetParent !== null;
    const label = (el) => ((el.innerText || el.getAttribute('aria-label') || '') + '').trim();
    const loginBtn = [...document.querySelectorAll('button, a, [role="button"]')]
        .some(el => vis(el) && /^(log in|login|sign in)$/i.test(label(el)));
    const profile = !!document.querySelector('[data-e2e="profile-icon"], [data-e2e="nav-profile"], ' +
        '[data-e2e="top-avatar"], #avatar-btn, [data-a-target="user-menu-toggle"]');
    // tt live pages show a "Recharge" (coins) control only to a logged-in account (ti_7 read 'unknown' while logged in)
    const recharge = [...document.querySelectorAll('button, a, [role="button"], div, span')]
        .some(el => el.childElementCount === 0 && el.getClientRects().length && /^recharge$/i.test(label(el)));
    return JSON.stringify({login_button_visible: loginBtn, profile_marker: profile || recharge, tt_recharge: recharge});
})()
"""


def _pick_new_room_js(visited: list[str]) -> str:
    """tt room rotation: click the first discover-list room not visited yet (falls back to any room link)."""
    return """
(function(visited) {
    const sels = ['[data-e2e="discover-list-live-card"] a[href*="/live"]',
                  '[data-e2e="discover_category-list-live-card"] a[href*="/live"]', 'a[href*="/@"][href*="/live"]'];
    for (const s of sels) {
        for (const a of document.querySelectorAll(s)) {
            if (a.offsetParent === null) continue;
            const href = (a.href || '').split('?')[0];
            if (!href || visited.some(v => v.split('?')[0] === href)) continue;
            a.click();
            return href;
        }
    }
    return null;
})(%s)
""" % json.dumps(visited)


ROOM_ROTATION_LIVE_URL = "https://www.tiktok.com/live"

# tt room choice and dead-room handling (supervisor / shagun 2026-10-17: the picker took the ended @tape05.tv room in
# ti_1, ti_2 and ti_3). The choice: rooms of the last TT_RECENT_ROOMS_N tt sessions are excluded; cards with a LIVE badge
# and >= TT_MIN_VIEWERS viewers first, most viewers first; each tried room must play (src set, readyState >= 3, playhead
# advancing) within TT_QUICK_CONFIRM_S, then pass a TT_PREROLL_S pre-roll (media appended, playhead advancing, no dead
# signal) before the session clock and the shaper start; up to TT_MAX_TRIES rooms. Candidates, tries and the choice go
# to meta.json tt_room_choice. Mid-session a dead room aborts the session with reason room_ended (TTRoomWatch: the
# rendered ended banner, or no progress for the platform's T with idle interfaces, outside stalls and low states).
TT_RECENT_ROOMS_N = 10
TT_MIN_VIEWERS = 100
TT_MAX_TRIES = 5
TT_QUICK_CONFIRM_S = 15
TT_PREROLL_S = 60
TT_ENDED_FLAT_S = 5         # the ended banner counts only with no media appended and no playhead advance this long
TT_AUTOPLAY_GRACE_S = 5     # a room is judged only after play was clicked and this long has passed
TT_POOL_MIN, TT_POOL_SCROLLS = 30, 8   # scroll /live for more cards until this many candidates (supervisor: ti_10 had 7)
DISCOVERY_SETTLE_S = 2.0    # levi 07:48: after the discovery tab closed, before the extension storage is cleared
DISCOVERY_EMPTY_SHOT = "discovery_empty.png"   # the list page when it offered nothing (after one re-navigate)
# levi 07:48: the list page's resources as the page saw them (resource timing; a failed fetch reads responseStatus 0)
_PAGE_RESOURCES_JS = """
JSON.stringify((() => { const e = performance.getEntriesByType('resource');
  return {resources: e.length, status_0: e.filter(x => x.responseStatus === 0).length,
          status_ge_400: e.filter(x => x.responseStatus >= 400).length}; })())
"""


DISCOVERY_CLOSE_WAIT_S = 5.0   # brad / levi 08:18 (i): the discovery target must be gone from /json within this
DISCOVERY_VISIBLE_WAIT_S = 3.0   # brad / levi 08:38: the session page must read visibilityState "visible" within this


def _cdp_targets() -> list[dict]:
    with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
        return json.loads(r.read())


def _discovery_tab() -> tuple[tuple[str | None, str | None], "CDPPage"]:
    """A tab for the unshaped discovery in its OWN browser context (brad / levi 08:18 (iii): Target.createBrowserContext
    -> createTarget; its socket pools, cache and cookies are not the session's, so no connection opened during discovery
    carries into the shaped join, and our extension does not run there): ((context id, target id), page). The session
    page and its listeners never load the list page."""
    ctx = _browser_call("Target.createBrowserContext", {"disposeOnDetach": False}).get("browserContextId")
    tid = _browser_call("Target.createTarget", {"url": "about:blank", "browserContextId": ctx}).get("targetId")
    ws, deadline = None, time.time() + 5
    while ws is None and time.time() < deadline:
        ws = next((t.get("webSocketDebuggerUrl") for t in _cdp_targets() if t.get("id") == tid), None)
        if ws is None:
            time.sleep(0.25)
    if ws is None:
        _browser_call("Target.disposeBrowserContext", {"browserContextId": ctx})
        raise RuntimeError(f"discovery tab {tid} not listed in /json")
    return (ctx, tid), CDPPage(ws)


def _close_discovery_tab(ids, page) -> bool:
    """Close the discovery page, dispose its browser context (its sockets close with it), then VERIFY (brad / levi 08:18
    (i)): True once the target is gone from /json within DISCOVERY_CLOSE_WAIT_S, else False; never raises (the caller
    raises before any shaping)."""
    ctx, tid = ids
    try:
        page.close()
    except Exception:
        pass
    try:
        _browser_call("Target.disposeBrowserContext", {"browserContextId": ctx})
    except Exception as exc:
        print(f"[browser] discovery context dispose failed: {exc!r}", flush=True)
        _close_target(tid)
    deadline = time.time() + DISCOVERY_CLOSE_WAIT_S
    while time.time() < deadline:
        try:
            if not any(t.get("id") == tid for t in _cdp_targets()):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False

_TT_CANDIDATES_JS = """
(function() {
    const out = [], seen = new Set();
    for (const a of document.querySelectorAll('a[href*="/@"][href*="/live"]')) {
        if (a.offsetParent === null) continue;
        const href = (a.href || '').split('?')[0];
        if (!href || seen.has(href)) continue;
        seen.add(href);
        // the card: the largest ancestor that still links to this room only (a shared container mixes cards' text)
        const rooms = (el) => new Set([...el.querySelectorAll('a[href*="/@"][href*="/live"]')].map(x => (x.href || '').split('?')[0])).size;
        let card = a;
        while (card.parentElement && card.parentElement !== document.body && rooms(card.parentElement) <= 1) {
            card = card.parentElement;
        }
        out.push({href: href, text: (card.innerText || '').replace(/\\s+/g, ' ').slice(0, 240),
                  discover: !!a.closest('[data-e2e*="live-card"]')});
    }
    return JSON.stringify(out);
})()
"""

# the tt player's state for the dead-room rule; ended_text: the room-ended banner ("LIVE has ended"). Not "Recommended
# LIVE": live room pages show that section too (31ded33 rejected 10 of 10 live rooms on it, chaos ti_4 / ti_5)
_TT_PLAYER_STATE_JS = """
(function() {
    // the largest visible video (a feed page can hold several)
    const vs = [...document.querySelectorAll('video')].filter(x => x.offsetParent !== null);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0] || document.querySelector('video');
    const hit = (ph) => {
        const xp = "//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '" + ph + "')]";
        const r = document.evaluate(xp, document.body || document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
        // rendered text only: both live and ended pages carry the phrase in a <script> JSON blob (shagun's fixtures);
        // getClientRects, not offsetParent, so a banner inside a position: fixed overlay still counts
        for (let i = 0; i < r.snapshotLength; i++) {
            const e = r.snapshotItem(i).parentElement;
            if (e && !e.closest('script, style, template, noscript') && e.getClientRects().length) return true;
        }
        return false;
    };
    let buf = null;
    try { if (v && v.buffered.length) buf = v.buffered.end(v.buffered.length - 1); } catch (e) {}
    return JSON.stringify({has_video: !!v, src: !!(v && (v.currentSrc || v.src || v.srcObject)), rs: v ? v.readyState : null,
                           ct: v ? v.currentTime : null, paused: v ? v.paused : null, buf_end: buf,
                           decoded: v ? (v.webkitVideoDecodedByteCount || null) : null,
                           dur_inf: v ? v.duration === Infinity : null,
                           tf: v && v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames : null,
                           ended_text: hit('live has ended'),
                           // a visible loading indicator (heuristic, diagnostics only: tt's two-dot loader on a blank
                           // room after a re-entry, ti_33); never part of a decision
                           loading: [...document.querySelectorAll('[class*="oading"], [class*="pinner"], [role="progressbar"], [data-e2e*="loading"]')]
                               .some(e => e.getClientRects().length > 0),
                           stall_label: document.documentElement.dataset.lvqoeStall === '1'});
})()
"""


def _norm_room(u: str | None) -> str:
    return (u or "").split("?")[0].rstrip("/").lower()


_NUM = r"(\d[\d,]*(?:\.\d+)?)\s*([KkMm])?"


def _parse_viewers(text: str) -> int | None:
    """Viewer count from a card's text. Logged-in /live cards (chaos ti_10) put it next to "watching" / "viewers",
    right after the LIVE badge ("LIVE 2 have fun ...", "LIVE 1,228 Welcome ..."), or at the end ("LIVE <handle> <name>
    216", "... 9.5K"); else a K / M token anywhere."""
    def num(m):
        return int(float(m.group(1).replace(",", "")) * {"k": 1e3, "m": 1e6}.get((m.group(2) or "").lower(), 1))
    t = (text or "").strip()
    for rx in (_NUM + r"\s*(?:viewers?|watching)\b", r"^LIVE\s+" + _NUM + r"(?![\w.,])", r"(?<![\w.,])" + _NUM + r"$",
               r"(?<![\w.,])(\d+(?:\.\d+)?)([KkMm])(?![\w])"):
        m = re.search(rx, t)
        if m:
            return num(m)
    return None


# twitch (supervisor 2026-10-17: tw_7 and tw_8 both took caseoh_, the Just Chatting top card): a directory pick skips
# any channel of the last TW_RECENT_CHANNELS_N twitch sessions and takes the first card not excluded; the candidates and
# the pick go to meta.json tw_pick
TW_RECENT_CHANNELS_N = 10


def _norm_channel(u: str | None) -> str:
    return (u or "").split("?")[0].rstrip("/").lower()


def recent_tw_channels(session_dir: Path | None, n: int = TW_RECENT_CHANNELS_N) -> list[str]:
    """Channel URLs of the last n twitch sessions (by meta.json started_at) over both roots, single mode's and the
    trio's, whichever the session is in (t58 L-7: the gap 8a93881 closed for tt rooms)."""
    if session_dir is None:
        return []
    root = Path(session_dir).resolve().parent.parent
    data = root.parent if root.name.startswith("_trio") else root
    metas = []
    for d in [x for r in (data, data / "_trio") for x in list((r / "twitch").glob("*")) + list((r / "_failed" / "twitch").glob("*"))]:
        m = d / "meta.json"
        if d.resolve() == Path(session_dir).resolve() or not m.exists():
            continue
        try:
            meta = json.loads(m.read_text())
        except Exception:
            continue
        metas.append((meta.get("started_at") or m.stat().st_mtime, meta.get("channel_url")))
    recent = sorted(metas, key=lambda x: float(x[0]) if isinstance(x[0], (int, float)) else 0.0)[-n:]
    return sorted({_norm_channel(u) for _, u in recent if u and "twitch.tv/" in u and "/directory" not in u})


def _pick_twitch_js(exclude: list[str], click: bool = True) -> str:
    """Click the directory's first live card whose channel is not in `exclude`; returns {pick, candidates} as JSON.
    click=False (levi 07:48, the unshaped discovery): read only; the channel is opened later by its URL."""
    return """
(function(exclude, click) {
    const norm = (h) => (h || '').split('?')[0].replace(/\\/$/, '').toLowerCase();
    const links = [...document.querySelectorAll('a[data-a-target="preview-card-image-link"], a[data-a-target="preview-card-title-link"]')]
        .filter(a => a.offsetParent !== null && !/\\/(directory|videos|clip)\\//.test(a.href || ''));
    const cands = [...new Set(links.map(a => norm(a.href)))].filter(Boolean);
    const pick = cands.find(h => !exclude.includes(h)) || null;
    if (pick && click) links.find(a => norm(a.href) === pick).click();
    return JSON.stringify({pick: pick, candidates: cands.slice(0, 20)});
})(%s, %s)
""" % (json.dumps(exclude), json.dumps(bool(click)))


# t56 (levi on peter's t37 facts): siblings never play the same yt channel, twitch channel or tt room at once. Each slot's
# landed stream goes to the run's slots log ("stream" events; exp.py sets LVDC_SLOT_TAG, orchestrate LVDC_TRIO_RUN);
# a refill's twitch channel / tt room choice excludes the streams its running siblings hold, recorded in the pick
def _slots_log(session_dir: Path | None) -> Path | None:
    if session_dir is None or not os.environ.get("LVDC_TRIO_RUN") or not os.environ.get("LVDC_SLOT_TAG"):
        return None
    return Path(session_dir).resolve().parent.parent / "_summaries" / "trio_slots.jsonl"


def publish_stream(session_dir: Path | None, platform: str, url: str | None, event: str = "stream") -> None:
    """This slot's stream to the slots log (best effort; never raises): "stream" once landed, "intent" when picked, before
    the confirm / preroll (t58 N-2, supervisor: a sibling refilling during that time read nothing and could take the same
    room). A later intent or stream of the slot's session replaces it."""
    log = _slots_log(session_dir)
    if log is None or not url:
        return
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as fh:
            fh.write(json.dumps({"event": event, "slot": os.environ["LVDC_SLOT_TAG"], "session": Path(session_dir).name,
                                 "platform": platform, "url": _norm_room(url), "run": os.environ["LVDC_TRIO_RUN"],
                                 "t": time.time()}) + "\n")
    except Exception:
        pass


def sibling_streams(session_dir: Path | None, platform: str, log: Path | None = None, run: str | None = None,
                    me: str | None = None) -> list[str]:
    """The streams (normalised URLs) of `platform` that this run's other slots hold now: each slot's latest session
    (a start without a later end / exit for that slot) and its latest stream or intent event (a pick claims its stream
    before the confirm / preroll: t58 N-2). run / me default to this process's LVDC_TRIO_RUN / LVDC_SLOT_TAG (orchestrate
    passes them for the slot it is about to fill)."""
    log = log or _slots_log(session_dir)
    if log is None or not log.exists():
        return []
    run, me = run or os.environ.get("LVDC_TRIO_RUN"), me or os.environ.get("LVDC_SLOT_TAG")
    cur: dict = {}                       # slot -> [session, platform, url] while running
    for line in log.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        s = e.get("slot")
        if e.get("event") == "start" and e.get("run") == run:
            cur[s] = [e.get("session"), e.get("platform"), None]
        elif e.get("event") in ("end", "exit"):
            cur.pop(s, None)
        elif e.get("event") in ("stream", "intent") and e.get("run") == run and s in cur and cur[s][0] == e.get("session"):
            cur[s][2] = e.get("url")
    return sorted({u for s, (_, pl, u) in cur.items() if s != me and pl == platform and u})


def recent_tt_rooms(session_dir: Path | None, n: int = TT_RECENT_ROOMS_N) -> list[str]:
    """Rooms of the last n tt sessions (by meta.json start time), normalised, over both roots: single mode's
    (data/tiktok, data/_failed/tiktok) and the trio's (data/_trio/...), whichever root the session is in (t56,
    supervisor 2026-10-19: every tt room repeat so far crossed modes, each mode having read only its own root)."""
    if session_dir is None:
        return []
    root = Path(session_dir).resolve().parent.parent
    data = root.parent if root.name.startswith("_trio") else root
    metas = []
    for d in [x for r in (data, data / "_trio") for x in list((r / "tiktok").glob("*")) + list((r / "_failed" / "tiktok").glob("*"))]:
        m = d / "meta.json"
        if d.resolve() == Path(session_dir).resolve() or not m.exists():
            continue
        try:
            meta = json.loads(m.read_text())
        except Exception:
            continue
        rooms = [meta.get("channel_url")] + [r.get("room") for r in (meta.get("room_rotation") or {}).get("rooms") or []]
        rooms += [t.get("room") for t in (meta.get("tt_room_choice") or {}).get("tries") or []]
        metas.append((meta.get("started_at") or m.stat().st_mtime, rooms))
    out: list[str] = []
    for _, rooms in sorted(metas, key=lambda x: float(x[0]) if isinstance(x[0], (int, float)) else 0.0)[-n:]:
        out += [_norm_room(r) for r in rooms if r and "/@" in r]
    return sorted(set(out))


TT_STALL_GRACE_S = 30          # src drops within this long after a stall label are the player reconnecting
TT_RX_IDLE_BYTES = 60_000      # "no media bytes": less than this received over the last TT_RX_WINDOW_S
TT_RX_WINDOW_S = 60
WATCH_LOG_MAX = 500            # player_dead_watch: at most this many low spans / dead clocks per session (record only)
TT_LOW_STATES = {1, 2, 3, 4}   # shaper states in which a dead-looking player is the shaping, never a dead room


# player_dead diagnostics (supervisor / shagun 2026-10-18, ti_29 / ti_30 / ti_32): the shaped downstream over the dead
# run, read on the ingress IFB's tx counter (post-shaping; _rx_bytes below sums every interface, so it counts the
# downstream about twice: wlp0 rx + ifb0 rx + ifb1). At the abort, a dead run whose shaped downstream is at least
# PLAYER_DEAD_ALIVE_SHARE of the shaper rate had the stream still arriving ("alive": the player stopped consuming a
# live stream; ti_32: about 0.9 of the shaper rate, the media flow at full rate, the element's 'end' 2 s before the
# run), else "no_media" (ti_21: about 0.13). Recorded in meta.json player_dead.dead_window; detection is unchanged.
# The reference is the smaller of the shaper rate and the shaped downstream while the player was still playing, over
# the PLAYING_REF_S before the dead clock (supervisor 2026-10-18, peter's ti_43 read): at state 8 (3636 kbit) tt's
# hd5 stream arrived at about 2 Mbit/s through the dead run (pcap) and the share of the shaper rate alone read no_media.
SHAPED_IFACE = os.environ.get("LVDC_IFB_INGRESS", "ifb0")
# t58 L-1 (brad's audit): in a trio / N-slot session exp.py sets SHAPED_IFACE to the slot's own ingress IFB (lvi_<tag>)
# and RX_OWN_IFB, so tt's room-end rule (b) reads the slot's own downstream bytes; summing every interface (siblings'
# streams included) meant its idle test never held in a trio, and ifb0 does not exist there (make disable). Single mode
# unchanged: every interface, as before
RX_OWN_IFB = False


def room_rx_source() -> str:
    """The bytes tt's room-end rule (b) reads (meta.json room_rx_source): the slot's own ingress IFB, or every
    interface (single mode)."""
    return SHAPED_IFACE if RX_OWN_IFB else "all_interfaces"
PLAYER_DEAD_ALIVE_SHARE = 0.5
PLAYING_REF_S = 30.0              # the pre-dead window whose shaped downstream is the stream's own rate
PLAYING_REF_MIN_PROGRESS = 0.8    # ... counted only when the player progressed on this share of its ticks


# recovery (supervisor ruling 2026-10-18): on player_dead, ONE re-entry of the same room, recorded as an entry (room
# rotation's rooms list: reason player_dead, the dead_window attached); the dead interval stays labelled stall, as a
# viewer saw it, and the session continues. A second death ends the session as before. A re-entry that lands on the
# ended banner takes the picker's ended rule (room_ended: the host ended), which separates that from a player death
# without a page probe. No reload anywhere else (stalls, ads, buffering).
PLAYER_DEAD_REENTRIES = 1
# the re-entry's playback confirmation window (supervisor 2026-10-18, ti_33: the room page took about 8 s to load and the
# new player sat at readyState 0 through the 15 s quick window, a spinner on a blank page): 60 s, the ended banner still
# ending it early; each sample's readyState / video / loading indicator is recorded
TT_REENTRY_CONFIRM_S = 60


def dead_action(reentries_done: int) -> str:
    """'reenter' while re-entries remain, else 'abort'."""
    return "reenter" if reentries_done < PLAYER_DEAD_REENTRIES else "abort"


def reentry_outcome(ok: bool, detail: dict | None) -> str:
    """After a re-entry: 'continue' when playback is confirmed; 'room_ended' when the room shows the ended banner (the
    picker's ended rule); else 'player_dead' (the player did not come back: the session ends as before)."""
    if ok:
        return "continue"
    d = detail or {}
    return "room_ended" if d.get("dead") == "ended_page" or d.get("ended_text") else "player_dead"


def _shaped_rx_bytes() -> int | None:
    try:
        return int((Path("/sys/class/net") / SHAPED_IFACE / "statistics" / "tx_bytes").read_text())
    except (OSError, ValueError):
        return None


def dead_window(srx: list[tuple[float, int]], shaper_kbit: float | None, playing_kbps: float | None = None) -> dict:
    """The shaped downstream over the window in kbit/s, its share of the shaper rate, and the stream verdict: "alive"
    when it is >= PLAYER_DEAD_ALIVE_SHARE of the reference, min(shaper rate, the pre-dead playing rate) (only the shaper
    rate when the playing rate is unknown)."""
    kbps = ((srx[-1][1] - srx[0][1]) * 8 / 1000 / (srx[-1][0] - srx[0][0])
            if len(srx) >= 2 and srx[-1][0] > srx[0][0] else None)
    share = kbps / shaper_kbit if kbps is not None and shaper_kbit else None
    refs = [x for x in (shaper_kbit, playing_kbps) if x]
    ref = min(refs) if refs else None
    share_ref = kbps / ref if kbps is not None and ref else None
    return {"shaped_rx_kbps": None if kbps is None else round(kbps, 1), "shaper_kbit": shaper_kbit,
            "share_of_shaper": None if share is None else round(share, 3),
            "playing_rx_kbps": None if playing_kbps is None else round(playing_kbps, 1),
            "reference_kbit": None if ref is None else round(ref, 1),
            "share_of_reference": None if share_ref is None else round(share_ref, 3),
            "stream": None if share_ref is None else ("alive" if share_ref >= PLAYER_DEAD_ALIVE_SHARE else "no_media")}


def _rx_bytes() -> int | None:
    """Bytes received on every non-loopback interface (Linux /sys; None elsewhere): the media-bytes signal tt's
    MediaStream player does not expose."""
    total, seen = 0, False
    for f in Path("/sys/class/net").glob("*/statistics/rx_bytes"):
        if f.parts[-3] == "lo":
            continue
        try:
            total += int(f.read_text())
            seen = True
        except (OSError, ValueError):
            pass
    return total if seen else None


def reentry_confirm_of(r: dict | None) -> dict:
    """The re-entry's own confirmation beside room_ended's pre-re-entry sample (boundary bundle item 4; supervisor
    2026-10-21 after ti_100037, whose room_ended.sample read ended_text False while the reloaded page read True): the
    navigation / entry times, the confirm reading (dead, rs, ct, ended_text), its samples and outcome."""
    r = r or {}
    return {k: r.get(k) for k in ("t_nav", "t_enter", "confirm", "samples", "samples_no_video", "outcome", "room")}


class TTRoomWatch:
    """The tt dead-room rule over 1 s player samples (_TT_PLAYER_STATE_JS) (supervisor 2026-10-17, after ti_12 / 13 / 14:
    under shaping tt tears its player down and reconnects for 10-15 s right after a stall, and a fast src_gone cut
    exactly the impaired sessions). A room has ended only when
      (a) the rendered "LIVE has ended" banner shows with no player or no progress for TT_ENDED_FLAT_S, or
      (b) there has been no progress (no source, or neither playhead nor media moving) for the platform's T (addendum N,
          FROZEN_PLAYHEAD_MAX_S) and the interfaces received under TT_RX_IDLE_BYTES over the last TT_RX_WINDOW_S;
    (b)'s clock does not run while the stall label is on, within TT_STALL_GRACE_S after it, or in shaper states 1-4.
    A source drop that comes back is recorded as a discontinuity (meta.json room_discontinuities), not a cut.
    Separately, player_dead (supervisor 2026-10-17, ti_20 / ti_21: 0x0, readyState 0, playhead 0 under the stall label
    for 8-12 min, media not arriving): readyState 0 (or no element) with no media appended or decoded for
    PLAYER_DEAD_MAX_S["tiktok"] -> "player_dead"; a reconnecting player gets its first bytes within seconds (old data:
    every 0x0 run that recovered was <= 20 s); one that recovers inside the limit is a discontinuity. Its clock holds in
    TT_LOW_STATES and for TT_STALL_GRACE_S after leaving them (levi 2026-10-21, t58 w2: below tt's lowest rendition the
    player sits at readyState 0 under the shaping)."""

    def __init__(self, t_max_s: float | None = None, log: dict | None = None, scale: float = 1.0) -> None:
        # record only (levi 09:54, the end-of-day-2 tip): the hold's low spans and each dead clock as THIS watch saw
        # them, so the reader can match the collector exactly (meta.json player_dead_watch; shared across re-entries)
        self.log = log if log is not None else {"low_spans": [], "dead_clocks": []}
        # B9 (levi 23:48): during the join the dead signals' clocks (player_dead's readyState-0, rule (b)'s no-progress)
        # scale by the join's scale, so a slow low-state join is not read as a dead room (selection by first state);
        # the ended banner stays as is; after the join (scale 1) as before
        self.scale = max(1.0, float(scale))
        self.t_max_s = (t_max_s if t_max_s is not None else FROZEN_PLAYHEAD_MAX_S.get("tiktok", 720)) * self.scale
        self.t_dead_s = PLAYER_DEAD_MAX_S.get("tiktok", 60) * self.scale
        self.dead_since: float | None = None          # player_dead clock
        self.last_low: float | None = None            # the last sample in TT_LOW_STATES (the player_dead hold)
        self.last_media: tuple | None = None
        self.flat_since: float | None = None          # banner rule: no progress since
        self.no_progress_since: float | None = None   # rule (b) clock
        self.last: tuple | None = None
        self.last_stall: float | None = None
        self.gone_since: float | None = None
        self.gone_ctx: dict = {}
        self.discontinuities: list[dict] = []
        self.rx: list[tuple[float, int]] = []
        self.srx: list[tuple[float, int]] = []        # shaped downstream (diagnostics only)
        self.hist: list[tuple[float, int, bool]] = []  # (t, shaped downstream, progressed) for playing_kbps (diagnostics)

    def playing_kbps(self, until: float | None) -> float | None:
        """The shaped downstream in kbit/s over the PLAYING_REF_S before `until` (the dead clock's start), when the
        player progressed on >= PLAYING_REF_MIN_PROGRESS of those ticks and they span >= 2/3 of it; else None."""
        if until is None:
            return None
        win = [x for x in self.hist if until - PLAYING_REF_S <= x[0] <= until]
        if len(win) < 2 or win[-1][0] - win[0][0] < PLAYING_REF_S * 2 / 3:
            return None
        if sum(1 for x in win if x[2]) < PLAYING_REF_MIN_PROGRESS * len(win):
            return None
        return (win[-1][1] - win[0][1]) * 8 / 1000 / (win[-1][0] - win[0][0])

    def close_open(self, now: float, how: str) -> None:
        """levi 03:51 (ti_100084, record only): a running dead clock's log entry closed when this watch stops counting
        for another reason than media back (reset) or T (fired): "replaced" (the loop rebuilds the watch: a room grace,
        a re-entry) or "session_end"; before, such entries stayed end None."""
        if self.dead_since is not None and self.log["dead_clocks"] and self.log["dead_clocks"][-1].get("end") is None:
            self.log["dead_clocks"][-1].update(end=round(now, 3), how=how)
        self.dead_since = None

    def feed(self, s: dict | None, now: float, shaper_state: int | None = None, rx_bytes: int | None = None,
             shaped_rx_bytes: int | None = None, lever_active: bool = False) -> str | None:
        s = s or {}
        # v3 B4 (levi 21:12): a lever episode holds like TT_LOW_STATES (the rate is below the lowest rung whatever the
        # scheduled state), with the same grace after it
        low = shaper_state in TT_LOW_STATES or bool(lever_active)
        if shaped_rx_bytes is not None:
            self.srx.append((now, shaped_rx_bytes))
            self.srx = [x for x in self.srx if now - x[0] <= TT_RX_WINDOW_S]
        if s.get("stall_label"):
            self.last_stall = now
        if rx_bytes is not None:
            self.rx.append((now, rx_bytes))
            self.rx = [x for x in self.rx if now - x[0] <= TT_RX_WINDOW_S]
        no_src = not s.get("has_video") or not s.get("src") or s.get("rs") == 0
        cur = (s.get("buf_end"), s.get("ct"), s.get("decoded"))
        progressed = not no_src and self.last is not None and cur != self.last
        if shaped_rx_bytes is not None:
            self.hist.append((now, shaped_rx_bytes, progressed))
            self.hist = [x for x in self.hist if now - x[0] <= TT_RX_WINDOW_S + 2 * PLAYING_REF_S]
        if no_src:
            if self.gone_since is None:
                self.gone_since = now
                self.gone_ctx = {"stall_recent": self.last_stall is not None and now - self.last_stall <= TT_STALL_GRACE_S,
                                 "shaper_state": shaper_state}
        elif self.gone_since is not None and progressed:
            self.discontinuities.append({"start": self.gone_since, "end": now, "duration_s": round(now - self.gone_since, 1),
                                         **self.gone_ctx})
            self.gone_since = None
        if not no_src:
            self.last = cur
        # player_dead: readyState 0 (or no element) and no media appended / decoded since the last sample
        media = (s.get("buf_end"), s.get("decoded"))
        dead_now = (not s.get("has_video") or s.get("rs") == 0) and media == self.last_media
        self.last_media = media
        # levi 2026-10-21 (t58 w2 stop-and-read, peter 00268): in TT_LOW_STATES the rate is below tt's lowest rendition and
        # the player sits at readyState 0 while bytes arrive at the shaped rate: the shaping, never a dead page. The
        # player_dead clock holds there and for TT_STALL_GRACE_S after leaving them, as rule (b); a real page death at a
        # low state is caught once the state rises
        if low:
            self.last_low = now
            spans = self.log["low_spans"]
            if spans and now - spans[-1][1] <= 2.0:
                spans[-1][1] = round(now, 3)
            elif len(spans) < WATCH_LOG_MAX:
                spans.append([round(now, 3), round(now, 3)])
        if self.last_low is not None and now - self.last_low <= TT_STALL_GRACE_S:
            dead_now = False
        if not dead_now:
            if self.dead_since is not None and self.log["dead_clocks"] and self.log["dead_clocks"][-1].get("end") is None:
                self.log["dead_clocks"][-1].update(end=round(now, 3), how="reset")
            self.dead_since = None
        elif self.dead_since is None:
            self.dead_since = now
            if len(self.log["dead_clocks"]) < WATCH_LOG_MAX:
                self.log["dead_clocks"].append({"start": round(now, 3), "end": None, "how": None})
        elif now - self.dead_since >= self.t_dead_s:
            if self.log["dead_clocks"] and self.log["dead_clocks"][-1].get("end") is None:
                self.log["dead_clocks"][-1].update(end=round(now, 3), how="fired")
            return "player_dead"
        # (a) the banner with no player or no progress
        if progressed:
            self.flat_since = None
        elif self.flat_since is None:
            self.flat_since = now
        if s.get("ended_text") and (no_src or not progressed) and now - self.flat_since >= TT_ENDED_FLAT_S:
            return "ended_page"
        # (b) no progress for T with idle interfaces, outside stalls / their grace / low shaper states
        exempt = (s.get("stall_label") or (self.last_stall is not None and now - self.last_stall <= TT_STALL_GRACE_S)
                  or low)
        if progressed or exempt:
            self.no_progress_since = None
            return None
        if self.no_progress_since is None:
            self.no_progress_since = now
        rx_idle = (len(self.rx) >= 2 and self.rx[-1][1] - self.rx[0][1] < TT_RX_IDLE_BYTES) if self.rx else True
        if now - self.no_progress_since >= self.t_max_s and rx_idle:
            return "no_progress"
        return None


def _tt_state(page: "CDPPage") -> dict:
    try:
        return json.loads(page.eval(_TT_PLAYER_STATE_JS) or "{}")
    except Exception:
        return {}


def _tt_confirm_playing(page: "CDPPage", seconds: float, trace: list | None = None,
                        scale: float = 1.0) -> tuple[bool, dict]:
    """src set, readyState >= 3 and the playhead advancing >= 2 s within `seconds` (no dead signal; its clocks scaled by
    `scale` during the B9 join). With `trace`, one (t, rs, has_video, loading) per sample is appended (re-entry
    diagnostics). Every outcome records watched_s, ct_advance_s, the frames gate and ct_trace (levi 12:19 (g), WC3c
    ti_100003: its failed try had none); a timeout also records showed_frames (the playhead moved, with a decoded frame
    when the counter is readable; a frozen first frame is not), which the B9 join reads as a slow start rather than no
    first frame (levi 12:18 (b))."""
    w, first_ct, s, fg = TTRoomWatch(scale=scale), None, {}, _FramesGate()
    ev0 = None                                        # B9 D3: the first sample's media counters (page-side bytes)
    t0, ct_trace = time.time(), []
    page.eval(_CLICK_PLAY_JS)                         # autoplay may not have started: click, then give it the grace
    time.sleep(TT_AUTOPLAY_GRACE_S)

    def measured() -> dict:
        adv = 0.0 if first_ct is None or s.get("ct") is None else max(0.0, s["ct"] - first_ct)
        return {**fg.record(), "watched_s": round(time.time() - t0, 1), "ct_advance_s": round(adv, 3),
                "ct_trace": ct_trace}
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.eval(_CLICK_PLAY_JS)
        s = _tt_state(page)
        ev0 = ev0 if ev0 is not None else {"buf_end": s.get("buf_end"), "decoded": s.get("decoded")}
        ct_trace.append((round(time.time() - t0, 1), s.get("ct"), s.get("rs"), s.get("tf")))
        if trace is not None:
            trace.append((round(time.time(), 2), s.get("rs"), bool(s.get("has_video")), bool(s.get("loading"))))
        dead = w.feed(s, time.time())
        if dead:
            return False, {"dead": dead, **s, **measured()}
        if s.get("has_video") and (s.get("rs") or 0) >= 3 and s.get("ct") is not None:
            first_ct = s["ct"] if first_ct is None else first_ct
            fg.feed(s.get("tf"))                      # WA2 r1: the clock alone confirmed a re-entry that showed no frame
            if s["ct"] - first_ct >= 2.0 and fg.ok():
                return True, {**s, **measured()}
        time.sleep(1)
    m = measured()
    showed = m["ct_advance_s"] > 0 and ((fg.last or 0) > 0 if fg.seen else True)     # brad 734f961d: frames AND ct
    return False, {"dead": "not_playing", **s, **m, "showed_frames": showed, "ev0": ev0 or {}}


TT_PREROLL_MIN_ADV_S = 1.0     # levi 06:40: the pre-roll's acceptance, identical at every state and throughput-free


def _tt_preroll(page: "CDPPage", seconds: float, scale: float = 1.0, cancel=None,
                until: float | None = None) -> tuple[bool, dict]:
    """The pre-roll: no dead signal for `seconds` (its clocks scaled by `scale` during the B9 join), and the playhead
    advancing >= TT_PREROLL_MIN_ADV_S within the scaled pre-roll (levi 06:40, WC3 r1 ti_100001: the old test, advance >=
    seconds / 2, is throughput-dependent on a shaped link, and skipped 4 live rooms at 204 kbit for the lowest-viewer
    one). A room still short of it after `seconds` waits up to seconds x scale for its first second; `until` (the join
    walk's cap) clips both, so the join cap is authoritative (brad / levi 06:45), and `cancel` stops it. The advance share
    (ct_advance_s / the seconds watched) is recorded as a covariate, never a filter."""
    w, s, adv, prev, fg = TTRoomWatch(scale=scale), {}, 0.0, _tt_state(page).get("ct"), _FramesGate()
    t0 = time.time()
    end = t0 + seconds
    longest = t0 + seconds * max(1.0, scale)
    if until is not None:                             # the join cap is authoritative: no overrun past it
        longest = min(longest, until)
        end = min(end, longest)
    while True:
        now = time.time()
        if now >= longest or (now >= end and adv >= TT_PREROLL_MIN_ADV_S) or (cancel is not None and cancel.is_set()):
            break
        s = _tt_state(page)
        fg.feed(s.get("tf"))
        dead = w.feed(s, time.time())
        if dead:
            return False, {"dead": dead, **s, "ct_advance_s": adv, "watched_s": round(time.time() - t0, 1),
                           **fg.record()}
        ct = s.get("ct")
        if ct is not None and prev is not None and ct > prev:
            adv += ct - prev                          # cumulative: a player reconnect restarts the playhead at 0
        prev = ct if ct is not None else prev
        time.sleep(1)
    watched = max(1e-9, time.time() - t0)
    ok = adv >= TT_PREROLL_MIN_ADV_S
    return ok, {"ct_advance_s": adv, "watched_s": round(watched, 1), "advance_share": round(adv / watched, 4),
                "extended": watched > seconds + 1.5, "buf_end": s.get("buf_end"), "decoded": s.get("decoded"),
                **fg.record(), **({} if ok else {"dead": "preroll_no_advance"})}


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


def _next_window_dim(samples: list[tuple[int, int]], target: int, cur_win: int) -> int:
    """Next window size along one axis from (window, player) samples: interpolate between the closest samples on
    either side of the target when it is bracketed (the player jumps at layout breakpoints, so proportional steps
    oscillate: laptop run9, tt 481 -> 593 -> 492 -> 581), else a proportional step from the latest sample."""
    below = [s for s in samples if s[1] and s[1] < target]
    above = [s for s in samples if s[1] and s[1] > target]
    if below and above:
        (w0, p0), (w1, p1) = max(below, key=lambda s: s[1]), min(above, key=lambda s: s[1])
        if p1 != p0 and w1 != w0:
            nxt = int(round(w0 + (target - p0) * (w1 - w0) / (p1 - p0)))
            if nxt in (w0, w1):
                nxt = (w0 + w1) // 2
            return nxt
    w, pl = samples[-1]
    return int(round(w + (target - pl) * w / max(pl, 1) * 0.9))


# t53 trio layout "fb" (design §4): each slot's window sits at its own origin on an X framebuffer enlarged beyond the
# monitor (xrandr --fb), so X11 crops of the three never overlap. Single mode: (0, 0) and None, behaviour unchanged.
WINDOW_ORIGIN: tuple[int, int] = (0, 0)
X_SCREEN_PX: tuple[int, int] | None = None     # the X root's size when it is larger than the page's screen


def pin_player(page: "CDPPage", target: tuple[int, int], target_id: str | None, iters: int = 8) -> dict:
    """Resize the browser window until the <video> client size is within PIN_TOLERANCE of `target`. Ends on the
    closest window seen when the target is not reached (screen- or layout-bound). Returns the trace for meta.json;
    never raises."""
    trace = {"mode": "player", "target": list(target), "steps": [], "ok": False}
    try:
        win = _browser_call("Browser.getWindowForTarget", {"targetId": target_id} if target_id else {})
        wid, bounds = win.get("windowId"), dict(win.get("bounds") or {})
        if wid is None:
            trace["error"] = "no window id"
            return trace
        _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        if WINDOW_ORIGIN != (0, 0):         # trio "fb" layout: the slot's own origin (single mode never moves it)
            _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": WINDOW_ORIGIN[0],
                                                                                   "top": WINDOW_ORIGIN[1]}})
            trace["origin"] = list(WINDOW_ORIGIN)
        scr = json.loads(page.eval("JSON.stringify([screen.availWidth, screen.availHeight])") or "[0, 0]")
        trace["screen_avail"] = scr
        sw, sh = [], []
        for _ in range(iters):
            ps = page.eval(_PLAYER_SIZE_JS)
            pw, ph = json.loads(ps) if ps else (0, 0)
            trace["steps"].append({"window": [bounds.get("width"), bounds.get("height")], "player": [pw, ph]})
            if pw and ph and abs(pw - target[0]) <= PIN_TOLERANCE * target[0] and abs(ph - target[1]) <= PIN_TOLERANCE * target[1]:
                trace["ok"] = True
                break
            if not (pw and ph and bounds.get("width") and bounds.get("height")):
                break
            sw.append((bounds["width"], pw))
            sh.append((bounds["height"], ph))
            nw, nh = _next_window_dim(sw, target[0], bounds["width"]), _next_window_dim(sh, target[1], bounds["height"])
            if scr[0] and scr[1]:                 # a window larger than the screen is not honoured; do not chase it
                nw, nh = min(nw, scr[0]), min(nh, scr[1])
            nb = {"width": max(400, nw), "height": max(300, nh)}
            if [nb["width"], nb["height"]] in [st["window"] for st in trace["steps"]]:
                trace["error"] = "no further window size to try (layout- or screen-bound)"
                break
            bounds = nb
            _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": bounds})
            time.sleep(1.5)                       # the page re-lays out the player
        if not trace["ok"] and trace["steps"]:
            # end on the closest window seen
            def err(st):
                return abs(st["player"][0] - target[0]) / target[0] + abs(st["player"][1] - target[1]) / target[1]
            best = min((st for st in trace["steps"] if st["player"][0]), key=err, default=None)
            if best and best["window"] != [bounds.get("width"), bounds.get("height")]:
                _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"width": best["window"][0],
                                                                                       "height": best["window"][1]}})
                time.sleep(1.5)
                ps = page.eval(_PLAYER_SIZE_JS)
                trace["steps"].append({"window": best["window"], "player": json.loads(ps) if ps else None, "best": True})
        trace["final_player"] = trace["steps"][-1]["player"] if trace["steps"] else None
        trace["final_window"] = trace["steps"][-1]["window"] if trace["steps"] else None
        # screen-bound (tw_6, supervisor 2026-10-17): the drawn size is larger than this screen can realise (the window
        # sits at the screen's available size and the player is still smaller). The target is clamped to the largest
        # player the screen gives, and the clamp is recorded: ok then means the clamped target was reached, and ok
        # False is left for a real failure (layout-bound short of the screen, no player)
        fw, fp = trace["final_window"], trace["final_player"]
        if (not trace["ok"] and fw and fp and fp[0] and scr[0] and scr[1]
                and (fw[0] >= scr[0] or fw[1] >= scr[1]) and fp[0] < target[0] and fp[1] < target[1]):
            trace.update(ok=True, clamped=True, clamped_to=list(fp), drawn=list(target), clamp_bound="screen")
    except Exception as exc:
        trace["error"] = repr(exc)
    return trace


def size_window(page: "CDPPage", target: tuple[int, int], target_id: str | None) -> dict:
    """geometry window:WxH (t37): set the browser window to W x H (clamped to the screen; a larger window is not
    honoured); the player takes what the layout gives it. Returns the trace for meta.json; never raises."""
    trace = {"mode": "window", "target": list(target), "ok": False}
    try:
        win = _browser_call("Browser.getWindowForTarget", {"targetId": target_id} if target_id else {})
        wid = win.get("windowId")
        if wid is None:
            trace["error"] = "no window id"
            return trace
        scr = json.loads(page.eval("JSON.stringify([screen.availWidth, screen.availHeight])") or "[0, 0]")
        w, h = target
        if scr[0] and scr[1]:
            w, h = min(w, scr[0]), min(h, scr[1])
        _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "normal"}})
        _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": WINDOW_ORIGIN[0], "top": WINDOW_ORIGIN[1],
                                                                               "width": w, "height": h}})
        time.sleep(1.5)
        got = _browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds") or {}
        ps = page.eval(_PLAYER_SIZE_JS)
        trace.update(screen_avail=scr, final_window=[got.get("width"), got.get("height")],
                     final_player=json.loads(ps) if ps else None,
                     clamped=[w, h] != list(target),
                     ok=abs((got.get("width") or 0) - target[0]) <= PIN_TOLERANCE * target[0]
                        and abs((got.get("height") or 0) - target[1]) <= PIN_TOLERANCE * target[1])
    except Exception as exc:
        trace["error"] = repr(exc)
    return trace


# yt theater mode (geometry theater:WxH): the player's own size button, once, when the page is not in theater already;
# the result and the player size after it are recorded (meta.json player_pin.theater). Layout only, never quality.
_YT_THEATER_JS = """
(function() {
    const f = document.querySelector('ytd-watch-flexy');
    if (!f) return JSON.stringify({action: 'no-flexy'});
    let action = 'already';
    if (!f.hasAttribute('theater')) {
        const b = document.querySelector('.ytp-size-button');
        if (!b) return JSON.stringify({action: 'no-button'});
        b.click();
        action = 'clicked';
    }
    return JSON.stringify({action});
})()
"""
_YT_THEATER_STATE_JS = """
(function() {
    const f = document.querySelector('ytd-watch-flexy');
    const v = document.querySelector('#movie_player video');
    return JSON.stringify({on: !!(f && f.hasAttribute('theater')), player: v ? [v.clientWidth, v.clientHeight] : null,
                           dpr: window.devicePixelRatio || 1});
})()
"""


def yt_theater(page: "CDPPage", settle_s: float = 1.5) -> dict:
    """Switch yt to theater mode and read back the state; never raises."""
    try:
        rec = json.loads(page.eval(_YT_THEATER_JS) or "{}")
        time.sleep(settle_s)
        rec.update(json.loads(page.eval(_YT_THEATER_STATE_JS) or "{}"))
        p, dpr = rec.get("player"), rec.get("dpr") or 1
        rec["player_device_px_h"] = round(p[1] * dpr) if p else None
        return rec
    except Exception as exc:
        return {"action": "error", "error": repr(exc)}


# yt fullscreen (geometry fullscreen:WxH; supervisor t37, 2026-10-18: the DPR route cannot pass 720 device px on a
# 1920x1080 panel): the player's own fullscreen, entered by the `f` key as a CDP key event (a user gesture; a script
# click cannot enter fullscreen), else by a CDP mouse click on its fullscreen button; then the pointer is parked over
# the picture, off the control bars, so the controls auto-hide. The state after it is recorded (meta.json
# player_pin.fullscreen). Layout only, never quality.
# `on` is yt's own player fullscreen: yt fullscreens the document element and marks it as #movie_player.ytp-fullscreen /
# ytd-watch-flexy[fullscreen] (hot-fix 3, shagun's check of 7c2711a: the f key entered at 150 ms, the test "#movie_player
# is the fullscreen element" read it as off and the fallback button click toggled it off at 1485 ms); a bare
# document.fullscreenElement is not enough either (hot-fix 2)
_FULLSCREEN_STATE_JS = """
(function() {
    const fe = document.fullscreenElement;
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    const mp = document.querySelector('#movie_player');
    const b = document.querySelector('.ytp-fullscreen-button');
    const br = b ? b.getBoundingClientRect() : null;
    const pr = mp ? mp.getBoundingClientRect() : null;
    const flexy = document.querySelector('ytd-watch-flexy');
    const chat = document.querySelector('ytd-live-chat-frame');
    const playerFs = !!(fe && mp && (mp.classList.contains('ytp-fullscreen') || (flexy && flexy.hasAttribute('fullscreen'))));
    return JSON.stringify({on: playerFs, element: fe ? (fe.id || fe.tagName.toLowerCase()) : null,
                           doc_fullscreen: fe === document.documentElement,
                           flexy_fullscreen: flexy ? flexy.hasAttribute('fullscreen') : null,
                           player: v ? [v.clientWidth, v.clientHeight] : null,
                           player_rect: pr ? [pr.x, pr.y, pr.width, pr.height] : null,
                           screen: [screen.width, screen.height],
                           viewport: [window.innerWidth, window.innerHeight], dpr: window.devicePixelRatio || 1,
                           controls_hidden: mp ? mp.classList.contains('ytp-autohide') : null,
                           chat_present: !!chat, chat_collapsed: chat ? chat.hasAttribute('collapsed') : null,
                           button: br ? [br.x + br.width / 2, br.y + br.height / 2] : null});
})()
"""
# the watch page's live chat collapsed before the player's fullscreen (t37 hot-fix 2: yt keeps the chat panel beside a
# fullscreen player, ~520 px of a 1920 px screen); a script click is enough for the toggle (no fullscreen grant needed)
_YT_HIDE_CHAT_JS = """
(function() {
    const fr = document.querySelector('ytd-live-chat-frame');
    if (!fr) return JSON.stringify({action: 'no-chat'});
    if (fr.hasAttribute('collapsed')) return JSON.stringify({action: 'already'});
    const b = fr.querySelector('#show-hide-button button') || fr.querySelector('#show-hide-button');
    if (!b) return JSON.stringify({action: 'no-button'});
    b.click();
    return JSON.stringify({action: 'clicked'});
})()
"""


def player_is_screen(rec: dict) -> bool:
    """The player element covers the screen (CSS px): its rect from (0, 0) at least the screen's size."""
    try:
        x, y, w, h = rec["player_rect"]
        sw, sh = rec["screen"]
        return x <= 0.5 and y <= 0.5 and w >= sw - 1 and h >= sh - 1
    except (TypeError, KeyError, ValueError):
        return False


def _key(page: "CDPPage", key: str, code: str, vk: int) -> None:
    for t in ("keyDown", "keyUp"):
        page.call("Input.dispatchKeyEvent", {"type": t, "key": key, "code": code, "windowsVirtualKeyCode": vk,
                                             **({"text": key} if t == "keyDown" else {})})


def _mouse(page: "CDPPage", kind: str, x: float, y: float) -> None:
    page.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                           "button": "none" if kind == "mouseMoved" else "left", "clickCount": 1})


_FULLSCREEN_TRACE_JS = ("(function() { if (!window.__lvqoeFs) { window.__lvqoeFs = []; const t0 = performance.now(); "
                        "document.addEventListener('fullscreenchange', () => __lvqoeFs.push([Math.round(performance.now() "
                        "- t0), !!document.fullscreenElement])); document.addEventListener('fullscreenerror', () => "
                        "__lvqoeFs.push([Math.round(performance.now() - t0), 'error'])); } return 'ok'; })()")


_SCREEN_JS = ("JSON.stringify({viewport: [window.innerWidth, window.innerHeight], screen: [screen.width, screen.height], "
              "dpr: window.devicePixelRatio || 1})")


def covers_screen(viewport, screen) -> bool:
    """The page viewport is the whole screen (CSS px)."""
    try:
        return bool(viewport and screen and viewport[0] >= screen[0] and viewport[1] >= screen[1])
    except (TypeError, IndexError):
        return False


def window_fullscreen(page: "CDPPage", target_id: str | None, settle_s: float = 1.5) -> dict:
    """The Chrome window itself in fullscreen (t37 hot-fix, shagun on yt_44: the player's fullscreen alone stayed
    inside a windowed Chrome, viewport 1818x868 on a 1920x1080 screen): CDP Browser.setWindowBounds windowState
    fullscreen, verified by the window state and by the viewport covering the screen. Never raises."""
    rec: dict = {"ok": False}
    try:
        win = _browser_call("Browser.getWindowForTarget", {"targetId": target_id} if target_id else {})
        wid = win.get("windowId")
        if wid is None:
            rec["error"] = "no window id"
            return rec
        before = (_browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds") or {}).get("windowState")
        if before != "fullscreen":
            _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"windowState": "fullscreen"}})
            time.sleep(settle_s)
        got = _browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds") or {}
        vs = json.loads(page.eval(_SCREEN_JS) or "{}")
        rec.update(state_before=before, window_state=got.get("windowState"),
                   bounds=[got.get("left"), got.get("top"), got.get("width"), got.get("height")],
                   viewport=vs.get("viewport"), screen=vs.get("screen"),
                   viewport_is_screen=covers_screen(vs.get("viewport"), vs.get("screen")))
        rec["ok"] = rec["window_state"] == "fullscreen" and rec["viewport_is_screen"]
    except Exception as exc:
        rec["error"] = repr(exc)
    return rec


def yt_fullscreen(page: "CDPPage", settle_s: float = 4.0, target_id: str | None = None) -> dict:
    """Put the Chrome window in fullscreen (window_fullscreen), then the yt player, and park the pointer; read back
    the state, with the page's fullscreenchange trace ([ms, on]; laptop 2026-10-18: macOS drops a background app's
    fullscreen after about 1 s, so entering is not staying) and whether the viewport covers the screen
    (`on_screen`). Never raises."""
    try:
        win = window_fullscreen(page, target_id)
        chat = json.loads(page.eval(_YT_HIDE_CHAT_JS) or "{}")
        time.sleep(0.8)
        chat["collapsed"] = json.loads(page.eval(_FULLSCREEN_STATE_JS) or "{}").get("chat_collapsed")
        page.eval(_FULLSCREEN_TRACE_JS)
        st = json.loads(page.eval(_FULLSCREEN_STATE_JS) or "{}")
        rec: dict = {"action": "already" if st.get("on") else None}
        if not st.get("on"):
            page.eval("(document.querySelector('#movie_player') || document.body).focus()")   # the hotkey's target
            _key(page, "f", "KeyF", 70)
            time.sleep(1.0)
            st = json.loads(page.eval(_FULLSCREEN_STATE_JS) or "{}")
            rec["action"] = "key_f"
            # the fallback only when the key changed nothing: a fullscreenchange to on means the key entered, and a
            # click would toggle it off again (hot-fix 3: 7c2711a's click did exactly that)
            try:
                entered = any(e[1] is True for e in json.loads(page.eval("JSON.stringify(window.__lvqoeFs || [])") or "[]"))
            except (TypeError, ValueError, IndexError):
                entered = False
            rec["key_entered"] = entered
            if not st.get("on") and not entered and st.get("button"):
                x, y = st["button"]
                _mouse(page, "mouseMoved", x, y)              # the button sits in the auto-hiding control bar
                time.sleep(0.3)
                _mouse(page, "mousePressed", x, y)
                _mouse(page, "mouseReleased", x, y)
                time.sleep(1.0)
                st = json.loads(page.eval(_FULLSCREEN_STATE_JS) or "{}")
                rec["action"] = "button_click"
        vw, vh = st.get("viewport") or [0, 0]
        rec["pointer_parked"] = [round(vw * 0.5), round(vh * 0.4)]    # over the picture, off both control bars
        _mouse(page, "mouseMoved", *rec["pointer_parked"])
        time.sleep(settle_s)
        rec.update(json.loads(page.eval(_FULLSCREEN_STATE_JS) or "{}"))
        rec.pop("button", None)
        try:
            ev = json.loads(page.eval("JSON.stringify(window.__lvqoeFs || null)") or "null")
            rec["events"] = ev if isinstance(ev, list) else None
        except (TypeError, ValueError):
            rec["events"] = None
        p, dpr = rec.get("player"), rec.get("dpr") or 1
        rec["player_device_px_h"] = round(p[1] * dpr) if p else None
        rec["window"] = win
        rec["chat_hidden"] = chat
        rec["on_screen"] = bool(rec.get("on")) and covers_screen(rec.get("viewport"), rec.get("screen"))
        rec["player_is_screen"] = bool(rec.get("on")) and player_is_screen(rec)
        return rec
    except Exception as exc:
        return {"action": "error", "error": repr(exc)}


YT_CHAT_WAIT_S = 30     # t83 (8): the bounded wait for yt's live chat frame before the one re-entry


def yt_fullscreen_held(page: "CDPPage", target_id: str | None = None, chat_wait_s: float = YT_CHAT_WAIT_S,
                       sleep=time.sleep, enter=None) -> dict:
    """yt_fullscreen, then (t83 (8), W1-W4 yt_100116 1348x758 / yt_100121 1400x788: the hide-chat ran once, before
    ytd-live-chat-frame existed, "no-chat", and the player stayed beside the chat; "clicked" on all 4 that held) when
    its settled player does not cover the screen: the hide-chat each second until the frame is there (clicked /
    already) or chat_wait_s passes, then one re-entry. The first entry's outcome and the retry are recorded with the
    final state (retry: polls, chat, reentered; first). Never raises."""
    enter = enter or yt_fullscreen
    rec = enter(page, target_id=target_id)
    if rec.get("player_is_screen") or rec.get("action") == "error":
        return rec
    retry = {"polls": 0, "chat": None, "reentered": False}
    try:
        for _ in range(int(chat_wait_s)):
            retry["polls"] += 1
            retry["chat"] = json.loads(page.eval(_YT_HIDE_CHAT_JS) or "{}").get("action")
            if retry["chat"] in ("clicked", "already"):
                sleep(2.0)                                   # the panel's collapse before the player is re-measured
                break
            sleep(1.0)
    except Exception as exc:
        retry["error"] = repr(exc)[:200]
    first = {k: rec.get(k) for k in ("action", "on", "player", "player_is_screen", "chat_hidden")}
    rec = enter(page, target_id=target_id)
    retry["reentered"] = True
    return {**rec, "retry": retry, "first": first}


# Window / viewport / player geometry (meta.json): the player size decides the top of yt's ladder. player_rect is the
# largest visible <video>'s bounding rect in viewport CSS px [x, y, w, h] (supervisor 2026-10-17, brad's R-O audit: the
# motion crop is derived per session from it, without detection); control_band is the player's own control bar where
# it exposes one (yt .ytp-chrome-bottom, twitch [data-a-target=player-controls]; tt none: the inset rule applies), with
# its rect and whether it was showing (both auto-hide).
_GEOMETRY_JS = """
(function() {
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    const vs = [...document.querySelectorAll('video')].filter(x => x.offsetParent !== null);
    const pv = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0] || v;
    const rect = el => { if (!el) return null; const r = el.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
    const flexy = document.querySelector('ytd-watch-flexy');
    const yb = document.querySelector('#movie_player .ytp-chrome-bottom');
    const tb = document.querySelector('[data-a-target="player-controls"]');
    const mp = document.querySelector('#movie_player');
    const band = yb ? {source: 'yt .ytp-chrome-bottom', rect: rect(yb), height: rect(yb)[3],
                       showing: mp ? !mp.classList.contains('ytp-autohide') : null}
               : tb ? {source: 'twitch [data-a-target=player-controls]', rect: rect(tb), height: rect(tb)[3],
                       showing: tb.dataset.aVisible === 'true'} : null;
    return JSON.stringify({
        window_outer: [window.outerWidth, window.outerHeight], viewport: [window.innerWidth, window.innerHeight],
        device_pixel_ratio: window.devicePixelRatio, screen: [screen.width, screen.height],
        player_client: v ? [v.clientWidth, v.clientHeight] : null,
        player_rect: rect(pv), control_band: band, at: Date.now() / 1000,
        yt_theater: flexy ? flexy.hasAttribute('theater') : null,
        yt_fullscreen: flexy ? flexy.hasAttribute('fullscreen') : null,
    });
})()
"""

def geometry_scales(geom: dict | None) -> dict | None:
    """levi 2026-10-17 (Addendum O amendment 2, R-O's MAD crop): the player's position and size in the screenshots'
    pixels beside its CSS-px rect. Device px = CSS px x device_pixel_ratio. A viewport shot (CDP) is the viewport in
    device px scaled to SHOT_WIDTH_PX wide: the player sits at player_in_viewport_shot_px. An x11 shot is the player
    rectangle itself scaled to SHOT_WIDTH_PX wide (x11_player_shot_scale; position 0, 0). Each shot's own scale is
    shot_px / capture_px in shots/index.jsonl."""
    try:
        x, y, w, h = geom["player_rect"]
        dpr = float(geom.get("device_pixel_ratio") or 1)
        vw = geom["viewport"][0] * dpr
        k = min(1.0, SHOT_WIDTH_PX / vw)
        return {"source": SHOT_SOURCE, "width_px": SHOT_WIDTH_PX, "dpr": dpr,
                "player_rect_device_px": [round(v * dpr, 1) for v in (x, y, w, h)],
                "viewport_shot_scale": round(k, 5),
                "player_in_viewport_shot_px": [round(v * dpr * k, 1) for v in (x, y, w, h)],
                "x11_player_shot_scale": round(min(1.0, SHOT_WIDTH_PX / (w * dpr)), 5) if w else None}
    except (TypeError, KeyError, ValueError, IndexError, ZeroDivisionError):
        return None


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

CDP_CALL_TIMEOUT_S = 20        # CDPPage: each call's socket wait (the session page and the export's extension page)


class CDPPage:
    """
    Wraps a CDP WebSocket target.  All calls are serialised by a lock so the
    instance can be shared safely between the main thread and the ad-watcher.
    """

    def __init__(self, ws_url: str):
        self._ws = websocket.WebSocket()
        self._ws.settimeout(CDP_CALL_TIMEOUT_S)
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


# t91 (rushi 2026-10-28; levi 17:4x, boundary 3): content.js's per-frame rVFC log (the tick row's field fl).
# LVQOE_FRAME_LOG: unset or "0" off (no config, no meta key: the session as without it); "1" on; "alt<N>" alternating
# OFF / ON blocks of N s (levi's instrumentation check: the OFF blocks are the control spread). levi 18:11: the counted
# mode is in this sha, so the record runs the code the dry check validated: a counted run (LVDC_TRIO_RECORD=1) or a
# session under a marked record root takes "1" only (the deployed window config's, named in its manifest); alt<N> there
# is refused, as everywhere with the instruments off or a value that does not parse (recorded, off). levi 21:0x (t93): the
# blocks run on the epoch clock (t0 0: arm = floor(unix s / N) odd), so every slot's page side and the host samplers
# (TcpStateSampler, gpu_record) switch together; frame_log_arm is that rule on the host.
FRAME_LOG_ENV = "LVQOE_FRAME_LOG"
FRAME_LOG_MSE_MAX = 2000      # t93 rows 9 / 20: the page-world MSE wrap's records per 1 s batch (past it counted, over)
MSE_WRAP_R2 = True            # t100 R2 (rushi 14:35): yt app tso + head time (tf / ctc), init / tcs; off = 971e444e's records
MSE_WRAP_WEBM = True          # t102 row (7) (levi 22:10): yt WebM video byte-stream state (cc / wc / nb / b0 / us); off = 7809b21c's records
MSE_WRAP_YT_KEY = True        # t102 yt key (levi 22:10): keyed WebM blocks (watcher.media.vcl_keys.key); off = no key rows
MSE_WRAP_KEY_EVERY = 30       # every Nth WebM video block by count, as WORKER_ANCHOR_KEY_EVERY (levi audit: N from S1's F1 headroom)
# t93 rows 9 / 20 (levi 00124; brad W3): yt's MSE calls in the page world, added before any page script by
# Page.addScriptToEvaluateOnNewDocument, only when the frame log is enabled on yt (tw's MSE runs in a worker, tt's live is
# a MediaStream: out by construction). Each wrapped call runs the original with the same this and arguments and returns
# its value, synchronously; the record is one push in a try (a recording fault never reaches the page). Records, ON blocks
# only: add (the mime / codec, the buffer's id), changeType, app (bytes), rm (start, end), end (updateend: the buffered end,
# the range count); every 1 s one postMessage batch to content.js (frames k:"mse"), over counted past FRAME_LOG_MSE_MAX.
# levi 22:44 (1): the wrap is in place before any page script (the CDP call precedes the join navigation); its install
# time on the page (inst: performance.now() at the script's run) and the page's first addSourceBuffer (first: its perf
# time and mime), recorded whatever the arm, ride every batch and an arm-independent first post; meta
# frame_log.mse_wrap.installed_at is the CDP call's epoch time. The script runs only while the Page domain is enabled on
# the registering connection (laptop 2026-10-28, a dash.js page: without Page.enable no batch, with it every batch; as
# tw_latency.py found), so the join enables Page, and after the join (mse_wrap_settle) the wrap's flag is read on the
# joined document (live), the script is removed and Page disabled again: the wrap stays in that document (laptop: batches
# went on after Page.disable) and the page connection is back to its usual state. A later document (a reload) has no
# wrap (laptop: a script left registered did not run on a reload after Page.disable; Page kept enabled all session is
# not taken: with Page enabled a JS dialog may wait on this connection (javascriptDialogOpening), which never answers
# one). levi 23:51 (2):
# so the wrap's presence is recorded per document: its first post goes out at install (1 s after document start) whatever
# the page does, content.js carries it as fl.mse_inst on every later ON tick of that document (cs); a yt document whose ON
# ticks carry no mse_inst had no wrap (absent, never an empty MSE record).
MSE_WRAP_JS = r"""(() => {
  if (window.__lvqoeMse) return;
  window.__lvqoeMse = true;
  const inst = performance.now();
  let first = null, firstSent = false, instSent = false;
  const BLOCK_S = %(block_s)d, MAX = %(max)d, R2 = %(r2)d;
  const on = () => !(BLOCK_S > 0) || Math.floor(Date.now() / (BLOCK_S * 1000)) %% 2 === 1;
  let recs = [], over = 0, next = 0;
  const ids = new WeakMap();
  const id = (sb) => { let i = ids.get(sb); if (i === undefined) { i = ++next; ids.set(sb, i); } return i; };
  const push = (r) => { if (!on()) return; if (recs.length >= MAX) { over += 1; return; } r.t = performance.now(); recs.push(r); };
  const wrap = (proto, name, before, after) => {
    const orig = proto && proto[name];
    if (typeof orig !== "function") return;
    proto[name] = function (...a) {
      if (before) { try { before(this, a); } catch (e) {} }
      const ret = orig.apply(this, a);
      if (after) { try { after(this, a, ret); } catch (e) {} }
      return ret;
    };
  };
  // t100 R2 (rushi 14:35, levi's budget; MSE_WRAP_R2): on app, the tso in force and the head's first time: fMP4 the first
  // traf's [track id, tfdt] (tf), WebM a Cluster's Timecode (ctc); init (fMP4 moov: [tid, timescale, handler]) and tcs
  // (WebM TimecodeScale, first 4 KB of an EBML header append) are state records, every block. Only the boxes / elements at
  // the head are read, never the media; a fault leaves tf null (r2err) and never reaches the page.
  const state = (r) => { if (recs.length >= MAX) { over += 1; return; } r.t = performance.now(); recs.push(r); };
  const u8 = (b) => b instanceof ArrayBuffer ? new Uint8Array(b) : (b && b.buffer ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength) : null);
  const vint = (b, i, keep) => { const x = b[i]; let n = 1; while (n <= 8 && !(x & (0x80 >> (n - 1)))) n++; if (n > 8 || i + n > b.length) return null;
    let v = keep ? x : x & (0xff >> n); for (let k = 1; k < n; k++) v = v * 256 + b[i + k]; return [v, n]; };
  const uint = (b, i, n) => { let v = 0; for (let k = 0; k < n && i + k < b.length; k++) v = v * 256 + b[i + k]; return v; };
  const ebml = (b, i, want, lim) => { let k = 0; while (i < Math.min(b.length, lim) && k++ < 8) { const d = vint(b, i, true); if (!d) return null;
      const z = vint(b, i + d[1]); if (!z) return null; const at = i + d[1] + z[1]; if (d[0] === want) return uint(b, at, z[0]); i = at + z[0]; } return null; };
  const r2 = (sb, b0, r) => { try {
    r.tso = sb.timestampOffset; const b = u8(b0); if (!b || b.length < 8) return;
    if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) { const lim = Math.min(b.length, 4096);
      for (let i = 0; i + 3 < lim; i++) if (b[i] === 0x2a && b[i + 1] === 0xd7 && b[i + 2] === 0xb1) { const z = vint(b, i + 3);
        if (z) state({ op: "tcs", sb: id(sb), v: uint(b, i + 3 + z[1], z[0]) }); break; }
      return; }
    if (b[0] === 0x1f && b[1] === 0x43 && b[2] === 0xb6 && b[3] === 0x75) { const z = vint(b, 4); if (z) { const v = ebml(b, 4 + z[1], 0xe7, 4096); if (v !== null) r.ctc = v; } return; }
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength), T = (o) => String.fromCharCode(b[o + 4], b[o + 5], b[o + 6], b[o + 7]);
    const each = (s, e, f) => { let o = s, n = 0; while (o + 8 <= e && n++ < 64) { const z = dv.getUint32(o); if (z < 8 || o + z > e) return; if (f(T(o), o + 8, o + z) === false) return; o += z; } };
    each(0, b.length, (t, bb, be) => {
      if (t === "moov") { const tr = []; each(bb, be, (t2, b2, e2) => { if (t2 !== "trak") return; const x = [null, null, null];
          each(b2, e2, (t3, b3, e3) => { if (t3 === "tkhd") x[0] = dv.getUint32(b3 + (b[b3] === 1 ? 20 : 12));
            if (t3 === "mdia") each(b3, e3, (t4, b4) => { if (t4 === "mdhd") x[1] = dv.getUint32(b4 + (b[b4] === 1 ? 20 : 12));
              if (t4 === "hdlr") x[2] = T(b4 + 4); }); });
          tr.push(x); });
        state({ op: "init", sb: id(sb), tr }); }
      if (t === "moof") { r.tf = null; each(bb, be, (t2, b2, e2) => { if (t2 !== "traf") return; let tid = null, bdt = null;
          each(b2, e2, (t3, b3) => { if (t3 === "tfhd") tid = dv.getUint32(b3 + 4);
            if (t3 === "tfdt") bdt = b[b3] === 1 ? dv.getUint32(b3 + 4) * 4294967296 + dv.getUint32(b3 + 8) : dv.getUint32(b3 + 4); });
          r.tf = [tid, bdt]; return false; });
        return false; } });
  } catch (e) { r.tf = null; r.r2err = 1; } };
  const ended = (sb) => () => { try { const b = sb.buffered, n = b.length;
    push({ op: "end", sb: id(sb), bend: n ? b.end(n - 1) : null, nr: n }); } catch (e) {} };
  if (typeof MediaSource !== "undefined") {
    wrap(MediaSource.prototype, "addSourceBuffer", null, (ms, a, sb) => {
      if (first === null) first = { t: performance.now(), mime: String(a[0]).slice(0, 160) };
      push({ op: "add", sb: id(sb), mime: String(a[0]).slice(0, 160) });
      sb.addEventListener("updateend", ended(sb));
    });
  }
  if (typeof SourceBuffer !== "undefined") {
    const p = SourceBuffer.prototype;
    wrap(p, "appendBuffer", (sb, a) => { const r = { op: "app", sb: id(sb), bytes: a[0] ? a[0].byteLength : null };
      if (R2) r2(sb, a[0], r);
      push(r); });
    wrap(p, "remove", (sb, a) => push({ op: "rm", sb: id(sb), s: a[0], e: a[1] }));
    wrap(p, "changeType", (sb, a) => push({ op: "ct", sb: id(sb), mime: String(a[0]).slice(0, 160) }));
  }
  setInterval(() => {
    if (!recs.length && instSent && (first === null || firstSent)) return;
    const batch = recs; recs = [];
    instSent = true;
    firstSent = first !== null;
    try { window.postMessage({ source: "lvqoe-mse", recs: batch, over, inst, first }, "*"); } catch (e) {}
  }, 1000);
})();"""


# t102 rows (3) / (7) + the yt key (levi 22:10; audit 00138; fast gate 00139 G0): the yt WebM additions, inserted into
# MSE_WRAP_JS only when MSE_WRAP_WEBM or MSE_WRAP_YT_KEY is on, so with both off the injected script is 7809b21c's,
# byte for byte. Each anchor occurs once in MSE_WRAP_JS (checked at import).
MSE_WRAP_YT_PARSER = r"""  // t102 rows (3) / (7) + the yt key (levi 22:10; audit 00138; MSE_WRAP_WEBM / MSE_WRAP_YT_KEY): a WebM video buffer's
  // appends are one byte stream; a generator per buffer walks its element headers through every append (masters Segment /
  // Info / Cluster / BlockGroup entered, others passed by size; payloads read only for TimecodeScale, a Cluster Timecode,
  // a block's 4-byte header and a keyed block's first / last 256 bytes). app gains cc (the Cluster Timecode in force at
  // byte 0, the carry), wc ([Timecode, byte offset of its Cluster's ID in this append] of the first Cluster Timecode read
  // here), nb / b0 (blocks starting here, the first one's relative tc), key ([abs tc = Cluster tc + rel, rel, L, FNV-1a 32
  // over the payload's first 256 + last 256 bytes (all when L <= 512): watcher.media.vcl_keys.key, keyframe bit or null
  // for a Block], ON blocks, every KEY_EVERY-th block by count and the first after a reset or a Cluster tc jump), lace
  // (laced blocks, never keyed), us (the reason the stream is unsynced: start / abort / ct / err; that append is not
  // parsed). abort(), changeType() and a parse fault (r2err) reset; remove() never does (levi audit 1: MSE keeps the
  // parser). Sync returns only on an append that starts with the EBML or Cluster ID (MSE: after an abort the next
  // append starts at a Cluster or an init), never by a payload scan. Each non-empty batch ends with an ms record (op
  // "ms", v: the wrap's own time so far, ms; levi audit 6).
  const WEBM = %(webm)d, YKEY = %(ykey)d, KEY_EVERY = %(key_every)d;
  let ms = 0;
  const VWEBM = /^\s*video\/webm/i;
  const I_EBML = 0x1a45dfa3, I_CL = 0x1f43b675, I_TC = 0xe7, I_TCS = 0x2ad7b1, I_SB = 0xa3, I_BL = 0xa1;
  const MASTER = [0x18538067, 0x1549a966, I_CL, 0xa0];
  const wst = new WeakMap();
  const W = (sb) => { let s = wst.get(sb); if (!s) { s = { vid: false, sync: false, why: "start", tcs: 1e6, ctc: null,
      last: null, nb: 0, pend: true, q: [], qn: 0, pos: 0, cl: null, g: null, r: null }; wst.set(sb, s); } return s; };
  const reset = (s, why) => { s.sync = false; s.why = why; s.g = null; s.q = []; s.qn = 0; s.ctc = null; s.pend = true; };
  const fnv = (h, p) => { for (let i = 0; i < p.length; i++) { h ^= p[i]; h = Math.imul(h, 0x01000193) >>> 0; } return h; };
  function* need(s, n) { while (s.qn < n) yield; const out = new Uint8Array(n); let k = 0;
    while (k < n) { const c = s.q[0], m = Math.min(n - k, c[0].length - c[1]); out.set(c[0].subarray(c[1], c[1] + m), k);
      k += m; c[1] += m; s.qn -= m; s.pos += m; if (c[1] === c[0].length) s.q.shift(); }
    return out; }
  function* pass(s, n) { while (n > 0) { while (!s.qn) yield; const c = s.q[0], m = Math.min(n, c[0].length - c[1]);
      c[1] += m; s.qn -= m; s.pos += m; n -= m; if (c[1] === c[0].length) s.q.shift(); } }
  function* vr(s, keep) { const a = (yield* need(s, 1))[0]; let n = 1; while (n <= 8 && !(a & (0x80 >> (n - 1)))) n++;
    if (n > 8) throw new Error("vint"); let v = keep ? a : a & (0xff >> n), ones = (a & (0xff >> n)) === (0xff >> n);
    if (n > 1) for (const x of yield* need(s, n - 1)) { v = v * 256 + x; if (x !== 0xff) ones = false; }
    return [v, n, ones]; }
  function* webm(s) { for (;;) {
    const at = s.pos, el = (yield* vr(s, true))[0], z = yield* vr(s, false);
    if (MASTER.includes(el)) { if (el === I_CL) s.cl = at; continue; }
    if (z[2]) throw new Error("unknown size " + el);
    const n = z[0];
    if (el === I_TC || el === I_TCS) { let v = 0; for (const x of yield* need(s, n)) v = v * 256 + x;
      if (el === I_TCS) { s.tcs = v; continue; }
      if (s.last !== null && (v < s.last || v - s.last > 5e9 / s.tcs)) s.pend = true;
      s.ctc = v; if (s.r && s.r.wc === undefined) s.r.wc = [v, s.cl - s.r.p0]; continue; }
    if ((el === I_SB || el === I_BL) && s.ctc !== null) {
      const tk = yield* vr(s, false), hb = yield* need(s, 3), L = n - tk[1] - 3;
      if (L < 0) throw new Error("block size");
      const rel = ((hb[0] << 8) | hb[1]) - (hb[0] & 0x80 ? 0x10000 : 0), abs = s.ctc + rel, r = s.r;
      s.last = abs; s.nb += 1; if (r) { r.nb = (r.nb || 0) + 1; if (r.b0 === undefined) r.b0 = rel; }
      if (hb[2] & 0x06) { if (r) r.lace = (r.lace || 0) + 1; yield* pass(s, L); continue; }
      if (YKEY && L > 0 && on() && (s.pend || s.nb %% KEY_EVERY === 0)) { s.pend = false; let h;
        if (L <= 512) h = fnv(0x811c9dc5, yield* need(s, L));
        else { h = fnv(0x811c9dc5, yield* need(s, 256)); yield* pass(s, L - 512); h = fnv(h, yield* need(s, 256)); }
        (s.r.key || (s.r.key = [])).push([abs, rel, L, h >>> 0, el === I_SB ? hb[2] >> 7 : null]);   // the append it completes in
        continue; }
      yield* pass(s, L); continue; }
    yield* pass(s, n); } }
  const feed = (sb, b0, r) => { const s = W(sb); if (!s.vid) return; const b = u8(b0); if (!b || !b.length) return;
    if (!s.sync) { const h4 = b.length >= 4 ? ((b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]) >>> 0 : 0;
      if (h4 !== I_EBML && h4 !== I_CL) { r.us = s.why; return; }
      reset(s, null); s.sync = true; s.g = webm(s); s.g.next(); }
    r.cc = s.ctc; r.p0 = s.pos + s.qn; s.r = r; s.q.push([b, 0]); s.qn += b.length;   // p0: this append's byte 0 in the stream
    try { s.g.next(); s.q = s.q.map((c) => [c[0].slice(c[1]), 0]); }   // the leftover (< 512 B) copied: the page may reuse b
    catch (e) { reset(s, "err"); r.r2err = 1; }
    s.r = null; delete r.p0; };
  const ROW1 = ["cc", "wc", "nb", "b0", "lace", "us"];
  const yt = (sb, a, r) => { const q = performance.now();                 // the parse runs for either row; Row 1's fields
    try { r.mode = sb.mode; } catch (e) {}                                // t102 #2: the mode in force at the append
    try { feed(sb, a[0], r); if (!WEBM) for (const f of ROW1) delete r[f]; } catch (e) {}   // only with MSE_WRAP_WEBM
    ms += performance.now() - q; };
"""
MSE_WRAP_YT_EDITS = [('  } catch (e) { r.tf = null; r.r2err = 1; } };\n', '  } catch (e) { r.tf = null; r.r2err = 1; } };\n__PARSER__'),
    ('      push({ op: "add", sb: id(sb), mime: String(a[0]).slice(0, 160) });\n', '      push({ op: "add", sb: id(sb), mime: String(a[0]).slice(0, 160) });\n      W(sb).vid = VWEBM.test(String(a[0]));\n'),
    ('      if (R2) r2(sb, a[0], r);\n', '      if (R2) r2(sb, a[0], r);\n      yt(sb, a, r);\n'),
    ('    wrap(p, "changeType", (sb, a) => push({ op: "ct", sb: id(sb), mime: String(a[0]).slice(0, 160) }));\n', '    wrap(p, "changeType", (sb, a) => { push({ op: "ct", sb: id(sb), mime: String(a[0]).slice(0, 160) });\n      const s = W(sb); reset(s, "ct"); s.vid = VWEBM.test(String(a[0])); });\n    wrap(p, "abort", (sb) => reset(W(sb), "abort"));\n'),
    ('    const batch = recs; recs = [];\n', '    if (recs.length) recs.push({ op: "ms", v: Math.round(ms * 100) / 100, t: performance.now() });   // the wrap\'s own ms so far, as a record (content.js keeps recs only)\n    const batch = recs; recs = [];\n'),
    ('  const ended = (sb) => () => { try { const b = sb.buffered, n = b.length;\n    push({ op: "end", sb: id(sb), bend: n ? b.end(n - 1) : null, nr: n }); } catch (e) {} };\n', '  const ended = (sb) => () => { try { const b = sb.buffered, n = b.length;\n    const r = { op: "end", sb: id(sb), bend: n ? b.end(n - 1) : null, nr: n };\n    try { const br = []; for (let i = 0; i < Math.min(n, 8); i++) br.push([b.start(i), b.end(i)]);   // t102 #2: the\n      r.br = br; r.tso = sb.timestampOffset; r.mode = sb.mode; } catch (e) {}    // ranges (<= 8), tso and mode after it\n    push(r); } catch (e) {} };\n')]


def _mse_wrap_yt_template() -> str:
    t = MSE_WRAP_JS
    for anchor, repl in MSE_WRAP_YT_EDITS:
        assert t.count(anchor) == 1, anchor
        t = t.replace(anchor, repl.replace("__PARSER__", MSE_WRAP_YT_PARSER))
    return t


MSE_WRAP_YT_JS = _mse_wrap_yt_template()


def mse_wrap_js(block_s: int, *, max_recs: int = FRAME_LOG_MSE_MAX, r2: bool = MSE_WRAP_R2, webm: bool = MSE_WRAP_WEBM,
                ykey: bool = MSE_WRAP_YT_KEY, key_every: int = MSE_WRAP_KEY_EVERY) -> str:
    """MSE_WRAP_JS for one session (the constants frozen with the tip; tests pass their own)."""
    if not (webm or ykey):                    # both off: 7809b21c's script, byte for byte (fast gate G0)
        return MSE_WRAP_JS % {"block_s": int(block_s), "max": int(max_recs), "r2": int(r2)}
    return MSE_WRAP_YT_JS % {"block_s": int(block_s), "max": int(max_recs), "r2": int(r2), "webm": int(webm),
                             "ykey": int(ykey), "key_every": max(1, int(key_every))}


def mse_wrap_settle(page, rec: dict) -> dict:
    """After the join: live = the wrap's flag on the joined document; then the script is removed and Page disabled (the
    connection's usual state). Never an abort: a failure is recorded."""
    rec = dict(rec)
    rec["page_disabled"] = False
    try:
        rec["live"] = page.eval("window.__lvqoeMse === true") is True
    except Exception as exc:
        rec["live"] = None
        rec["live_error"] = repr(exc)[:200]
    try:
        if rec.get("id"):
            page.call("Page.removeScriptToEvaluateOnNewDocument", {"identifier": rec["id"]})
    except Exception as exc:
        rec["remove_error"] = repr(exc)[:200]
    try:                                         # its own try: a failed remove never skips the disable
        page.call("Page.disable")
        rec["page_disabled"] = True
        rec["page_disabled_at"] = round(time.time(), 3)
    except Exception as exc:
        rec["disable_error"] = repr(exc)[:200]
    return rec


# t100 R1 (levi b3_anchor_rows_gates 14:28 + 14:32 amendments; kb collector.boundary3.add_worker_anchor): tw and tt run
# their MSE in a dedicated Web Worker (laptop t100: tw's IVS worker, tt's player-worker veworker; MediaSourceHandle to the
# page), where the page-world MSE_WRAP_JS never reaches. WorkerAnchor (below) attaches to the session page's dedicated
# workers only (Target filter: frames, service and shared workers never attach, so never pause), evaluates
# WORKER_ANCHOR_JS in each before it runs and always resumes it; about 1 s it drains the worker's batch and posts it into
# the page as the yt wrap's "lvqoe-mse" message, so content.js carries it unchanged (frames k:"mse", the F1 raw keep
# covers it). The appended media's own times give c (rVFC mt = (dt + cto) / timescale + tso: laptop tw 904 / 904 and
# 989 / 989 with tso set, tt 451 / 451; null without tso 0 / 989). Records (each wrapped call runs the original with the
# same this and arguments, the record in a try, never a fault into the player):
#   app (ON blocks; video-capable buffers only, every append, none sampled): sb, bytes, tso in force, v = per video traf
#     [tid, tfdt, first sample's cto, sample count, sum of durations] (timescale units; brad 14:26: tw's phase steps per TS
#     segment, so per append), key = keyed samples [tid, dt, cto, VCL length, FNV-1a 32 (an integer), NAL walk exact]:
#     every KEY_EVERY-th video sample by count, and the first sample after an init / abort / changeType / tso set / a tfdt
#     discontinuity (brad / levi 14:32: count-based, so every TS segment carries a key whatever the append timing). The key
#     (brad 14:07): the sample's VCL NALs (H.264 types 1-5; HEVC (b0 >> 1) & 0x3f < 32; AVCC 4-byte lengths dropped,
#     bytes raw) hashed over VCL[0:256] + VCL[max(256, L-256):L]; it pairs the sample to its wire frame (laptop cap4
#     2606 / 2606).
#   aapp / end (ON blocks; WORKER_ANCHOR_KINDS): audio per append (no keys); updateend buffered ranges per buffer.
#   state, every block (levi 14:30, the OFF-block residue): add (mime), init (per track [tid, timescale, handler, sample
#     entry, width, height, codec]), ct (mime), tso (value, was), abort (tso), rm (start, end); err (a parse fault).
# Only the moov / moof boxes at the head of an append and the keyed samples' NAL lengths are read; never the mdat.
# levi 14:59: t (0.1 ms), inst and first.t are the WORKER's performance.now(), from its own timeOrigin: every batch carries
# to (performance.timeOrigin, epoch ms) on the relayed message and in meta workers[].time_origin; epoch ms = t + to. The
# fl.mse_inst / mse_over content.js sets on tw / tt ON ticks from these messages are on that clock too.
WORKER_ANCHOR_PLATFORMS = ("twitch", "tiktok")
WORKER_ANCHOR_KEY_EVERY = 30
# the per-append kinds (rushi 14:35, levi's pre-stated budget B1-B3): "app" (video, the core, never cut), "aapp" (audio per
# append, the same fields, no keys), "end" (updateend: the buffer's ranges, at most 8, and their count); a kind left out
# here is never recorded. Frozen with the tip and recorded in meta frame_log.worker_anchor.kinds.
WORKER_ANCHOR_KINDS = ("app", "aapp", "end")
WORKER_ANCHOR_DRAIN_S = 1.0
WORKER_ANCHOR_DRAIN_AT = True     # t102 #8 (levi 01:06 / 00402): meta frame_log.worker_anchor.final_drain.at; off = 7809b21c's record
WORKER_ANCHOR_EVAL_TIMEOUT_MS = 50           # levi: the wrap's install is cut at 50 ms; the resume is sent regardless
WORKER_ANCHOR_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True,
                        "filter": [{"type": "worker", "exclude": False}, {"exclude": True}]}
WORKER_ANCHOR_JS = r"""(() => {
  if (self.__lvqoeAnchor) return "again";
  self.__lvqoeAnchor = true;
  const inst = performance.now();
  const BLOCK_S = __BLOCK_S__, MAX = __MAX__, KEY_EVERY = __KEY_EVERY__, K = __KINDS__;
  const on = () => !(BLOCK_S > 0) || Math.floor(Date.now() / (BLOCK_S * 1000)) % 2 === 1;
  let recs = [], over = 0, next = 0, first = null, ms = 0, nv = 0;
  const ids = new WeakMap(), st = new WeakMap();
  const id = (sb) => { let i = ids.get(sb); if (i === undefined) { i = ++next; ids.set(sb, i); } return i; };
  const S = (sb) => { let s = st.get(sb); if (!s) { s = { vid: false, hevc: false, tr: {}, tx: {}, nxt: {}, pend: true }; st.set(sb, s); } return s; };
  const kind = (s, mime) => { const m = String(mime); s.vid = /avc1|avc3|hev1|hvc1|av01|vp09|vp8|vp9/.test(m); s.hevc = /hev1|hvc1/.test(m); s.pend = true; };
  const push = (r, always) => { if (!always && !on()) return; if (recs.length >= MAX) { over += 1; return; }
    r.t = Math.round(performance.now() * 10) / 10; recs.push(r); };          // 0.1 ms: the bytes of record (levi B1)
  const timed = (f) => { const q = performance.now(); try { f(); } catch (e) { try { push({ op: "err", m: String(e).slice(0, 120) }, true); } catch (e2) {} } ms += performance.now() - q; };
  const u8 = (b) => b instanceof ArrayBuffer ? new Uint8Array(b) : (b && b.buffer ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength) : null);
  const typ = (a, o) => String.fromCharCode(a[o + 4], a[o + 5], a[o + 6], a[o + 7]);
  const r32 = (dv, o) => dv.getUint32(o);
  const r64 = (dv, o) => dv.getUint32(o) * 4294967296 + dv.getUint32(o + 4);
  const boxes = (a, dv, s, e, f) => { let o = s, n = 0; while (o + 8 <= e && n < 64) { let sz = r32(dv, o); const t = typ(a, o);
      let h = 8; if (sz === 1) { sz = r64(dv, o + 8); h = 16; } else if (sz === 0) sz = e - o; if (sz < h || o + sz > e) break; f(t, o, o + h, o + sz); o += sz; n++; } };
  const hx = (v) => (v < 16 ? "0" : "") + v.toString(16).toUpperCase();
  const vclKey = (a, dv, q, qe, hevc) => { const seg = []; let L = 0, k = 0;
    while (q + 5 <= qe && k++ < 64) { const n = r32(dv, q), b0 = a[q + 4]; if (q + 4 + n > qe) return [L, null, 0];
      const vcl = hevc ? ((b0 >> 1) & 0x3f) < 32 : (b0 & 0x1f) >= 1 && (b0 & 0x1f) <= 5;
      if (vcl) { seg.push([q + 4, q + 4 + n]); L += n; } q += 4 + n; }
    const ok = q === qe ? 1 : 0;
    const hr = (h, x0, x1) => { let v = 0; for (const [s0, e0] of seg) { const n = e0 - s0;
        const a0 = Math.max(x0 - v, 0), a1 = Math.min(x1 - v, n);
        for (let x = s0 + a0; x < s0 + a1; x++) { h ^= a[x]; h = Math.imul(h, 0x01000193) >>> 0; } v += n; if (v >= x1) break; } return h; };
    const lo = Math.min(256, L), hi = Math.max(256, L - 256);
    return [L, hr(hr(0x811c9dc5, 0, lo), hi, L) >>> 0, ok]; };
  const init = (a, dv, b, e, tx) => { const tr = [];
    boxes(a, dv, b, e, (t2, o2, b2, e2) => {
      if (t2 === "mvex") boxes(a, dv, b2, e2, (t3, o3, b3) => { if (t3 === "trex") tx.push([r32(dv, b3 + 4), r32(dv, b3 + 12), r32(dv, b3 + 16)]); });
      if (t2 !== "trak") return; const x = [null, null, null, null, null, null, null];
      boxes(a, dv, b2, e2, (t3, o3, b3, e3) => {
        if (t3 === "tkhd") x[0] = r32(dv, b3 + (a[b3] === 1 ? 20 : 12));
        if (t3 === "mdia") boxes(a, dv, b3, e3, (t4, o4, b4, e4) => {
          if (t4 === "mdhd") x[1] = r32(dv, b4 + (a[b4] === 1 ? 20 : 12));
          if (t4 === "hdlr") x[2] = typ(a, b4 + 4);
          if (t4 === "minf") boxes(a, dv, b4, e4, (t5, o5, b5, e5) => { if (t5 === "stbl") boxes(a, dv, b5, e5, (t6, o6, b6, e6) => {
            if (t6 !== "stsd" || b6 + 16 > e6) return; const en = b6 + 8; x[3] = typ(a, en);
            if (x[2] === "vide" && en + 36 <= e6) { x[4] = dv.getUint16(en + 32); x[5] = dv.getUint16(en + 34);
              boxes(a, dv, en + 86, Math.min(en + r32(dv, en), e6), (t7, o7, b7) => { if (t7 === "avcC") x[6] = x[3] + "." + hx(a[b7 + 1]) + hx(a[b7 + 2]) + hx(a[b7 + 3]); }); } }); });
        });
      });
      tr.push(x); });
    return tr; };
  const frag = (a, dv, s, r, video) => {
    boxes(a, dv, 0, a.length, (t, o, b, e) => { if (t !== "moof") return; let ti = 0;
      boxes(a, dv, b, e, (t2, o2, b2, e2) => { if (t2 !== "traf") return;
        let tid = null, bdt = null, dd = 0, ds = 0, bdo = ti++ > 0; const trs = [];   // a later traf's base: moof only if flagged
        boxes(a, dv, b2, e2, (t3, o3, b3) => {
          if (t3 === "tfhd") { const fl = (a[b3 + 1] << 16) | (a[b3 + 2] << 8) | a[b3 + 3]; tid = r32(dv, b3 + 4); let p = b3 + 8;
            const x = s.tx[tid]; if (x) { dd = x[0]; ds = x[1]; }                    // trex defaults; tfhd's own win
            if (fl & 0x20000) bdo = false; if (fl & 1) { bdo = true; p += 8; } if (fl & 2) p += 4; if (fl & 8) { dd = r32(dv, p); p += 4; } if (fl & 0x10) { ds = r32(dv, p); p += 4; } }
          if (t3 === "tfdt") bdt = a[b3] === 1 ? r64(dv, b3 + 4) : r32(dv, b3 + 4);
          if (t3 === "trun") trs.push(b3); });
        const h = s.tr[tid];
        if (!trs.length || (h ? (h === "vide") !== video : s.vid !== video)) return;
        if (video && bdt !== null && s.nxt[tid] !== undefined && s.nxt[tid] !== bdt) s.pend = true;
        let dt = bdt || 0, sd = 0, c0 = null, n = 0, q = null;
        for (const tru of trs) {                // levi 18:43: every trun, in order; a later one's data follows the last's
          const v = a[tru], fl = (a[tru + 1] << 16) | (a[tru + 2] << 8) | a[tru + 3], nt = r32(dv, tru + 4);
          let p = tru + 8; if (fl & 1) { q = bdo ? null : o + dv.getInt32(p); p += 4; } if (fl & 4) p += 4;
          for (let k = 0; k < nt && n < 1024; k++, n++) { let du = dd, sz = ds, cto = 0;
            if (fl & 0x100) { du = r32(dv, p); p += 4; } if (fl & 0x200) { sz = r32(dv, p); p += 4; } if (fl & 0x400) p += 4;
            if (fl & 0x800) { cto = v === 1 ? dv.getInt32(p) : r32(dv, p); p += 4; }
            if (n === 0) c0 = cto;
            if (video) nv += 1;
            if (video && (s.pend || nv % KEY_EVERY === 0)) { s.pend = false;
              const kk = q !== null && sz ? vclKey(a, dv, q, Math.min(q + sz, a.length), s.hevc) : [null, null, 0];
              (r.key || (r.key = [])).push([tid, dt, cto, kk[0], kk[1], kk[2]]); }
            dt += du; sd += du; if (q !== null) q += sz; } }
        if (trs.length > 1) r.ntr = Math.max(r.ntr || 0, trs.length);
        if (bdt !== null) s.nxt[tid] = bdt + sd;
        r.v.push([tid, bdt, c0, n, sd]); }); }); };
  const wrap = (proto, name, before, after) => {
    const orig = proto && proto[name];
    if (typeof orig !== "function") return;
    proto[name] = function (...a) {
      if (before) timed(() => before(this, a));
      const ret = orig.apply(this, a);
      if (after) timed(() => after(this, a, ret));
      return ret;
    };
  };
  if (typeof MediaSource !== "undefined") wrap(MediaSource.prototype, "addSourceBuffer", null, (ms_, a, sb) => {
    if (first === null) first = { t: performance.now(), mime: String(a[0]).slice(0, 160) };
    kind(S(sb), a[0]); const ad = { op: "add", sb: id(sb), mime: String(a[0]).slice(0, 160) }; try { ad.mode = sb.mode; } catch (e) {}
    push(ad, true);
    if (K.end) sb.addEventListener("updateend", () => timed(() => { const b = sb.buffered, n = b.length, br = [];
      for (let i = 0; i < Math.min(n, 8); i++) br.push([b.start(i), b.end(i)]); const r = { op: "end", sb: id(sb), br, nr: n };
      try { r.tso = sb.timestampOffset; r.mode = sb.mode; } catch (e) {} push(r); })); });
  if (typeof SourceBuffer !== "undefined") {
    const P = SourceBuffer.prototype;
    wrap(P, "appendBuffer", (sb, a) => { const b = u8(a[0]); if (!b || b.length < 8) return; const s = S(sb);
      const dv = new DataView(b.buffer, b.byteOffset, b.byteLength); let moov = null, moof = false;
      boxes(b, dv, 0, b.length, (t, o, bb, e) => { if (t === "moov") moov = [bb, e]; if (t === "moof") moof = true; });
      if (moov) { const tx = [], tr = init(b, dv, moov[0], moov[1], tx); for (const x of tr) if (x[0] !== null) s.tr[x[0]] = x[2];
        s.tx = {}; for (const x of tx) s.tx[x[0]] = [x[1], x[2]];
        s.pend = true; const ri = { op: "init", sb: id(sb), bytes: b.length, tr }; if (tx.length) ri.tx = tx; push(ri, true); }
      if (!moof || !on()) return;
      if (s.vid && K.app) { const r = { op: "app", sb: id(sb), bytes: b.length, tso: sb.timestampOffset, v: [] };
        try { r.mode = sb.mode; } catch (e) {} frag(b, dv, s, r, true); push(r); }
      if (K.aapp) { const r = { op: "aapp", sb: id(sb), bytes: b.length, tso: sb.timestampOffset, v: [] }; frag(b, dv, s, r, false);
        if (r.v.length) push(r); } });
    wrap(P, "remove", (sb, a) => push({ op: "rm", sb: id(sb), s: a[0], e: a[1] }, true));
    wrap(P, "abort", (sb) => { S(sb).pend = true; push({ op: "abort", sb: id(sb), tso: sb.timestampOffset }, true); });
    wrap(P, "changeType", (sb, a) => { kind(S(sb), a[0]); push({ op: "ct", sb: id(sb), mime: String(a[0]).slice(0, 160) }, true); });
    const d = Object.getOwnPropertyDescriptor(P, "timestampOffset");
    if (d && d.set && d.get) Object.defineProperty(P, "timestampOffset", { configurable: true, enumerable: d.enumerable, get: d.get,
      set(v) { timed(() => { S(this).pend = true; push({ op: "tso", sb: id(this), v, was: d.get.call(this) }, true); }); d.set.call(this, v); } });
  }
  self.__lvqoeAnchorTake = () => { const b = recs; recs = [];
    return JSON.stringify({ recs: b, over, inst, first, ms: Math.round(ms * 10) / 10, nv, to: performance.timeOrigin }); };
  return "ok";
})()"""


def worker_anchor_js(block_s: int) -> str:
    return (WORKER_ANCHOR_JS.replace("__BLOCK_S__", str(int(block_s))).replace("__MAX__", str(FRAME_LOG_MSE_MAX))
            .replace("__KEY_EVERY__", str(WORKER_ANCHOR_KEY_EVERY))
            .replace("__KINDS__", json.dumps({k: 1 for k in WORKER_ANCHOR_KINDS}, sort_keys=True)))


def frame_log_arm(block_s: int, now_s: float | None = None) -> bool:
    """The epoch block clock's arm (content.js frameLogArm, MSE_WRAP_JS on()): always ON for block_s 0 ("1"), else ON in
    odd blocks of block_s s since the epoch."""
    if not block_s:
        return True
    return int((time.time() if now_s is None else now_s) // block_s) % 2 == 1


# levi 02:52 (era v4 backstop): the eras whose counted sessions need the per-frame log (their label path), as LITERAL
# strings like check_session.PICTURE_RUN_ERAS; an era bump decides in the same commit whether it joins
FRAME_LOG_REQUIRED_ERAS = frozenset({"v4-collector-2026-10-29"})


def frame_log_record_refusal(env, counted: bool, era: str) -> str | None:
    """levi 02:52: a counted v4 session without the per-frame log has no label of record, a loss no later fix recovers.
    On a counted run (LVDC_TRIO_RECORD=1 or a record-marker root) of an era in FRAME_LOG_REQUIRED_ERAS, the session is
    refused unless the log would run: LVQOE_FRAME_LOG exactly "1" (unset, "0", alt<N>, anything else: refused) with the
    instruments on. A refusal, never a default. Dry / pilot runs (counted False) are unchanged: unset is OFF, as before."""
    if not counted or era not in FRAME_LOG_REQUIRED_ERAS:
        return None
    raw = env.get(FRAME_LOG_ENV)
    if raw != "1":
        return (f"{FRAME_LOG_ENV}={raw!r} on a counted {era} run: the per-frame log is its label path, only \"1\" "
                f"is accepted (unset / 0 / alt<N> refused)")
    if env.get("LVQOE_INSTRUMENTS", "1") == "0":
        return f"LVQOE_INSTRUMENTS=0 on a counted {era} run: the per-frame log needs the instruments on"
    return None


def frame_log_config(raw: str | None, session_dir: Path | None, of_record: bool, instruments: bool) -> dict | None:
    """meta.json frame_log: None when off; else what was asked and whether it runs ({enabled, block_s, t0} or a reason)."""
    raw = (raw or "").strip()
    if raw in ("", "0"):
        return None
    out: dict = {"requested": raw, "enabled": False}
    if raw == "1":
        block_s = 0
    elif raw.startswith("alt") and raw[3:].isdigit() and int(raw[3:]) > 0:
        block_s = int(raw[3:])
    else:
        return {**out, "reason": "unparsed (1 or alt<N>)"}
    import data_root
    marked = session_dir is not None and any((a / data_root.RECORD_MARKER).is_file()
                                             for a in (Path(session_dir).resolve(), *Path(session_dir).resolve().parents))
    if (of_record or marked) and block_s:
        return {**out, "reason": ("of record" if of_record else "under a record root") + ": alt<N> is dry only"}
    if of_record or marked:
        out["record"] = True
    if not instruments:
        return {**out, "reason": "instruments off"}
    return {**out, "enabled": True, "block_s": block_s, "t0": 0, "clock": "epoch"}


def _side_streams(store: dict) -> dict[str, list]:
    """v3 (t67 A2): the extension's side-stream batches (chrome.storage.local keys side:<stream>:<ms>:<seq>, one per
    batch) joined per stream in key order, i.e. arrival order."""
    out: dict[str, list] = {}
    for k in sorted(x for x in store if isinstance(x, str) and x.startswith("side:")):
        out.setdefault(k.split(":")[1], []).extend(store[k] or [])
    return out


# boundary after window 1 (codex, levi 00106): before the export reads storage, the session page posts one "lvqoe-flush"
# (content.js flushes its event, side and stats buffers); iframes flush on their own 1 s timer, so the wait covers one
FINAL_FLUSH_WAIT_S = 1.5


def _final_flush(page) -> None:
    """Ask the session page's content script for a last flush, then wait for it (and the iframes' timers) to land."""
    if page is None:
        return
    try:
        page.eval("window.postMessage({source: 'lvqoe-flush'}, '*'); true")
    except Exception as exc:
        print(f"[browser] final flush not posted: {exc!r}", flush=True)
        return
    time.sleep(FINAL_FLUSH_WAIT_S)


def _write_durable(path: Path, text: str) -> None:
    """tmp -> fsync -> rename: the file is whole on disk, or absent, before storage is cleared."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _ext_export_record(session_dir: Path, t0: float | None = None, **rec) -> None:
    """levi 09:23 (F1): meta.ext_export on every exit of the export, success too, so an absent value never means
    unknown; exp.py copies the raw extension storage before the clone goes when ok is not true. levi 14:51 (brad's B2):
    dur_ms (wall, the export call's start to this record) and timeout_s (CDP_CALL_TIMEOUT_S: the export has no total
    timeout; each of its CDP calls waits at most this long), record only, in their own try (absent on an error)."""
    try:                                         # its own try: a timing fault never costs the record itself
        if t0 is not None:
            rec = {**rec, "dur_ms": round((time.time() - t0) * 1000, 1), "timeout_s": CDP_CALL_TIMEOUT_S}
    except Exception:
        pass
    try:
        import session_meta
        session_meta.write(session_dir, ext_export=rec)
    except Exception as exc:
        print(f"[browser] ext_export not recorded: {exc!r} {rec}", flush=True)


# levi 15:41 (t100 F1): one get(null) of every side key came back empty past a 64-128 MB store (the laptop synthetic:
# CDPPage.call's deadline runs out while websocket-client validates the reply's UTF-8, eval answers None) and the export
# said ok:true with no side file. The side keys are listed first (with their stored size, nothing crosses CDP but the
# list), then read in chunks bounded by bytes; the export is ok only if every listed key came back. The list never
# deserialises values: get(null) of a 256 MB store did not answer in 20 s (laptop synthetic at 01b9d1e1), so it is
# getKeys + per-key getBytesInUse; get(null) only where getKeys is missing (Chrome < 130).
SIDE_CHUNK_BYTES = 2_000_000          # levi 15:41: each chunk call well inside half CDP_CALL_TIMEOUT_S (laptop ~3 MB/s)
SIDE_FALLBACK_KEYS = 8                # chunk size in keys when the sizes are unreadable (getKeys fallback)
_SIDE_LIST_JS = ("new Promise(r => chrome.storage.local.getKeys(ks => { const s = ks.filter(k => k.startsWith('side:'))"
                 ".sort(); Promise.all(s.map(k => new Promise(q => chrome.storage.local.getBytesInUse(k, q))))"
                 ".then(n => r(JSON.stringify(s.map((k, i) => [k, n[i]])))); }))")
_SIDE_KEYS_JS = ("new Promise(r => chrome.storage.local.getKeys(ks => r(JSON.stringify(ks.filter(k => "
                 "k.startsWith('side:')).sort().map(k => [k, null])))))")
_SIDE_LIST_ALL_JS = ("new Promise(r => chrome.storage.local.get(null, d => r(JSON.stringify(Object.keys(d)"
                     ".filter(k => k.startsWith('side:')).sort().map(k => [k, JSON.stringify(d[k]).length])))))")
_SIDE_CHUNK_JS = "new Promise(r => chrome.storage.local.get(%s, d => r(JSON.stringify(d))))"


def _side_chunks(sizes: list) -> list[list[str]]:
    """Consecutive key groups of at most SIDE_CHUNK_BYTES (a larger key alone); SIDE_FALLBACK_KEYS keys when unsized."""
    out, cur, b = [], [], 0
    for k, n in sizes:
        n = SIDE_CHUNK_BYTES // SIDE_FALLBACK_KEYS if n is None else n
        if cur and b + n > SIDE_CHUNK_BYTES:
            out.append(cur)
            cur, b = [], 0
        cur.append(k)
        b += n
    return out + ([cur] if cur else [])


def _read_side_paged(sw, skip=frozenset()) -> tuple[dict, list[str], dict]:
    """(side {key: batch}, the listed keys, the read's record). A chunk whose reply is missing or short is recorded
    and its keys read one by one; a key that never comes back is left out, so the caller sees listed != read. Keys in
    skip (already read: the re-list before the clear) are neither listed nor read."""
    t = time.time()
    raw, via = None, None
    for js, via in ((_SIDE_LIST_JS, "sizes"), (_SIDE_KEYS_JS, "keys"), (_SIDE_LIST_ALL_JS, "sizes_all")):
        raw = sw.eval(js, await_promise=True)
        if raw is not None:
            break
    rec = {"via": via if raw is not None else None, "list_ms": round((time.time() - t) * 1000, 1)}
    if raw is None:
        return {}, [], rec
    sizes = [(k, n) for k, n in json.loads(raw) if k not in skip]
    listed = [k for k, _ in sizes]
    side, ms, failed = {}, [], []

    def get(keys):
        t1 = time.time()
        got = sw.eval(_SIDE_CHUNK_JS % json.dumps(keys), await_promise=True)
        ms.append(round((time.time() - t1) * 1000, 1))
        got = json.loads(got) if got else {}
        return {k: got[k] for k in keys if k in got}
    for i, keys in enumerate(_side_chunks(sizes)):
        got = get(keys)
        if len(got) < len(keys):
            failed.append({"i": i, "keys": len(keys), "got": len(got), "ms": ms[-1]})
            for k in keys:
                got.update(get([k]) if k not in got else {})
        side.update(got)
    rec.update(chunk_bytes=SIDE_CHUNK_BYTES, bytes=sum(n or 0 for _, n in sizes), chunks=len(ms),
               max_chunk_ms=max(ms, default=None), sum_chunk_ms=round(sum(ms), 1), chunk_ms=ms, failed=failed)
    missing = [k for k in listed if k not in side]
    if missing:
        rec["missing"] = {"n": len(missing), "first": missing[:5]}
    return side, listed, rec


def _export_via_service_worker(session_dir: Path) -> bool:
    """
    Read QoE events from the extension's chrome.storage.local via CDP,
    write labels.csv, and clear the storage for the next run.
    Returns True on success.

    Boundary after window 1 (codex, levi 00106): every file (side streams, player_stats.jsonl, labels.csv) is written
    durably BEFORE the storage clear; an exception or a full disk on the way leaves storage uncleared (the start path
    of the next session clears it, so the record is lost only if nothing reads it first, never by our own clear).
    """
    t0 = time.time()
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
            targets = json.loads(r.read())

        sw, tmp_tab = _ext_page()
        if sw is None:
            print("[browser] No extension context found (service worker idle and no extension page)", flush=True)
            _ext_export_record(session_dir, t0, ok=False, reason="no_ext_context")
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
        # levi 15:41 (t100 F1): a read with no answer is a failure, never "empty" (an empty store answers "[]")
        if events_json is None or stats_json is None:
            sw.close()
            _close_target(tmp_tab)
            _ext_export_record(session_dir, t0, ok=False,
                               reason="events_read_failed" if events_json is None else "stats_read_failed")
            return False
        # v3 (t67 A2): the side streams (player_events, ...), one key per batch, each to <stream>.jsonl; levi 15:41:
        # listed first, then read in byte-bounded chunks (_read_side_paged)
        side, listed, side_read = _read_side_paged(sw)
        ext = {"side_keys": len(side), "side_keys_listed": len(listed), "side_read": side_read}   # brad / levi 18:17
        try:
            ext["bytes_in_use"] = sw.eval("new Promise(r => chrome.storage.local.getBytesInUse(null, r))", await_promise=True)
            # levi 18:26: the content script's final side_dropped / side_failed / off_arm_cb (the gate counters read meta)
            ext["counters"] = json.loads(sw.eval("new Promise(r => chrome.storage.local.get({side_counters: null}, "
                                                 "d => r(JSON.stringify(d.side_counters))))", await_promise=True) or "null")
        except Exception as exc:
            ext["error"] = repr(exc)[:200]
        try:
            import session_meta
            session_meta.write(session_dir, ext_storage=ext)
        except Exception as exc:
            print(f"[browser] ext_storage not recorded: {exc!r}", flush=True)
        # brad / levi 23:10-23:21: one map of side stream -> session file (A1 is rvfc_frames.jsonl), every name resolved
        # and every record written BEFORE storage is cleared; an unmapped stream goes to side_<stream>.jsonl, flagged
        import session_files
        unmapped = {}

        def write_side(keys):
            """(Re)write the files of the streams these keys belong to, from every key read so far."""
            streams = {k.split(":")[1] for k in keys}
            for stream, recs in _side_streams(side).items():
                if stream not in streams:
                    continue
                fname, mapped = session_files.side_file(stream)
                if not mapped:
                    unmapped[stream] = {"stream": stream, "file": fname, "records": len(recs)}
                _write_durable(session_dir / fname, "".join(json.dumps(rec) + "\n" for rec in recs))
                print(f"[browser] {stream} ({len(recs)} records) → {session_dir / fname}", flush=True)
            if unmapped:
                try:
                    import session_meta
                    session_meta.write(session_dir, side_streams_unmapped=list(unmapped.values()))
                except Exception as exc:
                    print(f"[browser] side_streams_unmapped not recorded: {exc!r} {unmapped}", flush=True)
        write_side(side)
        stats = json.loads(stats_json) if stats_json else []
        if stats:
            _write_durable(session_dir / "player_stats.jsonl", "".join(json.dumps(rec) + "\n" for rec in stats))
            print(f"[browser] Player stats ({len(stats)} records) → {session_dir / 'player_stats.jsonl'}", flush=True)

        def clear_storage():
            # Clear storage so the next run starts fresh: only after every file above / below is on disk. t102 #7: a
            # session the F1 reference keep selects (profile_clone.f1_ref_keep, its id only) keeps its storage for exp.py
            from profile_clone import f1_ref_keep
            ref = f1_ref_keep(session_dir.name)
            if not (ref and ref["selected"]):
                sw.eval("new Promise(r => chrome.storage.local.remove(['events', 'player_stats', 'side_counters'].concat(%s), r))"
                        % json.dumps(sorted(side)), await_promise=True)
            sw.close()
            _close_target(tmp_tab)

        def side_incomplete(n_events):
            # levi 15:58: the store is listed again just before the clear; a key written since the first list (a late
            # flush) is read the same way and its stream's file rewritten (n_late). levi 15:41 (a): every listed side
            # key exported, else ok:false and storage kept (exp.py's F1 raw keep)
            late, late_listed, relist = _read_side_paged(sw, skip=frozenset(listed))
            side.update(late)
            listed.extend(late_listed)
            side_read.update(n_late=len(late_listed), relist={k: relist.get(k) for k in
                                                              ("via", "list_ms", "chunks", "failed", "missing")})
            if late:
                write_side(late)
            ext.update(side_keys=len(side), side_keys_listed=len(listed))
            try:
                import session_meta
                session_meta.write(session_dir, ext_storage=ext)
            except Exception as exc:
                print(f"[browser] ext_storage not recorded: {exc!r}", flush=True)
            if side_read["via"] is not None and relist["via"] is not None and len(side) == len(listed):
                return False
            print(f"[browser] side streams incomplete: {len(side)} of {len(listed)} keys read; storage kept", flush=True)
            sw.close()
            _close_target(tmp_tab)
            _ext_export_record(session_dir, t0, ok=False, reason="side_incomplete", n_expected=len(listed),
                               n_got=len(side), events=n_events)
            return True

        events: list[dict] = json.loads(events_json) if events_json else []
        if not events:
            print("[browser] Extension storage empty — no QoE events collected", flush=True)
            if side_incomplete(0):
                return False
            clear_storage()
            _ext_export_record(session_dir, t0, ok=True, events=0)
            return False

        # v2 (LVQoE t35): new columns appended after the v1 ones (additive; v1 readers keep working)
        header = ["ts", "type", "video_w", "video_h", "client_w", "client_h",
                  "dropped", "current_time", "tabId", "url", "event_id",
                  "buffered_ahead_s", "playback_rate", "duration_s", "is_ad", "ad_signals", "src_changed",
                  "login_wall_text", "login_wall", "quality_countdown", "paused", "playhead_still_s", "ready_state",
                  "visibility", "total_frames",                        # t53 (am. 13 / gate-2 4a): additive
                  "el_id", "src_key",                                  # v3 A10 (t67): additive
                  "buffered_ranges",                                   # v3 A6 (t67): additive
                  # boundary after window 1 (codex item 1): the content-script instance, the sending frame / document,
                  # and the instance's cumulative overflow drops (empty when none): additive
                  "cs", "frameId", "documentId", "dropped_n", "dropped_first_id", "dropped_last_id"]
        rows = [",".join(
            '"{}"'.format(str(e.get(k, "")).replace('"', '""')) for k in header
        ) for e in events]
        csv_text = "\n".join([",".join(header)] + rows)

        dest = session_dir / "labels.csv"
        _write_durable(dest, csv_text)
        print(f"[browser] Extension labels ({len(events)} events) → {dest}", flush=True)
        print("[browser] labels.csv written before storage clear", flush=True)
        if side_incomplete(len(events)):
            return False
        clear_storage()
        print("[browser] Extension storage cleared after export", flush=True)
        _ext_export_record(session_dir, t0, ok=True, events=len(events))
        return True

    except Exception as exc:
        print(f"[browser] Extension CSV export error: {exc}", flush=True)
        _ext_export_record(session_dir, t0, ok=False, error=repr(exc)[:200])
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


# WA2 r1 (00:07, peter): a pinned tt room page (/@user/live) ran the tt pick, whose `a[href*="/@"][href*="/live"]` took
# the side nav's first "Suggested LIVE creator" (another room), and nothing compared the landed room with the intended
# one. A room page is never picked from, and every given first_url / picked room is checked after navigation (levi 00:09:
# every path): the identity is the tt @user, the twitch channel, the yt channel @handle; None = not verifiable, recorded.
_TT_ROOM_RE = re.compile(r"tiktok\.com/@([^/?#]+)/live", re.I)
_TW_CHANNEL_RE = re.compile(r"twitch\.tv/([A-Za-z0-9_]{2,})/?(?:[?#]|$)", re.I)
_TW_NOT_CHANNEL = {"directory", "videos", "search", "settings", "p", "downloads", "turbo", "jobs"}
_YT_HANDLE_RE = re.compile(r"youtube\.com/(@[^/?#]+)", re.I)
# the yt player's own owner (the watch page a /@handle/live lands on carries no handle in its URL)
_YT_OWNER_JS = ("(function(){try{const p=document.querySelector('#movie_player');"
                "const rs=[p&&p.getPlayerResponse&&p.getPlayerResponse(),window.ytInitialPlayerResponse];"
                "for(const r of rs){const m=r&&r.microformat&&r.microformat.playerMicroformatRenderer;"
                "if(m&&m.ownerProfileUrl&&/\\/@/.test(m.ownerProfileUrl))return m.ownerProfileUrl}"
                "const a=document.querySelector('ytd-video-owner-renderer a[href*=\"/@\"]');return a?a.href:null"
                "}catch(e){return null}})()")


def page_identity(platform: str, url: str | None) -> str | None:
    """The room / channel a URL names (lower case): tt @user of a /@user/live page, a twitch channel page's channel, a
    youtube /@handle; None for anything else (a directory, a watch page, a feed)."""
    u = url or ""
    if platform == "tiktok":
        m = _TT_ROOM_RE.search(u)
        return ("@" + m.group(1).lstrip("@")).lower() if m else None
    if platform == "twitch":
        m = _TW_CHANNEL_RE.search(u)
        return m.group(1).lower() if m and m.group(1).lower() not in _TW_NOT_CHANNEL else None
    if platform == "youtube":
        m = _YT_HANDLE_RE.search(u)
        return m.group(1).lower() if m else None
    return None


# brad / levi 00:45: after a re-entry the reload's player response / owner link can still be loading, and "unverifiable"
# on a pinned session stops the whole A/B: the re-entry read is retried while unreadable (the start path reads once)
IDENTITY_RETRY_S, IDENTITY_POLL_S = 5.0, 0.5


def identity_check_settled(platform: str, intended_url: str | None, page, retry_s: float = IDENTITY_RETRY_S) -> dict:
    """identity_check, re-read every IDENTITY_POLL_S for up to retry_s while the landed identity is unreadable (ok None
    with an intended identity); the attempts are recorded."""
    rec, n, end = identity_check(platform, intended_url, page), 1, time.time() + retry_s
    while rec["ok"] is None and rec["intended"] and time.time() < end:
        time.sleep(IDENTITY_POLL_S)
        rec, n = identity_check(platform, intended_url, page), n + 1
    rec["attempts"] = n
    return rec


def identity_check(platform: str, intended_url: str | None, page) -> dict:
    """{intended, landed, ok}: ok False = the page landed elsewhere (a named startup failure), None = not verifiable
    (no intended identity, or the landed one unreadable: recorded, never an abort)."""
    intended = page_identity(platform, intended_url)
    rec: dict = {"intended_url": intended_url, "intended": intended, "landed": None, "ok": None}
    try:
        href = page.eval("location.href")
        rec["landed_url"] = href
        landed = page_identity(platform, page.eval(_YT_OWNER_JS)) if platform == "youtube" else page_identity(platform, href)
    except Exception as exc:
        rec["error"] = repr(exc)[:200]
        landed = None
    rec["landed"] = landed
    if intended and landed:
        rec["ok"] = intended == landed
    return rec


# B9 (levi 23:21-23:22, peter's cost a57ef9b8): the join runs on the session's first shaped state, so its waits scale with
# that state's rate: per URL max(45, 1.5 x B / rate), B the most bytes WC1's unshaped joins fetched before the first frame
# (rounded up), capped at JOIN_CAP_S. A media-attached page with no first frame by then is a join_timeout (its own abort
# reason, never "Failed to navigate to a live": not a channel problem, never offline, never set aside).
JOIN_BASE_S = 45.0
JOIN_CAP_S = 600.0
JOIN_BYTES = {"youtube": 5.0e6, "twitch": 8.5e6, "tiktok": 6.5e6}
JOIN_TIMEOUT_PREFIX = "join_timeout at state "


def join_timeout_s(platform: str, rate_kbit: float | None) -> float:
    """The per-URL join wait at a first state of `rate_kbit` (None: unshaped, the base)."""
    if not rate_kbit or rate_kbit <= 0:
        return JOIN_BASE_S
    return round(min(JOIN_CAP_S, max(JOIN_BASE_S, 1.5 * JOIN_BYTES.get(platform, 8.5e6) / (rate_kbit * 125.0))), 1)


def _rx_or_none(rx) -> int | None:
    try:
        return int(rx()) if rx is not None else None
    except Exception:
        return None


class JoinTimeout(RuntimeError):
    """B9: the page loaded and media was attached, but no first frame by the scaled cap."""

    def __init__(self, state, seconds: float, detail: dict | None = None):
        super().__init__(f"{JOIN_TIMEOUT_PREFIX}{state} ({int(round(seconds))} s)")
        self.detail = detail or {}


JOIN_RX_MIN_BYTES = 65536        # D3 (levi 23:49): media bytes advancing over the wait, on the slot's ingress counter
_JOIN_EVIDENCE_JS = """
(function() {
    const v = document.querySelector('#movie_player video') || document.querySelector('video');
    if (!v) return JSON.stringify({attached: false});
    let buf = null;
    try { if (v.buffered.length) buf = v.buffered.end(v.buffered.length - 1); } catch (e) {}
    const badge = !!document.querySelector('.ytp-live-badge, .ytp-live, .channel-root--live, .live-time, ' +
                                           'p[data-a-target="stream-title"]');
    return JSON.stringify({attached: !!(v.currentSrc || v.srcObject), dur_inf: v.duration === Infinity, badge: badge,
                           buf_end: buf, decoded: v.webkitVideoDecodedByteCount || null});
})()
"""


def _join_evidence(page) -> dict:
    try:
        return json.loads(page.eval(_JOIN_EVIDENCE_JS) or "{}") or {}
    except Exception:
        return {}


def join_timeout_due(ev0: dict, ev1: dict, rx0: int | None, rx1: int | None, live: bool | None = None) -> tuple[bool, dict]:
    """B9 D3 / Q1 (levi 23:49): a join timed out (rather than a dead or not-live page) iff, over the full scaled wait,
    media was attached, media bytes advanced (the slot's ingress counter by >= JOIN_RX_MIN_BYTES; without one, the
    page's buffered end or decoded bytes moved) and the page showed the live evidence the playing check uses (a
    non-finite duration or the live badge; `live` overrides, tt's list badge). (due, the evidence). The rx counter is all
    of the slot's ingress (page, chat, ads too: brad 00:03), so "flowing" is nearly always true in a slow join; attached
    and live evidence do the discriminating."""
    attached = bool(ev1.get("attached"))
    if rx0 is not None and rx1 is not None:
        flowing, how = (rx1 - rx0) >= JOIN_RX_MIN_BYTES, "rx"
    else:
        flowing, how = ((ev1.get("buf_end") or 0) > (ev0.get("buf_end") or 0)
                        or (ev1.get("decoded") or 0) > (ev0.get("decoded") or 0)), "page_media"
    is_live = bool(live) if live is not None else bool(ev1.get("dur_inf") or ev1.get("badge"))
    why = {"attached": attached, "flowing": flowing, "flowing_by": how, "live": is_live,
           **({"rx_bytes": rx1 - rx0} if rx0 is not None and rx1 is not None else {})}
    return attached and flowing and is_live, why


def _navigate_to_live(page: CDPPage, platform: str, timeout: float = 45.0, first_url: str | None = None,
                      exclude: list[str] | None = None, record: dict | None = None, claim=None,
                      scale: float = 1.0, join_state=None, rx=None, cancel=None) -> str | None:
    """Returns the list URL that delivered a playing live video (None if none did). claim(url), when given, is called with
    the channel as soon as it is picked (a given channel page before navigating; twitch's directory pick on the click),
    before the playback check (t58 N-2: the sibling claim)."""
    """
    Try each URL in LIVE_URLS[platform].  For directory/discovery pages,
    execute platform-specific JS to click into a live room.  Then kick play
    via JS and verify the video timestamp is actually advancing.
    B9 (with join_state): each URL has ONE deadline, its start + timeout x scale (= browser.join_timeout_s), that every
    wait inside clips to (D1); the walk stops at JOIN_CAP_S over all URLs, or when cancel() (the session's schedule
    ended); a URL that spent its full wait with media attached, media bytes advancing (rx(), the slot's ingress
    counter) and live evidence raises JoinTimeout (D3 / Q1: the walk stops, never re-picked); else the next URL, and None
    at the end (the caller's "Failed to navigate to a live"). Without join_state: the pre-B9 waits.
    """
    urls = list(LIVE_URLS[platform])
    if first_url:
        # the rotation's channel only: falling back to another channel would break the rotation's accounting
        urls = [first_url]
    t_walk = time.time()
    stop = (lambda: bool(cancel and cancel()))                     # noqa: E731
    for url in urls:
        if join_state is not None and (time.time() - t_walk >= JOIN_CAP_S or stop()):
            print(f"[browser] join walk stopped ({'cancelled' if stop() else 'the cap'})", flush=True)
            break
        t_url = time.time()
        url_deadline = t_url + timeout * scale if join_state is not None else None

        def wait_s(own: float) -> float:                           # D1: every wait clips to the URL's deadline
            return own * scale if url_deadline is None else max(0.0, min(own * scale, url_deadline - time.time()))
        print(f"[browser] Navigating to {url}", flush=True)
        if claim is not None and first_url:
            claim(url)
        page.navigate(url)
        # Wait until location.href actually changes — _wait_for_load alone is not
        # enough because the old page may already be in readyState=complete.
        nav_deadline = time.time() + wait_s(25)
        while time.time() < nav_deadline:
            current = page.eval("location.href") or ""
            if current.startswith(url[:30]):
                break
            time.sleep(0.5)
        _wait_for_load(page, timeout=wait_s(15))

        # For directory/discovery pages, click into the first live stream.
        # Twitch is a heavy SPA — wait for stream cards to render after load.
        pick_js = _PICK_STREAM_JS.get(platform, "null")
        if platform == "twitch" and "/directory/" not in url:
            pick_js = "null"          # a channel page (esports rotation): no card to click; never click away from it
        if platform == "tiktok" and _TT_ROOM_RE.search(url):
            pick_js = "null"          # a room page (a W-A pin): never click into the side nav's suggested rooms (WA2 r1)
        if pick_js.strip() != "null":
            card_js = _CARD_READY_JS.get(platform)
            if card_js:
                # Poll until at least one card link is visible (max 15s); the pick otherwise runs before the list renders
                card_deadline = time.time() + wait_s(15)
                while time.time() < card_deadline:
                    if page.eval(card_js):
                        break
                    time.sleep(1)
            if platform == "twitch" and exclude is not None:
                try:
                    res = json.loads(page.eval(_pick_twitch_js(exclude)) or "{}")
                except Exception as exc:
                    res = {"error": repr(exc)}
                if record is not None:
                    record.update(directory=url, excluded_recent=exclude, candidates=res.get("candidates"),
                                  pick=res.get("pick"), error=res.get("error"))
                clicked = res.get("pick") or (page.eval(pick_js) if not res.get("candidates") else None)
                if claim is not None and isinstance(clicked, str) and clicked.startswith("http"):
                    claim(clicked)
            else:
                clicked = page.eval(pick_js)
            if clicked:
                print(f"[browser] Clicked into stream: {clicked}", flush=True)
                _wait_for_load(page, timeout=wait_s(15))

        # Kick playback (handles Chrome autoplay block)
        page.eval(_CLICK_PLAY_JS)
        time.sleep(2)
        page.eval(_CLICK_PLAY_JS)  # second nudge in case first hit an ad overlay

        # Verify the video is actually advancing (not just paused at t=0). Pre-roll ads are handled here too: the
        # ad watcher only starts after navigation, and a pre-roll would otherwise keep the content from advancing.
        deadline = url_deadline if url_deadline is not None else time.time() + timeout * scale
        ev0, rx0 = (_join_evidence(page), _rx_or_none(rx)) if join_state is not None else ({}, None)
        skip_js = _SKIP_AD_JS.get(platform, "null")
        while time.time() < deadline and not stop():
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
        if join_state is not None and time.time() >= deadline and not stop():   # B9: the full wait spent
            due, why = join_timeout_due(ev0, _join_evidence(page), rx0, _rx_or_none(rx))
            if record is not None:
                record.setdefault("join_tries", []).append({"url": url, "s": round(time.time() - t_url, 1), **why})
            if due:
                print(f"[browser] join timeout on {url}: {why}", flush=True)
                raise JoinTimeout(join_state, time.time() - t_url, {"url": url, **why})

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
# yt live auto-captions draw over the lower band of the player (brad 2026-10-17, yt_2's shots; R-O's motion crop): the
# collector turns them off, like the chat, at start and whenever they come back (checked every CAPTIONS_CHECK_S;
# each turn-off logged in ad_actions.jsonl as captions_off). Traffic: one timedtext request in yt_29's whole capture.
_CAPTIONS_OFF_JS = """
(function() {
    const b = document.querySelector('#movie_player .ytp-subtitles-button');
    if (!b || b.offsetParent === null) return 'no-button';
    if (b.getAttribute('aria-pressed') !== 'true') return 'already-off';
    b.click();
    return b.getAttribute('aria-pressed') === 'true' ? 'click-failed' : 'turned-off';
})()
"""
CAPTIONS_CHECK_S = 10
# levi 2026-10-17 (tw_10, tw_12): no human ever pauses these players, so every pause is a self-pause, and the collector
# presses play as a viewer would. _PLAYER_STATE_JS reads the largest visible <video> plus content.js's stall and ad
# labels; SelfPauseResumer decides; _PLAY_JS presses. Logged in ad_actions.jsonl as resume_after_self_pause.
_PLAYER_STATE_JS = """
(function() {
    const vs = [...document.querySelectorAll('video')].filter(v => v.getClientRects().length);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    if (!v) return null;
    const d = document.documentElement.dataset;
    // the buffer ahead of the playhead in its own buffered range (the self-pause press's floor, levi 00130)
    let buf = 0;
    for (let i = 0; i < v.buffered.length; i++) {
        if (v.buffered.start(i) <= v.currentTime + 0.05 && v.currentTime <= v.buffered.end(i)) buf = v.buffered.end(i) - v.currentTime;
    }
    return JSON.stringify({paused: v.paused, rs: v.readyState, ended: v.ended, ad: d.lvqoeAd === '1',
                           tf: v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames : null,
                           stall: d.lvqoeStall === '1', buf: Math.round(buf * 1000) / 1000,
                           ct: Math.round(v.currentTime * 1000) / 1000});
})()
"""
# v3 A8 (t67; levi n1): the screen as the page sees it (size, available size, DPR, colour depth) and the refresh it is
# paced at, measured: the median requestAnimationFrame interval over up to SCREEN_RAF_FRAMES frames (3 s cap; with the
# display off the pacing is what the session ran under, which A8's C5 reads against xrandr). Read-only; no request.
SCREEN_RAF_FRAMES = 120
CLOCK_SAMPLE_S = 60          # v3 A11: the watcher samples the time.time <-> page clock offset this often
_A8_SCREEN_JS = """
new Promise((res) => {
  const t = [];
  let done = false;
  const fin = () => {
    if (done) return;
    done = true;
    const d = t.slice(1).map((x, i) => x - t[i]).sort((a, b) => a - b);
    const med = d.length ? d[Math.floor(d.length / 2)] : null;
    res(JSON.stringify({w: screen.width, h: screen.height, avail_w: screen.availWidth, avail_h: screen.availHeight,
                        dpr: window.devicePixelRatio || 1, color_depth: screen.colorDepth,
                        raf_frames: t.length, raf_median_ms: med, refresh_hz: med ? Math.round(10000 / med) / 10 : null,
                        visibility: document.visibilityState}));
  };
  const f = (ts) => { t.push(ts); if (t.length < %d) requestAnimationFrame(f); else fin(); };
  requestAnimationFrame(f);
  setTimeout(fin, 3000);
})
""" % (SCREEN_RAF_FRAMES + 1)
_PLAY_JS = """
(function() {
    const vs = [...document.querySelectorAll('video')].filter(v => v.getClientRects().length);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    if (!v || !v.paused) return null;
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
    return 'pressed';
})()
"""
# t56 stall loops (levi's ruling 00130, at the N_collect boundary): the after_stall press (b370999, 2026-10-17: play at
# once when the stall label was on and readyState >= 3) is gone. It pressed at a buffer p50 of ~0.5 s and led most trio
# twitch stall-loop restarts (peter 00215); a viewer waits through a stall. The press stays only for a self-pause (tw_45):
# the element paused, not stalled (stall label off), not waiting (readyState >= 3), not an ad, not ended, its buffer
# ahead >= SELF_PAUSE_MIN_BUFFER_S held for SELF_PAUSE_HOLD_S, and RESUME_BACKOFF_S since the last restart (any paused ->
# playing, the collector's or the player's own). Every press logs its reason and buffer; the polls held back, by reason,
# go to meta.json self_pause_resume.
SELF_PAUSE_HOLD_S = 2.0
SELF_PAUSE_MIN_BUFFER_S = 1.5
RESUME_BACKOFF_S = 10.0
RESUME_RULE = ("levi 00130 (2026-10-20): no press in a stall; a self-pause pressed at buffer >= 1.5 s held 2 s, "
               "10 s after the last restart; levi w6 (2026-10-21): paused outside a stall label with no buffer growth for "
               "30 s -> one press, still paused 30 s later -> a stuck player; levi 13:09 (2026-10-23, tw_100109): "
               "paused at readyState >= 3 with buffer >= 1.5 s, flat for 30 s -> the same one press, whatever the stall "
               "label; levi 16:56 (2026-10-26, t79, WC3 tw_100001): paused under a stall label at readyState 1-2 with "
               "buffer < 1.5 s, the playhead past 1 s, buffer and playhead flat for 30 s -> the same one press (paused_dry_flat); "
               "levi 01:40 (2026-10-27, W1 tw_100113): the same at readyState >= 3 with 0 <= buffer < 1.5 s "
               "(paused_under_floor_flat)")
# levi w6 (15:38, tw_100079): 00130's buffer floor deadlocks a live player that paused itself: paused, it stops
# fetching, so the buffer never reaches the floor, nothing presses, and the frozen watchdog (content.js) skips paused
# ticks. A pause whose polls are held for "buffer" (levi / supervisor 15:40: never "stall"; the twitch startup
# rebuffer, readyState 2 under a stall label, never trips it) with the buffer flat and the playhead still
# (PAUSE_GROWTH_EPS_S) for PAUSE_DEADLOCK_S gets one press (logged, reason paused_no_buffer_growth); still paused PAUSE_DEADLOCK_S after it, the pause is fed to the
# stuck-player rule as a frozen run from the pause's start (stuck_frozen_s): re-entry / abort by the normal rules, the
# low-state hold applying there.
# levi 13:09 (2026-10-23, tw_100109: 1908 s paused at readyState 4 with 4 s buffered and the playhead still, under a
# stall label that never cleared, since content.js clears it only on "start"): the stall label does not hold this press
# back when the poll shows data (readyState >= 3, buffer >= SELF_PAUSE_MIN_BUFFER_S). Under a stall label a poll with
# no data (buffer under the floor or unknown) still restarts the clock: a viewer waits through a stall (00130). The
# one press is also reached after TW_BUTTON_MAX_FAILS (gave_up), which before held the above-floor pause for good.
# t79 (levi 16:56, 2026-10-26; WC3 tw_100001: paused 750 s to the session end at readyState 2, buffer 0.067 s and the
# playhead 989.72 s the same to the bit, under a stall label, while the link carried 1-2 Mbit; held "stall" 765, 0 presses):
# a paused live player stops fetching, so its dry buffer never refills, the label never clears, and the clock above never
# started. A paused poll under a stall label at readyState 1-2 with the buffer under the floor (or none) and the playhead
# past DRY_PAUSE_MIN_PLAYHEAD_S no longer restarts it: flat for PAUSE_DEADLOCK_S -> the one press, reason paused_dry_flat;
# still paused after it -> the stuck-player rule as above. A player that fetches grows its buffer well inside 30 s (twitch's
# lowest rung ~230 kbit at the lever's 115 kbit: a 2 s segment in ~4 s), so flat to 0.1 s for 30 s means not fetching. The
# playhead floor keeps the twitch startup rebuffer (playhead 0, levi / supervisor 15:40) out; the 9 pilot dry runs of
# >= 30 s (WB..WC3b) all sat at a playhead >= 4.7 s.
# levi 01:40 (2026-10-27, 00283 (a); counted window 1 tw_100113: paused 1272 s from +373.9 under a stall label, readyState 1
# for 16 s then 4 with 1.149 s buffered, the playhead 342.817 s still; freed only when the preview element's start cleared
# the page's label): the shape between the two rules above (readyState >= 3, a buffer known and under the floor, under a
# stall label, the playhead past DRY_PAUSE_MIN_PLAYHEAD_S) counts toward the same flat clock, reason
# paused_under_floor_flat. A pause moving from readyState 1-2 to >= 3 keeps its clock; only growth restarts it.
PAUSE_DEADLOCK_S = 30.0
PAUSE_GROWTH_EPS_S = 0.1
DRY_PAUSE_MIN_PLAYHEAD_S = 1.0
# t53 (supervisor, peter's tw_100005 / tw_100006 read): after a state-1 underrun Twitch's own controller stays "paused" and
# undoes a video.play() at once (start re-paused in the same ms, every press). On twitch a press re-paused within
# RESUME_CHECK_S is retried through Twitch's play button (a click, or a pause / play cycle when the button says
# playing over the paused element), which goes through the controller; after
# TW_BUTTON_MAX_FAILS button presses that leave it paused, the collector stops pressing for that pause and the frozen
# watchdog's re-entry (a reload) stays the fallback. Every press and its outcome go to ad_actions.jsonl.
RESUME_CHECK_S = 1.0
FROZEN_HOLD_POLL_MAX_AGE_S = 5.0     # _on_frozen's starvation evidence: the resumer's last poll at most this old
FROZEN_HOLD_RULE = ("levi 13:09 / 17:51 / 18:16 (2026-10-23, tw_100109): the low-state hold of addendum N's frozen clock "
                    "and of the stuck rule's T2 lifts only after 30 s of polls paused with data (readyState >= 3, buffer "
                    ">= 1.5 s, buffer and playhead flat within 0.1 s), and both clocks count from the lift; a last poll "
                    "older than 5 s: held")
TW_BUTTON_MAX_FAILS = 3
_TW_BUTTON_JS = """
(function() {
    const b = document.querySelector('button[data-a-target="player-play-pause-button"]');
    if (!b) return JSON.stringify({present: false});
    const st = b.getAttribute('data-a-player-state'), label = b.getAttribute('aria-label') || '';
    const out = {present: true, state: st || (/^play/i.test(label) ? 'paused' : 'playing'), label: label};
    // levi (252353d): the collector must never pause a playing element (a pause event it caused). The button says
    // playing: click (Twitch's pause) only while the element is paused, in this same evaluation; a pause() on a paused
    // element fires no pause event
    const vs = [...document.querySelectorAll('video')].filter(v => v.getClientRects().length);
    const v = vs.sort((a, c) => c.clientWidth * c.clientHeight - a.clientWidth * a.clientHeight)[0];
    if (out.state === 'playing' && v && !v.paused) { out.skipped = 'element playing'; }
    else if (%s) b.click();
    return JSON.stringify(out);
})()
"""
TW_BUTTON_CYCLE_GAP_S = 0.5     # the controller says playing over a paused element: pause, then play, through it


RELOAD_CT_DROP_S = 5.0     # t83 (1): a playhead this far behind the press's is the source reloaded (Twitch's live-edge
# reload sets it to ~0; a seek back of a live player does not happen in the collector)


class SelfPauseResumer:
    """When to press play (RESUME_RULE, the comment above). A press that does not take is retried only after another
    SELF_PAUSE_HOLD_S of eligibility. Each press returns a log record with its reason, buffer and paused_s (the time
    since the element was first seen paused); record() is meta.json's self_pause_resume."""

    def __init__(self, hold_s: float = SELF_PAUSE_HOLD_S, min_buffer_s: float = SELF_PAUSE_MIN_BUFFER_S,
                 backoff_s: float = RESUME_BACKOFF_S):
        self.hold_s, self.min_buffer_s, self.backoff_s = hold_s, min_buffer_s, backoff_s
        self.paused_since = None      # first observation of the current pause
        self.eligible_since = None    # start of the current eligible run (or of the wait after a press)
        self.button_fails = 0         # twitch: button presses that left the current pause paused
        self.gave_up = False          # twitch: TW_BUTTON_MAX_FAILS reached; the frozen watchdog takes over
        self.was_paused = None        # the last observation's paused, for the restarts
        self.restart_at = None        # the last paused -> playing transition observed
        self.presses = 0
        self.held: collections.Counter = collections.Counter()   # paused polls not pressed, by reason
        self.buf_peak = None          # the current pause's highest buffer ahead
        self.flat_since = None        # the last time the buffer grew (or the pause began)
        self.deadlock_press_at = None # this pause's deadlock press (once per pause)
        self.ct_last = None           # the last poll's playhead
        self.deadlocks: list[dict] = []   # meta.json self_pause_resume.deadlocks (capped)
        self.last_poll = None         # (time, state) of the last poll with a state
        self.data_flat_since = None   # levi 18:16: start of the current run of polls paused with data and flat (the lift)
        self._df_ref = (None, None)   # that run's first (buffer, playhead)
        # levi 00176 (W-B yt_100003): the same run counted WITH ad polls, for the ad_frozen clock's lift (an ad break stuck
        # over a paused, buffered element); the press trigger's run above keeps excluding ads
        self.data_flat_any_since = None
        self._dfa_ref = (None, None)
        self.press_log: list[dict] = []   # t83 (2): every press, any reason (deadlocks keeps the _deadlock ones), capped
        self._open_press = None       # press_log's row awaiting resume_press's outcome
        self._unresolved: list[dict] = []   # press_log rows not yet followed by playback (t83 (1))
        self.stopped_at = None        # t83 (3): no press once the session's teardown begins (stop())

    def record(self) -> dict:
        return {"rule": RESUME_RULE, "hold_s": self.hold_s, "min_buffer_s": self.min_buffer_s,
                "backoff_s": self.backoff_s, "presses": self.presses, "held_polls": dict(sorted(self.held.items())),
                "deadlock_s": PAUSE_DEADLOCK_S, "deadlocks": self.deadlocks[:WATCH_LOG_MAX],
                "press_log": [dict(r) for r in self.press_log[:WATCH_LOG_MAX]], "stopped_at": self.stopped_at}

    def stop(self, now: float | None = None) -> None:
        """t83 (3), s1 tw_100001: the teardown pressed after meta.json's snapshot. From here step() presses nothing
        (its paused polls are held as "stopping"); the first call's time is kept."""
        if self.stopped_at is None:
            self.stopped_at = time.time() if now is None else now

    def _logged(self, press: dict, state: dict, now: float) -> dict:
        """t83 (2): the press into press_log with its poll (resume_press adds the outcome); the press returned."""
        if len(self.press_log) < WATCH_LOG_MAX:
            self.press_log.append({"t": now, "reason": press["reason"], "paused_since": self.paused_since,
                                   "buffer_s": press.get("buffer_s"), "rs": state.get("rs"), "ct": state.get("ct"),
                                   "stall_label": bool(state.get("stall"))})
            self._open_press = self.press_log[-1]
            self._unresolved.append(self.press_log[-1])
        return press

    def _follow_presses(self, state: dict, now: float) -> None:
        """t83 (1) (peter's re-pause read, 39 tw presses): Twitch undoes video.play(), the button cycle reloads the
        source at the live edge (playhead to ~0, readyState 1) and playback follows seconds later, after resume_press's
        +2.5 s check. Each press row gets the first later poll with the playhead back by > RELOAD_CT_DROP_S (reload_at,
        reload_ct) and the first poll playing at readyState >= 3 (resumed_at, resumed_after_s); a row without
        resumed_at at the end was not followed by playback."""
        ct, playing = state.get("ct"), not state.get("paused") and (state.get("rs") or 0) >= 3
        for row in list(self._unresolved):
            if ("reload_at" not in row and ct is not None and row.get("ct") is not None
                    and ct < row["ct"] - RELOAD_CT_DROP_S):
                row["reload_at"], row["reload_ct"] = now, ct
            if playing and now > row["t"]:
                row["resumed_at"], row["resumed_after_s"] = now, round(now - row["t"], 3)
                self._unresolved.remove(row)

    def note_outcome(self, rec: dict | None) -> None:
        """resume_press's outcome on the last press's press_log row (None: video.play() found no element to press)."""
        row, self._open_press = self._open_press, None
        if row is None:
            return
        if rec is None:
            row["outcome"] = "not_pressed"
            return
        row.update({k: rec[k] for k in ("via", "outcome", "button_outcome", "gave_up") if k in rec})

    def stuck_frozen_s(self, now: float) -> float:
        """The pause as a frozen run for the stuck-player rule once the deadlock press has not taken within
        PAUSE_DEADLOCK_S (seconds since the pause began), else 0."""
        if self.deadlock_press_at is None or self.paused_since is None or not self.was_paused:
            return 0.0
        return now - self.paused_since if now - self.deadlock_press_at >= PAUSE_DEADLOCK_S else 0.0

    def _deadlock(self, state: dict, now: float) -> dict | None:
        """One press when the paused poll is not an ad, not ended, readyState >= 3, and either without a stall label
        (levi / supervisor 15:40; at any buffer) or with the buffer >= the floor (levi 13:09; whatever the label), and
        the buffer is flat AND the playhead still for PAUSE_DEADLOCK_S. Any other poll restarts the clock (the startup
        rebuffer: readyState 2 under a stall label; a stall label over a buffer under the floor), except a dry pause (t79:
        a stall label, readyState 1-2, the buffer under the floor or none, the playhead past DRY_PAUSE_MIN_PLAYHEAD_S),
        which counts toward the same flat test (reason paused_dry_flat), and the same at readyState >= 3 with a known
        buffer under the floor (levi 01:40, W1 tw_100113; reason paused_under_floor_flat)."""
        buf, ct = state.get("buf"), state.get("ct")
        rs = state.get("rs") or 0
        above = buf is not None and buf >= self.min_buffer_s
        starved = (bool(state.get("stall")) and not above and ct is not None and ct > DRY_PAUSE_MIN_PLAYHEAD_S)
        dry = starved and rs in (1, 2)
        under = starved and rs >= 3 and buf is not None
        if (state.get("ad") or state.get("ended")
                or (not dry and not under and (rs < 3 or (state.get("stall") and not above)))):
            self.flat_since, self.buf_peak, self.ct_last = now, buf, ct
            return None
        grew = buf is not None and (self.buf_peak is None or buf > self.buf_peak + PAUSE_GROWTH_EPS_S)
        moved = ct is not None and self.ct_last is not None and abs(ct - self.ct_last) > PAUSE_GROWTH_EPS_S
        if grew or moved:
            self.flat_since = now
        if grew:
            self.buf_peak = buf
        if ct is not None:
            self.ct_last = ct
        if self.flat_since is None:
            self.flat_since = now
        if self.deadlock_press_at is not None or now - self.flat_since < PAUSE_DEADLOCK_S:
            return None
        self.deadlock_press_at = now
        self.presses += 1
        rec = {"action": "resume_after_self_pause", "reason": "paused_dry_flat" if dry else
               "paused_under_floor_flat" if under else
               "paused_flat_above_floor" if above else "paused_no_buffer_growth", "buffer_s": buf, "paused_s": round(now - self.paused_since, 3),
               "flat_s": round(now - self.flat_since, 3), "stall_label": bool(state.get("stall"))}
        self.deadlocks.append({"t": now, "paused_since": self.paused_since, "buffer_s": buf, "reason": rec["reason"],
                               "stall_label": rec["stall_label"]})
        return rec

    def _track_data_flat(self, state: dict, now: float) -> None:
        """levi 18:16: the run of polls that are paused with data (readyState >= 3, buffer >= the floor, not an ad, not
        ended) and flat (buffer and playhead within PAUSE_GROWTH_EPS_S of the run's first poll), the press trigger's test.
        Growth starts a new run; any other poll ends it."""
        buf, ct = state.get("buf"), state.get("ct")
        ok = (bool(state.get("paused")) and not state.get("ad") and not state.get("ended")
              and (state.get("rs") or 0) >= 3 and buf is not None and buf >= self.min_buffer_s)
        ok_any = (bool(state.get("paused")) and not state.get("ended")
                  and (state.get("rs") or 0) >= 3 and buf is not None and buf >= self.min_buffer_s)
        a0, d0 = self._dfa_ref
        if not (ok_any and self.data_flat_any_since is not None and abs(buf - a0) <= PAUSE_GROWTH_EPS_S
                and (ct is None or d0 is None or abs(ct - d0) <= PAUSE_GROWTH_EPS_S)):
            self.data_flat_any_since, self._dfa_ref = (now, (buf, ct)) if ok_any else (None, (None, None))
        b0, c0 = self._df_ref
        if ok and self.data_flat_since is not None and abs(buf - b0) <= PAUSE_GROWTH_EPS_S \
                and (ct is None or c0 is None or abs(ct - c0) <= PAUSE_GROWTH_EPS_S):
            return
        self.data_flat_since, self._df_ref = (now, (buf, ct)) if ok else (None, (None, None))

    def note_button_fail(self) -> None:
        self.button_fails += 1
        self.gave_up = self.button_fails >= TW_BUTTON_MAX_FAILS

    def step(self, state: dict | None, now: float) -> dict | None:
        paused = bool(state and state.get("paused"))
        if state is not None:
            self._follow_presses(state, now)
            self.last_poll = (now, state)
            self._track_data_flat(state, now)
            if self.was_paused and not paused:
                self.restart_at = now
            self.was_paused = paused
        if not paused:
            if self.deadlock_press_at is not None and self.deadlocks and self.paused_since is not None:
                self.deadlocks[-1]["resumed_at"] = now
            self.paused_since = self.eligible_since = None
            self.button_fails, self.gave_up = 0, False
            self.buf_peak = self.flat_since = self.deadlock_press_at = self.ct_last = None
            return None
        if self.paused_since is None:
            self.paused_since = now
        if self.stopped_at is not None:
            self.held["stopping"] += 1
            return None
        dl = self._deadlock(state, now)
        if dl is not None:
            return self._logged(dl, state, now)
        if self.gave_up:
            return None
        buf = state.get("buf")
        why = ("ad" if state.get("ad") else "ended" if state.get("ended") else "stall" if state.get("stall")
               else "waiting" if (state.get("rs") or 0) < 3
               else "buffer" if buf is None or buf < self.min_buffer_s
               else "backoff" if self.restart_at is not None and now - self.restart_at < self.backoff_s else None)
        if why is not None:
            self.held[why] += 1
            self.eligible_since = None
            return None
        if self.eligible_since is None:
            self.eligible_since = now
        if now - self.eligible_since < self.hold_s:
            return None
        self.eligible_since = now                        # retry only after another hold
        self.presses += 1
        return self._logged({"action": "resume_after_self_pause", "reason": "self_pause", "buffer_s": buf,
                             "paused_s": round(now - self.paused_since, 3)}, state, now)


def resume_press(page, platform: str, resumer: "SelfPauseResumer", press: dict, wait) -> dict | None:
    """One resume: video.play(); on twitch, if the element is paused again RESUME_CHECK_S later, Twitch's own play
    button, then its outcome (a button press that leaves it paused counts toward resumer.gave_up). Returns the log
    record (None when nothing was pressed)."""
    if not page.eval(_PLAY_JS):
        return None
    rec = {**press, "via": "video_play"}
    if platform != "twitch":
        return rec

    def paused() -> bool | None:
        s = json.loads(page.eval(_PLAYER_STATE_JS) or "null")
        return None if not s else bool(s.get("paused"))
    wait(RESUME_CHECK_S)
    if paused() is not True:
        rec["outcome"] = "playing"
        return rec
    rec["outcome"] = "re_paused"
    # Twitch's button: when its controller says paused, one click plays; when it says playing over the paused element
    # (the 2026-10-17 self-pause DOM: "Pause (space/k)", data-a-player-state="playing"), a pause / play cycle through it
    btn = json.loads(page.eval(_TW_BUTTON_JS % "true") or "null") or {"present": False}
    rec["button"] = btn
    if btn.get("present"):
        if btn.get("state") == "playing":
            wait(TW_BUTTON_CYCLE_GAP_S)
            rec["button_cycle"] = json.loads(page.eval(_TW_BUTTON_JS % "true") or "null")
        wait(RESUME_CHECK_S)
        still = paused()
        rec["button_outcome"] = "still_paused" if still is True else "playing"
        if still is True:
            resumer.note_button_fail()
    else:
        resumer.note_button_fail()
    rec["button_fails"], rec["gave_up"] = resumer.button_fails, resumer.gave_up
    return rec


# addendum N (stall-only run, levi 2026-10-19): content.js's frozen-playhead run, in seconds (= check_session.frozen_runs); the
# backstop watchdog and the stuck-player rule (stuck.py) read the same value
_FROZEN_JS = "Number(document.documentElement.dataset.lvqoeFrozenS || 0)"
TICK_STARTUP_TIMEOUT_S = 15


_USHER_RE = re.compile(r"usher\.ttvnw\.net/api/(?:v2/)?channel/hls/[^/?]+\.m3u8")
# v3 A3 source 2 (levi 18:32): twitch's media playlists (fetched by the player's worker every few s) mark stitched ads;
# the listener reads each body it already sees (no request of ours) and records every flip of "an ad marker present"
_TW_MEDIA_RE = re.compile(r"\.m3u8(?:\?|$)")
_TW_AD_MARK_RE = re.compile(r'stitched-ad|CLASS="twitch-ad|X-TV-TWITCH-AD', re.I)
# meta.json tw_stream_type (record only, 2026-10-25): the page's own GQL responses (gql.twitch.tv/gql, fetched by the
# twitch page itself) carry the channel's stream type in user.stream.type ("live" for a broadcast; "rerun" etc. otherwise;
# StreamMetadata is the operation that carries it, the one streamlink's --twitch-disable-reruns reads). The listener
# reads the bodies it already sees (Network.getResponseBody, no request of ours) and keeps only type / stream id /
# the channel login per operation, never a body.
_TW_GQL_RE = re.compile(r"^https://gql\.twitch\.tv/gql(?:[?#]|$)")
TW_STREAM_TYPE_OP = "StreamMetadata"      # the preferred operation for the session's value
TW_GQL_LOGIN_VARS = ("channelLogin", "login", "channel", "channelName")


LADDER_PROBE_FROZEN_S = 60.0   # twitch, master unread: the quality menu is read only inside a frozen run this long
LADDER_PROBE_ATTEMPTS = 2      # ... once per frozen run, a second attempt only when the first raised
MEDIA_EVENTS_FILE = "media_events.jsonl"
MEDIA_MESSAGE_LEVELS = ("error", "warning", "info")   # playerMessagesLogged kept (debug dropped: volume)
MEDIA_RECENT_N = 40
MEDIA_SNAPSHOT_MAX_S = 2.0   # the dead-trigger snapshot's cap: it delays the re-entry (levi's audit of 1bcdc5e)


def _media_load_kind(value) -> str | None:
    """A Media-domain event value (JSON text) -> its kLoad's kind: the URL scheme (blob / data / https ...), or "stream"
    for a MediaStream player (WebMediaPlayerMS logs the stream's id, a UUID, not a URL); None for other events."""
    try:
        ev = json.loads(value) if isinstance(value, str) else (value or {})
    except ValueError:
        return None
    if not isinstance(ev, dict) or ev.get("event") != "kLoad":
        return None
    url = str(ev.get("url") or "")
    return url.split(":", 1)[0] if ":" in url else ("stream" if url else None)


class MediaListener:
    """Chrome's media-internals over CDP (the Media domain) by a second CDP client on the page, recording only
    (supervisor 2026-10-18, peter's ti_43 read: the player reset to readyState 0 while the FLV stream kept arriving;
    this tells a decoder error, e.g. the VA-API HEVC path, from a player-side fault on the next death). Every player's
    properties (kVideoDecoderName, kIsPlatformVideoDecoder, kResolution, ...), events, errors and its error / warning /
    info messages go to <session>/media_events.jsonl; snapshot() is attached to the player_dead record. No Chrome flag.
    Every platform since decision A (levi 00271 item 3, supervisor (b)): summary() is meta.json media_decoders, each
    player's load kind and video decoder. tt's live stream is a MediaStream player ("stream": its kLoad names a stream
    id, not a URL), which has no decoder in the media log: its decode happens in the page, so tt's decoders here are
    only its blob clips' (a stated gap for the N = 3 regime)."""

    def __init__(self, ws_url: str, path: Path | None):
        self.ws_url, self.path, self.error = ws_url, path, None
        self.players: dict[str, dict] = {}
        self.loads: dict[str, dict] = {}          # player -> its kLoad's kind ("blob" / "data" / "https" / "stream") and t
        self.recent: list[dict] = []
        self.written = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> "MediaListener":
        self._t = threading.Thread(target=self._run, daemon=True, name="media-listener")
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def handle(self, msg: dict, now: float) -> dict | None:
        """One CDP message -> the record to write (None for the rest); keeps the players' properties and the recent
        non-property records for snapshot()."""
        m, prm = msg.get("method") or "", msg.get("params") or {}
        if not m.startswith("Media."):
            return None
        if m == "Media.playerMessagesLogged":
            prm = {**prm, "messages": [x for x in prm.get("messages") or [] if x.get("level") in MEDIA_MESSAGE_LEVELS]}
            if not prm["messages"]:
                return None
        rec = {"t": round(now, 3), "method": m[len("Media."):], **prm}
        with self._lock:
            if m == "Media.playerPropertiesChanged":
                props = self.players.setdefault(str(prm.get("playerId")), {})
                for x in prm.get("properties") or []:
                    props[x.get("name")] = x.get("value")
            else:
                self.recent = (self.recent + [rec])[-MEDIA_RECENT_N:]
                if m == "Media.playerEventsAdded":
                    for e in prm.get("events") or []:
                        kind = _media_load_kind(e.get("value"))
                        if kind is not None:
                            self.loads.setdefault(str(prm.get("playerId")), {"load": kind, "t": round(now, 3)})
        return rec

    def summary(self) -> dict:
        """meta.json media_decoders: players created, their load kinds, and every player that named a video decoder."""
        with self._lock:
            kinds: dict[str, int] = {}
            for v in self.loads.values():
                kinds[v["load"]] = kinds.get(v["load"], 0) + 1
            dec = []
            for pid, pr in self.players.items():
                if "kVideoDecoderName" not in pr:
                    continue
                try:
                    codec = (json.loads(pr.get("kVideoTracks") or "[]") or [{}])[0].get("codec")
                except (ValueError, TypeError, AttributeError, IndexError):
                    codec = None
                dec.append({"player": pid, "load": (self.loads.get(pid) or {}).get("load"),
                            "t_load": (self.loads.get(pid) or {}).get("t"), "video_decoder": pr.get("kVideoDecoderName"),
                            "platform_video_decoder": pr.get("kIsPlatformVideoDecoder"), "codec": codec,
                            "resolution": pr.get("kResolution")})
            return {"players": len(set(self.loads) | set(self.players)), "loads": kinds, "video_decoders": dec,
                    "written": self.written, "error": self.error}

    def snapshot(self) -> dict:
        with self._lock:
            return {"players": {k: dict(v) for k, v in self.players.items()}, "recent": list(self.recent),
                    "written": self.written, "error": self.error}

    def _run(self) -> None:
        try:
            ws = websocket.WebSocket()
            ws.settimeout(1)
            ws.connect(self.ws_url)
            ws.send(json.dumps({"id": 1, "method": "Media.enable", "params": {}}))
        except Exception as exc:
            self.error = repr(exc)
            return
        fh = open(self.path, "a") if self.path else None
        try:
            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                rec = self.handle(msg, time.time())
                if rec is not None and fh is not None:
                    fh.write(json.dumps(rec) + "\n")
                    fh.flush()
                    self.written += 1
        except Exception as exc:
            self.error = repr(exc)
        finally:
            for c in (fh, ws):
                try:
                    if c is not None:
                        c.close()
                except Exception:
                    pass


# every <video>'s error (Chromium puts the pipeline status in its message) and state, at a tt dead trigger
_VIDEO_ERRORS_JS = """(() => [...document.querySelectorAll('video')].map(v => ({
  rs: v.readyState, ns: v.networkState, paused: v.paused, w: v.videoWidth, h: v.videoHeight,
  src: !!(v.currentSrc || v.src || v.srcObject),
  error: v.error ? {code: v.error.code, message: String(v.error.message || '').slice(0, 400)} : null})))()"""


class MasterListener:
    """twitch: the channel's HLS master playlist, read by a second CDP client on the page (Network domain; the body via
    Network.getResponseBody, no request of ours). Its lowest BANDWIDTH is the stuck-player rule's rung (stuck.py).
    twitch's player fetches the master from its Web Worker, which the page target's Network domain never sees (shagun
    2026-10-18: no master captured on 6 of 6 sessions, tw_29-34): the listener auto-attaches to the page's workers
    (Target.setAutoAttach, flatten) and enables Network in each, reading bodies in that worker's session."""

    WORKER_TYPES = ("worker", "shared_worker", "service_worker")

    def __init__(self, ws_url: str):
        self.ws_url, self.latest, self.error = ws_url, None, None
        self.workers: list[str] = []
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._wanted: dict = {}
        self._pending: dict = {}
        self.playlist_fetches = 0                  # v3 A3 source 2: media playlists read
        self.ad_edges: list[dict] = []             # their ad-marker flips (meta.json tw_playlist_ads)
        self._in_ad: dict[str, bool] = {}          # brad 18:45: per playlist (rendition), so two renditions never alternate
        self.ad_edges_overflow = 0                 # levi 18:51: edges past WATCH_LOG_MAX (0 expected)
        self.ladder_reads: list[dict] = []         # v3 A4: every master read whose video variants changed (tw_ladder)
        self.ladder_n_reads = 0
        self.ladder_overflow = 0
        self.ladder_errors = 0                     # levi 18:22: master reads whose parse raised (the offer reads missing)
        self.ladder_last_error: str | None = None
        self._ladder_last = None
        self.gql_bodies = 0                        # tw_stream_type: GQL response bodies read
        self.gql_errors = 0                        # ... whose parse raised
        self.gql_last_error: str | None = None
        self._gql_ops: dict = {}                   # (session, requestId) -> [(operationName, login)] from the request
        self.gql_ops_overflow = 0                  # pending ops dropped at the WATCH_LOG_MAX cap (oldest first)
        self.stream_type_edges: list[dict] = []    # each operation's (login, stream id, type) read, on change
        self.stream_type_reads: dict[str, int] = {}   # operation -> reads that carried user.stream
        self.stream_type_overflow = 0
        self._stream_type_last: dict[str, tuple] = {}

    def start(self) -> "MasterListener":
        self._t = threading.Thread(target=self._run, daemon=True, name="master-listener")
        self._t.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def handle(self, msg: dict, send) -> None:
        """One CDP message; send(method, params, session_id) -> the command id."""
        import base64
        from urllib.parse import urlsplit
        m, prm, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
        if m == "Target.attachedToTarget" and (prm.get("targetInfo") or {}).get("type") in self.WORKER_TYPES:
            self.workers.append((prm.get("targetInfo") or {}).get("type"))
            send("Network.enable", {"maxTotalBufferSize": 4_000_000, "maxResourceBufferSize": 500_000}, prm.get("sessionId"))
        elif m == "Network.requestWillBeSent" and _TW_GQL_RE.search((prm.get("request") or {}).get("url", "")):
            self._gql_ops[(sid, prm.get("requestId"))] = self.gql_request_ops((prm.get("request") or {}).get("postData"))
            while len(self._gql_ops) > WATCH_LOG_MAX:      # brad (#11): bounded; the oldest pending op goes first
                self._gql_ops.pop(next(iter(self._gql_ops)))
                self.gql_ops_overflow += 1
        elif m == "Network.loadingFailed":                 # brad (#11): a cancelled / navigated-away request never finishes
            self._gql_ops.pop((sid, prm.get("requestId")), None)
            self._wanted.pop((sid, prm.get("requestId")), None)
        elif m == "Network.responseReceived" and _TW_GQL_RE.search((prm.get("response") or {}).get("url", "")):
            self._wanted[(sid, prm.get("requestId"))] = prm["response"]["url"]   # tw_stream_type: the page's own GQL
        elif m == "Network.responseReceived" and _USHER_RE.search((prm.get("response") or {}).get("url", "")):
            self._wanted[(sid, prm.get("requestId"))] = prm["response"]["url"]
        elif m == "Network.responseReceived" and _TW_MEDIA_RE.search((prm.get("response") or {}).get("url", "")):
            self._wanted[(sid, prm.get("requestId"))] = prm["response"]["url"]   # a media playlist (A3 source 2)
        elif m == "Network.loadingFinished" and (sid, prm.get("requestId")) in self._wanted:
            url = self._wanted.pop((sid, prm["requestId"]))
            self._pending[send("Network.getResponseBody", {"requestId": prm["requestId"]}, sid)] = (sid, url, prm["requestId"])
        elif msg.get("id") in self._pending:
            (req_sid, url, rid), res = self._pending.pop(msg["id"]), msg.get("result") or {}
            body = res.get("body") or ""
            if res.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", "replace")
            if _TW_GQL_RE.search(url):
                hint = self._gql_ops.pop((req_sid, rid), None)
                if body:                               # a preflight / evicted body reads empty: nothing to parse
                    try:                               # a malformed body never breaks the listener
                        self.stream_type_read(body, time.time(), hint)
                    except Exception as exc:
                        self.gql_errors += 1
                        self.gql_last_error = repr(exc)[:200]
            elif _USHER_RE.search(url):
                kbit = stuck.rung_from_master(body)
                if kbit:
                    self.latest = {"kbit": kbit, "t": time.time(), "path": urlsplit(url).path, "from_worker": req_sid is not None}
                try:                                   # levi 18:22: a malformed master never breaks the listener
                    self.ladder(body, url, time.time())
                except Exception as exc:
                    self.ladder_errors += 1
                    self.ladder_last_error = repr(exc)[:200]
            else:
                self.playlist_ad(body, url, time.time())

    def ladder(self, body: str, url: str, t: float) -> None:
        """v3 A4: one master read's video variants (offer.tw_variants), recorded when they changed; capped, counted."""
        import offer
        from urllib.parse import urlsplit
        self.ladder_n_reads += 1
        v = offer.tw_variants(body)
        key = json.dumps(v, sort_keys=True)
        if not v or key == self._ladder_last:
            return
        self._ladder_last = key
        if len(self.ladder_reads) < WATCH_LOG_MAX:
            self.ladder_reads.append({"t": t, "path": urlsplit(url).path, "variants": v})
        else:
            self.ladder_overflow += 1

    def tw_ladder(self) -> dict:
        return {"n_reads": self.ladder_n_reads, "reads": self.ladder_reads, "overflow": self.ladder_overflow,
                "errors": self.ladder_errors, "last_error": self.ladder_last_error,
                "rule": "collector v3 A4: each HLS master read's STREAM-INF video variants, on change"}

    @staticmethod
    def gql_request_ops(post_data) -> list | None:
        """A GQL request's postData (one operation or a batch) -> [(operationName, channel login)] in batch order; None
        when absent or unreadable (the response's own extensions.operationName is then the op)."""
        try:
            req = json.loads(post_data) if post_data else None
        except (ValueError, TypeError):
            return None
        if req is None:
            return None
        out = []
        for op in (req if isinstance(req, list) else [req]):
            op = op if isinstance(op, dict) else {}
            v = op.get("variables") if isinstance(op.get("variables"), dict) else {}
            login = next((v[k] for k in TW_GQL_LOGIN_VARS if isinstance(v.get(k), str)), None)
            out.append((op.get("operationName"), login))
        return out

    def stream_type_read(self, body: str, t: float, hint: list | None = None) -> None:
        """tw_stream_type: one GQL response body (one result or a batch): every result whose data.user (or
        data.userOrError) has a `stream` key gives (login, stream id, type); a null stream reads as type None (offline).
        Recorded per operation on change; capped, counted."""
        self.gql_bodies += 1
        res = json.loads(body)
        for i, r in enumerate(res if isinstance(res, list) else [res]):
            if not isinstance(r, dict) or not isinstance(r.get("data"), dict):
                continue
            h = (hint[i] if hint and i < len(hint) else (None, None))
            op = ((r.get("extensions") or {}).get("operationName") if isinstance(r.get("extensions"), dict) else None) \
                or h[0] or "?"
            for key in ("user", "userOrError"):
                u = r["data"].get(key)
                if not isinstance(u, dict) or "stream" not in u:
                    continue
                st = u["stream"]
                if st is not None and not (isinstance(st, dict) and "type" in st):
                    continue                           # a stream object without its type: not this field's source
                value = None if st is None else st.get("type")
                sid = None if st is None else st.get("id")
                login = h[1] if h[1] is not None else (u.get("login") if isinstance(u.get("login"), str) else None)
                self.stream_type_reads[op] = self.stream_type_reads.get(op, 0) + 1
                cur = (login, sid, value)
                if self._stream_type_last.get(op) != cur:
                    self._stream_type_last[op] = cur
                    if len(self.stream_type_edges) < WATCH_LOG_MAX:
                        self.stream_type_edges.append({"t": t, "op": op, "login": login, "stream_id": sid,
                                                       "value": value})
                    else:
                        self.stream_type_overflow += 1
                break

    def tw_stream_type(self) -> dict:
        """meta.json tw_stream_type: {value, source, t} from the first non-null read of TW_STREAM_TYPE_OP (else of any
        operation), with the channel login and stream id it named; value None with a reason when no read carried a type."""
        edges = self.stream_type_edges
        typed = [e for e in edges if e["value"] is not None]
        pick = next((e for e in typed if e["op"] == TW_STREAM_TYPE_OP), typed[0] if typed else None)
        if pick is not None:
            reason = None
        elif self.error is not None:
            reason = f"listener error: {self.error}"
        elif edges:
            reason = "user.stream was null in every read (channel offline)"
        elif self.gql_bodies:
            reason = "no GQL response carried user.stream.type"
        else:
            reason = "no GQL response body read"
        same_op = [e for e in typed if pick is not None and e["op"] == pick["op"]]
        return {"value": pick["value"] if pick else None, "source": f"gql:{pick['op']}" if pick else None,
                "t": pick["t"] if pick else None, "login": pick["login"] if pick else None,
                "stream_id": pick["stream_id"] if pick else None, "reason": reason,
                "changed": len({(e["login"], e["stream_id"], e["value"]) for e in same_op}) > 1,
                "edges": edges, "reads_by_op": dict(self.stream_type_reads), "gql_bodies": self.gql_bodies,
                "overflow": self.stream_type_overflow, "ops_overflow": self.gql_ops_overflow,
                "errors": self.gql_errors, "last_error": self.gql_last_error,
                "rule": "record only: the page's own GQL responses (user.stream.type), bodies read via "
                        "Network.getResponseBody, no request of ours; value from the first typed StreamMetadata read"}

    def playlist_ad(self, body: str, url: str, t: float) -> None:
        """v3 A3 source 2: one media playlist body: a flip of 'an ad marker present' is an edge (time.time; the A11 clock
        offsets join it to the label clock)."""
        import hashlib
        from urllib.parse import urlsplit
        self.playlist_fetches += 1
        marks = _TW_AD_MARK_RE.findall(body or "")
        full = urlsplit(url).path
        in_ad, path = bool(marks), hashlib.sha256(full.encode()).hexdigest()[:16]   # levi 18:51: the whole path, hashed
        prev = self._in_ad.get(path)
        # levi 18:51: a playlist's first sighting is an edge only when it carries a marker (rotated playlist tokens would
        # otherwise fill the log with no-ad "edges"); an overflow past the cap is counted, never silent
        if in_ad != (prev or False) or (prev is None and in_ad):
            if len(self.ad_edges) < WATCH_LOG_MAX:
                self.ad_edges.append({"t": t, "in_ad": in_ad, "prev": prev, "marks": len(marks), "path": path,
                                      "path_tail": full[-40:]})
            else:
                self.ad_edges_overflow += 1
        self._in_ad[path] = in_ad

    def _run(self) -> None:
        try:
            ws = websocket.WebSocket()
            ws.settimeout(1)
            ws.connect(self.ws_url)
        except Exception as exc:
            self.error = repr(exc)
            return
        seq = 0

        def send(method: str, params: dict, session_id: str | None = None) -> int:
            nonlocal seq
            seq += 1
            ws.send(json.dumps({"id": seq, "method": method, "params": params,
                                **({"sessionId": session_id} if session_id else {})}))
            return seq
        try:
            send("Network.enable", {"maxTotalBufferSize": 4_000_000, "maxResourceBufferSize": 500_000})
            send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                self.handle(msg, send)
        except Exception as exc:
            self.error = repr(exc)
        finally:
            try:
                ws.close()
            except Exception:
                pass


class WorkerAnchor:
    """t100 R1 (tw, tt; frame log enabled only): a CDP client on the session page that auto-attaches to its dedicated
    workers only (WORKER_ANCHOR_ATTACH's filter: frames, service and shared workers are never attached, so never paused),
    installs worker_anchor_js in each before it runs and resumes it, then about every WORKER_ANCHOR_DRAIN_S drains each
    live worker's batch and posts it into the page as the "lvqoe-mse" message (content.js: frames k:"mse"), every record
    tagged with its worker's index wi. levi's hard requirement: a worker is never left paused. The resume is sent in a
    finally, in the same call as the install and never after a reply (the install itself is cut at
    WORKER_ANCHOR_EVAL_TIMEOUT_MS by Chrome), whatever the install did. record() is meta frame_log.worker_anchor: the
    coverage per worker (target, url kind, attach time, install result, resume latency, detach, time origin), the relay
    counts (recs: delivered; post_lost_recs: posts the page refused), and per record kind its count and JSON bytes as
    posted (levi 14:35: the cost of record is measured)."""

    def __init__(self, ws_url: str, block_s: int):
        self.ws_url, self.block_s = ws_url, block_s
        self.js = worker_anchor_js(block_s)
        self.workers: list[dict] = []
        self.workers_overflow = 0
        self._live: dict[str, dict] = {}           # sessionId -> its worker record
        self._pending: dict[int, tuple] = {}       # command id -> (kind, sessionId, n)
        self.armed = threading.Event()               # the auto-attach call answered without error
        self._ready = threading.Event()              # ... or failed: start() stops waiting either way
        self.armed_error = None
        self._by_sid: dict[str, dict] = {}         # every attached worker's record, detached ones too (late replies)
        self.batches = self.recs = self.post_errors = self.post_lost = self.take_errors = 0
        self.kinds: dict[str, list[int]] = {}      # op -> [records, JSON bytes]
        self.handle_errors, self.handle_last_error = 0, None
        self._ws = None
        self.error: str | None = None
        self.final_drain: dict | None = None
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._last_take = 0.0

    def start(self, wait_s: float = 3.0) -> "WorkerAnchor":
        """The listener thread; returns once the auto-attach is armed (or wait_s passed: recorded, never an abort)."""
        self._t = threading.Thread(target=self._run, daemon=True, name="worker-anchor")
        self._t.start()
        self._ready.wait(wait_s)
        return self

    def stop(self, wait_s: float = 4.0) -> None:
        """A last drain of every live worker, posted before the session's final flush; then the thread ends."""
        self._stop.set()
        if self._t is not None:
            self._t.join(timeout=wait_s)

    def handle(self, msg: dict, send, now: float | None = None) -> None:
        """One CDP message; send(method, params, session_id) -> the command id. levi 14:59: a fault in one message is
        counted (handle_errors) and never ends the anchor."""
        try:
            self._handle(msg, send, now)
        except Exception as exc:
            self.handle_errors += 1
            self.handle_last_error = repr(exc)[:200]

    def _handle(self, msg: dict, send, now: float | None) -> None:
        now = time.time() if now is None else now
        m, prm = msg.get("method"), msg.get("params") or {}
        if m == "Target.attachedToTarget":
            self._attached(prm, send, now)
        elif m == "Target.detachedFromTarget":
            w = self._live.pop(prm.get("sessionId"), None)
            if w is not None:
                w["detached"] = round(now, 3)
        elif msg.get("id") in self._pending:
            self._reply(msg, send, now)

    def _attached(self, prm: dict, send, now: float) -> None:
        sid, info = prm.get("sessionId"), prm.get("targetInfo") or {}
        url = info.get("url") or ""
        w = {"wi": len(self.workers) + self.workers_overflow, "target": info.get("targetId"), "type": info.get("type"),
             "url_kind": url.split(":", 1)[0] if ":" in url else (url or None), "t": round(now, 3),
             "waiting": prm.get("waitingForDebugger"), "wrap": None, "recs": 0, "over": 0}
        if len(self.workers) < WATCH_LOG_MAX:
            self.workers.append(w)
        else:
            self.workers_overflow += 1
        self._by_sid[sid] = w
        try:
            if info.get("type") == "worker":       # the filter admits dedicated workers only; anything else just resumes
                self._live[sid] = w
                self._pending[send("Runtime.evaluate", {"expression": self.js, "returnByValue": True,
                                                        "timeout": WORKER_ANCHOR_EVAL_TIMEOUT_MS}, sid)] = ("wrap", sid, 0)
        except Exception as exc:
            w["wrap_error"] = repr(exc)[:200]
        finally:
            try:
                self._pending[send("Runtime.runIfWaitingForDebugger", {}, sid)] = ("run", sid, 0)
                w["resume_sent_ms"] = round((time.time() - now) * 1000, 2)
            except Exception as exc:
                w["resume_error"] = repr(exc)[:200]

    def _reply(self, msg: dict, send, now: float) -> None:
        kind, sid, n = self._pending.pop(msg["id"])
        res = msg.get("result") or {}
        val = (res.get("result") or {}).get("value")
        err = msg.get("error") or res.get("exceptionDetails")
        w = self._by_sid.get(sid)
        if kind == "wrap" and w is not None:
            w["wrap"] = val if not err else None
            if err:
                w["wrap_error"] = json.dumps(err, default=str)[:200]
        elif kind == "run" and w is not None:
            w["resume_ms"] = round((now - w["t"]) * 1000, 1)
            if msg.get("error"):
                w["resume_error"] = json.dumps(msg["error"], default=str)[:200]
        elif kind == "take":
            if err or not isinstance(val, str):
                self.take_errors += int(bool(err))
                return
            self._relay(json.loads(val), w, send)
        elif kind == "post":
            if err:
                self.post_errors += 1
                self.post_lost += n
            else:
                self.batches += 1
                self.recs += n

    def _relay(self, batch: dict, w: dict | None, send) -> None:
        recs = batch.get("recs") or []
        wi = w["wi"] if w is not None else None
        for r in recs:
            r["wi"] = wi
            k = self.kinds.setdefault(str(r.get("op")), [0, 0])
            k[0] += 1
            k[1] += len(json.dumps(r, separators=(",", ":")))
        if w is not None:
            w["recs"] += len(recs)
            w["over"] = batch.get("over") or 0
            w["ms"] = batch.get("ms")
            w["video_samples"] = batch.get("nv")
            w["time_origin"] = batch.get("to")      # levi 14:59: t / inst / first.t are on the worker's clock; + to = epoch ms
            if not recs and w.get("posted_inst"):
                return
            w["posted_inst"] = True               # the first post goes out even empty: inst / first reach content.js
        msg = {"source": "lvqoe-mse", "recs": recs, "over": batch.get("over") or 0, "inst": batch.get("inst"),
               "first": batch.get("first"), "wi": wi, "to": batch.get("to")}
        self._pending[send("Runtime.evaluate", {"expression": "window.postMessage(%s, '*'); true"
                                                % json.dumps(msg, separators=(",", ":"))}, None)] = ("post", None, len(recs))

    def take(self, send) -> None:
        """Ask every live worker for its batch (the reply is relayed in _reply)."""
        for sid in list(self._live):
            self._pending[send("Runtime.evaluate", {"expression": "self.__lvqoeAnchorTake ? self.__lvqoeAnchorTake() "
                                                                  ": null", "returnByValue": True}, sid)] = ("take", sid, 0)

    def record(self) -> dict:
        workers = [{k: v for k, v in w.items() if k != "posted_inst"} for w in self.workers]
        return {"armed": self.armed.is_set(), "armed_error": self.armed_error, "attach": WORKER_ANCHOR_ATTACH,
                "kinds_on": list(WORKER_ANCHOR_KINDS), "key_every": WORKER_ANCHOR_KEY_EVERY,
                "eval_timeout_ms": WORKER_ANCHOR_EVAL_TIMEOUT_MS, "workers": workers,
                "workers_overflow": self.workers_overflow, "batches": self.batches, "recs": self.recs,
                "post_errors": self.post_errors, "post_lost_recs": self.post_lost, "take_errors": self.take_errors,
                "kinds": {k: {"n": v[0], "bytes": v[1]} for k, v in sorted(self.kinds.items())},
                "worker_over": sum(w.get("over") or 0 for w in self.workers), "final_drain": self.final_drain,
                "handle_errors": self.handle_errors, "handle_last_error": self.handle_last_error, "error": self.error}

    def _run(self) -> None:
        try:
            ws = websocket.WebSocket()
            ws.settimeout(0.2)
            ws.connect(self.ws_url)
            self._ws = ws
        except Exception as exc:
            self.error = self.armed_error = repr(exc)[:200]
            self._ready.set()
            return
        seq = 0

        def send(method: str, params: dict, session_id: str | None = None) -> int:
            nonlocal seq
            seq += 1
            ws.send(json.dumps({"id": seq, "method": method, "params": params,
                                **({"sessionId": session_id} if session_id else {})}))
            return seq

        def pump(until: float) -> None:
            while time.time() < until:
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    return
                self.handle(msg, send)
        try:
            arm_id = send("Target.setAutoAttach", WORKER_ANCHOR_ATTACH)
            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    msg = None
                if msg is not None:
                    if msg.get("id") == arm_id:
                        if msg.get("error"):
                            self.armed_error = json.dumps(msg["error"])[:200]
                        else:
                            self.armed.set()
                        self._ready.set()
                    self.handle(msg, send)
                if time.time() - self._last_take >= WORKER_ANCHOR_DRAIN_S:
                    self._last_take = time.time()
                    self.take(send)
            t0 = time.time()                       # the last drain: take, relay, the posts' replies (<= 3 s)
            self.take(send)
            while time.time() - t0 < 3.0 and any(k in ("take", "post") for k, _, _ in self._pending.values()):
                pump(time.time() + 0.2)
            self.final_drain = {"s": round(time.time() - t0, 3),
                                "pending": sum(1 for k, _, _ in self._pending.values() if k in ("take", "post"))}
            if WORKER_ANCHOR_DRAIN_AT:            # t102 #8 (levi 01:06): the drain's end, epoch s (the host clock)
                self.final_drain["at"] = round(time.time(), 3)
        except Exception as exc:
            self.error = repr(exc)[:200]
        finally:
            self._ready.set()
            try:
                ws.close()
            except Exception:
                pass


_TT_ROOM_API_RE = re.compile(r"/webcast/room/(?:enter|info)/")
TT_ROOM_SDK_KEYS = ("VCodec", "resolution", "vbitrate", "v_rtbitrate", "fps", "gop")
TT_ROOM_OPTION_KEYS = ("name", "sdk_key", "level", "resolution")
TT_ROOM_STREAM_KEYS = ("default_resolution", "resolution_name", "stream_size_width", "stream_size_height")


def _json_or_dict(v):
    """tt nests JSON as strings (pull_data.stream_data, each quality's sdk_params); a dict is taken as is."""
    if isinstance(v, dict) or v is None:
        return v
    return json.loads(v)


def _size(v):
    return len(v) if isinstance(v, (dict, list, tuple)) else None


class TtRoomListener:
    """tiktok (record only, item #9): the page's own /webcast/room/enter/ (and /webcast/room/info/) responses, read by a
    second CDP client on the page exactly as MasterListener reads twitch's master (Network domain on the page and on every
    worker it auto-attaches; the body via Network.getResponseBody on loadingFinished, no request of ours). tt_codec reads
    the SSR document's sdk_params, a stale ladder; room/enter carries the ladder the player is handed, and the room's
    link-mic / co-host / battle state (a multi-guest room composites several hosts into one stream). Each read is parsed
    (parse(); never the body itself, never a pull URL) and recorded when its content changed for that path; capped at
    WATCH_LOG_MAX and counted. A body that does not parse is counted (errors, last_error) and never breaks the listener.
    tt_room_enter() is meta.json tt_room_enter."""

    WORKER_TYPES = ("worker", "shared_worker", "service_worker")
    NETWORK = {"maxTotalBufferSize": 8_000_000, "maxResourceBufferSize": 2_000_000}   # a room/enter body is large
    RULE = ("item #9: each /webcast/room/enter|info/ response the page read (Network.getResponseBody), parsed to the "
            "offered qualities (stream_data sdk_params) + options.qualities + the multi-guest fields, on change per path; "
            "multi_guest = with_linkmic OR linkmic_layout != 0 OR multi_stream_scene != 0 OR linker_map size > 0 OR "
            "battle_status != 0 (None when none of them is present)")

    def __init__(self, ws_url: str):
        self.ws_url, self.error = ws_url, None
        self.workers: list[str] = []
        self.reads: list[dict] = []
        self.n_reads = 0
        self.overflow = 0
        self.errors = 0
        self.last_error: str | None = None
        self.ready = threading.Event()             # set once Network.enable + setAutoAttach are sent
        self._last: dict[str, str] = {}            # path -> the last recorded content key
        self._wanted: dict = {}
        self._pending: dict = {}
        self._stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self, wait_s: float = 2.0) -> "TtRoomListener":
        """Started before tt's room navigation; waits (<= wait_s) until Network is enabled so the first room/enter is
        seen."""
        self._t = threading.Thread(target=self._run, daemon=True, name="tt-room-listener")
        self._t.start()
        self.ready.wait(wait_s)
        return self

    def stop(self) -> None:
        self._stop.set()

    def _err(self, what: str) -> None:
        self.errors += 1
        self.last_error = what[:200]

    def handle(self, msg: dict, send) -> None:
        """One CDP message; send(method, params, session_id) -> the command id."""
        import base64
        from urllib.parse import urlsplit
        m, prm, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
        if m == "Target.attachedToTarget" and (prm.get("targetInfo") or {}).get("type") in self.WORKER_TYPES:
            self.workers.append((prm.get("targetInfo") or {}).get("type"))
            send("Network.enable", dict(self.NETWORK), prm.get("sessionId"))
        elif m == "Network.responseReceived":
            url = (prm.get("response") or {}).get("url", "")
            if _TT_ROOM_API_RE.search(urlsplit(url).path):
                self._wanted[(sid, prm.get("requestId"))] = urlsplit(url).path      # the path only: no query tokens
        elif m == "Network.loadingFinished" and (sid, prm.get("requestId")) in self._wanted:
            path = self._wanted.pop((sid, prm["requestId"]))
            self._pending[send("Network.getResponseBody", {"requestId": prm["requestId"]}, sid)] = (sid, path)
        elif m == "Network.loadingFailed":
            self._wanted.pop((sid, prm.get("requestId")), None)
        elif msg.get("id") in self._pending:
            (req_sid, path) = self._pending.pop(msg["id"])
            try:                                   # a malformed body (or an evicted one) never breaks the listener
                if msg.get("error"):
                    raise ValueError(f"getResponseBody: {(msg['error'] or {}).get('message')}")
                res = msg.get("result") or {}
                body = res.get("body") or ""
                if res.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", "replace")
                self.record(body, path, time.time(), req_sid is not None)
            except Exception as exc:
                self._err(repr(exc))

    def record(self, body: str, path: str, t: float, from_worker: bool = False) -> None:
        """One body read: parsed, recorded when its content changed for this path (capped, counted)."""
        self.n_reads += 1
        r = self.parse(body)                       # raises on a body that is not a JSON object
        if r.pop("parse_errors"):
            self._err(r["parse_error"])
        key = json.dumps(r, sort_keys=True, default=str)
        if key == self._last.get(path):
            return
        self._last[path] = key
        if len(self.reads) < WATCH_LOG_MAX:
            self.reads.append({"t": t, "path": path, "from_worker": from_worker, "body_bytes": len(body), **r})
        else:
            self.overflow += 1

    @staticmethod
    def parse(body: str) -> dict:
        """A room/enter body -> {status_code, room_id, qualities, options, stream, multi_guest, multi_guest_signals,
        fired, parse_error}. Every value read is a scalar or a size (never a URL). A section that does not parse is None
        with its error in parse_error; a missing key is None, never False."""
        top = json.loads(body)
        if not isinstance(top, dict):
            raise ValueError(f"room body is {type(top).__name__}, not an object")
        data = top.get("data") if isinstance(top.get("data"), dict) else {}
        errs: list[str] = []
        su = data.get("stream_url") if isinstance(data.get("stream_url"), dict) else {}
        pull = ((su.get("live_core_sdk_data") or {}).get("pull_data") or {}) if isinstance(
            su.get("live_core_sdk_data"), dict) else {}
        qualities = None
        if pull.get("stream_data") is not None:
            try:
                sd = (_json_or_dict(pull["stream_data"]) or {}).get("data") or {}
                qualities = []
                for name, q in sd.items():
                    main = (q or {}).get("main") or {}
                    try:
                        sp = _json_or_dict(main.get("sdk_params")) or {}
                    except (ValueError, TypeError) as exc:
                        sp = {}
                        errs.append(f"sdk_params[{name}]: {exc!r}")
                    if "fps" not in sp and "Fps" in sp:
                        sp = {**sp, "fps": sp["Fps"]}
                    qualities.append({"name": name, **{k: sp.get(k) for k in TT_ROOM_SDK_KEYS}})
            except (ValueError, TypeError, AttributeError) as exc:
                qualities = None
                errs.append(f"stream_data: {exc!r}")
        options = None
        opts = pull.get("options")
        if isinstance(opts, dict) and isinstance(opts.get("qualities"), list):
            options = [{k: q.get(k) for k in TT_ROOM_OPTION_KEYS} for q in opts["qualities"] if isinstance(q, dict)]
        lm = data.get("link_mic") if isinstance(data.get("link_mic"), dict) else {}
        bs = lm.get("battle_settings") if isinstance(lm.get("battle_settings"), dict) else {}
        sig = {"with_linkmic": data.get("with_linkmic"), "linkmic_layout": data.get("linkmic_layout"),
               "multi_stream_scene": data.get("multi_stream_scene"), "linker_map_size": _size(data.get("linker_map")),
               "battle_status": bs.get("battle_status"), "battle_id": bs.get("battle_id"),
               "audience_id_list_n": _size(lm.get("audience_id_list")), "battle_scores_n": _size(lm.get("battle_scores"))}
        conds = {"with_linkmic": sig["with_linkmic"] in (True, 1),   # levi 15:39: a bool in W-B; an int 1 counts too
                 "linkmic_layout": sig["linkmic_layout"] not in (None, 0),
                 "multi_stream_scene": sig["multi_stream_scene"] not in (None, 0),
                 "linker_map": (sig["linker_map_size"] or 0) > 0,
                 "battle_status": sig["battle_status"] not in (None, 0)}
        fired = [k for k, v in conds.items() if v]
        present = any(sig[k] is not None for k in ("with_linkmic", "linkmic_layout", "multi_stream_scene",
                                                   "linker_map_size", "battle_status"))
        return {"status_code": top.get("status_code"), "room_id": data.get("id_str") or data.get("id"),
                "qualities": qualities, "options": options, "stream": {k: su.get(k) for k in TT_ROOM_STREAM_KEYS},
                "multi_guest": True if fired else (False if present else None), "multi_guest_signals": sig,
                "fired": fired, "parse_errors": bool(errs), "parse_error": "; ".join(errs)[:200] if errs else None}

    def tt_room_enter(self) -> dict:
        return {"rule": self.RULE, "n_reads": self.n_reads, "reads": self.reads, "overflow": self.overflow,
                "errors": self.errors, "last_error": self.last_error, "workers": len(self.workers),
                "ready": self.ready.is_set(), "error": self.error}

    def _run(self) -> None:
        try:
            ws = websocket.WebSocket()
            ws.settimeout(1)
            ws.connect(self.ws_url)
        except Exception as exc:
            self.error = repr(exc)
            self.ready.set()
            return
        seq = 0

        def send(method: str, params: dict, session_id: str | None = None) -> int:
            nonlocal seq
            seq += 1
            ws.send(json.dumps({"id": seq, "method": method, "params": params,
                                **({"sessionId": session_id} if session_id else {})}))
            return seq
        try:
            send("Network.enable", dict(self.NETWORK))
            send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            self.ready.set()
            while not self._stop.is_set():
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                self.handle(msg, send)
        except Exception as exc:
            self.error = repr(exc)
        finally:
            self.ready.set()
            try:
                ws.close()
            except Exception:
                pass


DRAIN_FLAT_S = 0.5     # v3 A12: downstream bytes flat this long = the in-flight responses landed
DRAIN_MAX_S = 5.0      # ... or this cap (the pcap end moves by at most this)
DRAIN_POLL_S = 0.1


class CaptureDrain:
    """v3 A12 (levi 21:06 / 23:19; peter's test 23:20: a frozen page stalls in-flight bodies, Fetch blocking does not):
    after the label export and before Page.close (which aborts in-flight responses), a CDP client of its own on the page
    fails every NEW request (Fetch.enable {urlPattern "*", stage Request} on the page and on every worker and out-of-process
    iframe target it auto-attaches; each Fetch.requestPaused -> Fetch.failRequest Aborted, counted) while in-flight responses finish, then
    waits until the session's downstream bytes are flat for DRAIN_FLAT_S, or DRAIN_MAX_S. The connection stays open
    (interception on) until close(), called after Page.close. Never fatal: errors are recorded. run() -> meta.json drain."""

    WORKER_TYPES = ("worker", "shared_worker", "service_worker")
    FRAME_TYPES = ("iframe",)       # brad 16:39: an out-of-process iframe (a yt ad iframe, an embed) is its own target
    PATTERNS = {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}

    def __init__(self, ws_url: str, read_bytes, source: str, ws_factory=None, clock=time.monotonic, sleep=time.sleep):
        self.ws_url, self.read_bytes, self.source, self.clock, self.sleep = ws_url, read_bytes, source, clock, sleep
        self.ws_factory = ws_factory or (lambda: websocket.WebSocket())
        self.ws = None
        self.seq = 0
        self.failed = {"page": 0, "worker": 0, "frame": 0}
        self.workers = 0
        self.frames = 0
        self.kind: dict[str, str] = {}             # attached session id -> "worker" | "frame"

    def _send(self, method: str, params: dict, session_id: str | None = None) -> None:
        self.seq += 1
        self.ws.send(json.dumps({"id": self.seq, "method": method, "params": params,
                                 **({"sessionId": session_id} if session_id else {})}))

    def handle(self, msg: dict) -> None:
        m, prm, sid = msg.get("method"), msg.get("params") or {}, msg.get("sessionId")
        ttype = (prm.get("targetInfo") or {}).get("type") if m == "Target.attachedToTarget" else None
        if ttype in self.WORKER_TYPES or ttype in self.FRAME_TYPES:
            kind = "worker" if ttype in self.WORKER_TYPES else "frame"
            self.kind[prm.get("sessionId")] = kind
            if kind == "worker":
                self.workers += 1
            else:
                self.frames += 1
            self._send("Fetch.enable", self.PATTERNS, prm.get("sessionId"))
            if kind == "frame":                    # levi 16:48: nested OOPIFs and their workers attach (and block) too
                self._send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                           prm.get("sessionId"))
            self._send("Runtime.runIfWaitingForDebugger", {}, prm.get("sessionId"))
        elif m == "Fetch.requestPaused":
            self.failed[self.kind.get(sid, "worker") if sid else "page"] += 1
            self._send("Fetch.failRequest", {"requestId": prm.get("requestId"), "errorReason": "Aborted"}, sid)

    def _pump(self, until: float) -> None:
        while self.clock() < until:
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                return
            if raw:
                self.handle(json.loads(raw))

    def run(self) -> dict:
        rec = {"blocked": None, "error": None, "wait_s": None, "reason": None, "bytes_during": None,
               "source": self.source, "flat_s": DRAIN_FLAT_S, "max_s": DRAIN_MAX_S}
        t0 = self.clock()
        try:
            self.ws = self.ws_factory()
            self.ws.settimeout(DRAIN_POLL_S / 2)
            self.ws.connect(self.ws_url)
            self._send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
            self._send("Fetch.enable", self.PATTERNS)
            rec["blocked"] = True
        except Exception as exc:                        # never fatal: the session ends as before, recorded
            rec["blocked"], rec["error"] = False, repr(exc)[:200]
        b0 = self.read_bytes()
        if b0 is None:
            rec["reason"] = "no_counter"
        else:
            last_b, last_change = b0, self.clock()
            while True:
                if self.ws is not None and rec["blocked"]:
                    try:
                        self._pump(self.clock() + DRAIN_POLL_S)
                    except Exception as exc:
                        rec["error"] = rec["error"] or repr(exc)[:200]
                        self.sleep(DRAIN_POLL_S)
                else:
                    self.sleep(DRAIN_POLL_S)
                b, now = self.read_bytes(), self.clock()
                if b is not None and b != last_b:
                    last_b, last_change = b, now
                if now - last_change >= DRAIN_FLAT_S:
                    rec["reason"] = "quiet"
                    break
                if now - t0 >= DRAIN_MAX_S:
                    rec["reason"] = "cap"
                    break
            rec["bytes_during"] = last_b - b0
        rec["wait_s"] = round(self.clock() - t0, 3)
        rec["requests_failed"] = dict(self.failed)
        rec["workers_attached"] = self.workers
        rec["frames_attached"] = self.frames
        return rec

    def close(self) -> None:
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:
            pass

# WA2 r1 ti_100001 (levi 00:28 (5a)): a re-entry was confirmed "playing" on the clock alone (ct 2.13 at readyState 4) and
# then presented no frame for 949 s. A confirm now also needs decoded frames to advance, by the frozen rule's own
# threshold (brad 00:45 S3 / levi 00:38: one definition of "frames advance"): the last sample pair's step (samples ~1 s
# apart) > check_session.FROZEN_FRAMES_MAX; a reset pair is no step; an unreadable counter falls back to the clock, recorded
from check_session import FROZEN_FRAMES_MAX as _FROZEN_FRAMES_MAX
CONFIRM_MIN_STEP = _FROZEN_FRAMES_MAX + 1


class _FramesGate:
    """Decoded frames (totalVideoFrames) over the confirm's samples; ok() when the last pair advanced by >=
    CONFIRM_MIN_STEP, or the counter was never readable (then confirm_basis clock_fallback)."""
    def __init__(self):
        self.first = self.last = self.step = None
        self.seen = False

    def feed(self, tf) -> None:
        if not isinstance(tf, (int, float)):
            return
        self.seen = True
        reset = self.last is not None and tf < self.last
        self.step = None if self.last is None or reset else tf - self.last
        if self.first is None or reset:
            self.first = tf                       # the first sample, or a new element / reset: count from here
        self.last = tf

    def ok(self) -> bool:
        return not self.seen or (self.step is not None and self.step >= CONFIRM_MIN_STEP)

    def record(self) -> dict:
        # levi 00:35 (2): which basis confirmed it, counted per window; a clock_fallback re-entry is listed in the read
        return {"frames": None if not self.seen else self.last - self.first, "last_step": self.step,
                "frames_unreadable": not self.seen,
                "confirm_basis": "frames" if self.seen else "clock_fallback"}


def _confirm_advancing(page: "CDPPage", seconds: float) -> tuple[bool, dict]:
    """After a re-entry (yt / twitch): play pressed while paused; the playhead advancing >= 2 s at readyState >= 3 and
    decoded frames advancing (_FramesGate) within `seconds`."""
    first, st, fg = None, {}, _FramesGate()
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            st = json.loads(page.eval(_PLAYER_STATE_JS) or "null") or {}
            if st.get("paused"):
                page.eval(_PLAY_JS)
            ct = page.eval(_CURRENT_TIME_JS)
        except Exception:
            ct = None
        if ct is not None and ct >= 0 and (st.get("readyState") or st.get("rs") or 0) >= 3:
            first = ct if first is None else first
            fg.feed(st.get("tf"))
            if ct - first >= 2.0 and fg.ok():
                return True, {"ct": ct, **fg.record()}
        time.sleep(1)
    return False, {"not_playing": True, **fg.record(),
                   **{k: st.get(k) for k in ("paused", "readyState", "rs") if k in st}}


# t56 tw_100016 (levi's ruling, for the N_collect boundary): Twitch's Error #2000 page keeps the "Ad" badge, so every tick
# read as an ad, the frozen run (non-ad ticks only) stayed 0, and the session waited ~23 min on a destroyed player. Two
# triggers (a third, ad_frozen, below), yt / twitch (tt's dead-room watcher reads bytes, not the ad label): a player
# error page (Twitch "Error #<n>" /
# "Reload Player" in the player, yt's .ytp-error) seen for ERROR_PAGE_CONFIRM_S; an ad state with no live player (no
# <video>, or readyState 0) for more than AD_NO_PLAYER_MAX_S (levi: treated as player-dead). Either one: the session's one
# recovery (stuck.action's budget, shared with the frozen / dead re-entries) as a re-entry in player_dead_reentries
# (LVQoE's labeler censors t_nav -> the next video_start): Twitch's reload button clicked when the page shows one, else the
# channel URL navigated; then the playhead must advance (stuck.REENTRY_CONFIRM_S) or the session aborts "player_error".
# The ad-wait exposure (ad runs, total and longest seconds) and every trigger go to meta.json (player_errors, ad_wait).
ERROR_PAGE_CONFIRM_S = 5.0
AD_NO_PLAYER_MAX_S = 60.0
# t56 tw_100022 (shagun, boundary-era round 1; levi's ruling 00135): a third variant, a frozen ad. The ad label on from
# 566 s; the pre-roll ran (countdown 1:14 at 567 s, 0:42 at 600 s, a 26 s pause, 0:32 at 640 s, 0:22 at 650 s) until the
# element paused at 650.6 s (readyState 4, playhead still) and the countdown stopped at 0:21 for the 800 s left. In an
# ad state with a player, progress is the ad countdown's text changing or the playhead moving more than
# AD_FROZEN_TOL_S; neither for more than AD_FROZEN_MAX_S is player-dead ("ad_frozen"; the real ad's own pauses in that
# session lasted 7-27 s).
AD_FROZEN_MAX_S = 60.0
AD_FROZEN_TOL_S = 0.05
SKIP_FUTILE_CLICKS = 10      # levi 00176 (2b): yt's skip loop stops after this many clicks with the ad unchanged
# t83 (11), W7 yt_100138 (levi 03:14): the ad element paused at its own end (ct = dur, ended false, readyState 1) and yt
# never left the ad: 393 s of skip_futile to the session's end, the ad_frozen clock held by the low state all along. An
# ad state with skip_futile for AD_END_STUCK_S over a paused element at its end (ct >= dur - AD_END_TOL_S) is a fourth
# trigger, ad_stuck_at_end, never held (levi: recover as the stuck-player re-entry does); the hold's state is recorded.
AD_END_STUCK_S = 30.0
AD_END_TOL_S = 0.25
PLAYER_ERROR_PLATFORMS = ("youtube", "twitch")
LOWSTATE_FREE_MAX = 3      # levi 11:48: at most this many low-state error_page re-entries skip the one recovery
_PLAYER_ERROR_JS = """
(function() {
    const vis = (el) => !!el && el.offsetParent !== null && el.getClientRects().length > 0;
    const h = location.hostname;
    let text = '', code = null, btn = null;
    if (h.includes('twitch.')) {
        const root = document.querySelector('[data-a-target="video-player"]') || document.querySelector('.video-player');
        if (!root) return null;
        text = root.innerText || '';
        const m = text.match(/Error #(\\d+)/);
        btn = [...root.querySelectorAll('button')].find(b => vis(b) &&
            /reload player/i.test((b.textContent || '') + ' ' + (b.getAttribute('aria-label') || '')));
        if (!m && !btn) return null;
        code = m ? m[1] : null;
        if (%(click)s && btn) btn.click();
    } else if (h.includes('youtube.')) {
        const e = document.querySelector('#movie_player .ytp-error');
        if (!vis(e)) return null;
        text = e.innerText || '';
    } else {
        return null;
    }
    return JSON.stringify({code: code, text: text.replace(/\\s+/g, ' ').trim().slice(0, 200), reload_button: !!btn});
})()
"""
_PLAYER_ERROR_CHECK_JS = _PLAYER_ERROR_JS % {"click": "false"}
_PLAYER_ERROR_RELOAD_JS = _PLAYER_ERROR_JS % {"click": "true"}
_AD_PLAYER_JS = """
(function() {
    const vs = [...document.querySelectorAll('video')];
    const v = vs.filter(x => x.getClientRects().length).sort((a, b) => b.clientWidth * b.clientHeight
                                                                   - a.clientWidth * a.clientHeight)[0] || vs[0];
    // the ad's countdown text (twitch: "... Ad (0:21)"; yt: the ad's remaining-time badge): its changes are progress
    let cd = null;
    for (const sel of ['[data-a-target="video-ad-countdown"]', '[data-a-target="video-ad-label"]',
                       '#movie_player .ytp-ad-duration-remaining', '#movie_player .ytp-ad-preview-text-modern']) {
        const e = document.querySelector(sel);
        if (e && (e.innerText || '').trim()) { cd = (e.innerText || '').trim().slice(0, 80); break; }
    }
    if (cd === null) {
        const m = (document.body ? document.body.innerText || '' : '').match(/Ad \\((\\d+:\\d\\d)\\)/);
        cd = m ? m[1] : null;
    }
    return JSON.stringify({ad: document.documentElement.dataset.lvqoeAd === '1', video: !!v, rs: v ? v.readyState : null,
                           ct: v ? v.currentTime : null, paused: v ? v.paused : null, cd: cd,
                           dur: v && isFinite(v.duration) ? v.duration : null});
})()
"""


class PlayerErrorWatch:
    """The two triggers above, on one poll a second: step(error, ad_player, now) -> a trigger record or None. Also the
    ad-wait exposure: runs of the ad label (start, end), their total and the longest."""

    def __init__(self, confirm_s: float = ERROR_PAGE_CONFIRM_S, no_player_s: float = AD_NO_PLAYER_MAX_S,
                 frozen_s: float = AD_FROZEN_MAX_S):
        self.confirm_s, self.no_player_s, self.frozen_s = confirm_s, no_player_s, frozen_s
        self.err_since = self.np_since = self.ad_since = None
        self.on_onset = None                       # v3: called once at each error-page onset (BrowserSession: a shot)
        self.fz_since, self.fz_ct, self.fz_cd = None, None, None   # the frozen-ad run: since, playhead, countdown
        self.ad_runs: list[list[float]] = []
        self.held_fn = None          # yt / twitch (levi 10:27): now -> True while the shaper's low-state hold runs
        self.skip_futile_since = None   # levi 14:35: the yt skip loop stopped (skip_futile) in the current ad run
        # sup 14:13 (4) / levi 00176: stuck-ad spans, the ad state over a player whose playhead and countdown stay still
        # for more than frozen_s, recorded whatever the low-state hold (meta.json ad_wait.ad_stuck_runs)
        self.stk_since, self.stk_ct, self.stk_cd, self.stk_st = None, None, None, None
        self.ad_stuck_runs: list[dict] = []

    def _close_stuck(self, end: float) -> None:
        if self.stk_since is not None and end - self.stk_since > self.frozen_s and len(self.ad_stuck_runs) < WATCH_LOG_MAX:
            st = self.stk_st or {}
            self.ad_stuck_runs.append({"start": round(self.stk_since, 3), "end": round(end, 3),
                                       "s": round(end - self.stk_since, 1), "skip_futile": self.skip_futile_since is not None,
                                       **{k: st.get(k) for k in ("ct", "rs", "paused", "cd")}})
        self.stk_since = self.stk_ct = self.stk_cd = self.stk_st = None

    def step(self, err: dict | None, st: dict | None, now: float) -> dict | None:
        ad = bool((st or {}).get("ad"))
        if ad and self.ad_since is None:
            self.ad_since = now
            self.ad_runs.append([now, now])
        elif ad:
            self.ad_runs[-1][1] = now
        else:
            self.ad_since = None
            self.skip_futile_since = None
        if err:
            if self.err_since is None and self.on_onset is not None:   # v3 (levi 16:57): the error page shot, at onset
                try:
                    self.on_onset(now)
                except Exception:
                    pass
            self.err_since = self.err_since or now
            if now - self.err_since >= self.confirm_s:
                return {"reason": "error_page", "since": self.err_since, "t_trigger": now, "error": err, "ad": ad}
        else:
            self.err_since = None
        no_player = st is None or not st.get("video") or (st.get("rs") or 0) == 0
        if ad and no_player:
            self.np_since = self.np_since or now
            if now - self.np_since > self.no_player_s:
                return {"reason": "ad_no_player", "since": self.np_since, "t_trigger": now, "error": err,
                        "player": st}
        else:
            self.np_since = None
        ct, cd = (st or {}).get("ct"), (st or {}).get("cd")
        if ad and not no_player and isinstance(ct, (int, float)):     # the stuck-ad span, never held
            if (self.stk_since is None or cd != self.stk_cd or self.stk_ct is None
                    or abs(ct - self.stk_ct) > AD_FROZEN_TOL_S):
                self._close_stuck(now)
                self.stk_since, self.stk_ct, self.stk_cd, self.stk_st = now, ct, cd, dict(st or {})
        else:
            self._close_stuck(now)
        dur = (st or {}).get("dur")
        if (ad and self.skip_futile_since is not None and now - self.skip_futile_since >= AD_END_STUCK_S
                and (st or {}).get("paused") and isinstance(ct, (int, float)) and isinstance(dur, (int, float))
                and ct >= dur - AD_END_TOL_S):
            trig = {"reason": "ad_stuck_at_end", "since": self.skip_futile_since, "t_trigger": now, "error": err,
                    "player": st, "skip_futile": True, "held": bool(self.held_fn and self.held_fn(now))}
            self._close_stuck(now)
            return trig
        if ad and not no_player and isinstance(ct, (int, float)):     # an unread playhead is no evidence of a freeze
            held = bool(self.held_fn and self.held_fn(now))       # levi 10:27: the ad_frozen clock holds in low states
            moved = (held or self.fz_since is None or cd != self.fz_cd or self.fz_ct is None
                     or abs(ct - self.fz_ct) > AD_FROZEN_TOL_S)
            if moved:                                         # the countdown or the playhead moved: the run starts here
                self.fz_since, self.fz_ct, self.fz_cd = now, ct, cd
            elif now - self.fz_since > self.frozen_s:
                trig = {"reason": "ad_frozen", "since": self.fz_since, "t_trigger": now, "error": err, "player": st,
                        "skip_futile": self.skip_futile_since is not None}
                self._close_stuck(now)                        # the re-entry ends the span
                return trig
        else:
            self.fz_since = self.fz_ct = self.fz_cd = None
        return None

    def reset(self) -> None:
        """After a recovery: every trigger starts over."""
        self.err_since = self.np_since = self.fz_since = self.fz_ct = self.fz_cd = None
        self.skip_futile_since = None
        self.stk_since = self.stk_ct = self.stk_cd = self.stk_st = None

    def exposure(self) -> dict:
        d = [b - a for a, b in self.ad_runs]
        return {"ad_runs": len(d), "ad_total_s": round(sum(d), 1), "ad_longest_s": round(max(d), 1) if d else 0.0,
                "error_page_confirm_s": self.confirm_s, "ad_no_player_max_s": self.no_player_s,
                "ad_frozen_max_s": self.frozen_s, "ad_stuck_runs": self.ad_stuck_runs,
                "ad_stuck_total_s": round(sum(r["s"] for r in self.ad_stuck_runs), 1),
                "skip_futile_clicks": SKIP_FUTILE_CLICKS}


# boundary bundle item 3 (levi f2fb3ee7 + brad / levi 03:35, the reader's stream_ended rule in LVQoE checkpoint_session_list,
# applied live): a yt / twitch tab that left its live stream (the broadcast ended and the page fell back to the recording,
# or twitch raided / hosted another channel) aborts the session "stream_ended". Signs, as the reader's:
#   U  the url's page leaves the channel page (after a sample has shown it): on twitch alone; on yt only with the
#      recording confirmed after it (VOD below);
#   P  the non-ad playhead falls by > STREAM_RESET_DROP_S to <= STREAM_RESET_TO_S, with the recording confirmed after it.
# VOD: the next STREAM_FIXED_N non-ad samples carry a finite duration fixed within STREAM_FIXED_TOL_S and not the one
# before, and the playhead advances >= STREAM_VOD_PLAY_S from the reset within STREAM_VOD_WITHIN samples, staying below
# STREAM_RESET_DROP_S (a reload that jumps back to the live position, or sits stuck at 0, is no sign; yt_100052).
STREAM_RESET_DROP_S, STREAM_RESET_TO_S = 60.0, 10.0
STREAM_FIXED_N, STREAM_FIXED_TOL_S = 10, 1.0
STREAM_VOD_PLAY_S, STREAM_VOD_WITHIN = 5.0, 60
STREAM_ENDED_PLATFORMS = ("youtube", "twitch")
_STREAM_STATE_JS = """
(function() {
    const vs = [...document.querySelectorAll('video')].filter(v => v.getClientRects().length);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    const d = document.documentElement.dataset;
    return JSON.stringify({url: location.href, ct: v ? v.currentTime : null,
                           dur: v ? (isFinite(v.duration) ? v.duration : 'inf') : null, ad: d.lvqoeAd === '1'});
})()
"""


def stream_page(url: str | None) -> str:
    """The url's page: yt watch:<id>, else the path (/@ch/live, /<channel>), lower-cased (LVQoE page_of)."""
    u = url or ""
    m = re.search(r"[?&]v=([^&]+)", u)
    if m:
        return f"watch:{m.group(1)}"
    return re.sub(r"https?://([a-z]+\.)?(youtube|twitch)\.(com|tv)", "", u.split("?")[0]).rstrip("/").lower()


class StreamEndedWatch:
    """The stream_ended rule over 1 s samples ({url, ct, dur, ad}); feed() -> a trigger record once a sign is confirmed,
    else None. The sign's own time (sign_t) is recorded: the reader cuts at the earliest sign, not at the trigger."""

    def __init__(self, platform: str, channel_url: str | None):
        self.platform, self.ref = platform, stream_page(channel_url) if channel_url else None
        self.seen_ref = False
        self.samples: list[tuple[float, float | None, float | None]] = []     # non-ad (t, ct, dur)
        self.pending: list[dict] = []
        self.fired: dict | None = None
        self.call_ms: list[float] = []

    @staticmethod
    def _dur(x) -> float | None:
        if x in (None, "inf"):
            return math.inf if x == "inf" else None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def _vod(self, i: int) -> bool | None:
        """True / False once decided, None while the samples after index i are too few."""
        ct = [c for _, c, _ in self.samples]
        du = [d for _, _, d in self.samples]
        after = du[i + 1:i + 1 + STREAM_FIXED_N]
        if any(d is None or not math.isfinite(d) for d in after):
            return False
        if len(after) < STREAM_FIXED_N:
            return None
        if max(after) - min(after) > STREAM_FIXED_TOL_S:
            return False
        b = du[i - 1] if i >= 1 else None
        if not (b is None or not math.isfinite(b) or abs(b - after[-1]) > STREAM_FIXED_TOL_S):
            return False
        c0 = ct[i] if ct[i] is not None else 0.0
        for c, d in zip(ct[i + 1:i + 1 + STREAM_VOD_WITHIN], du[i + 1:i + 1 + STREAM_VOD_WITHIN]):
            if c is None or c >= STREAM_RESET_DROP_S or d is None or not math.isfinite(d) or abs(d - after[-1]) > STREAM_FIXED_TOL_S:
                return False
            if c - c0 >= STREAM_VOD_PLAY_S:
                return True
        return None if len(ct) - (i + 1) < STREAM_VOD_WITHIN else False

    def feed(self, s: dict | None, now: float) -> dict | None:
        if self.fired is not None or not s:
            return None
        page = stream_page(s.get("url")) if s.get("url") else None
        if self.ref is None and page:
            self.ref = page
        if page == self.ref:
            self.seen_ref = True
        if s.get("ad"):
            return None
        ct = s.get("ct") if isinstance(s.get("ct"), (int, float)) else None
        self.samples.append((now, ct, self._dur(s.get("dur"))))
        i = len(self.samples) - 1
        if page and self.seen_ref and page != self.ref and not any(p["sign"] == "U" for p in self.pending):
            if self.platform == "twitch":
                return self._fire({"sign": "U", "sign_t": now, "i": i, "from_page": self.ref, "to_page": page}, now)
            self.pending.append({"sign": "U", "sign_t": now, "i": i, "from_page": self.ref, "to_page": page})
        if i >= 1 and ct is not None and self.samples[i - 1][1] is not None \
                and self.samples[i - 1][1] - ct > STREAM_RESET_DROP_S and ct <= STREAM_RESET_TO_S:
            self.pending.append({"sign": "P", "sign_t": now, "i": i, "ct_before": self.samples[i - 1][1]})
        for p in list(self.pending):
            v = self._vod(p["i"])
            if v is True:
                return self._fire(p, now)
            if v is False:
                self.pending.remove(p)
        return None

    def note_call(self, ms: float) -> None:
        """One _STREAM_STATE_JS read's wall time (ms), the item's cost measure (levi 11:22: the read's own per-call
        timing, not slot CPU, which content dominates)."""
        self.call_ms.append(ms)

    def timing(self) -> dict:
        """The reads' timing summary for meta.json stream_read_timing: n, mean / p50 / p95 / max ms."""
        return _timing(self.call_ms)

    def _fire(self, p: dict, now: float) -> dict:
        self.fired = {"reason": "stream_ended", "t_trigger": now, **{k: v for k, v in p.items() if k != "i"},
                      "dur_after": self.samples[-1][2] if self.samples else None}
        return self.fired


# levi 16:26 / 16:27 (record-only, the 10-22 tip): yt's ladder from what the page already holds: the player response's
# streamingData.adaptiveFormats (the source stuck.YT_FORMATS_JS reads for the lowest rung) and the player API's
# getAvailableQualityLevels(), a read that never sets a quality (no request, no click, no menu, no playback change).
# Read every YT_LADDER_EVERY_S, written to meta.json yt_ladder only when it changed; each read's wall time goes to
# meta.json stream_read_timing.yt_ladder (item 3's per-call timing). No decision reads it.
YT_LADDER_EVERY_S = 30.0
YT_LADDER_JS = """(() => { try {
  const p = document.querySelector('#movie_player');
  const r = (p && p.getPlayerResponse && p.getPlayerResponse()) || window.ytInitialPlayerResponse || {};
  const f = ((r.streamingData || {}).adaptiveFormats || []).map(x => ({itag: x.itag, mimeType: x.mimeType,
      bitrate: x.bitrate, width: x.width, height: x.height, qualityLabel: x.qualityLabel, fps: x.fps}));
  const levels = (p && p.getAvailableQualityLevels) ? p.getAvailableQualityLevels() : null;
  return JSON.stringify({formats: f, levels: levels, ad: document.documentElement.dataset.lvqoeAd === '1'});
} catch (e) { return null; } })()"""


def _timing(call_ms: list) -> dict:
    """n, mean / p50 / p95 / max ms of a reader's per-call wall times (stream_read_timing's form)."""
    v = sorted(call_ms)
    if not v:
        return {"n": 0}
    pick = lambda q: round(v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))], 3)
    return {"n": len(v), "mean_ms": round(sum(v) / len(v), 3), "p50_ms": pick(0.5), "p95_ms": pick(0.95),
            "max_ms": round(v[-1], 3)}


# levi 18:36 (option A, record-only, the 10-22 tip): tt's codec and renditions from what the page already holds: the
# WebCodecs capability flag the player stored (localStorage webcodecs_ability_key_temporary.isSupportH265, as
# stuck.TT_ROOM_JS reads it) and each sdk_params rendition in the room data (VCodec, resolution, vbitrate,
# stream_suffix), parsed in the page so only that short list crosses CDP. No injection, no request, no click. Every
# TT_CODEC_EVERY_S, written to meta.json tt_codec on change, timed into stream_read_timing.tt_codec. tt's decode_hw is
# not readable from the page and stays absent (levi 18:36).
TT_CODEC_EVERY_S = 30.0
TT_CODEC_JS = """(() => { try {
  let h265 = null;
  try { const a = JSON.parse(localStorage.getItem('webcodecs_ability_key_temporary') || 'null'); if (a) h265 = !!a.isSupportH265; } catch (e) {}
  const t = Array.from(document.scripts).map(s => s.textContent || '').filter(x => x.includes('sdk_params')).join('\\n').split('\\\\').join('');
  const key = '"sdk_params":"{', out = [], seen = new Set();
  for (let i = t.indexOf(key); i >= 0 && out.length < 64; i = t.indexOf(key, i + 1)) {
    let j = i + key.length - 1, depth = 0, k = j;
    for (; k < Math.min(t.length, j + 4000); k++) { depth += t[k] === '{' ? 1 : t[k] === '}' ? -1 : 0; if (depth === 0) break; }
    const b = t.slice(j, k + 1), g = (re) => { const m = b.match(re); return m ? m[1] : null; };
    const r = {codec: g(/"VCodec":"(\\w+)"/), resolution: g(/"resolution":"([0-9x]*)"/),
               vbitrate: g(/"vbitrate":(\\d+)/), suffix: g(/"stream_suffix":"([^"]*)"/)};
    const id = JSON.stringify(r);
    if (r.codec && !seen.has(id)) { seen.add(id); out.push(r); }
  }
  return JSON.stringify({h265: h265, renditions: out});
} catch (e) { return null; } })()"""


class YtLadderRecorder:
    """meta.json yt_ladder: the reads that changed ({t, formats (itag, mimeType with its codecs, bitrate, width, height,
    qualityLabel, fps), levels, ad}, at most WATCH_LOG_MAX), n_reads, n_null. TtCodecRecorder (meta.json tt_codec) is
    the same recorder over TT_CODEC_JS's {h265, renditions}."""
    KEYS = ("formats", "levels")                   # a read with none of these non-empty is a null read
    RULE = "levi 16:27: record-only; page-held formats + getAvailableQualityLevels, written on change"

    def __init__(self, every_s: float = YT_LADDER_EVERY_S):
        self.every_s, self.next_at = every_s, 0.0
        self.reads: list[dict] = []
        self.n_reads = self.n_null = 0
        self.call_ms: list[float] = []
        self._last = None

    def due(self, now: float) -> bool:
        return now >= self.next_at

    def feed(self, value: str | None, now: float, ms: float | None = None) -> None:
        self.next_at, self.n_reads = now + self.every_s, self.n_reads + 1
        if ms is not None:
            self.call_ms.append(ms)
        try:
            v = json.loads(value) if value else None
        except ValueError:
            v = None
        if not v or not any(v.get(k) for k in self.KEYS):
            self.n_null += 1
            return
        key = json.dumps(v, sort_keys=True)
        if key != self._last and len(self.reads) < WATCH_LOG_MAX:
            self.reads.append(self._row(v, now))
        self._last = key

    def _row(self, v: dict, now: float) -> dict:
        return {"t": now, "formats": v.get("formats") or [], "levels": v.get("levels"),
                "ad": bool(v.get("ad"))}                        # brad 16:27: an ad's player response is marked

    def timing(self) -> dict:
        return _timing(self.call_ms)

    def record(self) -> dict:
        return {"every_s": self.every_s, "n_reads": self.n_reads, "n_null": self.n_null, "reads": self.reads,
                "rule": self.RULE}


class TtCodecRecorder(YtLadderRecorder):
    """meta.json tt_codec (levi 18:36, option A): the reads that changed ({t, h265, renditions [{codec, resolution,
    vbitrate, suffix}]}); a read with no rendition and no capability flag is a null read."""
    KEYS = ("renditions", "h265")
    RULE = ("levi 18:36 (A): record-only; the page-held WebCodecs capability flag + the room's sdk_params renditions, "
            "written on change; decode_hw not readable (absent)")

    def __init__(self, every_s: float = TT_CODEC_EVERY_S):
        super().__init__(every_s)

    def _row(self, v: dict, now: float) -> dict:
        return {"t": now, "h265": v.get("h265"), "renditions": v.get("renditions") or []}


def _ad_watcher_loop(page: CDPPage, platform: str, stop: threading.Event, log_path: Path | None = None,
                     on_wall=None, on_frozen=None, on_stuck=None, on_player_error=None,
                     error_watch: PlayerErrorWatch | None = None, resumer: "SelfPauseResumer | None" = None,
                     on_stream_ended=None, stream_watch: "StreamEndedWatch | None" = None,
                     yt_ladder: "YtLadderRecorder | None" = None, clock_sample=None, on_action=None) -> None:
    """Run the platform's ad action once a second; every action is logged with a timestamp (ad_actions.jsonl).
    Also watches for the login wall content.js marks (t37) and calls on_wall() once when it appears, and (addendum N)
    calls on_frozen(seconds) once when the frozen-playhead run passes the platform's T (check_session table); passes the
    same frozen run to on_stuck(seconds) every second (the stuck-player rule, stuck.py). Presses play on a self-paused
    player (SelfPauseResumer)."""
    skip_js = _SKIP_AD_JS.get(platform, "null")
    walled = False
    frozen_max_s = FROZEN_PLAYHEAD_MAX_S.get(platform)
    froze = False
    resumer = resumer if resumer is not None else SelfPauseResumer()
    n = 0
    while not stop.wait(timeout=1):
        n += 1
        if clock_sample is not None and n % CLOCK_SAMPLE_S == 0:   # v3 A11
            try:
                clock_sample("tick")
            except Exception:
                pass
        if platform == "youtube" and n % CAPTIONS_CHECK_S == 0:
            try:
                cap = page.eval(_CAPTIONS_OFF_JS)
                if cap in ("turned-off", "click-failed") and log_path is not None:
                    with open(log_path, "a") as fh:
                        fh.write(json.dumps({"ts": time.time(), "platform": platform, "action": "captions_off",
                                             "result": cap}) + "\n")
            except Exception:
                pass
        try:
            now = time.time()
            press = resumer.step(json.loads(page.eval(_PLAYER_STATE_JS) or "null"), now)
            if press:
                rec = resume_press(page, platform, resumer, press, stop.wait)
                resumer.note_outcome(rec)
                if rec is not None and log_path is not None:
                    with open(log_path, "a") as fh:
                        fh.write(json.dumps({"ts": now, "platform": platform, **rec}) + "\n")
        except Exception:
            pass
        try:
            result = page.eval(skip_js)
            if result:
                print(f"[browser] Ad action: {result}", flush=True)
                ff = None
                if str(result).startswith("fast-forwarded"):     # t83 (11): the element's state at the fast-forward
                    try:
                        ff = json.loads(page.eval("document.documentElement.dataset.lvqoeFf || 'null'") or "null")
                    except Exception as exc:
                        ff = {"error": repr(exc)[:120]}
                if log_path is not None:
                    with open(log_path, "a") as fh:
                        fh.write(json.dumps({"ts": time.time(), "platform": platform, "action": result,
                                             **({"ff": ff} if ff is not None else {})}) + "\n")
                if str(result).startswith("skip_futile") and error_watch is not None \
                        and error_watch.skip_futile_since is None:    # levi 14:35: the re-entry is tagged with it
                    error_watch.skip_futile_since = time.time()
                if on_action is not None:                     # batch #10: tt's guide dismiss into meta.json too
                    try:
                        on_action(str(result), time.time())
                    except Exception:
                        pass
        except Exception:
            pass
        if on_player_error is not None and error_watch is not None:   # t56 tw_100016: error page / ad, no player
            try:
                err = json.loads(page.eval(_PLAYER_ERROR_CHECK_JS) or "null")
                st = json.loads(page.eval(_AD_PLAYER_JS) or "null")
            except Exception:
                err = st = None
            trig = error_watch.step(err, st, time.time())
            if trig is not None:
                try:
                    on_player_error(trig)
                except Exception as exc:
                    print(f"[browser] player-error recovery failed: {exc!r}", flush=True)
                error_watch.reset()
        if on_stream_ended is not None and stream_watch is not None:    # boundary bundle item 3: stream_ended
            try:
                t_call = time.perf_counter()
                sample = json.loads(page.eval(_STREAM_STATE_JS) or "null")
                stream_watch.note_call((time.perf_counter() - t_call) * 1000.0)   # levi 11:22 (7): its own cost
                trig = stream_watch.feed(sample, time.time())
            except Exception:
                trig = None
            if trig is not None:
                try:
                    on_stream_ended(trig)
                except Exception as exc:
                    print(f"[browser] stream_ended abort failed: {exc!r}", flush=True)
        if yt_ladder is not None and yt_ladder.due(time.time()):           # levi 16:27 / 18:36: record-only, timed
            try:
                t_call = time.perf_counter()
                val = page.eval(TT_CODEC_JS if isinstance(yt_ladder, TtCodecRecorder) else YT_LADDER_JS)
                yt_ladder.feed(val, time.time(), (time.perf_counter() - t_call) * 1000.0)
            except Exception:
                yt_ladder.feed(None, time.time())
        if on_wall is not None and not walled:
            try:
                if page.eval(_WALL_JS):
                    walled = True
                    on_wall()
            except Exception:
                pass
        if on_frozen is not None or on_stuck is not None:
            try:
                frozen_s = float(page.eval(_FROZEN_JS) or 0)
            except Exception:
                frozen_s = 0.0
            # levi w6: a deadlocked pause the press did not take counts as a frozen run (content.js skips paused)
            frozen_s = max(frozen_s, resumer.stuck_frozen_s(time.time()))
            if on_stuck is not None:
                try:
                    on_stuck(frozen_s)
                except Exception as exc:
                    print(f"[browser] stuck-player check failed: {exc!r}", flush=True)
            if on_frozen is not None and frozen_max_s is not None and not froze and frozen_s > frozen_max_s:
                froze = on_frozen(frozen_s) is not False      # False: held (yt / twitch low states), asked again next tick


SHOT_INTERVAL_S = _CS_CHECKS["shot_interval_s"]   # one source: check_session (WA2 r1). levi 16:57 (v3 gate list): 60 s cadence + event triggers (start, A1 dims change, STALL onset,
                         # error page); the instrumentation A/B measures the shot cost against a pseudo-shot null
SHOT_EVENT_MIN_GAP_S = 10.0   # levi 00:53: event shots of one reason >= this apart (content.js SHOT_EVENT_MIN_GAP_S, parity)
# levi 17:45 (A7's C5 needs an uncropped shot): "player" (default, as before) crops to the player rect; "viewport" keeps
# the whole page area (composite: the window's viewport; x11 / cdp: the viewport), so the content box is found in the image
SHOT_CROPS = ("player", "viewport")
# levi 18:34 (boundary after counted window 1): the default is viewport, the crop every v3 window ran (WA..WCJb, and window 1
# by env.local.sh), so a counted run never depends on the env; player stays selectable
SHOT_CROP_DEFAULT = "viewport"
SHOT_CROP_RAW = os.environ.get("LVQOE_SHOT_CROP", SHOT_CROP_DEFAULT)
SHOT_CROP = SHOT_CROP_RAW if SHOT_CROP_RAW in SHOT_CROPS else SHOT_CROP_DEFAULT   # an unknown value keeps the default, recorded
SHOT_WIDTH_PX = 640
SHOT_JPEG_QUALITY = 40
# shots are grabbed from the X display, outside Chrome's renderer (supervisor 2026-10-17: a CDP capture costs dropped
# frames near every shot -- tw_9 0.061 per tick within 1 s of a shot vs 0.0038 elsewhere -- and yt's ABR watches dropped
# frames); CDP is the fallback when there is no X display, Pillow lacks X support, or the grab is blank (a dark screen)
SHOT_SOURCE = os.environ.get("LVQOE_SHOT_SOURCE", "x11")
_VIEWPORT_JS = ("JSON.stringify({sx: window.screenX, sy: window.screenY, ow: window.outerWidth, oh: window.outerHeight, "
                "iw: window.innerWidth, ih: window.innerHeight, dpr: window.devicePixelRatio || 1})")


def _viewport_on_screen(v: dict) -> tuple[int, int, int, int] | None:
    """The page viewport's (x, y, w, h) on the X screen from window metrics: the browser frame is the same width on
    both sides and below, the tab strip / toolbar take the rest of the top."""
    try:
        dpr = float(v.get("dpr") or 1)
        side = max(0, (v["ow"] - v["iw"]) // 2)
        top = max(0, v["oh"] - v["ih"] - side)
        return (round((v["sx"] + side) * dpr), round((v["sy"] + top) * dpr), round(v["iw"] * dpr), round(v["ih"] * dpr))
    except (KeyError, TypeError, ValueError):
        return None


def _player_on_screen(viewport: tuple[int, int, int, int], player: dict | None) -> tuple[int, int, int, int] | None:
    """The player rectangle on the X screen: the viewport's screen origin plus the <video> rect (CSS px x dpr),
    clipped to the viewport."""
    try:
        vx, vy, vw, vh = viewport
        x, y, w, h = player["rect"]
        k = vw / float(player["inner"][0])
        x0, y0 = max(0, round(x * k)), max(0, round(y * k))
        x1, y1 = min(vw, round((x + w) * k)), min(vh, round((y + h) * k))
        if x1 - x0 < 16 or y1 - y0 < 16:
            return None
        return vx + x0, vy + y0, x1 - x0, y1 - y0
    except (TypeError, KeyError, ValueError, ZeroDivisionError, IndexError):
        return None


# X11 shot origin calibration (supervisor t37, 2026-10-18; yt_43 / tw_35 / tw_36: the window-metric model put the
# region about 20 device px below the viewport, so every window-geometry shot lost the player's top rows and gained a
# band of page or wallpaper at the bottom). Once per session, after the geometry is set and before the shot loop and
# the schedule start: one CDP capture of the viewport (no clip, no scale: no device-metric emulation) is matched against
# an X11 grab of the model's viewport +- SHOT_CALIB_MARGIN_PX, over the page outside the <video> rect (the picture moves
# between the two captures); coarse on 1 / SHOT_CALIB_SCALE grey images, refined at full size. Accepted when the mean
# |X11 - CDP| at the best offset is <= SHOT_CALIB_MAX_RESIDUAL and the best coarse offset beats every other coarse
# offset more than one coarse step away by >= SHOT_CALIB_MIN_GAP; the offset then moves every X11 region (DOM / CDP
# rects untouched). Otherwise the current model stands, flagged shot_origin_uncalibrated (meta and every index row).
SHOT_CALIB_MARGIN_PX = 64
SHOT_CALIB_SCALE = 4
SHOT_CALIB_MAX_RESIDUAL = 8.0
SHOT_CALIB_MIN_GAP = 4.0


def match_offset(tpl, area, mask=None, scale: int = SHOT_CALIB_SCALE) -> tuple[int, int, float, float | None]:
    """(y, x, residual, gap): the template's top-left in `area` at the least mean |area - tpl| over `mask` (True =
    compared; all when absent or too small), coarse on block means of `scale`, refined at full size +- scale. Grey
    float arrays; area at least the template's size."""
    import numpy as np
    tpl, area = np.asarray(tpl, float), np.asarray(area, float)
    h, w = tpl.shape
    if mask is None or mask.sum() < 256:
        mask = np.ones(tpl.shape, bool)

    def ds(a):
        hh, ww = a.shape[0] // scale * scale, a.shape[1] // scale * scale
        return a[:hh, :ww].reshape(hh // scale, scale, ww // scale, scale).mean(axis=(1, 3))
    t, A, m = ds(tpl), ds(area), ds(mask.astype(float)) > 0.5
    if m.sum() < 16:
        m = np.ones(t.shape, bool)
    ny, nx = A.shape[0] - t.shape[0] + 1, A.shape[1] - t.shape[1] + 1
    cost = np.full((max(ny, 1), max(nx, 1)), np.inf)
    for y in range(ny):
        for x in range(nx):
            cost[y, x] = np.abs(A[y:y + t.shape[0], x:x + t.shape[1]] - t)[m].mean()
    by, bx = np.unravel_index(int(np.argmin(cost)), cost.shape)
    far = cost.copy()
    far[max(0, by - 1):by + 2, max(0, bx - 1):bx + 2] = np.inf
    gap = float(far.min() - cost[by, bx]) if np.isfinite(far.min()) else None
    best = None
    for y in range(max(0, by * scale - scale), min(area.shape[0] - h, by * scale + scale) + 1):
        for x in range(max(0, bx * scale - scale), min(area.shape[1] - w, bx * scale + scale) + 1):
            c = float(np.abs(area[y:y + h, x:x + w] - tpl)[mask].mean())
            if best is None or c < best[0]:
                best = (c, y, x)
    return best[1], best[2], round(best[0], 3), (None if gap is None else round(gap, 3))


def calibration_verdict(residual: float | None, gap: float | None) -> bool:
    return residual is not None and residual <= SHOT_CALIB_MAX_RESIDUAL and gap is not None and gap >= SHOT_CALIB_MIN_GAP


# t53 pilot-1, trio layout "composite" (probe v3, 8d0270c: an occluded window's own XComposite pixmap matched its CDP
# capture, about 1 against about 100 for the other window's): each slot's shots are crops of its own window's pixmap.
# The window is found by the browser's pid (_NET_WM_PID; the pid from /proc by the CDP port); the viewport's position in
# the frame is calibrated once per session against the window's own CDP capture (the identity check: these are this
# window's pixels), accepted as the X11 calibration is. Per grab, a staleness record: the player crop barely changed
# (mean |diff| < COMPOSITE_STALE_DIFF on a 64 x 36 grey thumbnail) while the playhead advanced > COMPOSITE_STALE_CT_S.
COMPOSITE_STALE_DIFF = 0.5
COMPOSITE_STALE_CT_S = 0.5
_PLAYHEAD_JS = "(function(){var v=document.querySelector('video');return v?v.currentTime:null;})()"


def calibrate_composite(page: "CDPPage") -> dict:
    """The composite layout's calibration (see above); never raises. accepted: the frame grab holds the window's own
    viewport (calibration_verdict on the match, the <video> rect masked: the picture moves between the captures)."""
    t0 = time.time()
    rec: dict = {"method": "composite_vs_cdp_match", "at": t0, "offset": None, "residual": None, "gap": None,
                 "accepted": False, "frame": None,
                 "thresholds": {"max_residual": SHOT_CALIB_MAX_RESIDUAL, "min_gap": SHOT_CALIB_MIN_GAP,
                                "margin_px": SHOT_CALIB_MARGIN_PX, "scale": SHOT_CALIB_SCALE}}
    try:
        import base64
        import io
        import numpy as np
        from PIL import Image
        import session_meta
        import xcomposite
        pid = session_meta.chrome_cmdline(CDP_BASE).get("pid")
        rec["browser_pid"] = pid
        wins = xcomposite.windows_of_pid(pid) if pid else []
        rec["windows"] = wins[:3]
        if not wins:
            rec["why"] = "no viewable window with the browser's _NET_WM_PID" if pid else "no browser pid"
            return rec
        img, grec = xcomposite.grab(wins[0]["frame"])
        area = np.asarray(img.convert("L"), float)
        pr = json.loads(page.eval(_PLAYER_RECT_JS) or "{}")
        dpr = float(pr.get("dpr") or 1)
        shot = page.call("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False})
        cdp = np.asarray(Image.open(io.BytesIO(base64.b64decode(shot.get("data") or ""))).convert("L"), float)
        M = SHOT_CALIB_MARGIN_PX
        tpl = cdp[M:cdp.shape[0] - M, M:cdp.shape[1] - M]
        mask = np.ones(tpl.shape, bool)
        p = (pr.get("player") or {}).get("rect")
        if p:
            x0, y0 = int(p[0] * dpr) - M, int(p[1] * dpr) - M
            mask[max(0, y0):max(0, y0 + int(p[3] * dpr)), max(0, x0):max(0, x0 + int(p[2] * dpr))] = False
        rec.update(compared_share=round(float(mask.mean()), 3), grab=grec, cdp_px=[cdp.shape[1], cdp.shape[0]])
        if area.shape[0] < tpl.shape[0] or area.shape[1] < tpl.shape[1]:
            rec["why"] = f"frame {list(area.shape)} smaller than the viewport template {list(tpl.shape)}"
            return rec
        y, x, res, gap = match_offset(tpl, area, mask)
        rec.update(frame=wins[0]["frame"], viewport_in_frame=[int(x) - M, int(y) - M], viewport_px=[cdp.shape[1], cdp.shape[0]],
                   residual=res, gap=gap, accepted=calibration_verdict(res, gap))
    except Exception as exc:
        rec["why"] = repr(exc)[:200]
    rec["elapsed_s"] = round(time.time() - t0, 3)
    return rec


def _composite_shot(origin: dict, player: dict | None, page: "CDPPage", prev: dict) -> tuple:
    """(jpeg bytes | None, capture_px, region in the frame, crop, record) of one composite grab: the player rect (else
    the viewport) cropped from the window's own pixmap; the staleness record against the previous grab (prev, updated)."""
    import io
    import numpy as np
    from PIL import Image, ImageStat
    import xcomposite
    img, _ = xcomposite.grab(origin["frame"])
    vx, vy = origin["viewport_in_frame"]
    vw, vh = origin["viewport_px"]
    region, crop = (vx, vy, vw, vh), "viewport"
    pr = _player_on_screen(region, player) if player and SHOT_CROP == "player" else None
    if pr:
        region, crop = pr, "player"
    x, y, w, h = region
    im = img.crop((x, y, x + w, y + h))
    try:
        ct = page.eval(_PLAYHEAD_JS)
        ct = float(ct) if ct is not None else None
    except Exception:
        ct = None
    thumb = np.asarray(im.convert("L").resize((64, 36)), float)
    diff = None if prev.get("thumb") is None or prev["thumb"].shape != thumb.shape else float(np.abs(thumb - prev["thumb"]).mean())
    step = None if ct is None or prev.get("ct") is None else ct - prev["ct"]
    rec = {"frame": origin["frame"], "diff_prev": None if diff is None else round(diff, 3),
           "ct_step_s": None if step is None else round(step, 3),
           "stale": bool(diff is not None and step is not None and step > COMPOSITE_STALE_CT_S and diff < COMPOSITE_STALE_DIFF)}
    prev.update(thumb=thumb, ct=ct)
    if max(ImageStat.Stat(im.convert("L").resize((64, 36))).stddev) < 1.0:
        rec["blank"] = True
        return None, [w, h], region, crop, rec
    if im.width > SHOT_WIDTH_PX:
        im = im.resize((SHOT_WIDTH_PX, round(im.height * SHOT_WIDTH_PX / im.width)), Image.BILINEAR)
    out = io.BytesIO()
    im.save(out, format="JPEG", quality=SHOT_JPEG_QUALITY)
    return out.getvalue(), [w, h], region, crop, rec


def _window_state(target_id: str | None) -> str | None:
    """The Chrome window's state over CDP (Browser.getWindowBounds windowState); None when it cannot be read."""
    try:
        wid = _browser_call("Browser.getWindowForTarget", {"targetId": target_id} if target_id else {}).get("windowId")
        if wid is None:
            return None
        return (_browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds") or {}).get("windowState")
    except Exception:
        return None


def calibrate_shot_origin(page: "CDPPage", target_id: str | None = None) -> dict:
    """The X11 viewport origin's correction to the model (device px); never raises. The model is the window metrics,
    or the screen when the page is fullscreen and its viewport covers the screen; there the match must say [0, 0]
    for the screen to be taken as the viewport (fullscreen_screen; t37 hot-fix: yt_44's fullscreen stayed in a
    windowed Chrome and the shots assumed the screen).
    fullscreen_geometry (t37, yt_47, supervisor): where the player is the screen there is no page outside the picture
    to match (compared_share 0), so the match runs on moving video and fails by construction. There the origin is
    [0, 0] by geometry when every check holds: the window's CDP state is fullscreen, the page is fullscreen with its
    viewport covering the screen, the <video> rect covers the screen (player_is_screen) and the X screen is the CDP
    screen's size in device px (the whole X root grabbed). The match is still run and reported (template_match), not
    required. When a check fails, the match decides as before (geometry records which)."""
    t0 = time.time()
    rec: dict = {"method": "cdp_vs_x11_match", "at": t0, "offset": None, "residual": None, "gap": None,
                 "accepted": False, "thresholds": {"max_residual": SHOT_CALIB_MAX_RESIDUAL, "min_gap": SHOT_CALIB_MIN_GAP,
                                                   "margin_px": SHOT_CALIB_MARGIN_PX, "scale": SHOT_CALIB_SCALE}}
    try:
        import base64
        import io
        import numpy as np
        from PIL import Image, ImageGrab
        pr = json.loads(page.eval(_PLAYER_RECT_JS) or "{}")
        dpr = float(pr.get("dpr") or 1)
        sc = pr.get("screen") or [0, 0]
        sw, sh = round(sc[0] * dpr), round(sc[1] * dpr)
        fs_claim = bool(pr.get("fullscreen")) and covers_screen(pr.get("viewport"), sc)
        root = None
        if fs_claim:
            rec["method"] = "cdp_vs_x11_match (fullscreen: the screen as the model)"
            model = (0, 0, sw, sh)
            if SHOT_SOURCE == "x11":
                root = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY") or ":0")   # the whole X screen
            pl = (pr.get("player") or {}).get("rect")
            geo = {"window_state": _window_state(target_id), "player_rect": pl, "screen": sc, "dpr": dpr,
                   "x_root_px": list(root.size) if root is not None else None,
                   "player_is_screen": player_is_screen({"player_rect": pl, "screen": sc})}
            geo["ok"] = bool(geo["window_state"] == "fullscreen" and geo["player_is_screen"] and geo["x_root_px"] == [sw, sh])
            rec["geometry"] = geo
        else:
            model = _viewport_on_screen(json.loads(page.eval(_VIEWPORT_JS) or "{}"))
        if SHOT_SOURCE != "x11" or not model:
            rec["why"] = "no x11 source or no window metrics"
            return rec
        M = SHOT_CALIB_MARGIN_PX
        vx, vy, vw, vh = model
        ax, ay = max(0, vx - M), max(0, vy - M)
        bx, by = vx + vw + M, vy + vh + M
        lim = X_SCREEN_PX or (sw, sh)                  # never grab beyond the screen (trio "fb": the X root)
        if lim[0] and lim[1]:
            bx, by = min(bx, lim[0]), min(by, lim[1])
        area = (root.crop((ax, ay, bx, by)) if root is not None
                else ImageGrab.grab(bbox=(ax, ay, bx, by), xdisplay=os.environ.get("DISPLAY") or ":0"))
        shot = page.call("Page.captureScreenshot", {"format": "png", "fromSurface": True, "captureBeyondViewport": False})
        rec["capture_s"] = round(time.time() - t0, 3)
        cdp = np.asarray(Image.open(io.BytesIO(base64.b64decode(shot.get("data") or ""))).convert("L"), float)
        a = np.asarray(area.convert("L"), float)
        # the template: the viewport less M on every side (so every offset within +-M keeps it on the grab)
        tpl = cdp[M:cdp.shape[0] - M, M:cdp.shape[1] - M]
        mask = np.ones(tpl.shape, bool)
        p = (pr.get("player") or {}).get("rect")
        if p:                                         # the picture changes between the captures: compare the page
            x0, y0 = int(p[0] * dpr) - M, int(p[1] * dpr) - M
            mask[max(0, y0):max(0, y0 + int(p[3] * dpr)), max(0, x0):max(0, x0 + int(p[2] * dpr))] = False
        rec["compared_share"] = round(float(mask.mean()), 3)
        y, x, res, gap = match_offset(tpl, a, mask)
        # the template's model position on the grab is (vy + M - ay, vx + M - ax)
        dx, dy = int(x - (vx + M - ax)), int(y - (vy + M - ay))
        rec.update(offset=[dx, dy], residual=res, gap=gap, accepted=calibration_verdict(res, gap),
                   model_viewport=list(model), cdp_px=[cdp.shape[1], cdp.shape[0]],
                   fullscreen_screen=fs_claim and calibration_verdict(res, gap) and [dx, dy] == [0, 0])
        if fs_claim and rec["geometry"]["ok"]:       # yt_47: the screen by geometry; the match reported, not required
            rec["template_match"] = {k: rec[k] for k in ("offset", "residual", "gap", "accepted", "compared_share")}
            rec.update(method="fullscreen_geometry", offset=[0, 0], residual=None, gap=None, accepted=True,
                       fullscreen_screen=True)
    except Exception as exc:
        rec["why"] = repr(exc)[:200]
    rec["elapsed_s"] = round(time.time() - t0, 3)
    return rec


def _fullscreen_on_screen(pr: dict | None) -> tuple[int, int, int, int] | None:
    """In fullscreen with the viewport covering the screen the player is the screen: the whole screen in device px.
    The shot loop takes it only where the calibration confirmed it (origin fullscreen_screen)."""
    try:
        if not (pr or {}).get("fullscreen") or not covers_screen(pr.get("viewport"), pr.get("screen")):
            return None
        dpr = float(pr.get("dpr") or 1)
        w, h = pr["screen"]
        return 0, 0, round(w * dpr), round(h * dpr)
    except (TypeError, KeyError, ValueError):
        return None


def _grab_x11(region: tuple[int, int, int, int]) -> bytes | None:
    """The region of the X display as a SHOT_WIDTH_PX JPEG; None when it cannot be grabbed or comes back blank."""
    try:
        import io
        from PIL import Image, ImageGrab, ImageStat
        x, y, w, h = region
        im = ImageGrab.grab(bbox=(x, y, x + w, y + h), xdisplay=os.environ.get("DISPLAY") or ":0").convert("RGB")
        if max(ImageStat.Stat(im.convert("L").resize((64, 36))).stddev) < 1.0:
            return None                               # blank (monitor off / not rendered): fall back to CDP
        if im.width > SHOT_WIDTH_PX:
            im = im.resize((SHOT_WIDTH_PX, round(im.height * SHOT_WIDTH_PX / im.width)), Image.BILINEAR)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=SHOT_JPEG_QUALITY)
        return out.getvalue()
    except Exception:
        return None


def _downscale_jpeg(raw: bytes) -> tuple[bytes, list | None, list | None]:
    """The captured surface, SHOT_WIDTH_PX wide at SHOT_JPEG_QUALITY (the shots' size before the change), with the
    capture's and the shot's pixel sizes; the raw capture when Pillow is missing."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        orig = [im.width, im.height]
        if im.width > SHOT_WIDTH_PX:
            im = im.resize((SHOT_WIDTH_PX, round(im.height * SHOT_WIDTH_PX / im.width)), Image.BILINEAR)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=SHOT_JPEG_QUALITY)
        return out.getvalue(), orig, [im.width, im.height]
    except Exception:
        return raw, None, None


# the player's rectangle in CSS px and the viewport, recorded with each shot (addendum O: motion is computed on the
# player crop, chat / side panes outside it); the viewport size and dpr come back even with no player (supervisor
# 2026-10-17: every shot carries the viewport at capture). One eval, so the shot loop adds no extra page call.
_RECT_JS_TEMPLATE = """
(function() {
    const vs = [...document.querySelectorAll('video')].filter(v => v.offsetParent !== null);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    const inner = [window.innerWidth, window.innerHeight], dpr = window.devicePixelRatio || 1;
    let player = null;
    if (v) { const r = v.getBoundingClientRect(); player = {rect: [r.x, r.y, r.width, r.height], inner, dpr}; }
    /*GRID*/
    return JSON.stringify({player, viewport: inner, dpr, fullscreen: !!document.fullscreenElement,
                           screen: [screen.width, screen.height]/*GRID_OUT*/});
})()
"""
_PLAYER_RECT_JS = _RECT_JS_TEMPLATE.replace("    /*GRID*/\n", "").replace("/*GRID_OUT*/", "")

# levi 88410f0b (#8', record-only; brad on WA3 tw_100003: twitch's "Stream Chat" panel is drawn over the right ~40 % of
# the <video> element, so player.rect is the element's box, not the visible picture's): each shot row carries an
# SHOT_GRID (cols x rows) document.elementFromPoint grid over player.rect, read in the SAME eval as player.rect (the same
# moment, the same chosen <video>: the largest visible one). Cell centres, row-major from the top-left. Per cell one code:
#   "v" the topmost element at the centre is the chosen <video> itself
#   "o" the centre lies outside the viewport (not on the video)
#   "n" elementFromPoint returned nothing (not on the video)
#   other letters (never v / o / n): a legend entry {"id": "tag#id.class1.class2" (truncated), "up": levels from the <video> up to the
#       lowest common ancestor (null if none), "bg": the element's own background is not transparent}
#   "*" the legend is full (more distinct top elements than letters)
# Note elementFromPoint skips pointer-events:none elements; a transparent full-player overlay that takes events (a
# click handler) is the top element over the whole picture: "up" / "bg" let a reader tell it from a side panel.
# occlusion = {grid, cells, legend} (null with no player; {"error"} if the grid threw: player.rect is unaffected).
# video_cell_share (levi 9048534c's on-video rule, named on the row as video_cell_rule): a cell is VIDEO when its top
# element is the chosen <video>, or lies in the player subtree with up <= SHOT_VIDEO_CELL_MAX_UP and bg false (a
# transparent in-player layer: a click overlay); opaque (bg true or unreadable), outside the subtree (up > max or null)
# and o / n cells are not video. Share = VIDEO cells / (cols x rows); the raw cells / legend are kept as recorded.
SHOT_GRID = (16, 9)
SHOT_VIDEO_CELL_MAX_UP = 3
SHOT_VIDEO_CELL_RULE = "levi 9048534c: video, or in-player up<=3 and bg false"
_SHOT_GRID_JS = """
    let occlusion = null;
    if (v) {
        try {
            const r = v.getBoundingClientRect(), C = %d, R = %d, MAXUP = %d, letters = 'abcdefghijklmpqrstuwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
            const anc = new Map();
            for (let e = v, k = 0; e; e = e.parentElement, k++) anc.set(e, k);
            const ident = el => {
                let s = String(el.tagName || '?').toLowerCase();
                if (el.id) s += '#' + String(el.id).slice(0, 24);
                const cl = typeof el.className === 'string' ? el.className.trim().split(/\\s+/).filter(Boolean) : [];
                for (const c of cl.slice(0, 2)) s += '.' + c.slice(0, 24);
                return s.slice(0, 64);
            };
            const up = el => { for (let e = el; e; e = e.parentElement) if (anc.has(e)) return anc.get(e); return null; };
            const bg = el => {
                try {
                    const cs = getComputedStyle(el), c = cs.backgroundColor || '';
                    const m = c.match(/rgba?\\(([^)]*)\\)/), parts = m ? m[1].split(',') : [];
                    const alpha = parts.length === 4 ? parseFloat(parts[3]) : (c === 'transparent' ? 0 : 1);
                    return alpha > 0 || (cs.backgroundImage || 'none') !== 'none';
                } catch (e) { return null; }
            };
            const codes = new Map(), legend = {};
            let cells = '', on = 0;
            for (let j = 0; j < R; j++) for (let i = 0; i < C; i++) {
                const x = r.x + (i + 0.5) * r.width / C, y = r.y + (j + 0.5) * r.height / R;
                if (!(x >= 0 && y >= 0 && x < inner[0] && y < inner[1])) { cells += 'o'; continue; }
                const el = document.elementFromPoint(x, y);
                if (!el) { cells += 'n'; continue; }
                if (el === v) { cells += 'v'; on++; continue; }
                if (!codes.has(el)) {
                    const code = codes.size < letters.length ? letters[codes.size] : '*', u = up(el), b = bg(el);
                    codes.set(el, {code, video: u !== null && u <= MAXUP && b === false});   // levi 9048534c
                    if (code !== '*') legend[code] = {id: ident(el), up: u, bg: b};
                    else legend['*'] = {id: 'legend full', up: null, bg: null};
                }
                const c = codes.get(el);
                cells += c.code;
                if (c.video) on++;
            }
            occlusion = {grid: [C, R], cells, legend, share: Math.round(on / (C * R) * 1e4) / 1e4};
        } catch (e) { occlusion = {error: String(e).slice(0, 160)}; }
    }
""" % (*SHOT_GRID, SHOT_VIDEO_CELL_MAX_UP)
# the shot loop's one eval: _PLAYER_RECT_JS plus the grid (the calibrations keep the plain _PLAYER_RECT_JS)
_SHOT_RECT_JS = _RECT_JS_TEMPLATE.replace("/*GRID*/", _SHOT_GRID_JS.strip()).replace("/*GRID_OUT*/", ", occlusion")


# t53 fullscreen in the trio (supervisor decision (a); adopted only at a count boundary, probe v4 first): a composite-
# layout slot whose geometry is fullscreen runs single mode's fullscreen path in its own process (x11 root grabs,
# fullscreen_geometry origin), plus a focus keeper (mutter keeps a fullscreen window above others only while it has the
# focus; a sibling's new window can take it) and a per-shot on-top gate (a root grab is this window's pixels only while
# it is the top-level window at the screen's centre; otherwise the CDP fallback, recorded).
FS_KEEPER_S = 1.0


class FullscreenKeeper:
    """Every FS_KEEPER_S: the top-level window at the screen's centre (xcomposite.window_at); not ours: _NET_ACTIVE_WINDOW
    and raise (xcomposite.activate). record(): checks, how many found another window on top, the raises sent (counted
    in full) and each raise (the event list bounded)."""

    def __init__(self, client: str, frame: str, centre: tuple[int, int]):
        self.client, self.frame, self.centre = client, frame, centre
        self.checks = self.not_on_top = self.raises = 0
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def on_top(self) -> tuple[bool, str | None]:
        import xcomposite
        top = xcomposite.window_at(*self.centre)
        return top in (self.frame, self.client), top

    def _run(self) -> None:
        import xcomposite
        while not self._stop.wait(FS_KEEPER_S):
            try:
                ok, top = self.on_top()
                self.checks += 1
                if not ok:
                    self.not_on_top += 1
                    sent = xcomposite.activate(self.client)
                    self.raises += bool(sent)
                    if len(self.events) < 200:
                        self.events.append({"t": round(time.time(), 3), "top_was": top, "activate_sent": sent})
            except Exception as exc:
                if len(self.events) < 200:
                    self.events.append({"t": round(time.time(), 3), "error": repr(exc)[:120]})

    def start(self) -> "FullscreenKeeper":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def record(self) -> dict:
        return {"client": self.client, "frame": self.frame, "centre": list(self.centre), "period_s": FS_KEEPER_S,
                "checks": self.checks, "not_on_top": self.not_on_top, "raises": self.raises, "events": self.events}


FS_KEEP: dict = {}              # this process's keeper (set by BrowserSession.start in the fullscreen-in-trio case)
TRIO_FULLSCREEN_OK = os.environ.get("LVDC_TRIO_YT_FULLSCREEN") == "1"   # orchestrate --trio-yt-fullscreen (default off)


def _start_fullscreen_keeper(page: "CDPPage") -> dict:
    """The keeper for this session's fullscreen window (the browser pid's largest viewable window; the screen's centre
    from the page in device px); its start record. Never raises."""
    try:
        import session_meta
        import xcomposite
        pid = session_meta.chrome_cmdline(CDP_BASE).get("pid")
        wins = xcomposite.windows_of_pid(pid) if pid else []
        if not wins:
            return {"started": False, "why": "no window for the browser pid", "pid": pid}
        pr = json.loads(page.eval(_PLAYER_RECT_JS) or "{}")
        dpr = float(pr.get("dpr") or 1)
        sw, sh = (pr.get("screen") or [1920, 1080])
        k = FullscreenKeeper(wins[0]["client"], wins[0]["frame"], (round(sw * dpr / 2), round(sh * dpr / 2))).start()
        FS_KEEP["keeper"] = k
        return {"started": True, "pid": pid, "client": k.client, "frame": k.frame, "centre": list(k.centre)}
    except Exception as exc:
        return {"started": False, "why": repr(exc)[:200]}


def _screenshot_loop(page: CDPPage, shots_dir: Path, stop: threading.Event, origin: dict | None = None,
                     pending=None) -> None:
    """A small JPEG of the tab every SHOT_INTERVAL_S ('cadence'), plus one at every tier change, ad start, STALL onset and
    playback start (t37 / v3: content.js raises <html data-lvqoe-shot-req="<n>|<reason>|<event_id>">, polled each second)
    and at each error-page onset (`pending`: BrowserSession's Python-side triggers). Each shot is
    stamped with the extension's latest tick (<html data-lvqoe-tick>) and its reason, for the human label-vs-screen
    check. Nothing here feeds labels. The loop starts after navigation: a pre-roll that ends during navigation has no
    event shot (laptop run8, yt), and two events within one poll second give one shot (the later reason)."""
    import base64
    shots_dir.mkdir(parents=True, exist_ok=True)
    index = shots_dir / "index.jsonl"
    last_cadence, last_req = time.time(), None
    off = (origin or {}).get("offset") if (origin or {}).get("accepted") else None
    comp_prev: dict = {}                           # t53 composite layout: the previous grab, for the staleness record
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
            if reason is None and pending is not None:   # v3 (levi 16:57): Python-side triggers (the error page onset)
                try:
                    reason = pending()
                except Exception:
                    reason = None
            if reason is None and time.time() - last_cadence >= SHOT_INTERVAL_S:
                reason = "cadence"
            if reason is None:
                continue
            if reason == "cadence":
                last_cadence = time.time()
            # no clip / scale: a scaled clip makes Chrome emulate device metrics for the capture, a visible re-render
            # (rushi saw the screen flicker; dropped frames per tick near shots were 0.153 vs 0.023 elsewhere on yt_10).
            # The surface is grabbed as is and downscaled here.
            t_decide = time.time()
            try:
                pr_ = json.loads(page.eval(_SHOT_RECT_JS) or "null") or {}
            except Exception:
                pr_ = {}
            player, viewport = pr_.get("player"), {"inner": pr_.get("viewport"), "dpr": pr_.get("dpr")}
            occ = pr_.get("occlusion")                 # #8' (levi 88410f0b): the grid, same eval as player.rect
            occ_share = occ.pop("share", None) if isinstance(occ, dict) else None
            data, capture, capture_px, shot_px, region, crop = None, None, None, None, None, "viewport"
            t_js = t_grab = None
            comp = None
            if SHOT_SOURCE == "composite":            # t53 trio layout "composite": the window's own pixmap
                comp = {"uncalibrated": True}
                if (origin or {}).get("accepted"):
                    try:
                        data, capture_px, region, crop, comp = _composite_shot(origin, player, page, comp_prev)
                        t_js = t_grab = time.time()
                        if data:
                            capture = "composite"
                            from PIL import Image
                            import io
                            shot_px = list(Image.open(io.BytesIO(data)).size)
                    except Exception as exc:
                        comp = {"error": repr(exc)[:160]}
                        data = None
                if data is None:                       # the CDP fallback below takes the viewport
                    region, crop, capture_px = None, "viewport", None
                else:
                    comp["region_in"] = "frame"        # screen_region is the crop's place in the window's frame
            on_top = None
            keeper = FS_KEEP.get("keeper")
            if SHOT_SOURCE == "x11" and keeper is not None:   # t53 fullscreen in trio: root pixels only while on top
                try:
                    on_top, _top = keeper.on_top()
                except Exception:
                    on_top = False
            if SHOT_SOURCE == "x11" and on_top is not False:
                fs = _fullscreen_on_screen(pr_) if (origin or {}).get("fullscreen_screen") else None   # t37
                try:
                    region = fs or _viewport_on_screen(json.loads(page.eval(_VIEWPORT_JS) or "{}"))
                except Exception:
                    region = None
                if region and not fs and off:          # the calibrated origin (t37): X11 regions only
                    region = (region[0] + off[0], region[1] + off[1], region[2], region[3])
                t_js = time.time()
                # the player rectangle only (supervisor 2026-10-17: the smallest XGetImage), else the viewport
                pr = (_player_on_screen(region, player) if region and SHOT_CROP == "player"
                      else None)                     # the player within it (hot-fix 2); LVQOE_SHOT_CROP=viewport: none
                if fs:                                 # the screen is the player: crop "player", as readers expect
                    crop = "player"
                elif pr:
                    region, crop = pr, "player"
                data = _grab_x11(region) if region else None
                t_grab = time.time()
                if data:
                    capture, capture_px = "x11", [region[2], region[3]]
                    try:
                        from PIL import Image
                        import io
                        shot_px = list(Image.open(io.BytesIO(data)).size)
                    except Exception:
                        shot_px = None
            if data is None:
                res = page.call("Page.captureScreenshot", {"format": "jpeg", "quality": 90, "fromSurface": True,
                                                           "captureBeyondViewport": False})
                raw = res.get("data")
                if not raw:
                    continue
                data, capture_px, shot_px = _downscale_jpeg(base64.b64decode(raw))
                capture, crop = ("cdp" if SHOT_SOURCE == "cdp" else "cdp_fallback"), "viewport"
                t_grab = time.time()
            ts = time.time()
            tag = "" if reason == "cadence" else f"_{reason}"
            name = f"{int(ts * 1000)}_tick{tick_id if tick_id is not None else 'na'}{tag}.jpg"
            (shots_dir / name).write_bytes(data)
            import hashlib                             # levi 22:56: the image's own hash (a static source: QA only)
            with open(index, "a") as fh:
                fh.write(json.dumps({"ts": ts, "file": name, "tick": tick_id, "tick_ts": tick_ts, "reason": reason,
                                     "sha256": hashlib.sha256(data).hexdigest(),
                                     "request": req if reason != "cadence" else None, "player": player,
                                     "viewport": viewport, "occlusion": occ, "video_cell_share": occ_share,
                                     "video_cell_rule": SHOT_VIDEO_CELL_RULE,
                                     "capture_px": capture_px, "shot_px": shot_px, "capture": capture,
                                     "screen_region": region, "crop": crop, "fullscreen": bool((pr_ or {}).get("fullscreen")),
                                     "origin_offset": off if capture == "x11" and not (pr_ or {}).get("fullscreen") else None,
                                     "shot_origin_uncalibrated": capture == "x11" and not (pr_ or {}).get("fullscreen")
                                                                 and off is None,
                                     # per-step timing (tw_10: the near-shot dropped frames sit on the tick about
                                     # 1 s before ts; these say which step that is)
                                     "t_decide": t_decide, "js_s": None if t_js is None else round(t_js - t_decide, 4),
                                     "grab_s": None if t_grab is None else round(t_grab - (t_js or t_decide), 4),
                                     "encode_save_s": round(ts - (t_grab or t_decide), 4),
                                     **({"composite": comp} if comp is not None else {}),
                                     **({"on_top": on_top} if keeper is not None else {})}) + "\n")
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
                 first_url: str | None = None, geometry: str | None = None, room_rotation_s: float | None = None,
                 entry_quality: dict | None = None, tw_low_latency: str | None = None):
        self.platform = platform
        # B9 (levi 23:21): set by exp.py's join before start(): the first shaped state and the waits' scale (1 = unshaped,
        # the pre-B9 waits; join_state None never raises JoinTimeout)
        self.join_scale: float = 1.0
        self.join_state = None
        self.join_rx = None                             # B9 D3: the slot's ingress byte counter (callable), when known
        self.join_cancel = threading.Event()            # B9 D2: set when the schedule ended with the join still running
        self._prepared = False                          # levi 07:48: discover() ran the setup (unshaped) before the join
        self.discovery: dict = {}                       # meta.join.discovery: its kind, times, pool / pick
        self._tt_discovered = None                      # (tt_room_choice, try order) from the unshaped discovery
        self._tw_discovered = None                      # the twitch directory pick from the unshaped discovery
        # levi 16:27: yt's ladder, record-only, one recorder for the session (re-entries included)
        self.yt_ladder: "YtLadderRecorder | None" = YtLadderRecorder() if platform == "youtube" else None
        # levi 18:36 (A): tt's codec / renditions, record-only, one recorder for the session
        self.tt_codec: "TtCodecRecorder | None" = TtCodecRecorder() if platform == "tiktok" else None
        self.tt_onboarding: list[dict] = []          # batch #10 (levi 03:29): the feed-navigation guide's dismiss actions
        # restart §8: twitch's drawn low-latency setting ("on" / "off"; None = the profile's own, untouched), set before
        # the first navigation and read back once (tw_latency.py); its record goes to meta.json tw_low_latency
        self.tw_low_latency = tw_low_latency if platform == "twitch" else None
        self.tw_latency: dict | None = None
        # Q am. 1 / am. 10: the entry-quality pick (entry_quality.py), only with exp.py --entry-quality on yt / twitch;
        # the config holds the coverage snapshot the entry tier is chosen from; the result goes to meta.json
        import entry_quality as EQ
        self.entry_quality_cfg = entry_quality if platform in EQ.ENTRY_QUALITY_PLATFORMS else None
        self.entry_quality: dict | None = None
        # addendum M: login state (meta.json) and the tt room-rotation fallback (None = off; seconds between rooms)
        self.login_state: dict | None = None
        self.room_rotation_s = room_rotation_s if platform == "tiktok" else None
        self.room_rotation: dict = {"active": bool(self.room_rotation_s), "interval_s": self.room_rotation_s, "rooms": []}
        self._rotator: threading.Thread | None = None
        # t37: the drawn geometry ("player:WxH" / "window:WxH", geometry.py rotation); None = the platform's pin
        self.geometry_target = geometry or (f"player:{PLAYER_PIN[platform][0]}x{PLAYER_PIN[platform][1]}"
                                            if platform in PLAYER_PIN else None)
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
        self.caption_action: str | None = None   # yt: what the captions-off step did at start (meta.json)
        self.geometry: dict | None = None        # window / player geometry after navigation (meta.json)
        self.player_pin: dict | None = None      # pin_player trace (meta.json, check.md)
        self.abort_reason: str | None = None     # set by a watcher (t37: "login_wall"); exp.py stops the session
        self.video_stats: str | None = None      # twitch Video Stats overlay result (meta.json)
        self.abort_at: float | None = None
        self.tt_room_choice: dict | None = None   # tt: candidates, tries and the chosen room (meta.json)
        self.pin_check: dict | None = None        # the landed room / channel vs the given first_url (meta.json; WA2 r1)
        self.channel_pick: dict | None = None     # twitch / tt: the channel cap's state at the pick (meta.json channel_pick)
        self.room_ended: dict | None = None       # tt: the dead-room abort's detail (meta.json)
        self.stream_ended: dict | None = None     # yt / twitch: the stream_ended abort's detail (meta.json)
        self.stream_watch: "StreamEndedWatch | None" = None
        self.drain: dict | None = None             # v3 A12: meta.json drain (CaptureDrain.run, inside stop())
        self.lowstate_hold_available: bool | None = None   # yt / twitch: False when a hold check found no net labels
        self.player_dead_watch: dict = {"low_spans": [], "dead_clocks": []}   # tt: TTRoomWatch's own view (record)
        self.player_dead: dict | None = None      # tt: the dead-player abort's detail (meta.json)
        self.player_dead_reentries: list[dict] = []   # tt: the re-entries after a player_dead (meta.json)
        self.frozen_reentries: list[dict] = []    # the stuck-player re-entry (Addendum Q am. 6; meta.json)
        self.error_watch = PlayerErrorWatch()       # t56 tw_100016: yt / twitch error page or ad with no player
        self.error_watch.held_fn = self._ad_frozen_held     # levi 00176: lifted while paused with data, ad or not
        self._shot_reasons: list[str] = []          # v3 (levi 16:57): Python-side shot triggers, consumed by the shooter
        self.shot_triggers_py: dict[str, int] = {"error_page": 0}   # meta.json shot_triggers_py (brad / levi 18:00)
        # levi 00:53: the same event-shot gap on the Python side; suppressed onsets counted (meta shot_triggers_py_suppressed)
        self.shot_triggers_py_suppressed: dict[str, int] = {"error_page": 0}
        self._shot_last_py: dict[str, float] = {}
        self.error_watch.on_onset = self._on_error_onset
        self.shot_crop = {"crop": SHOT_CROP, "env": SHOT_CROP_RAW}   # meta.json shot_crop (levi 17:45)
        self.resumer = SelfPauseResumer()           # the self-pause press (levi 00130; meta.json self_pause_resume)
        self.frozen_hold_bypassed: list = []         # yt / twitch: a frozen clock's low-state hold skipped, no starvation
        # v3 (t67; levi 18:33): content.js's whole v3 instrument set (A1 / A2 / A3 / A7), on unless LVQOE_INSTRUMENTS=0
        # (the gate's instrumentation A/B off arm); meta.json instruments
        self.instruments = {"enabled": os.environ.get("LVQOE_INSTRUMENTS", "1") != "0", "config_set": False}
        self.frame_log = frame_log_config(os.environ.get(FRAME_LOG_ENV), session_dir,      # t91: meta.json frame_log
                                          os.environ.get("LVDC_TRIO_RECORD") == "1", self.instruments["enabled"])
        self.screen: dict | None = None             # v3 A8: meta.json screen
        self.clock_offsets: list = []               # v3 A11: meta.json clock_offsets (time.time vs the page clock)
        self.player_errors: list[dict] = []         # its triggers and their outcome (meta.json)
        self.censored: dict | None = None         # the stuck-player censoring (am. 6): time, reason, shaper state
        self.lowest_rung: dict | None = None      # the stuck-player rule's rung (kbit, source; meta.json)
        self.decode_gpu: dict | None = None       # Chrome's GPU video-decode report at launch (decode_path.py; meta.json)
        self.decode_page: dict | None = None      # the page's HEVC probe (MSE / WebCodecs hvc1)
        self._master: MasterListener | None = None   # twitch: the master playlist listener
        self._tt_room: TtRoomListener | None = None  # tiktok: the room/enter listener (meta.json tt_room_enter)
        self._anchor: WorkerAnchor | None = None     # t100 R1: tw / tt worker MSE anchor (frame log enabled only)
        self.t2_skipped: list[dict] = []            # stuck rule: T2_S frozen with only the fallback rung (no T2)
        self.player_ladder: dict | None = None      # twitch: the quality menu's options when the master was not read
        self.shot_origin: dict | None = None        # the X11 shot origin calibration (t37; calibrate_shot_origin)
        self.fs_keeper: dict | None = None          # t53 (a): fullscreen in trio, the keeper's start record
        self.rung_probes: list[dict] = []           # twitch: each menu read (open / close times): shots near it are flagged
        self._media: MediaListener | None = None     # the media-internals recorder (media_events.jsonl), every platform
        self.media_decoders: dict | None = None      # its summary() at stop (meta.json media_decoders)
        self._rung_probe_at = 0.0
        self._recovery_lock = threading.Lock()    # one recovery of either kind per session (am. 6)
        self.tw_pick: dict | None = None          # twitch: directory candidates, recent channels excluded, the pick
        self.room_play_clicks = 0                 # tt: play clicks on a paused, flat player (room watcher)
        self.room_discontinuities: list = []      # tt: player source drops that came back (room watcher)
        self.net_labels: list = []                # exp.py's shaper episodes (the current state for the room watcher)
        self._room_grace_until = 0.0              # tt: the room watcher pauses while the rotation moves rooms
        self._room_watcher: threading.Thread | None = None

    def start(self, cdp_ready_timeout: float = 30.0) -> None:
        """The stream join (B9: on the shaped link). The setup runs here only when discover() did not run it first
        (callers without the unshaped discovery: manual / tests); exp.py's auto mode calls discover() before the shaper."""
        if not self._prepared:
            self._prepare(cdp_ready_timeout)
            self._reset_for_session()
        try:
            self._join_nav()
        finally:
            self._mse_wrap_settle_once("join_exit")       # levi 00:06 (b): an exception or a timeout still disables Page

    def discover(self, cdp_ready_timeout: float = 30.0) -> dict:
        """levi 07:48 (supervisor 07:48; WC3b r1 ti_100001: the /live list loaded on the shaped link at 1.3-1.6 Mbit, 0
        candidates): room / channel DISCOVERY runs UNSHAPED, before the shaper's first apply, so which streams are offered
        never depends on the state. The setup, then the platform's list page in a separate tab (tt /live; a twitch
        directory first_url), CLOSED before this returns (i); the stream is opened afterwards by its direct URL on the
        session page, never by a tile (ii). yt / a twitch channel / a tt room pin: a direct URL, nothing to discover.
        The extension storage is cleared after the tab closed (its previews may tick). Raises when nothing is found: a
        startup failure before any shaping. Recorded in self.discovery (meta.join.discovery), outside join_s."""
        t0 = time.time()
        if not self._prepared:
            self._prepare(cdp_ready_timeout)
        rec: dict = {"t_start": round(t0, 3)}
        self.discovery = rec
        try:
            if self.platform == "tiktok" and not self.first_url:
                rec["kind"] = "tt /live"
                self._tt_discovered = self._tt_discover_rooms()
                rec["pool_size"] = len(self._tt_discovered[1])
            elif self.platform == "twitch" and self.first_url and "/directory/" in self.first_url:
                rec["kind"] = "twitch directory"
                self._tw_discovered = self._tw_discover_channel(self.first_url)
                rec["pick"] = self._tw_discovered.get("pick")
            else:
                rec["kind"] = "none (a direct stream URL)"
            if rec.get("tab_closed") is False:      # brad / levi 08:18 (i): never shape with the list page still open
                raise RuntimeError(f"Failed to navigate to a live {self.platform} stream (discovery: its tab did not "
                                   f"close within {DISCOVERY_CLOSE_WAIT_S:g} s)")
        finally:
            rec.update(t_end=round(time.time(), 3), s=round(time.time() - t0, 3))
        self._reset_for_session()
        return rec

    def _prepare(self, cdp_ready_timeout: float = 30.0) -> None:
        self._prepared = True
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
        # Only ordinary tabs are candidates: the extension's own page (popup.html, opened and closed by
        # ensure_extension_loaded) and devtools pages can vanish between listing and connecting ("No such target id",
        # laptop runs 9 and 11), so they are never reused, and the connect is retried on a fresh listing.
        def _ordinary(t: dict) -> bool:
            u = t.get("url", "")
            return t.get("type") == "page" and not u.startswith(("chrome-extension://", "devtools://"))
        page = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
                    all_tabs = json.loads(r.read())
                page_tabs = [t for t in all_tabs if _ordinary(t)]
                if attempt == 0:
                    for tab in page_tabs[1:]:     # keep the first ordinary tab; close the rest (session-restored)
                        try:
                            urllib.request.urlopen(urllib.request.Request(f"{CDP_BASE}/json/close/{tab['id']}",
                                                                          method="GET"), timeout=3)
                        except Exception:
                            pass
                    time.sleep(1)
                    page_tabs = page_tabs[:1]
            except Exception:
                page_tabs = []
            ws_url = page_tabs[0].get("webSocketDebuggerUrl") if page_tabs else None
            if not ws_url:
                ws_url = _open_new_tab() or _get_page_ws_url()
            if not ws_url:
                time.sleep(1)
                continue
            try:
                page = CDPPage(ws_url)
                break
            except Exception as exc:
                print(f"[browser] CDP page connect failed (attempt {attempt + 1}): {exc!r}", flush=True)
                time.sleep(1)
        if page is None:
            raise RuntimeError("Could not get a CDP page target")

        print(f"[browser] Connected to CDP: {ws_url[:60]}...", flush=True)
        try:   # the decode path as Chrome reports it, once at launch (decode_path.py; a covariate)
            import decode_path
            self.decode_gpu = decode_path.gpu_summary(_browser_call("SystemInfo.getInfo"))
        except Exception as exc:
            self.decode_gpu = {"error": repr(exc)[:200]}
        print(f"[browser] Decode path: {self.decode_gpu}", flush=True)
        self._ws_url = ws_url
        self._page = page
        # A hidden / occluded page does not start yt playback and throttles the extension's 1 Hz timer (laptop test
        # 2026-10-16: visibilityState hidden, player unstarted until brought to front). Keep the tab visible and focused.
        self._page.call("Page.bringToFront")
        self._page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        try:                                     # v3 A8 (t67): what the page sees of the screen, once at start
            self.screen = json.loads(self._page.eval(_A8_SCREEN_JS, await_promise=True) or "null")
        except Exception as exc:
            self.screen = {"error": repr(exc)[:200]}
        self._clock_sample("start")                # v3 A11
        self._start_time = time.time()

        # Direct extension CSV downloads into the session directory
        download_dir = self._session_dir or (Path(__file__).parent.parent / "current_data")
        download_dir.mkdir(parents=True, exist_ok=True)
        _set_download_dir(self._page, str(download_dir))
        print(f"[browser] Downloads → {download_dir}", flush=True)

    def _reset_for_session(self) -> None:
        """After the setup and any discovery (its tab closed): the extension storage cleared (a crashed session's
        leftovers, and the discovery tab's preview ticks), this session's extension config set, the page's listeners
        started. Then only the session page navigates."""
        if self.discovery.get("kind", "").startswith(("tt", "twitch")):
            time.sleep(DISCOVERY_SETTLE_S)        # the content script's last batch from the closed tab lands first
            try:                                  # brad 08:37 (a): the discovery context had its own window; the
                self._page.call("Page.bringToFront")            # session page is visible and focused again (a
                self._page.call("Emulation.setFocusEmulationEnabled", {"enabled": True})   # hidden page stalls yt /
                self.discovery["refocused"] = True             # throttles the extension's timer, as at _prepare)
            except Exception as exc:
                self.discovery["refocused"] = f"error: {exc!r}"[:200]
            vis, deadline = None, time.time() + DISCOVERY_VISIBLE_WAIT_S   # brad / levi 08:38: VERIFIED, the real
            while True:                           # visibilityState (emulated focus makes hasFocus() true regardless)
                try:
                    vis = self._page.eval("document.visibilityState")
                except Exception as exc:
                    vis = f"error: {exc!r}"[:120]
                if vis == "visible" or time.time() >= deadline:
                    break
                time.sleep(0.25)
            self.discovery["visibility"] = vis
            if vis != "visible":                  # fail closed before any shaping, as tab_closed
                raise RuntimeError(f"Failed to navigate to a live {self.platform} stream (discovery: the session page "
                                   f"is {vis!r} after the discovery window closed, not visible)")
        # Clear any stale events from a previous killed/crashed session
        try:
            sw, tmp_tab = _ext_page()
            if sw is not None:
                sw.eval("new Promise(r => chrome.storage.local.remove(['events', 'player_stats', 'side_counters'], r))",
                        await_promise=True)
                # v3: stale side-stream batches out; this session's extension config in (A1: rVFC on / off)
                sw.eval("new Promise(r => chrome.storage.local.get(null, d => chrome.storage.local.remove("
                        "Object.keys(d).filter(k => k.startsWith('side:')), r)))", await_promise=True)
                sw.eval("new Promise(r => chrome.storage.local.set({lvqoe_config: %s}, r))"
                        % json.dumps({"instruments": self.instruments["enabled"],
                                      **({"frame_log": {k: self.frame_log[k] for k in ("block_s", "t0")}}
                                         if self.frame_log and self.frame_log["enabled"] else {})}), await_promise=True)
                self.instruments["config_set"] = True
                sw.close()
                _close_target(tmp_tab)
                print(f"[browser] Extension storage cleared; config instruments={self.instruments['enabled']}", flush=True)
        except Exception as e:
            print(f"[browser] Could not pre-clear extension storage: {e}", flush=True)

        # every platform (decision A, levi 00271 item 3): the decoder per session; tt also attaches snapshot() to player_dead
        self._media = MediaListener(self._ws_url, (Path(self._session_dir) / MEDIA_EVENTS_FILE)
                                    if self._session_dir else None).start()
        if self.platform == "tiktok":            # item #9: before any room navigation, so the first room/enter is read
            self._tt_room = TtRoomListener(self._ws_url).start()
        self._mse_wrap_install()                 # t93 rows 9 / 20, before the join (yt, frame log enabled only)
        self._worker_anchor_install()            # t100 R1, before the join (tw / tt, frame log enabled only)

    def _mse_wrap_install(self) -> None:
        """t93 rows 9 / 20: Page enabled and MSE_WRAP_JS registered on the session page, only when the frame log is
        enabled on yt; otherwise no CDP call at all (levi 00:06 (a): unset / 0 send what 79d4f7d8 sent)."""
        if not (self.frame_log and self.frame_log.get("enabled") and self.platform == "youtube"):
            return
        self.frame_log["mse_wrap"] = {"installed": False}    # from here on, every exit settles (Page.disable)
        try:
            self._page.call("Page.enable")
            r = self._page.call("Page.addScriptToEvaluateOnNewDocument", {
                "source": mse_wrap_js(self.frame_log["block_s"])})
            self.frame_log["mse_wrap"].update({"installed": bool(r.get("identifier")), "id": r.get("identifier"),
                                               "installed_at": round(time.time(), 3), "webm": int(MSE_WRAP_WEBM),
                                               "ykey": int(MSE_WRAP_YT_KEY), "key_every": MSE_WRAP_KEY_EVERY})
        except Exception as exc:
            self.frame_log["mse_wrap"]["error"] = repr(exc)[:200]

    def _worker_anchor_install(self) -> None:
        """t100 R1: WorkerAnchor on the session page, armed before the join navigation, only when the frame log is
        enabled on tw / tt; otherwise nothing is attached (FRAME_LOG unset: what 971e444e sends). Never an abort: a
        failure is recorded in meta frame_log.worker_anchor."""
        if not (self.frame_log and self.frame_log.get("enabled") and self.platform in WORKER_ANCHOR_PLATFORMS):
            return
        t0 = time.time()
        try:
            self._anchor = WorkerAnchor(self._ws_url, self.frame_log["block_s"]).start()
            self.frame_log["worker_anchor"] = {"installed_at": round(t0, 3), "armed_s": round(time.time() - t0, 3),
                                               "armed": self._anchor.armed.is_set(), "armed_error": self._anchor.armed_error}
        except Exception as exc:
            self.frame_log["worker_anchor"] = {"installed_at": round(t0, 3), "error": repr(exc)[:200]}

    def _worker_anchor_stop(self) -> None:
        """The anchor's last drain, posted before the final flush; its record into meta frame_log.worker_anchor."""
        if self._anchor is None:
            return
        a, self._anchor = self._anchor, None
        try:
            a.stop()
            self.frame_log["worker_anchor"] = {**(self.frame_log.get("worker_anchor") or {}), **a.record()}
        except Exception as exc:
            (self.frame_log.setdefault("worker_anchor", {}))["stop_error"] = repr(exc)[:200]

    def _mse_wrap_settle_once(self, where: str) -> None:
        """levi 00:06 (b): Page is disabled on every exit of the join (its end, an exception, a timeout: start()'s
        finally) and, if the join never ran (a failure between discover() and start()), at stop(); once. settled = where
        it ran; a failed Page.disable is recorded (disable_error), never silent. No wrap: no call."""
        rec = self.frame_log.get("mse_wrap") if self.frame_log else None
        if rec is None or "settled" in rec:
            return
        rec.update(mse_wrap_settle(self._page, rec))
        rec["settled"] = where

    def _join_nav(self) -> None:
        """The stream join on the session page: a discovered tt room list tried by direct URL (else the old shaped
        /live pick), a twitch channel (the discovered one by direct URL), a yt channel; then the post-entry records."""
        if self.platform == "tiktok" and not self.first_url:
            self.list_url = (self._tt_try_rooms(*self._tt_discovered) if self._tt_discovered is not None
                             else self._tt_choose_room())
        elif self.platform == "twitch":
            if self.tw_low_latency:              # restart §8: before any twitch document, so the player starts in it
                try:                             # never an abort: a failed set is recorded and the session goes on
                    import tw_latency
                    self.tw_latency = tw_latency.apply(self._page, self.tw_low_latency)
                except Exception as exc:
                    self.tw_latency = {"drawn": self.tw_low_latency, "applied": False, "error": repr(exc)[:200]}
                print(f"[browser] Twitch low latency {self.tw_low_latency}: set before playback {self.tw_latency}",
                      flush=True)
            self._master = MasterListener(self._ws_url).start()
            sib = sibling_streams(self._session_dir, "twitch")
            import not_usable                    # trio rotation hygiene: 2 trio startup_failures set a channel aside
            troot = not_usable.trio_root_of(self._session_dir)
            na = not_usable.set_aside(troot, "twitch") if troot is not None else {}
            import channel_cap, record           # the next boundary's cap (levi): a channel at the cap is passed over
            cap = channel_cap.cap_state(troot, "twitch", record.counted_any_era)
            self.channel_pick = cap
            self.tw_pick = {**({"sibling_excluded": sib} if sib else {}), **({"not_usable_excluded": na} if na else {}),
                            **({"cap_excluded": cap["capped"]} if cap and cap["capped"] else {})}
            first = self.first_url
            if self._tw_discovered is not None:  # levi 07:48: the channel discovered unshaped, opened by its URL (ii)
                self.tw_pick.update(self._tw_discovered)
                first = self._tw_discovered.get("pick")
                if not first or _norm_room(first) in set(sib):      # brad / levi 08:18 (2): never the directory walk
                    raise RuntimeError(f"Failed to navigate to a live twitch stream (the discovered channel {first} is "
                                       f"held by a sibling at the join, or none; no directory walk on the shaped link)")
            if first and "/directory/" not in first and _norm_room(first) in set(sib):
                # t58 L-7: never the channel a sibling plays; levi 11:54 (b) / (c): and never a walk to another category
                # either (its meta named this channel and category): a startup failure, pin_mismatch on a pinned session
                self.tw_pick["first_url_held_by_sibling"] = first
                kind = "pin_mismatch: " if self._pinned() else ""
                raise RuntimeError(f"Failed to navigate to a live twitch stream ({kind}sibling_held: {first} is held by a "
                                   f"sibling slot; no walk to another category)")
            nav_first = first
            self.list_url = _navigate_to_live(self._page, self.platform, first_url=first,
                                              exclude=sorted(set(recent_tw_channels(self._session_dir)) | set(sib) | set(na)
                                                             | set((cap or {}).get("capped") or [])),
                                              record=self.tw_pick,
                                              claim=lambda u: publish_stream(self._session_dir, "twitch", u, "intent"),
                                              scale=self.join_scale, join_state=self.join_state,
                                              rx=self.join_rx, cancel=self.join_cancel.is_set)
        else:
            nav_first = self.first_url
            self.list_url = _navigate_to_live(self._page, self.platform, first_url=self.first_url,
                                              scale=self.join_scale, join_state=self.join_state,
                                              rx=self.join_rx, cancel=self.join_cancel.is_set)
        if not self.list_url:
            raise RuntimeError(f"Failed to navigate to a live {self.platform} stream")
        if self.platform != "tiktok" or self.first_url:          # tt unpinned: checked per try in _tt_choose_room
            nav = self.first_url if self.platform != "twitch" else nav_first
            if nav:
                self._check_landed(nav)
        publish_stream(self._session_dir, self.platform, self._page.eval("location.href") or self.list_url)

        self._t_entry = time.time()   # the stream entry: Q am. 10's 60 s for an ad-free pick count from here
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
        if self.platform == "youtube":
            try:
                self.caption_action = self._page.eval(_CAPTIONS_OFF_JS)
            except Exception as exc:
                self.caption_action = f"error: {exc}"
            print(f"[browser] Captions: {self.caption_action}", flush=True)
        try:
            raw = json.loads(self._page.eval(_LOGIN_STATE_JS) or "{}")
            # a visible Log In button wins: twitch keeps a user-menu node when logged out (tw_6 read "logged_in")
            state = ("logged_out" if raw.get("login_button_visible") else
                     "logged_in" if raw.get("profile_marker") else "unknown")
            self.login_state = {"state": state, **raw}
        except Exception as exc:
            self.login_state = {"state": "unknown", "error": repr(exc)}
        print(f"[browser] Login state: {self.login_state}", flush=True)
        try:
            import decode_path
            self.decode_page = json.loads(self._page.eval(decode_path.HEVC_PROBE_JS, await_promise=True) or "null")
        except Exception as exc:
            self.decode_page = {"error": repr(exc)[:200]}
        if self.platform == "tiktok":
            self.room_rotation["rooms"].append({"t_playing": time.time(), "room": self.channel_url, "ok": True,
                                                "first": True})
        if self.platform == "twitch":
            try:
                self.video_stats = self._page.eval(_TWITCH_VIDEO_STATS_JS, await_promise=True)
            except Exception as exc:
                self.video_stats = f"error: {exc}"
            print(f"[browser] Twitch Video Stats: {self.video_stats}", flush=True)
            if self.tw_latency is not None:      # restart §8: read back once from the overlay, then the hook goes
                try:                             # never an abort: a failure is recorded and the session goes on
                    import tw_latency
                    self.tw_latency = tw_latency.read_back(self._page, self.tw_latency)
                except Exception as exc:
                    self.tw_latency = {**self.tw_latency, "ok": None, "read_back_error": repr(exc)[:200]}
                print(f"[browser] Twitch low latency read back: {self.tw_latency.get('read_back')} "
                      f"(ok {self.tw_latency.get('ok')})", flush=True)
        self._mse_wrap_settle_once("join")
        if self.geometry_target:
            tid = None
            try:
                tid = next((t.get("id") for t in _targets() if t.get("webSocketDebuggerUrl") == self._ws_url), None)
            except Exception:
                pass
            from geometry import parse
            mode, gw, gh = parse(self.geometry_target)
            if mode == "player":
                self.player_pin = pin_player(self._page, (gw, gh), tid)
            else:
                self.player_pin = size_window(self._page, (gw, gh), tid)
                if mode == "theater":
                    self.player_pin["theater"] = yt_theater(self._page) if self.platform == "youtube" else "not yt"
                if mode == "fullscreen":
                    self.player_pin["fullscreen"] = (yt_fullscreen_held(self._page, target_id=tid)
                                                     if self.platform == "youtube" else "not yt")
            self.player_pin["geometry_target"] = self.geometry_target
            from geometry import device_scale   # a `@D` launch scale: requested beside the page's own devicePixelRatio
            self.player_pin["device_scale_requested"] = device_scale(self.geometry_target)
            try:
                self.player_pin["device_pixel_ratio"] = self._page.eval("window.devicePixelRatio")
            except Exception:
                pass
            print(f"[browser] Geometry {self.geometry_target}: {self.player_pin}", flush=True)
        try:
            self.geometry = json.loads(self._page.eval(_GEOMETRY_JS) or "null")
            if self.geometry:
                self.geometry["shot"] = geometry_scales(self.geometry)
        except Exception:
            self.geometry = None
        global SHOT_SOURCE
        if (self._session_dir and SHOT_SOURCE == "composite" and TRIO_FULLSCREEN_OK
                and str(self.geometry_target or "").startswith("fullscreen")):
            SHOT_SOURCE = "x11"                               # t53 (a): this slot's own process, single mode's path
            self.fs_keeper = _start_fullscreen_keeper(self._page)
            print(f"[browser] Fullscreen in trio: x11 path, keeper {self.fs_keeper}", flush=True)
        if self._session_dir and SHOT_SOURCE == "composite":  # t53 trio layout "composite": the window's own pixmap
            self.shot_origin = calibrate_composite(self._page)
            if self.geometry is not None:
                if not isinstance(self.geometry.get("shot"), dict):
                    self.geometry["shot"] = {}
                self.geometry["shot"]["origin"] = self.shot_origin
            print(f"[browser] Shot origin (composite): {self.shot_origin}", flush=True)
        if self._session_dir and SHOT_SOURCE == "x11":        # t37: before the shot loop and the schedule
            tid = None
            try:
                tid = next((t.get("id") for t in _targets() if t.get("webSocketDebuggerUrl") == self._ws_url), None)
            except Exception:
                pass
            self.shot_origin = calibrate_shot_origin(self._page, tid)
            if self.geometry is not None:
                if not isinstance(self.geometry.get("shot"), dict):
                    self.geometry["shot"] = {}
                self.geometry["shot"]["origin"] = self.shot_origin
            print(f"[browser] Shot origin: {self.shot_origin}", flush=True)
        print(f"[browser] Geometry: {self.geometry}", flush=True)

        # Start ad-skip background thread (actions logged to ad_actions.jsonl)
        log_path = (self._session_dir / "ad_actions.jsonl") if self._session_dir else None
        self._watcher = threading.Thread(
            target=_ad_watcher_loop,
            args=(self._page, self.platform, self._stop, log_path, self._on_wall, self._on_frozen,
                  self._stuck_tick if self.platform in stuck.FALLBACK_RUNG_KBIT else None,
                  self._player_error if self.platform in PLAYER_ERROR_PLATFORMS else None, self.error_watch,
                  self.resumer, self._stream_ended if self.platform in STREAM_ENDED_PLATFORMS else None,
                  self._stream_watch(), self.yt_ladder or self.tt_codec, self._clock_sample, self._on_ad_action),
            daemon=True,
            name="ad-watcher",
        )
        self._watcher.start()
        print("[browser] Ad-watcher running", flush=True)
        if self.entry_quality_cfg is not None:
            self._entry_quality_pick()

        if self.platform == "tiktok":
            self._room_watcher = threading.Thread(target=self._room_watch_loop, daemon=True, name="room-watch")
            self._room_watcher.start()
            print("[browser] tt dead-room watcher running", flush=True)

        if self.room_rotation_s:
            self._rotator = threading.Thread(target=self._room_rotation_loop, daemon=True, name="room-rotation")
            self._rotator.start()
            print(f"[browser] tt room rotation every {self.room_rotation_s:.0f} s", flush=True)

        if self._session_dir:
            self._shooter = threading.Thread(
                target=_screenshot_loop,
                args=(self._page, self._session_dir / "shots", self._stop, self.shot_origin, self._pop_shot_reason),
                daemon=True,
                name="screenshots",
            )
            self._shooter.start()
            print(f"[browser] Screenshots every {SHOT_INTERVAL_S:.0f} s → {self._session_dir / 'shots'}", flush=True)

    @staticmethod
    def _pinned() -> bool:
        """A W-A / gate pin (orchestrate --trio-pin; exp.py records it from LVDC_PICK_PINNED)."""
        return bool(os.environ.get("LVDC_PICK_PINNED"))

    def _identity_failure(self, idc: dict) -> str | None:
        """The named failure of an identity read, else None: a mismatch is pin_mismatch (pinned) / room_mismatch; on a
        PINNED session an unverifiable identity is a refusal too (levi 00:38 S2: "not verifiable" is never a pass)."""
        kind = "pin_mismatch" if self._pinned() else "room_mismatch"
        if idc["ok"] is False:
            return f"{kind}: wanted {idc['intended']}, landed {idc['landed']} ({idc.get('landed_url')})"
        if idc["ok"] is None and self._pinned():
            return f"{kind}: unverifiable (intended {idc['intended']}, landed {idc['landed']}, {idc.get('landed_url')})"
        return None

    def _check_landed(self, nav: str) -> None:
        """The landed room / channel vs the URL navigated (WA2 r1): a failure (_identity_failure) is a named startup
        failure, recorded in meta pin_check; unverifiable on an unpinned session is recorded, never an abort."""
        self.pin_check = identity_check(self.platform, nav, self._page)
        why = self._identity_failure(self.pin_check)
        if why:
            raise RuntimeError(why)

    def _check_reentry_identity(self, r: dict) -> None:
        """levi 00:38 M3: after EVERY re-entry confirm, the page is still the session's own room / channel; a failure is a
        named abort "<pin|room>_mismatch (reentry): ..." (the session never silently changes room), recorded in the
        re-entry record (identity)."""
        first, chan = getattr(self, "first_url", None), getattr(self, "channel_url", None)
        nav = first if first and page_identity(self.platform, first) else chan
        idc = identity_check_settled(self.platform, nav, self._page)
        r["identity"] = idc
        why = self._identity_failure(idc)
        if why:
            r["ok"] = False
            kind, rest = why.split(":", 1)
            if self.abort_reason is None:
                self.abort_reason, self.abort_at = f"{kind} (reentry):{rest}", time.time()

    def _tw_discover_channel(self, url: str) -> dict:
        """levi 07:48: the twitch directory read in its own tab, unshaped and closed before the shaper's first apply: its
        first live card whose channel is not recent / held by a sibling / set aside / capped (the exclusions the join
        uses), NOT clicked; one re-navigate when it offers none, then the shot and the resources; none raises (a startup
        failure before any shaping). The pick is claimed (t58 N-2) at once."""
        import channel_cap, not_usable, record
        sib = sibling_streams(self._session_dir, "twitch")
        troot = not_usable.trio_root_of(self._session_dir)
        na = not_usable.set_aside(troot, "twitch") if troot is not None else {}
        cap = channel_cap.cap_state(troot, "twitch", record.counted_any_era)
        exclude = sorted(set(recent_tw_channels(self._session_dir)) | set(sib) | set(na)
                         | set((cap or {}).get("capped") or []))
        rec: dict = {"directory": url, "excluded_recent": exclude, "discovery": "unshaped, own tab (levi 07:48)"}
        ids, tp = _discovery_tab()
        try:
            res: dict = {}
            for attempt in range(2):
                tp.navigate(url)
                _wait_for_load(tp, timeout=15)
                deadline = time.time() + 15
                while time.time() < deadline and not tp.eval(_CARD_READY_JS["twitch"]):
                    time.sleep(1)
                try:
                    res = json.loads(tp.eval(_pick_twitch_js(exclude, click=False)) or "{}")
                except Exception as exc:
                    res = {"error": repr(exc)}
                if res.get("candidates"):
                    break
                rec["renavigated"] = attempt == 0 or rec.get("renavigated", False)
            if not res.get("candidates"):
                try:
                    rec["page_resources"] = json.loads(tp.eval(_PAGE_RESOURCES_JS) or "null")
                except Exception as exc:
                    rec["page_resources"] = {"error": repr(exc)[:200]}
                rec["empty_shot"] = self._discovery_shot(tp)
        finally:
            self.discovery["tab_closed"] = _close_discovery_tab(ids, tp)
        rec.update(candidates=res.get("candidates"), pick=res.get("pick"), error=res.get("error"))
        self.tw_pick = {**(self.tw_pick or {}), **rec}
        if not res.get("pick"):
            raise RuntimeError(f"Failed to navigate to a live twitch stream (discovery: no live channel on {url}, "
                               f"unshaped; {len(res.get('candidates') or [])} cards, all excluded or none)")
        publish_stream(self._session_dir, "twitch", res["pick"], "intent")
        return rec

    def _tt_choose_room(self) -> str | None:
        """tt: pick a live, playing room that no recent session used (see TT_* above); returns the list URL or None. The
        discovery on the session page itself (the path before levi 07:48; exp.py's auto mode discovers unshaped first)."""
        rec, order = self._tt_discover_rooms(self._page, self.join_scale)
        return self._tt_try_rooms(rec, order)

    def _tt_pool(self, page, sc: float) -> tuple[list[dict], int]:
        """/live's room cards: navigate, wait for cards, scroll until TT_POOL_MIN / 2 stale reads / TT_POOL_SCROLLS."""
        page.navigate(ROOM_ROTATION_LIVE_URL)
        _wait_for_load(page, timeout=15 * sc)
        deadline = time.time() + 15 * sc
        while time.time() < deadline and not page.eval(_CARD_READY_JS["tiktok"]):
            time.sleep(1)
        time.sleep(2)                                   # let the viewer counts render
        cands, seen, stale, scrolls = [], set(), 0, 0
        while True:
            try:
                batch = json.loads(page.eval(_TT_CANDIDATES_JS) or "[]")
            except Exception:
                batch = []
            new = [c for c in batch if c.get("href") and c["href"] not in seen]
            for c in new:
                seen.add(c["href"])
                cands.append(c)
            stale = 0 if new else stale + 1
            if len(cands) >= TT_POOL_MIN or stale >= 2 or scrolls >= TT_POOL_SCROLLS:
                break
            page.eval("window.scrollBy(0, Math.max(window.innerHeight, 800))")   # load more cards
            scrolls += 1
            time.sleep(1.5)
        return cands, scrolls

    def _discovery_shot(self, page) -> str | None:
        """The list page when it offered nothing (levi 07:48): a PNG in the session dir; None if it failed."""
        import base64
        try:
            shot = page.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
            if self._session_dir and shot.get("data"):
                (Path(self._session_dir) / DISCOVERY_EMPTY_SHOT).write_bytes(base64.b64decode(shot["data"]))
                return DISCOVERY_EMPTY_SHOT
        except Exception as exc:
            print(f"[browser] discovery shot failed: {exc!r}", flush=True)
        return None

    def _tt_discover_rooms(self, page=None, sc: float = 1.0) -> tuple[dict, list[dict]]:
        """tt's room list and its try order. page None (levi 07:48): the unshaped discovery in its own tab, closed here
        before returning; an empty /live is re-navigated once, then shot and its resources recorded; no room raises (a
        startup failure before any shaping). Returns (tt_room_choice record, ordered candidates)."""
        own, ids = page is None, None
        if own:
            ids, page = _discovery_tab()
        try:
            return self._tt_discover_in(page, sc, own)
        finally:
            if own:
                self.discovery["tab_closed"] = _close_discovery_tab(ids, page)

    def _tt_discover_in(self, page, sc: float, own: bool) -> tuple[dict, list[dict]]:
        sib = sibling_streams(self._session_dir, "tiktok")      # t56: a running sibling's room is never taken
        import not_usable                        # trio rotation hygiene: 2 trio startup_failures set a room aside
        troot = not_usable.trio_root_of(self._session_dir)
        na = not_usable.set_aside(troot, "tiktok") if troot is not None else {}
        import channel_cap, record               # the next boundary's cap (levi): a host at the cap is passed over
        cap = channel_cap.cap_state(troot, "tiktok", record.counted_any_era)
        self.channel_pick = cap
        recent = sorted(set(recent_tt_rooms(self._session_dir)) | set(sib) | set(na)
                        | {_norm_room(k) for k in ((cap or {}).get("capped") or [])})
        rec: dict = {"n_recent_sessions": TT_RECENT_ROOMS_N, "recent_excluded": recent, "min_viewers": TT_MIN_VIEWERS,
                     "candidates": [], "tries": [], "choice": None, "sibling_excluded": sib,
                     **({"not_usable_excluded": na} if na else {})}
        self.tt_room_choice = rec
        if own:
            rec["discovery"] = "unshaped, own tab (levi 07:48)"
        cands, scrolls = self._tt_pool(page, sc)
        if not cands and own:                     # levi 07:48 (2): one re-navigate, then the shot and the resources
            rec["renavigated"] = True
            try:
                rec["page_resources_first"] = json.loads(page.eval(_PAGE_RESOURCES_JS) or "null")
            except Exception as exc:
                rec["page_resources_first"] = {"error": repr(exc)[:200]}
            cands, scrolls = self._tt_pool(page, sc)
            if not cands:
                try:
                    rec["page_resources"] = json.loads(page.eval(_PAGE_RESOURCES_JS) or "null")
                except Exception as exc:
                    rec["page_resources"] = {"error": repr(exc)[:200]}
                rec["empty_shot"] = self._discovery_shot(page)
        rec["pool_size"], rec["pool_scrolls"] = len(cands), scrolls
        for c in cands:
            c["viewers"] = _parse_viewers(c.get("text", ""))
            c["live_badge"] = bool(re.search(r"\bLIVE\b", c.get("text", "")))
            c["recent"] = _norm_room(c.get("href")) in set(recent)
        rec["candidates"] = cands
        order = sorted((c for c in cands if not c["recent"]),
                       key=lambda c: (not (c["live_badge"] and (c["viewers"] or 0) >= TT_MIN_VIEWERS), -(c["viewers"] or 0)))
        if not cands:
            # /live rendered as the LIVE feed (no room links; seen logged out): the old first-card / feed path, recorded,
            # then the same playing check and pre-roll
            rec["fallback"] = page.eval(_PICK_STREAM_JS["tiktok"])
            _wait_for_load(page, timeout=15 * sc)
            href = page.eval("location.href")
            if href and "/@" in href and _norm_room(href) not in set(recent):
                # own tab: the room is opened later by its URL (levi 07:48 (ii)), so it is a normal try then
                order = [{"href": _norm_room(href) if own else href, "viewers": None, "live_badge": None,
                          "fallback": not own, **({"feed_fallback": True} if own else {})}]
        print(f"[browser] tt rooms: {len(cands)} candidates, {sum(1 for c in cands if c['recent'])} used in the last "
              f"{TT_RECENT_ROOMS_N} sessions", flush=True)
        if own and not order:
            raise RuntimeError("Failed to navigate to a live tiktok stream (discovery: no room on /live, unshaped, "
                               "after a re-navigate)")
        return rec, order

    def _tt_try_rooms(self, rec: dict, order: list[dict]) -> str | None:
        """The tries, on the session page: each room opened by its URL (B9: on the shaped link), confirmed playing, its
        identity checked and its pre-roll passed; returns the list URL, None when none played."""
        page, sc = self._page, self.join_scale
        self.tt_room_choice = rec
        t_walk = time.time()
        for c in order[:TT_MAX_TRIES]:
            if self.join_state is not None and (time.time() - t_walk >= JOIN_CAP_S or self.join_cancel.is_set()):
                rec["join_walk_stopped"] = "cancelled" if self.join_cancel.is_set() else "cap"      # B9 D1 / D2
                break
            t: dict = {"room": c["href"], "viewers": c["viewers"], "live_badge": c["live_badge"], "t_nav": time.time()}
            try_deadline = time.time() + JOIN_BASE_S * sc if self.join_state is not None else None
            left = (lambda own: own if try_deadline is None else max(0.0, min(own, try_deadline - time.time())))  # noqa
            rx0 = _rx_or_none(self.join_rx) if self.join_state is not None else None
            publish_stream(self._session_dir, "tiktok", c["href"], "intent")      # t58 N-2: claimed before the preroll
            try:
                if not c.get("fallback"):
                    page.navigate(c["href"])
                    _wait_for_load(page, timeout=left(15 * sc))
                # levi 12:18 (a), WC3c ti_100003: under B9 the confirm runs to the per-URL deadline (the room's <video>
                # can appear late: +18.7 s there); the quick window is off-B9 only
                confirm_s = (TT_QUICK_CONFIRM_S if try_deadline is None
                             else max(0.0, left(JOIN_BASE_S * sc) - TT_AUTOPLAY_GRACE_S))
                t["confirm_s"] = round(confirm_s, 1)
                ok, detail = _tt_confirm_playing(page, confirm_s, scale=sc)
                if not ok and detail.get("dead") == "not_playing" and detail.get("showed_frames"):
                    detail["dead"] = "slow_start"       # (b): frames but < 2 s of playhead by the deadline: next card
                if ok and not c.get("fallback"):        # levi 00:09: the landed room is the picked one, else next
                    t["identity"] = idc = identity_check("tiktok", c["href"], page)
                    if idc["ok"] is False:
                        ok, detail = False, {**detail, "dead": "room_mismatch", "landed": idc["landed"]}
                        # levi 00:35 (1): never silent: it changes which channel a counted session holds (caps, folds)
                        rec.setdefault("room_mismatches", []).append(
                            {"intended": c["href"], "landed_url": idc.get("landed_url"), "try": len(rec["tries"])})
                if ok:
                    t["t_playing"] = time.time()
                    ok, detail = _tt_preroll(page, TT_PREROLL_S, scale=sc,
                                             cancel=self.join_cancel if self.join_state is not None else None,
                                             until=(t_walk + JOIN_CAP_S) if self.join_state is not None else None)
            except Exception as exc:
                ok, detail = False, {"dead": "error", "error": repr(exc)}
            for k in ("watched_s", "ct_advance_s", "frames"):      # (g): every try carries them (None: not measured)
                detail.setdefault(k, None)
            t.update(ok=ok, detail=detail, t_done=time.time())
            rec["tries"].append(t)
            # B9 D3: no first frame by the per-URL deadline (levi 12:18 (b): a room that showed frames is a slow_start,
            # the next card; ti_100003 raised at 21 of 45 s with tf 42)
            if not ok and self.join_state is not None and detail.get("dead") == "not_playing" \
                    and time.time() >= try_deadline - 1.0 and not self.join_cancel.is_set():
                due, why = join_timeout_due(detail.get("ev0") or {},
                                            {"attached": bool(detail.get("has_video") and detail.get("src")),
                                             "buf_end": detail.get("buf_end"), "decoded": detail.get("decoded")},
                                            rx0, _rx_or_none(self.join_rx),
                                            live=bool(c.get("live_badge") or detail.get("dur_inf")))
                t["join_evidence"] = why
                if due:                                     # media flowing on a live room, no first frame: a slow join
                    rec["join_timeout"] = c["href"]
                    self._room_mismatch_next(rec)
                    raise JoinTimeout(self.join_state, time.time() - t["t_nav"], {"room": c["href"], **why})
            print(f"[browser] tt room {c['href']} ({c['viewers']} viewers): {'OK' if ok else detail.get('dead')}", flush=True)
            if ok:
                rec["choice"] = c["href"]
                self._room_mismatch_next(rec)
                return ROOM_ROTATION_LIVE_URL
        self._room_mismatch_next(rec)
        return None

    @staticmethod
    def _room_mismatch_next(rec: dict) -> None:
        """Each room_mismatch names the next candidate opened after it (None when it was the last try)."""
        for m in rec.get("room_mismatches") or []:
            nxt = m["try"] + 1
            m["next_opened"] = rec["tries"][nxt]["room"] if nxt < len(rec["tries"]) else None

    def _room_watch_loop(self) -> None:
        """tt: abort the session with reason room_ended when the room dies mid-session, or player_dead when the player
        sits at readyState 0 with no media (TTRoomWatch)."""
        w = TTRoomWatch(log=self.player_dead_watch)
        while not self._stop.wait(timeout=1):
            if time.time() < self._room_grace_until:
                w.close_open(time.time(), "replaced")
                w = TTRoomWatch(log=self.player_dead_watch)
                continue
            s = _tt_state(self._page)
            labels = getattr(self, "net_labels", None)
            try:
                state = int(labels[-1].state) if labels else None
            except (ValueError, AttributeError, TypeError):
                state = None
            lever_on = bool(labels) and getattr(labels[-1], "lever", None) is not None    # v3 B4 (levi 21:12)
            # tt can pause itself (ti_8: a "pause" event, then flat until room_ended): click play while paused and flat,
            # before the no-media rule can fire; each click is counted in meta.json room_play_clicks
            if s.get("paused") and w.flat_since is not None and time.time() - w.flat_since >= TT_AUTOPLAY_GRACE_S:
                try:
                    self._page.eval(_CLICK_PLAY_JS)
                    self.room_play_clicks += 1
                except Exception:
                    pass
            own = _shaped_rx_bytes()
            dead = w.feed(s, time.time(), shaper_state=state, rx_bytes=own if RX_OWN_IFB else _rx_bytes(),
                          shaped_rx_bytes=own, lever_active=lever_on)
            self.room_discontinuities = w.discontinuities
            if dead:
                rec = {"reason": dead, "at": time.time(), "sample": s, "shaper_state": state,
                       "room": self._page.eval("location.href") if self._page else None}
                why = "player_dead" if dead == "player_dead" else "room_ended"
                if why == "player_dead":
                    try:
                        rate = float(labels[-1].speed) if labels else None
                    except (ValueError, AttributeError, TypeError):
                        rate = None
                    pd = {**rec, "dead_s": round(time.time() - (w.dead_since or time.time()), 1),
                          "dead_window": dead_window(w.srx, rate, w.playing_kbps(w.dead_since)),
                          "media": self._media_snapshot()}
                    with self._recovery_lock:
                        used = self._recoveries_used()
                        r = self._reenter_room(pd) if dead_action(used) == "reenter" else None
                    if r is not None:
                        print(f"[browser] PLAYER_DEAD: re-entered the room ({r['outcome']})", flush=True)
                        if r["outcome"] == "continue":
                            w.close_open(time.time(), "replaced")
                            w = TTRoomWatch(log=self.player_dead_watch)
                            continue
                        if r["outcome"] == "room_ended":
                            why = "room_ended"
                            # bundle item 4 (supervisor 2026-10-21): `sample` stays the pre-re-entry one; what the
                            # reloaded page showed rides beside it
                            self.room_ended = {**rec, "reason": "ended_page", "after_player_dead_reentry": True,
                                               "reentry_confirm": reentry_confirm_of(r)}
                    if why == "player_dead":
                        self.player_dead = {**pd, "reentries": len(self.player_dead_reentries)}
                else:
                    self.room_ended = rec
                if self.abort_reason is None:
                    self.abort_reason, self.abort_at = why, time.time()
                print(f"[browser] {why.upper()} ({dead}) — aborting the session", flush=True)
                w.close_open(time.time(), "session_end")
                return
        w.close_open(time.time(), "session_end")

    def _media_snapshot(self) -> dict:
        """At a tt dead trigger (recording only): every <video>'s error and state, Chrome's video-decode status and the
        media-internals recorder's players and recent records (MediaListener). It runs before the re-entry, so it is
        capped at MEDIA_SNAPSHOT_MAX_S (levi's audit of 1bcdc5e): the page and SystemInfo reads run in a worker joined
        for that long; whatever finished is kept, with snapshot_s and timed_out."""
        t0 = time.time()
        out: dict = {"media_events": self._media.snapshot() if self._media is not None else None}

        def work() -> None:
            try:
                out["videos"] = self._page.eval(_VIDEO_ERRORS_JS) if self._page else None
            except Exception as exc:
                out["videos"] = {"error": repr(exc)[:200]}
            try:
                import decode_path
                g = decode_path.gpu_summary(_browser_call("SystemInfo.getInfo"))
                out["gpu"] = {k: g.get(k) for k in ("video_decode", "hevc", "error")}
            except Exception as exc:
                out["gpu"] = {"error": repr(exc)[:200]}
        th = threading.Thread(target=work, daemon=True, name="media-snapshot")
        th.start()
        th.join(MEDIA_SNAPSHOT_MAX_S)
        return {**out, "timed_out": th.is_alive(), "snapshot_s": round(time.time() - t0, 3)}

    def _reenter_room(self, pd: dict, reason: str = "player_dead") -> dict:
        """The one re-entry after a player_dead: navigate to the same room, confirm playback (_tt_confirm_playing:
        play clicked, the playhead advancing within TT_REENTRY_CONFIRM_S, the ended banner read), and record it as an
        entry in room_rotation's rooms (entries.py) and in player_dead_reentries (meta.json)."""
        room = pd.get("room") or self.channel_url
        rec = {"t_nav": time.time(), "room": room, "reason": reason, "dead_s": pd.get("dead_s"),
               "dead_window": pd.get("dead_window")}
        if pd.get("media") is not None:
            rec["media"] = pd["media"]
        detail: dict = {}
        try:
            self._page.navigate(room)
            _wait_for_load(self._page, timeout=15)
            rec["t_enter"] = time.time()
            trace: list = []
            ok, detail = _tt_confirm_playing(self._page, TT_REENTRY_CONFIRM_S, trace=trace)
            rs3 = next((t for t, rs, _, _ in trace if (rs or 0) >= 3), None)
            rec.update(ok=bool(ok), t_playing=time.time() if ok else None, room=self._page.eval("location.href"),
                       confirm={k: detail.get(k) for k in ("dead", "rs", "ct", "ended_text", "frames",
                                                           "frames_unreadable", "confirm_basis") if k in detail},
                       confirm_s=TT_REENTRY_CONFIRM_S, samples=len(trace),
                       samples_loading=sum(1 for *_, ld in trace if ld),
                       samples_no_video=sum(1 for _, _, hv, _ in trace if not hv),
                       s_to_data=None if rs3 is None else round(rs3 - rec["t_enter"], 1))
            if ok:
                self._check_reentry_identity(rec)
        except Exception as exc:
            rec.update(ok=False, error=repr(exc))
        rec["outcome"] = reentry_outcome(rec.get("ok", False), detail)
        self.room_rotation["rooms"].append(rec)
        if reason == "player_dead":
            self.player_dead_reentries.append(rec)
        return rec

    def _room_rotation_loop(self) -> None:
        """addendum M's pre-declared tt fallback: every room_rotation_s, back to /live and into a room not visited yet;
        each move is recorded (navigation time, playing time, room URL, ok) so room-change startups can be told apart
        from within-room tier switches."""
        while not self._stop.wait(timeout=self.room_rotation_s):
            rec: dict = {"t_nav": time.time()}
            self._room_grace_until = time.time() + 120     # the dead-room watcher sits out the move
            try:
                visited = ([r.get("room") or "" for r in self.room_rotation["rooms"]] + recent_tt_rooms(self._session_dir)
                           + sibling_streams(self._session_dir, "tiktok"))
                self._page.navigate(ROOM_ROTATION_LIVE_URL)
                _wait_for_load(self._page, timeout=15)
                deadline = time.time() + 15
                while time.time() < deadline and not self._page.eval(_CARD_READY_JS["tiktok"]):
                    time.sleep(1)
                picked = self._page.eval(_pick_new_room_js(visited))
                rec["picked"], rec["t_enter"] = picked, time.time()
                if isinstance(picked, str) and picked:
                    publish_stream(self._session_dir, "tiktok", picked, "intent")     # t58 N-2: claimed on the click
                ok = False
                deadline = time.time() + 30
                while time.time() < deadline and not self._stop.is_set():
                    self._page.eval(_CLICK_PLAY_JS)
                    if _video_is_advancing(self._page):
                        ok = True
                        break
                    time.sleep(1)
                rec.update(t_playing=time.time() if ok else None, room=self._page.eval("location.href"), ok=ok)
                publish_stream(self._session_dir, "tiktok", rec["room"])
            except Exception as exc:
                rec.update(ok=False, error=repr(exc))
            try:
                rec["geometry"] = json.loads(self._page.eval(_GEOMETRY_JS) or "null")
                if rec["geometry"]:
                    rec["geometry"]["shot"] = geometry_scales(rec["geometry"])
            except Exception:
                rec["geometry"] = None
            self.room_rotation["rooms"].append(rec)
            self._room_grace_until = time.time() + 15      # settle in the new room, then watch it
            print(f"[browser] Room rotation: {rec}", flush=True)

    # ------------------------------------------------------------------ the stuck-player rule (Addendum Q am. 6)
    def _current_rung(self, frozen_s: float = 0.0) -> dict:
        """The lowest-rung record (stuck.rung_record): twitch from the master listener, yt / tt from page JS (retried
        every 30 s while only the fallback is known)."""
        kbit, src, detail = None, None, None
        if self.platform == "twitch" and self._master is not None and self._master.latest:
            kbit, src, detail = self._master.latest["kbit"], "hls_master_lowest_bandwidth", \
                {"path": self._master.latest["path"], "from_worker": self._master.latest.get("from_worker")}
        elif self.platform == "twitch":
            # no master read: the player's own quality menu decides the one exception. Reading it opens the settings
            # menu (entry_quality.Driver.state; closed again after the read), so it runs only inside a frozen run of
            # >= LADDER_PROBE_FROZEN_S, retried every 30 s until it reads (supervisor's condition on d40e263)
            if self.lowest_rung and self.lowest_rung.get("source") == "ladder_160p_transcode":
                return self.lowest_rung
            run0 = round(time.time() - frozen_s)
            tries = [x for x in self.rung_probes if abs(x["run_start"] - run0) <= 2]
            # once per frozen run (levi / supervisor on 7089cb2); a retry only when the read raised, at most 2 attempts
            if frozen_s >= LADDER_PROBE_FROZEN_S and len(tries) < LADDER_PROBE_ATTEMPTS and \
                    all(x.get("error") for x in tries):
                rec = {"run_start": run0, "frozen_s": round(frozen_s, 1), "t_open": time.time()}
                try:
                    import entry_quality as EQ
                    opts = [i.get("label") for i in (EQ.Driver(self._page, "twitch").state().get("items") or [])]
                    rec["options"] = opts
                    self.player_ladder = {"options": opts, "t": time.time(), "frozen_s": round(frozen_s, 1)}
                    if any(EQ.option_height(o) == 160 for o in opts):
                        kbit, src, detail = stuck.TWITCH_160P_TRANSCODE_KBIT, "ladder_160p_transcode", {"options": opts}
                except Exception as exc:
                    rec["error"] = detail = {"error": repr(exc)[:200]}
                rec["t_close"] = time.time()
                self.rung_probes.append(rec)
                print(f"[browser] twitch ladder probe (master unread, frozen {frozen_s:.0f} s): "
                      f"{rec.get('options') or rec.get('error')}", flush=True)
            elif self.lowest_rung:
                return self.lowest_rung
        elif self.platform in ("youtube", "tiktok"):
            if self.lowest_rung and self.lowest_rung.get("source") != "fallback":
                return self.lowest_rung
            if time.time() - self._rung_probe_at >= 30:
                self._rung_probe_at = time.time()
                try:
                    js = stuck.YT_FORMATS_JS if self.platform == "youtube" else stuck.TT_ROOM_JS
                    kbit, src, detail = stuck.rung_from_page(self.platform, self._page.eval(js))
                except Exception as exc:
                    detail = {"error": repr(exc)[:200]}
            elif self.lowest_rung:
                return self.lowest_rung
        rec = stuck.rung_record(self.platform, kbit, src, detail)
        if rec != self.lowest_rung:
            self.lowest_rung = {**rec, "t": time.time()}
        return self.lowest_rung

    def _stuck_tick(self, frozen_s: float) -> None:
        """Every second, from the ad watcher: T2 on the frozen run above the lowest rung -> one re-entry, else censor."""
        if self.abort_reason is not None or frozen_s <= 0:
            return
        now = time.time()
        rung = self._current_rung(frozen_s)
        labels = getattr(self, "net_labels", None) or []
        read = stuck.t2_rung(rung)
        if read is None:          # only the fallback: no T2 (supervisor 2026-10-18, tw_34); logged once per frozen run
            run0 = round(now - frozen_s)
            if frozen_s >= stuck.T2_S and not any(abs(x["frozen_since"] - run0) <= 2 for x in self.t2_skipped):
                self.t2_skipped.append({"reason": "t2_skipped_rung_unknown", "t": now, "frozen_since": run0,
                                        "frozen_s": round(frozen_s, 1), "rung": rung, "shaper": stuck.shaper_at(labels, now)})
                print(f"[browser] t2_skipped_rung_unknown: frozen {frozen_s:.0f} s, rung {rung.get('source')}", flush=True)
            return
        if self.platform in PLAYER_ERROR_PLATFORMS:     # levi 10:27: the clock holds in the low states + grace
            he = self._low_hold_end(now)
            lift = self._hold_lifted(now, frozen_s, "t2") if he is not None else None   # levi 17:51 / 18:16
            if lift is not None:
                he, frozen_s = None, min(frozen_s, now - lift)   # T2 counts from the lift
            if he is not None:
                if now <= he:
                    return
                frozen_s = min(frozen_s, now - he)           # the frozen run counts only after the hold
        t2, t2_start = stuck.t2_clock(frozen_s, now, labels, read)
        if t2 is None or t2 < stuck.T2_S:
            return
        try:
            url = self._page.eval("location.href")
        except Exception:
            url = None
        rec = {"t_trigger": now, "frozen_s": round(frozen_s, 1), "frozen_window": [now - frozen_s, now],
               "t2_s": round(t2, 1), "t2_window": [t2_start, now], "rung": rung, "shaper": stuck.shaper_at(labels, now),
               "url": url or self.channel_url}
        with self._recovery_lock:
            used = self._recoveries_used()
            if stuck.action(used) == "reenter":
                r = self._reenter_frozen(rec)
                print(f"[browser] STUCK PLAYER: frozen {frozen_s:.0f} s, {t2:.0f} s above the lowest rung "
                      f"({rung.get('kbit')} kbit, {rung.get('source')}): re-entered ({r['outcome']})", flush=True)
                if r["outcome"] == "continue":
                    return
                if r["outcome"] == "room_ended":          # tt: the re-entry found the ended banner (the host ended)
                    self.room_ended = {"reason": "ended_page", "after_frozen_reentry": True, "at": time.time(),
                                       "reentry_confirm": reentry_confirm_of(r)}
                    if self.abort_reason is None:
                        self.abort_reason, self.abort_at = "room_ended", time.time()
                    return
                why = "reentry_not_playing"
            else:
                why = "second_freeze" if self.frozen_reentries else "freeze_after_recovery"
        self.censored = {**rec, "reason": why, "t": time.time()}
        if self.abort_reason is None:
            self.abort_reason, self.abort_at = "player_frozen", time.time()
        print(f"[browser] PLAYER FROZEN (stuck-player rule, {why}): censored at {self.censored['t']:.0f}, shaper "
              f"{rec['shaper']} — aborting the session", flush=True)

    def _stream_watch(self) -> "StreamEndedWatch | None":
        """The session's StreamEndedWatch (yt / twitch), kept for meta.json stream_read_timing."""
        self.stream_watch = StreamEndedWatch(self.platform, self.channel_url) if self.platform in STREAM_ENDED_PLATFORMS \
            else None
        return self.stream_watch

    def _stream_ended(self, trig: dict) -> None:
        """StreamEndedWatch's trigger (boundary bundle item 3): the tab left its live stream; abort "stream_ended" (no
        re-entry: the broadcast is over, or the page moved to another channel). meta.json stream_ended records it."""
        if self.abort_reason is not None:
            return
        self.stream_ended = trig
        self.abort_reason, self.abort_at = "stream_ended", time.time()
        print(f"[browser] STREAM ENDED ({trig.get('sign')}: {trig.get('from_page')} -> {trig.get('to_page')}) — "
              f"aborting the session", flush=True)

    def _recoveries_used(self) -> int:
        """The session's re-entries that count against its one recovery (a low-state error_page re-entry does not;
        levi 10:27)."""
        return sum(1 for r in self.player_dead_reentries + self.frozen_reentries if not r.get("lowstate_free"))

    def _low_hold_end(self, t: float) -> float | None:
        """stuck.low_hold_end on the session's live net labels; with none (None or empty) the hold never applies (the
        old behaviour), recorded for meta.json lowstate_hold_available (levi 11:48)."""
        labels = getattr(self, "net_labels", None) or []
        if not labels:
            self.lowstate_hold_available = False
            return None
        if getattr(self, "lowstate_hold_available", None) is None:
            self.lowstate_hold_available = True
        return stuck.low_hold_end(labels, t)

    def _on_ad_action(self, result: str, t: float) -> None:
        """batch #10: tt's guide actions (dismissed / stuck) recorded with their time (meta.json tt_onboarding)."""
        if not result.startswith("tt_onboarding"):
            return
        action = result.split(" [", 1)[0]
        last = self.tt_onboarding[-1] if self.tt_onboarding else None
        if last is not None and last["action"] == "tt_onboarding_stuck" and action == last["action"]:
            last["n"] = last.get("n", 1) + 1                  # brad 15:03: a stuck guide is one entry, counted
            last["t_last"] = round(t, 3)
            return
        if len(self.tt_onboarding) < WATCH_LOG_MAX:
            self.tt_onboarding.append({"t": round(t, 3), "action": action,
                                       "text": result.split(" [", 1)[1].rstrip("]") if " [" in result else None})

    def _lowstate_held(self, t: float) -> bool:
        """The shaper's low-state hold runs at t."""
        he = self._low_hold_end(t)
        return he is not None and t <= he

    def _ad_frozen_held(self, t: float) -> bool:
        """The ad_frozen clock's hold (levi 10:27), lifted as the frozen clocks' is (levi 13:09 / 18:16) and, unlike
        them, with ad polls counted (levi 00176, W-B yt_100003: an ad break stuck over a PAUSED element with 9.6 s
        buffered sat 16 min under the low-state hold, because this clock never looked at the lift and the lift's run
        skips ad polls): low bandwidth cannot explain a paused player with data. Held = the low-state hold runs and the
        resumer's polls have NOT shown the element paused with data and flat (readyState >= 3, buffer >= the floor,
        ad or not) for PAUSE_DEADLOCK_S, with a poll no older than FROZEN_HOLD_POLL_MAX_AGE_S. Each lift is recorded
        once per run (meta.json frozen_hold.bypassed, clock "ad_frozen")."""
        if not self._lowstate_held(t):
            return False
        r = getattr(self, "resumer", None)
        last, since = getattr(r, "last_poll", None), getattr(r, "data_flat_any_since", None)
        if last is None or t - last[0] > FROZEN_HOLD_POLL_MAX_AGE_S or since is None or t < since + PAUSE_DEADLOCK_S:
            return True
        if not any(x.get("clock") == "ad_frozen" and abs(x.get("data_flat_since", 0) - since) <= 1e-6
                   for x in self.frozen_hold_bypassed) and len(self.frozen_hold_bypassed) < WATCH_LOG_MAX:
            st = last[1]
            self.frozen_hold_bypassed.append({"clock": "ad_frozen", "t": t, "data_flat_since": since,
                                              "lift": since + PAUSE_DEADLOCK_S,
                                              **{k: st.get(k) for k in ("paused", "rs", "buf", "stall", "ad")}})
        return False

    def _on_error_onset(self, t: float) -> None:
        last = getattr(self, "_shot_last_py", {}).get("error_page")
        if last is not None and t - last < SHOT_EVENT_MIN_GAP_S:      # levi 00:53: never a shot storm; counted
            sup = getattr(self, "shot_triggers_py_suppressed", None)
            if sup is None:
                sup = self.shot_triggers_py_suppressed = {"error_page": 0}
            sup["error_page"] += 1
            return
        if not hasattr(self, "_shot_last_py"):
            self._shot_last_py = {}
        self._shot_last_py["error_page"] = t
        self._shot_reasons.append("error_page")
        self.shot_triggers_py["error_page"] += 1

    def _pop_shot_reason(self) -> str | None:
        return self._shot_reasons.pop(0) if self._shot_reasons else None

    def _player_error(self, trig: dict) -> None:
        """PlayerErrorWatch's trigger: the session's one recovery as a re-entry (player_dead_reentries), else abort
        "player_error" (session_class reads it as player_dead; startup_failure keeps precedence)."""
        if self.abort_reason is not None:
            return
        try:
            url = self._page.eval("location.href")
        except Exception:
            url = None
        rec = {**trig, "url": url or self.channel_url}
        # levi 10:27: an error page that began in the low-state hold keeps its re-entry (a viewer reloads an error page)
        # but does not consume the session's one recovery
        n_free = sum(1 for r in self.player_dead_reentries + self.frozen_reentries if r.get("lowstate_free"))
        free = trig.get("reason") == "error_page" and isinstance(trig.get("since"), (int, float)) \
            and n_free < LOWSTATE_FREE_MAX and self._lowstate_held(float(trig["since"]))   # levi 11:48: bounded
        with self._recovery_lock:
            used = self._recoveries_used()
            if free or stuck.action(used) == "reenter":
                r = self._reenter_error(rec)
                if free:
                    r["lowstate_free"] = rec["lowstate_free"] = True
                rec["reentry"] = {k: r.get(k) for k in ("t_nav", "method", "ok", "outcome")}
                self.player_errors.append(rec)
                print(f"[browser] PLAYER ERROR ({trig['reason']}: {(trig.get('error') or {}).get('text')}): "
                      f"re-entered by {r['method']} ({r['outcome']})", flush=True)
                if r["outcome"] == "continue":
                    return
                why = "reentry_not_playing"
            else:
                why = "recovery_used"
        rec["abort"] = why
        if rec not in self.player_errors:
            self.player_errors.append(rec)
        if self.abort_reason is None:
            self.abort_reason, self.abort_at = "player_error", time.time()
        print(f"[browser] PLAYER ERROR ({trig['reason']}, {why}) — aborting the session", flush=True)

    def _reenter_error(self, rec: dict) -> dict:
        """The recovery after a player error: Twitch's reload button when the page shows one (a viewer's click), else
        the channel URL navigated with the entry setup redone; the playhead must advance within stuck.REENTRY_CONFIRM_S.
        Recorded in player_dead_reentries (the labeler censors t_nav -> the next video_start)."""
        r = {"reason": rec["reason"], "t_trigger": rec["t_trigger"], "since": rec["since"], "error": rec.get("error"),
             "t_nav": time.time()}
        try:
            clicked = None
            if (rec.get("error") or {}).get("reload_button"):
                clicked = json.loads(self._page.eval(_PLAYER_ERROR_RELOAD_JS) or "null")
            if clicked and clicked.get("reload_button"):
                r["method"] = "reload_button"
            else:
                r["method"] = "navigate"
                r["t_nav"] = time.time()
                self._page.navigate(rec["url"])
                _wait_for_load(self._page, timeout=15)
            r["t_enter"] = time.time()
            ok, detail = _confirm_advancing(self._page, stuck.REENTRY_CONFIRM_S)
            r.update(ok=bool(ok), t_playing=time.time() if ok else None, confirm=detail,
                     confirm_s=stuck.REENTRY_CONFIRM_S, url_after=self._page.eval("location.href"))
            if ok:
                self._check_reentry_identity(r)
            if r.get("ok") and r["method"] == "navigate":
                r["setup"] = self._after_reentry_setup()
        except Exception as exc:
            r.update(ok=False, error_reentry=repr(exc))
        r["outcome"] = "continue" if r.get("ok") else "player_error"
        self.player_dead_reentries.append(r)
        return r

    def _reenter_frozen(self, rec: dict) -> dict:
        """The one stuck-player re-entry: the same room (tt, through _reenter_room's confirmation) or channel (yt /
        twitch: navigate, confirm the playhead advancing within stuck.REENTRY_CONFIRM_S, redo the entry setup)."""
        if self.platform == "tiktok":
            self._room_grace_until = time.time() + stuck.REENTRY_CONFIRM_S + 30
            r = self._reenter_room({"room": rec.get("url"), "dead_s": None, "dead_window": None}, reason="player_frozen")
            r.update({k: v for k, v in rec.items() if k not in ("url",)})
            self.frozen_reentries.append(r)
            self._room_grace_until = time.time() + 15
            return r
        r = {**rec, "t_nav": time.time(), "reason": "player_frozen"}
        try:
            self._page.navigate(rec["url"])
            _wait_for_load(self._page, timeout=15)
            r["t_enter"] = time.time()
            ok, detail = _confirm_advancing(self._page, stuck.REENTRY_CONFIRM_S)
            r.update(ok=bool(ok), t_playing=time.time() if ok else None, confirm=detail,
                     confirm_s=stuck.REENTRY_CONFIRM_S, url_after=self._page.eval("location.href"))
            if ok:
                self._check_reentry_identity(r)
            if r.get("ok"):
                r["setup"] = self._after_reentry_setup()
        except Exception as exc:
            r.update(ok=False, error=repr(exc))
        r["outcome"] = "continue" if r.get("ok") else "player_frozen"
        self.frozen_reentries.append(r)
        return r

    def _entry_quality_pick(self) -> None:
        """Q am. 10 (entry_quality.py): the once-per-session pick at the unshaped entry, before exp.py starts the shaper;
        then the settle trace in the background. A failure is recorded; after a click the state decides auto_restored."""
        import entry_quality as EQ
        drv = EQ.Driver(self._page, self.platform)
        try:
            rec = EQ.entry_pick(drv, self.entry_quality_cfg.get("snapshot"), getattr(self, "_t_entry", None))
        except Exception as exc:
            rec = {"rule": EQ.ENTRY_RULE, "error": repr(exc)[:200], "selected": None, "menu_available": None}
            try:
                rec["auto_restored"], rec["verify"] = EQ.verify_auto(self.platform, drv.state())
            except Exception:
                rec["auto_restored"] = False
        self.entry_quality = rec
        print(f"[browser] Entry quality: selected {rec.get('selected')}, auto_restored {rec.get('auto_restored')}, "
              f"{rec.get('reason') or ''}", flush=True)
        if rec.get("t_auto"):
            def settle() -> None:
                rec["settle_trace"] = EQ.settle_trace(EQ.Driver(self._page, self.platform), stop=self._stop.is_set)
                rec["t_settle_s"] = EQ.t_settle(self.platform, rec["settle_trace"], rec["t_auto"],
                                                (rec.get("choice") or {}).get("target_height"))
            threading.Thread(target=settle, daemon=True, name="entry-quality-settle").start()

    def _after_reentry_setup(self) -> dict:
        """What start() did after the entry that a page load undoes: the chat closed, yt captions off and theater mode,
        twitch Video Stats; and the geometry after it."""
        out: dict = {}
        try:
            if self.close_chat and self.platform in _CLOSE_CHAT_JS:
                time.sleep(2)
                out["chat"] = self._page.eval(_CLOSE_CHAT_JS[self.platform])
            if self.platform == "youtube":
                out["captions"] = self._page.eval(_CAPTIONS_OFF_JS)
                if (self.geometry_target or "").startswith("theater"):
                    out["theater"] = yt_theater(self._page)
                if (self.geometry_target or "").startswith("fullscreen"):     # a page load leaves fullscreen
                    try:
                        tid = next((t.get("id") for t in _targets() if t.get("webSocketDebuggerUrl") == self._ws_url), None)
                    except Exception:
                        tid = None
                    out["fullscreen"] = yt_fullscreen_held(self._page, target_id=tid)
            if self.platform == "twitch":
                out["video_stats"] = self._page.eval(_TWITCH_VIDEO_STATS_JS, await_promise=True)
            if (self.entry_quality or {}).get("t_auto"):   # Q am. 10: a failed restore persists in the profile
                import entry_quality as EQ
                out["entry_quality"] = EQ.reentry_check(EQ.Driver(self._page, self.platform))
            out["geometry"] = json.loads(self._page.eval(_GEOMETRY_JS) or "null")
        except Exception as exc:
            out["error"] = repr(exc)[:200]
        return out

    def _on_wall(self) -> None:
        if self.abort_reason is not None:     # hardening stop (coverage audit): the first abort stands, as every setter
            return
        self.abort_reason, self.abort_at = "login_wall", time.time()
        print("[browser] LOGIN WALL detected (content.js login_wall) — aborting the session", flush=True)

    def _clock_sample(self, reason: str) -> None:
        """v3 A11 (t67; levi n5): one time.time <-> page clock (performance.timeOrigin + now, the label events' ts) sample:
        the page clock read between two time.time reads, kept with that round trip, so every Python record (presses,
        net_labels, shots) joins the label clock by a measured offset. At start, every CLOCK_SAMPLE_S, and at stop."""
        if not hasattr(self, "clock_offsets"):
            self.clock_offsets = []
        if getattr(self, "_page", None) is None or len(self.clock_offsets) >= WATCH_LOG_MAX:
            return
        try:
            t0 = time.time()
            js = float(self._page.eval("performance.timeOrigin + performance.now()"))
            t1 = time.time()
        except Exception as exc:
            self.clock_offsets.append({"reason": reason, "t": time.time(), "error": repr(exc)[:120]})
            return
        mid = (t0 + t1) / 2 * 1000.0
        self.clock_offsets.append({"reason": reason, "t": t1, "js_ms": round(js, 3), "py_mid_ms": round(mid, 3),
                                   "offset_ms": round(js - mid, 3), "rtt_ms": round((t1 - t0) * 1000.0, 3)})

    def _hold_lifted(self, now: float, frozen_s: float, clock: str) -> float | None:
        """levi 13:09 / 17:51 / 18:16 (tw_100109): a low-state hold on a frozen clock (addendum N's or the stuck rule's T2)
        lifts only once the resumer's polls have shown the player paused with data and flat for PAUSE_DEADLOCK_S (the
        press trigger's test: readyState >= 3, buffer >= the floor, buffer and playhead flat), so a stall that starts
        recovering is never cut; the clocks then count from the lift. A last poll older than FROZEN_HOLD_POLL_MAX_AGE_S:
        held. Returns the lift time, else None; each lift is recorded once per frozen run and clock (meta.json
        frozen_hold.bypassed)."""
        r = getattr(self, "resumer", None)
        last = getattr(r, "last_poll", None)
        since = getattr(r, "data_flat_since", None)
        if last is None or now - last[0] > FROZEN_HOLD_POLL_MAX_AGE_S or since is None or now < since + PAUSE_DEADLOCK_S:
            return None
        lift = since + PAUSE_DEADLOCK_S
        run0 = round(now - frozen_s)
        if not any(x.get("clock") == clock and abs(x.get("frozen_since", 0) - run0) <= 2 for x in self.frozen_hold_bypassed) \
                and len(self.frozen_hold_bypassed) < WATCH_LOG_MAX:
            st = last[1]
            self.frozen_hold_bypassed.append({"clock": clock, "t": now, "frozen_s": round(frozen_s, 1), "frozen_since": run0,
                                              "data_flat_since": since, "lift": lift,
                                              **{k: st.get(k) for k in ("paused", "rs", "buf", "stall")}})
        return lift

    def _on_frozen(self, frozen_s: float) -> bool:
        # addendum N: the player froze in a stall and has not recovered within T; no reload (that would be a user
        # action inside the session). exp.py exits EXIT_PLAYER_FROZEN, which the batch does not count as a failure.
        # levi gate 49d (C), yt_900027: on yt / twitch this clock holds in the low states as the T2 rule does
        # (_stuck_tick): no abort while held, then T counts from the hold's end + grace; False = held, not aborted.
        # levi 13:09 / 18:16 (tw_100109): the hold lifts after 30 s of polls paused with data and flat (_hold_lifted); a
        # frozen player with data in a low state is not the network's, and T counts from the lift (frozen_hold.bypassed)
        if self.platform in PLAYER_ERROR_PLATFORMS:
            now = time.time()
            he = self._low_hold_end(now)
            lift = self._hold_lifted(now, frozen_s, "addendum_n") if he is not None else None
            if lift is not None:                          # levi 18:16: T counts from the lift
                he, frozen_s = None, min(frozen_s, now - lift)
                if frozen_s <= FROZEN_PLAYHEAD_MAX_S[self.platform]:
                    return False
            if he is not None:
                if now <= he or min(frozen_s, now - he) <= FROZEN_PLAYHEAD_MAX_S[self.platform]:
                    return False
                frozen_s = min(frozen_s, now - he)
        if self.abort_reason is None:
            self.abort_reason, self.abort_at = "player_frozen", time.time()
        print(f"[browser] PLAYER FROZEN: playhead frozen {frozen_s:.0f} s in a stall > T "
              f"{FROZEN_PLAYHEAD_MAX_S.get(self.platform)} s — aborting the session", flush=True)
        return True

    def stop(self) -> None:
        self.resumer.stop()                        # t83 (3): no press in the teardown (exp.py stops it before meta too)
        k = FS_KEEP.pop("keeper", None)                 # t53 (a): the keeper ends with the session, its record kept
        if k is not None:
            k.stop()
            self.fs_keeper = {**(self.fs_keeper or {}), **k.record()}
        self._clock_sample("stop")                 # v3 A11: the last offset, before the tab closes
        self._mse_wrap_settle_once("stop")        # levi 00:06 (b): the join never ran (no-op when it settled)
        self._worker_anchor_stop()                 # t100 R1: its last batch posted before the final flush
        # Read QoE events from the extension service worker before closing the tab
        if self._session_dir:
            _final_flush(self._page)
            _export_via_service_worker(self._session_dir)

        # v3 A12: the labels are exported; new requests now fail while in-flight responses land (bytes flat or 5 s), so
        # Page.close no longer cuts the last media body short (yt_100048)
        drain = None
        if self._page and getattr(self, "_ws_url", None):   # a start that failed early never set it
            try:
                drain = CaptureDrain(self._ws_url, _shaped_rx_bytes if RX_OWN_IFB else _rx_bytes, room_rx_source())
                self.drain = drain.run()
            except Exception as exc:
                self.drain = {"blocked": False, "error": repr(exc)[:200]}
            print(f"[browser] drain: {self.drain}", flush=True)

        # Close the tab — triggers chrome.tabs.onRemoved in the extension
        if self._page:
            try:
                self._page.call("Page.close")
            except Exception:
                pass
        if drain is not None:
            drain.close()                          # interception off only after the tab is gone

        self._stop.set()
        if self._master is not None:
            self._master.stop()
        if self._tt_room is not None:
            self._tt_room.stop()
        if self._media is not None:
            self._media.stop()
            self.media_decoders = self._media.summary()
        if self._rotator:
            self._rotator.join(timeout=5)
        if self._watcher:
            self._watcher.join(timeout=5)
        if self._shooter:
            self._shooter.join(timeout=5)
        if self._page:
            self._page.close()

        print("[browser] Session stopped", flush=True)
