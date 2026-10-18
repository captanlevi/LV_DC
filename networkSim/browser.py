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
import re
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path

import websocket  # websocket-client

from yt_channels import YT_CHANNELS, url_of
from check_session import FROZEN_PLAYHEAD_MAX_S, PLAYER_DEAD_MAX_S
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
    """Channel URLs of the last n twitch sessions (data/twitch and data/_failed/twitch, by meta.json started_at)."""
    if session_dir is None:
        return []
    root = Path(session_dir).resolve().parent.parent
    metas = []
    for d in list((root / "twitch").glob("*")) + list((root / "_failed" / "twitch").glob("*")):
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


def _pick_twitch_js(exclude: list[str]) -> str:
    """Click the directory's first live card whose channel is not in `exclude`; returns {pick, candidates} as JSON."""
    return """
(function(exclude) {
    const norm = (h) => (h || '').split('?')[0].replace(/\\/$/, '').toLowerCase();
    const links = [...document.querySelectorAll('a[data-a-target="preview-card-image-link"], a[data-a-target="preview-card-title-link"]')]
        .filter(a => a.offsetParent !== null && !/\\/(directory|videos|clip)\\//.test(a.href || ''));
    const cands = [...new Set(links.map(a => norm(a.href)))].filter(Boolean);
    const pick = cands.find(h => !exclude.includes(h)) || null;
    if (pick) links.find(a => norm(a.href) === pick).click();
    return JSON.stringify({pick: pick, candidates: cands.slice(0, 20)});
})(%s)
""" % json.dumps(exclude)


def recent_tt_rooms(session_dir: Path | None, n: int = TT_RECENT_ROOMS_N) -> list[str]:
    """Rooms of the last n tt sessions (data/tiktok and data/_failed/tiktok, by meta.json start time), normalised."""
    if session_dir is None:
        return []
    root = Path(session_dir).resolve().parent.parent
    metas = []
    for d in list((root / "tiktok").glob("*")) + list((root / "_failed" / "tiktok").glob("*")):
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
    PLAYER_DEAD_MAX_S["tiktok"] -> "player_dead". No exemption: a reconnecting player gets its first bytes within
    seconds (old data: every 0x0 run that recovered was <= 20 s); one that recovers inside the limit is a discontinuity."""

    def __init__(self, t_max_s: float | None = None) -> None:
        self.t_max_s = t_max_s if t_max_s is not None else FROZEN_PLAYHEAD_MAX_S.get("tiktok", 720)
        self.t_dead_s = PLAYER_DEAD_MAX_S.get("tiktok", 60)
        self.dead_since: float | None = None          # player_dead clock
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

    def feed(self, s: dict | None, now: float, shaper_state: int | None = None, rx_bytes: int | None = None,
             shaped_rx_bytes: int | None = None) -> str | None:
        s = s or {}
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
        if not dead_now:
            self.dead_since = None
        elif self.dead_since is None:
            self.dead_since = now
        elif now - self.dead_since >= self.t_dead_s:
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
                  or (shaper_state in TT_LOW_STATES))
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


def _tt_confirm_playing(page: "CDPPage", seconds: float, trace: list | None = None) -> tuple[bool, dict]:
    """src set, readyState >= 3 and the playhead advancing >= 2 s within `seconds` (no dead signal). With `trace`, one
    (t, rs, has_video, loading) per sample is appended (re-entry diagnostics)."""
    w, first_ct, s = TTRoomWatch(), None, {}
    page.eval(_CLICK_PLAY_JS)                         # autoplay may not have started: click, then give it the grace
    time.sleep(TT_AUTOPLAY_GRACE_S)
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.eval(_CLICK_PLAY_JS)
        s = _tt_state(page)
        if trace is not None:
            trace.append((round(time.time(), 2), s.get("rs"), bool(s.get("has_video")), bool(s.get("loading"))))
        dead = w.feed(s, time.time())
        if dead:
            return False, {"dead": dead, **s}
        if s.get("has_video") and (s.get("rs") or 0) >= 3 and s.get("ct") is not None:
            first_ct = s["ct"] if first_ct is None else first_ct
            if s["ct"] - first_ct >= 2.0:
                return True, s
        time.sleep(1)
    return False, {"dead": "not_playing", **s}


def _tt_preroll(page: "CDPPage", seconds: float) -> tuple[bool, dict]:
    """The pre-roll before the session clock: no dead signal for `seconds`, playhead advancing >= half of it and media
    appended (buffered end moved)."""
    w, s, adv, prev = TTRoomWatch(), {}, 0.0, _tt_state(page).get("ct")
    end = time.time() + seconds
    while time.time() < end:
        s = _tt_state(page)
        dead = w.feed(s, time.time())
        if dead:
            return False, {"dead": dead, **s}
        ct = s.get("ct")
        if ct is not None and prev is not None and ct > prev:
            adv += ct - prev                          # cumulative: a player reconnect restarts the playhead at 0
        prev = ct if ct is not None else prev
        time.sleep(1)
    # media arriving = the playhead advancing at play rate (tt's MediaStream player exposes no buffer or decoded bytes)
    ok = adv >= seconds / 2
    return ok, {"ct_advance_s": adv, "buf_end": s.get("buf_end"), "decoded": s.get("decoded"),
                **({} if ok else {"dead": "preroll_short"})}


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
        _browser_call("Browser.setWindowBounds", {"windowId": wid, "bounds": {"left": 0, "top": 0, "width": w, "height": h}})
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
                  "login_wall_text", "login_wall", "quality_countdown", "paused", "playhead_still_s", "ready_state"]
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


def _navigate_to_live(page: CDPPage, platform: str, timeout: float = 45.0, first_url: str | None = None,
                      exclude: list[str] | None = None, record: dict | None = None) -> str | None:
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
        if platform == "twitch" and "/directory/" not in url:
            pick_js = "null"          # a channel page (esports rotation): no card to click; never click away from it
        if pick_js.strip() != "null":
            card_js = _CARD_READY_JS.get(platform)
            if card_js:
                # Poll until at least one card link is visible (max 15s); the pick otherwise runs before the list renders
                card_deadline = time.time() + 15
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
            else:
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
    return JSON.stringify({paused: v.paused, rs: v.readyState, ended: v.ended, ad: d.lvqoeAd === '1',
                           stall: d.lvqoeStall === '1'});
})()
"""
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
SELF_PAUSE_HOLD_S = 3.0


class SelfPauseResumer:
    """When to press play (levi 2026-10-17). Eligible = not an ad, paused, not ended, readyState >= 3.
    - after_stall: the stall label is on (the player paused itself in a stall and has data again) -> press at once,
      so the stall ends at the element's own playing -> start, as before;
    - no_stall: eligible without a stall label for more than SELF_PAUSE_HOLD_S (clear of tick jitter and of the stall
      path) -> press.
    A press that does not take is retried only after another SELF_PAUSE_HOLD_S. Each press returns a log record with
    the reason and paused_s, the time since the element was first seen paused."""

    def __init__(self, hold_s: float = SELF_PAUSE_HOLD_S):
        self.hold_s = hold_s
        self.paused_since = None      # first observation of the current pause
        self.eligible_since = None    # start of the current eligible run (or of the wait after a press)
        self.stall_seen = False       # stall label on at any observation of the current pause

    def step(self, state: dict | None, now: float) -> dict | None:
        if not state or not state.get("paused"):
            self.paused_since = self.eligible_since = None
            self.stall_seen = False
            return None
        if self.paused_since is None:
            self.paused_since = now
        self.stall_seen = self.stall_seen or bool(state.get("stall"))
        if state.get("ad") or state.get("ended") or (state.get("rs") or 0) < 3:
            self.eligible_since = None
            return None
        if self.eligible_since is None:
            self.eligible_since = now
            if not state.get("stall"):                   # after_stall presses at once; no_stall starts its hold
                return None
        elif now - self.eligible_since <= self.hold_s:
            return None
        self.eligible_since = now                        # retry only after another hold
        return {"action": "resume_after_self_pause", "reason": "after_stall" if self.stall_seen else "no_stall",
                "paused_s": round(now - self.paused_since, 3)}


# addendum N (fixed 2026-10-18): content.js's frozen-playhead run, in seconds (= check_session.frozen_runs); the
# backstop watchdog and the stuck-player rule (stuck.py) read the same value
_FROZEN_JS = "Number(document.documentElement.dataset.lvqoeFrozenS || 0)"
TICK_STARTUP_TIMEOUT_S = 15


_USHER_RE = re.compile(r"usher\.ttvnw\.net/api/(?:v2/)?channel/hls/[^/?]+\.m3u8")


LADDER_PROBE_FROZEN_S = 60.0   # twitch, master unread: the quality menu is read only inside a frozen run this long
LADDER_PROBE_ATTEMPTS = 2      # ... once per frozen run, a second attempt only when the first raised
MEDIA_EVENTS_FILE = "media_events.jsonl"
MEDIA_MESSAGE_LEVELS = ("error", "warning", "info")   # playerMessagesLogged kept (debug dropped: volume)
MEDIA_RECENT_N = 40
MEDIA_SNAPSHOT_MAX_S = 2.0   # the dead-trigger snapshot's cap: it delays the re-entry (levi's audit of 1bcdc5e)


class MediaListener:
    """tt: Chrome's media-internals over CDP (the Media domain) by a second CDP client on the page, recording only
    (supervisor 2026-10-18, peter's ti_43 read: the player reset to readyState 0 while the FLV stream kept arriving;
    this tells a decoder error, e.g. the VA-API HEVC path, from a player-side fault on the next death). Every player's
    properties (kVideoDecoderName, kIsPlatformVideoDecoder, kResolution, ...), events, errors and its error / warning /
    info messages go to <session>/media_events.jsonl; snapshot() is attached to the player_dead record. No Chrome flag."""

    def __init__(self, ws_url: str, path: Path | None):
        self.ws_url, self.path, self.error = ws_url, path, None
        self.players: dict[str, dict] = {}
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
        return rec

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
        elif m == "Network.responseReceived" and _USHER_RE.search((prm.get("response") or {}).get("url", "")):
            self._wanted[(sid, prm.get("requestId"))] = prm["response"]["url"]
        elif m == "Network.loadingFinished" and (sid, prm.get("requestId")) in self._wanted:
            url = self._wanted.pop((sid, prm["requestId"]))
            self._pending[send("Network.getResponseBody", {"requestId": prm["requestId"]}, sid)] = (sid, url)
        elif msg.get("id") in self._pending:
            (req_sid, url), res = self._pending.pop(msg["id"]), msg.get("result") or {}
            body = res.get("body") or ""
            if res.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", "replace")
            kbit = stuck.rung_from_master(body)
            if kbit:
                self.latest = {"kbit": kbit, "t": time.time(), "path": urlsplit(url).path, "from_worker": req_sid is not None}

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


def _confirm_advancing(page: "CDPPage", seconds: float) -> tuple[bool, dict]:
    """After a re-entry (yt / twitch): play pressed while paused; the playhead advancing >= 2 s at readyState >= 3 within
    `seconds`."""
    first, st = None, {}
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
            if ct - first >= 2.0:
                return True, {"ct": ct}
        time.sleep(1)
    return False, {"not_playing": True, **{k: st.get(k) for k in ("paused", "readyState", "rs") if k in st}}


def _ad_watcher_loop(page: CDPPage, platform: str, stop: threading.Event, log_path: Path | None = None,
                     on_wall=None, on_frozen=None, on_stuck=None) -> None:
    """Run the platform's ad action once a second; every action is logged with a timestamp (ad_actions.jsonl).
    Also watches for the login wall content.js marks (t37) and calls on_wall() once when it appears, and (addendum N)
    calls on_frozen(seconds) once when the frozen-playhead run passes the platform's T (check_session table); passes the
    same frozen run to on_stuck(seconds) every second (the stuck-player rule, stuck.py). Presses play on a self-paused
    player (SelfPauseResumer)."""
    skip_js = _SKIP_AD_JS.get(platform, "null")
    walled = False
    frozen_max_s = FROZEN_PLAYHEAD_MAX_S.get(platform)
    froze = False
    resumer = SelfPauseResumer()
    n = 0
    while not stop.wait(timeout=1):
        n += 1
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
            if press and page.eval(_PLAY_JS) and log_path is not None:
                with open(log_path, "a") as fh:
                    fh.write(json.dumps({"ts": now, "platform": platform, **press}) + "\n")
        except Exception:
            pass
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
        if on_frozen is not None or on_stuck is not None:
            try:
                frozen_s = float(page.eval(_FROZEN_JS) or 0)
            except Exception:
                frozen_s = 0.0
            if on_stuck is not None:
                try:
                    on_stuck(frozen_s)
                except Exception as exc:
                    print(f"[browser] stuck-player check failed: {exc!r}", flush=True)
            if on_frozen is not None and frozen_max_s is not None and not froze and frozen_s > frozen_max_s:
                froze = True
                on_frozen(frozen_s)


SHOT_INTERVAL_S = 10.0
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
        if sw and sh:                                  # never grab beyond the screen
            bx, by = min(bx, sw), min(by, sh)
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
_PLAYER_RECT_JS = """
(function() {
    const vs = [...document.querySelectorAll('video')].filter(v => v.offsetParent !== null);
    const v = vs.sort((a, b) => b.clientWidth * b.clientHeight - a.clientWidth * a.clientHeight)[0];
    const inner = [window.innerWidth, window.innerHeight], dpr = window.devicePixelRatio || 1;
    let player = null;
    if (v) { const r = v.getBoundingClientRect(); player = {rect: [r.x, r.y, r.width, r.height], inner, dpr}; }
    return JSON.stringify({player, viewport: inner, dpr, fullscreen: !!document.fullscreenElement,
                           screen: [screen.width, screen.height]});
})()
"""


def _screenshot_loop(page: CDPPage, shots_dir: Path, stop: threading.Event, origin: dict | None = None) -> None:
    """A small JPEG of the tab every SHOT_INTERVAL_S ('cadence'), plus one at every tier change and every ad start
    (t37: content.js raises <html data-lvqoe-shot-req="<n>|<reason>|<event_id>">, polled each second). Each shot is
    stamped with the extension's latest tick (<html data-lvqoe-tick>) and its reason, for the human label-vs-screen
    check. Nothing here feeds labels. The loop starts after navigation: a pre-roll that ends during navigation has no
    event shot (laptop run8, yt), and two events within one poll second give one shot (the later reason)."""
    import base64
    shots_dir.mkdir(parents=True, exist_ok=True)
    index = shots_dir / "index.jsonl"
    last_cadence, last_req = time.time(), None
    off = (origin or {}).get("offset") if (origin or {}).get("accepted") else None
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
            # no clip / scale: a scaled clip makes Chrome emulate device metrics for the capture, a visible re-render
            # (rushi saw the screen flicker; dropped frames per tick near shots were 0.153 vs 0.023 elsewhere on yt_10).
            # The surface is grabbed as is and downscaled here.
            t_decide = time.time()
            try:
                pr_ = json.loads(page.eval(_PLAYER_RECT_JS) or "null") or {}
            except Exception:
                pr_ = {}
            player, viewport = pr_.get("player"), {"inner": pr_.get("viewport"), "dpr": pr_.get("dpr")}
            data, capture, capture_px, shot_px, region, crop = None, None, None, None, None, "viewport"
            t_js = t_grab = None
            if SHOT_SOURCE == "x11":
                fs = _fullscreen_on_screen(pr_) if (origin or {}).get("fullscreen_screen") else None   # t37
                try:
                    region = fs or _viewport_on_screen(json.loads(page.eval(_VIEWPORT_JS) or "{}"))
                except Exception:
                    region = None
                if region and not fs and off:          # the calibrated origin (t37): X11 regions only
                    region = (region[0] + off[0], region[1] + off[1], region[2], region[3])
                t_js = time.time()
                # the player rectangle only (supervisor 2026-10-17: the smallest XGetImage), else the viewport
                pr = _player_on_screen(region, player) if region else None   # the player within it (hot-fix 2)
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
            with open(index, "a") as fh:
                fh.write(json.dumps({"ts": ts, "file": name, "tick": tick_id, "tick_ts": tick_ts, "reason": reason,
                                     "request": req if reason != "cadence" else None, "player": player,
                                     "viewport": viewport,
                                     "capture_px": capture_px, "shot_px": shot_px, "capture": capture,
                                     "screen_region": region, "crop": crop, "fullscreen": bool((pr_ or {}).get("fullscreen")),
                                     "origin_offset": off if capture == "x11" and not (pr_ or {}).get("fullscreen") else None,
                                     "shot_origin_uncalibrated": capture == "x11" and not (pr_ or {}).get("fullscreen")
                                                                 and off is None,
                                     # per-step timing (tw_10: the near-shot dropped frames sit on the tick about
                                     # 1 s before ts; these say which step that is)
                                     "t_decide": t_decide, "js_s": None if t_js is None else round(t_js - t_decide, 4),
                                     "grab_s": None if t_grab is None else round(t_grab - (t_js or t_decide), 4),
                                     "encode_save_s": round(ts - (t_grab or t_decide), 4)}) + "\n")
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
                 entry_quality: dict | None = None):
        self.platform = platform
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
        self.room_ended: dict | None = None       # tt: the dead-room abort's detail (meta.json)
        self.player_dead: dict | None = None      # tt: the dead-player abort's detail (meta.json)
        self.player_dead_reentries: list[dict] = []   # tt: the re-entries after a player_dead (meta.json)
        self.frozen_reentries: list[dict] = []    # the stuck-player re-entry (Addendum Q am. 6; meta.json)
        self.censored: dict | None = None         # the stuck-player censoring (am. 6): time, reason, shaper state
        self.lowest_rung: dict | None = None      # the stuck-player rule's rung (kbit, source; meta.json)
        self.decode_gpu: dict | None = None       # Chrome's GPU video-decode report at launch (decode_path.py; meta.json)
        self.decode_page: dict | None = None      # the page's HEVC probe (MSE / WebCodecs hvc1)
        self._master: MasterListener | None = None   # twitch: the master playlist listener
        self.t2_skipped: list[dict] = []            # stuck rule: T2_S frozen with only the fallback rung (no T2)
        self.player_ladder: dict | None = None      # twitch: the quality menu's options when the master was not read
        self.shot_origin: dict | None = None        # the X11 shot origin calibration (t37; calibrate_shot_origin)
        self.rung_probes: list[dict] = []           # twitch: each menu read (open / close times): shots near it are flagged
        self._media: MediaListener | None = None     # tt: the media-internals recorder (media_events.jsonl)
        self._rung_probe_at = 0.0
        self._recovery_lock = threading.Lock()    # one recovery of either kind per session (am. 6)
        self.tw_pick: dict | None = None          # twitch: directory candidates, recent channels excluded, the pick
        self.room_play_clicks = 0                 # tt: play clicks on a paused, flat player (room watcher)
        self.room_discontinuities: list = []      # tt: player source drops that came back (room watcher)
        self.net_labels: list = []                # exp.py's shaper episodes (the current state for the room watcher)
        self._room_grace_until = 0.0              # tt: the room watcher pauses while the rotation moves rooms
        self._room_watcher: threading.Thread | None = None

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

        if self.platform == "tiktok":
            self._media = MediaListener(self._ws_url, (Path(self._session_dir) / MEDIA_EVENTS_FILE)
                                        if self._session_dir else None).start()
        if self.platform == "tiktok" and not self.first_url:
            self.list_url = self._tt_choose_room()
        elif self.platform == "twitch":
            self._master = MasterListener(self._ws_url).start()
            self.tw_pick = {}
            self.list_url = _navigate_to_live(self._page, self.platform, first_url=self.first_url,
                                              exclude=recent_tw_channels(self._session_dir), record=self.tw_pick)
        else:
            self.list_url = _navigate_to_live(self._page, self.platform, first_url=self.first_url)
        if not self.list_url:
            raise RuntimeError(f"Failed to navigate to a live {self.platform} stream")

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
                    self.player_pin["fullscreen"] = (yt_fullscreen(self._page, target_id=tid) if self.platform == "youtube"
                                                     else "not yt")
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
                  self._stuck_tick if self.platform in stuck.FALLBACK_RUNG_KBIT else None),
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
                args=(self._page, self._session_dir / "shots", self._stop, self.shot_origin),
                daemon=True,
                name="screenshots",
            )
            self._shooter.start()
            print(f"[browser] Screenshots every {SHOT_INTERVAL_S:.0f} s → {self._session_dir / 'shots'}", flush=True)

    def _tt_choose_room(self) -> str | None:
        """tt: pick a live, playing room that no recent session used (see TT_* above); returns the list URL or None."""
        page = self._page
        recent = recent_tt_rooms(self._session_dir)
        rec: dict = {"n_recent_sessions": TT_RECENT_ROOMS_N, "recent_excluded": recent, "min_viewers": TT_MIN_VIEWERS,
                     "candidates": [], "tries": [], "choice": None}
        self.tt_room_choice = rec
        page.navigate(ROOM_ROTATION_LIVE_URL)
        _wait_for_load(page, timeout=15)
        deadline = time.time() + 15
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
            _wait_for_load(page, timeout=15)
            href = page.eval("location.href")
            if href and "/@" in href and _norm_room(href) not in set(recent):
                order = [{"href": href, "viewers": None, "live_badge": None, "fallback": True}]
        print(f"[browser] tt rooms: {len(cands)} candidates, {sum(1 for c in cands if c['recent'])} used in the last "
              f"{TT_RECENT_ROOMS_N} sessions", flush=True)
        for c in order[:TT_MAX_TRIES]:
            t: dict = {"room": c["href"], "viewers": c["viewers"], "live_badge": c["live_badge"], "t_nav": time.time()}
            try:
                if not c.get("fallback"):
                    page.navigate(c["href"])
                    _wait_for_load(page, timeout=15)
                ok, detail = _tt_confirm_playing(page, TT_QUICK_CONFIRM_S)
                if ok:
                    t["t_playing"] = time.time()
                    ok, detail = _tt_preroll(page, TT_PREROLL_S)
            except Exception as exc:
                ok, detail = False, {"dead": "error", "error": repr(exc)}
            t.update(ok=ok, detail=detail, t_done=time.time())
            rec["tries"].append(t)
            print(f"[browser] tt room {c['href']} ({c['viewers']} viewers): {'OK' if ok else detail.get('dead')}", flush=True)
            if ok:
                rec["choice"] = c["href"]
                return ROOM_ROTATION_LIVE_URL
        return None

    def _room_watch_loop(self) -> None:
        """tt: abort the session with reason room_ended when the room dies mid-session, or player_dead when the player
        sits at readyState 0 with no media (TTRoomWatch)."""
        w = TTRoomWatch()
        while not self._stop.wait(timeout=1):
            if time.time() < self._room_grace_until:
                w = TTRoomWatch()
                continue
            s = _tt_state(self._page)
            labels = getattr(self, "net_labels", None)
            try:
                state = int(labels[-1].state) if labels else None
            except (ValueError, AttributeError, TypeError):
                state = None
            # tt can pause itself (ti_8: a "pause" event, then flat until room_ended): click play while paused and flat,
            # before the no-media rule can fire; each click is counted in meta.json room_play_clicks
            if s.get("paused") and w.flat_since is not None and time.time() - w.flat_since >= TT_AUTOPLAY_GRACE_S:
                try:
                    self._page.eval(_CLICK_PLAY_JS)
                    self.room_play_clicks += 1
                except Exception:
                    pass
            dead = w.feed(s, time.time(), shaper_state=state, rx_bytes=_rx_bytes(), shaped_rx_bytes=_shaped_rx_bytes())
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
                        used = len(self.player_dead_reentries) + len(self.frozen_reentries)
                        r = self._reenter_room(pd) if dead_action(used) == "reenter" else None
                    if r is not None:
                        print(f"[browser] PLAYER_DEAD: re-entered the room ({r['outcome']})", flush=True)
                        if r["outcome"] == "continue":
                            w = TTRoomWatch()
                            continue
                        if r["outcome"] == "room_ended":
                            why = "room_ended"
                            self.room_ended = {**rec, "reason": "ended_page", "after_player_dead_reentry": True}
                    if why == "player_dead":
                        self.player_dead = {**pd, "reentries": len(self.player_dead_reentries)}
                else:
                    self.room_ended = rec
                if self.abort_reason is None:
                    self.abort_reason, self.abort_at = why, time.time()
                print(f"[browser] {why.upper()} ({dead}) — aborting the session", flush=True)
                return

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
                       confirm={k: detail.get(k) for k in ("dead", "rs", "ct", "ended_text") if k in detail},
                       confirm_s=TT_REENTRY_CONFIRM_S, samples=len(trace),
                       samples_loading=sum(1 for *_, ld in trace if ld),
                       samples_no_video=sum(1 for _, _, hv, _ in trace if not hv),
                       s_to_data=None if rs3 is None else round(rs3 - rec["t_enter"], 1))
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
                visited = [r.get("room") or "" for r in self.room_rotation["rooms"]] + recent_tt_rooms(self._session_dir)
                self._page.navigate(ROOM_ROTATION_LIVE_URL)
                _wait_for_load(self._page, timeout=15)
                deadline = time.time() + 15
                while time.time() < deadline and not self._page.eval(_CARD_READY_JS["tiktok"]):
                    time.sleep(1)
                picked = self._page.eval(_pick_new_room_js(visited))
                rec["picked"], rec["t_enter"] = picked, time.time()
                ok = False
                deadline = time.time() + 30
                while time.time() < deadline and not self._stop.is_set():
                    self._page.eval(_CLICK_PLAY_JS)
                    if _video_is_advancing(self._page):
                        ok = True
                        break
                    time.sleep(1)
                rec.update(t_playing=time.time() if ok else None, room=self._page.eval("location.href"), ok=ok)
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
            used = len(self.player_dead_reentries) + len(self.frozen_reentries)
            if stuck.action(used) == "reenter":
                r = self._reenter_frozen(rec)
                print(f"[browser] STUCK PLAYER: frozen {frozen_s:.0f} s, {t2:.0f} s above the lowest rung "
                      f"({rung.get('kbit')} kbit, {rung.get('source')}): re-entered ({r['outcome']})", flush=True)
                if r["outcome"] == "continue":
                    return
                if r["outcome"] == "room_ended":          # tt: the re-entry found the ended banner (the host ended)
                    self.room_ended = {"reason": "ended_page", "after_frozen_reentry": True, "at": time.time()}
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
                    out["fullscreen"] = yt_fullscreen(self._page, target_id=tid)
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
        self.abort_reason, self.abort_at = "login_wall", time.time()
        print("[browser] LOGIN WALL detected (content.js login_wall) — aborting the session", flush=True)

    def _on_frozen(self, frozen_s: float) -> None:
        # addendum N: the player froze in a stall and has not recovered within T; no reload (that would be a user
        # action inside the session). exp.py exits EXIT_PLAYER_FROZEN, which the batch does not count as a failure.
        if self.abort_reason is None:
            self.abort_reason, self.abort_at = "player_frozen", time.time()
        print(f"[browser] PLAYER FROZEN: playhead frozen {frozen_s:.0f} s in a stall > T "
              f"{FROZEN_PLAYHEAD_MAX_S.get(self.platform)} s — aborting the session", flush=True)

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
        if self._master is not None:
            self._master.stop()
        if self._media is not None:
            self._media.stop()
        if self._rotator:
            self._rotator.join(timeout=5)
        if self._watcher:
            self._watcher.join(timeout=5)
        if self._shooter:
            self._shooter.join(timeout=5)
        if self._page:
            self._page.close()

        print("[browser] Session stopped", flush=True)
