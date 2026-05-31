#!/usr/bin/env python3
"""
Calibrate provider quality thresholds at fixed bandwidths.

For each test speed:
  1. Apply TC shaping at that bandwidth
  2. Navigate to a live stream (skipping ads for up to AD_TIMEOUT_S)
  3. Sample actual video resolution for MEASURE_S seconds
  4. Report the dominant resolution

Output: a table and a suggested SCENARIOS dict for shaping.py

Usage:
    python calibrate.py                              # youtube, default speeds
    python calibrate.py --platform twitch
    python calibrate.py --speeds 500,1000,2000,5000  # kbit/s comma-separated
    python calibrate.py --speed 1500                 # single speed, interactive
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from browser import CDPPage, _wait_for_cdp, _get_page_ws_url, _open_new_tab, _wait_for_load

# ── tunables ─────────────────────────────────────────────────────────────────
DEFAULT_SPEEDS = [250, 450, 650, 900, 1200, 1800, 2500, 3500, 5500, 8000]
AD_TIMEOUT_S   = 60    # max wait for ads to clear
MEASURE_S      = 60    # seconds of clean video to sample
SAMPLE_EVERY_S = 2     # resolution poll interval (more samples in less time)
LIVE_CLICK_S   = 30    # click live-edge button this often
FORCE_QUALITY_ABOVE_KBIT = 1500

PLATFORM_URLS = {
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
        # Esports directories as backup
        "https://www.twitch.tv/directory/game/League%20of%20Legends",
        "https://www.twitch.tv/directory/game/Valorant",
    ],
    "tiktok": ["https://www.tiktok.com/live"],
    "bilibili": ["https://live.bilibili.com/"],
}

# Platform-specific JS to click into a live stream from a directory/listing page
_PICK_STREAM_JS: dict[str, str] = {
    "youtube": "null",  # /live URL already lands on the stream
    "twitch": """
        (function(){
            var sels = [
                'a[data-a-target="preview-card-image-link"]',
                'a[data-a-target="preview-card-title-link"]',
                'article a[href^="/"]',
                'a[href^="/"][data-a-id]',
            ];
            for(var s of sels){
                var a=document.querySelector(s);
                // Skip VODs (/videos/), clips (/clip/), and directory links
                if(a && !a.href.includes('/directory') && !a.href.includes('/videos/') && !a.href.includes('/clip/')){
                    a.click(); return a.href;
                }
            }
            return null;
        })()
    """,
    "tiktok": """
        (function(){
            var sels=[
                '[data-e2e="live-room-info"] a','[data-e2e="live-card-container"] a',
                'a[href*="/@"][href*="/live"]','a[href*="/live"]',
                '[class*="LiveCard"] a','[class*="liveCard"] a',
            ];
            for(var s of sels){
                var el=document.querySelector(s);
                if(el&&el.offsetParent!==null){
                    var a=el.closest('a')||el;
                    a.click(); return a.href||'clicked';
                }
            }
            return null;
        })()
    """,
    "bilibili": """
        (function(){
            var sels=[
                '.room-card-box a[href*="live.bilibili.com"]',
                '.card-wrapper a[href*="live.bilibili.com"]',
                'a[href*="live.bilibili.com/"]',
            ];
            for(var s of sels){
                var a=document.querySelector(s);
                if(a&&a.offsetParent!==null){ a.click(); return a.href; }
            }
            return null;
        })()
    """,
}

# Platform-specific quality forcing after ads clear
_FORCE_QUALITY_JS: dict[str, str] = {
    "youtube": """
        (function(){
            var p=document.querySelector('#movie_player');
            if(!p||typeof p.getAvailableQualityLevels!=='function') return 'no-api';
            var avail=p.getAvailableQualityLevels();
            var prefer=['hd2160','hd1440','hd1080','hd720','large'];
            var chosen=null;
            for(var q of prefer){ if(avail.indexOf(q)>=0){ chosen=q; break; } }
            if(!chosen) return 'avail:'+avail.join(',');
            if(typeof p.setPlaybackQualityRange==='function') p.setPlaybackQualityRange(chosen,chosen);
            return 'forced:'+chosen+' avail:'+avail.join(',');
        })()
    """,
    "twitch": """
        (function(){
            // Click gear → Quality menu → first (highest) option
            try {
                var gear=document.querySelector('[data-a-target="player-settings-button"]');
                if(!gear) return 'no-gear';
                gear.click();
                setTimeout(function(){
                    var qItem=document.querySelector('[data-a-target="player-settings-menu-item-quality"]');
                    if(qItem){ qItem.click();
                        setTimeout(function(){
                            // Pick first quality option (highest)
                            var opts=document.querySelectorAll('[data-a-target="player-settings-submenu-quality-option"] input');
                            if(opts.length>0){ opts[0].click(); }
                        }, 300);
                    }
                }, 300);
                return 'twitch-quality-click';
            } catch(e){ return 'err:'+e.message; }
        })()
    """,
    "tiktok": "null",    # TikTok live has no quality selection
    "bilibili": """
        (function(){
            // Try bilibili quality selector — click 原画 (original) or highest available
            var btns=document.querySelectorAll('.bpx-player-ctrl-quality-menu-item,.squirtle-quality-item');
            if(btns.length>0){ btns[0].click(); return 'bilibili-quality:'+btns[0].textContent.trim(); }
            return 'no-quality-menu';
        })()
    """,
}

# Platform-specific "click live edge" button
_CLICK_LIVE_JS: dict[str, str] = {
    "youtube":  "(function(){var b=document.querySelector('.ytp-live-badge');if(b){b.click();return true;}return false;})()",
    "twitch":   "(function(){var b=document.querySelector('[data-a-target=\"player-seek-live-button\"]');if(b){b.click();return true;}return false;})()",
    "tiktok":   "null",
    "bilibili": "null",
}

# ── JS snippets ───────────────────────────────────────────────────────────────

# Reliable ad detection: live streams have duration=Infinity; ads have finite duration.
# Also check class-based and UI-element signals as belt-and-suspenders.
_JS_IS_AD = """
(function(){
    if(document.querySelector('#movie_player.ad-showing')) return true;
    if(document.querySelector('.ytp-ad-preview-container,.ytp-ad-text,.ytp-ad-badge')) return true;
    var v=document.querySelector('video');
    if(v && isFinite(v.duration) && v.duration > 0 && v.duration < 600) return true;
    return false;
})()
"""

_JS_SKIP = """
(function(){
    var v = document.querySelector('video');
    // Detect ad via any signal
    var adShowing = !!(document.querySelector('#movie_player.ad-showing'));
    var adUI      = !!(document.querySelector('.ytp-ad-preview-container,.ytp-ad-text,.ytp-ad-badge,.ytp-ad-module'));
    var finiteDur = !!(v && isFinite(v.duration) && v.duration > 0 && v.duration < 600);
    var isAd = adShowing || adUI || finiteDur;

    // 1. Click any visible skip button (many selector variants — YouTube rotates them)
    var sel = [
        '.ytp-ad-skip-button', '.ytp-skip-ad-button',
        '.ytp-ad-skip-button-modern', 'button.ytp-ad-skip-button-modern',
        '.ytp-ad-skip-button-container button', '[class*="skip-button"]',
        '.videoAdUiSkipButton'
    ];
    for(var s of sel){
        var b=document.querySelector(s);
        if(b && b.offsetParent!==null){ b.click(); return 'css-skip:'+s; }
    }

    if(!isAd) return null;

    // 2. Text/aria skip (any visible button with "skip" in label)
    for(var btn of document.querySelectorAll('button')){
        if(!btn.offsetParent) continue;
        var lbl=(btn.textContent+' '+(btn.getAttribute('aria-label')||'')).toLowerCase();
        if(lbl.includes('skip') && !lbl.includes('chapter')){ btn.click(); return 'text-skip'; }
    }

    // 3. Fast-forward to end — most reliable, works for skippable AND non-skippable ads
    if(v && isFinite(v.duration) && v.duration > 0){
        v.currentTime = v.duration - 0.1;
        v.play().catch(function(){});
        return 'ff-skip';
    }

    return 'waiting-ad';
})()
"""

_JS_CLICK_PLAY = "(function(){var v=document.querySelector('video');if(v&&v.paused)v.play().catch(function(){});})()"
_JS_QUALITY    = """
(function(){
    var v=document.querySelector('video');
    if(!v||!v.videoWidth) return null;
    var adShowing = !!(document.querySelector('#movie_player.ad-showing'));
    var adUI      = !!(document.querySelector('.ytp-ad-preview-container,.ytp-ad-text,.ytp-ad-badge'));
    var finiteDur = isFinite(v.duration) && v.duration > 0 && v.duration < 600;
    return {w:v.videoWidth, h:v.videoHeight, paused:v.paused,
            ad: adShowing || adUI || finiteDur,
            t:v.currentTime};
})()
"""
_JS_IS_LIVE = """
(function(){
    var v = document.querySelector('video');
    if(!v) return {live: false, reason: 'no-video'};
    var onYT  = location.hostname.includes('youtube.com');
    var onTwi = location.hostname.includes('twitch.tv');

    // YouTube uses a large finite DVR duration (e.g. 14h), not Infinity.
    // Rely on the live badge instead of duration for YouTube.
    if(!onYT && isFinite(v.duration) && v.duration < 86400)
        return {live: false, reason: 'finite-duration:' + Math.round(v.duration) + 's'};

    if(onYT){
        var yt = !!(document.querySelector('.ytp-live-badge, .ytp-live'));
        if(!yt) return {live: false, reason: 'no-yt-live-badge'};
    }

    if(onTwi){
        if(location.href.includes('/videos/') || location.href.includes('/clip/'))
            return {live: false, reason: 'twitch-vod'};
        var twitchLive = !!(document.querySelector('.channel-root--live, .live-time'))
                      || !!(document.querySelector('p[data-a-target="stream-title"]'));
        if(!twitchLive) return {live: false, reason: 'no-twitch-live-indicator'};
    }

    return {live: true, reason: 'ok'};
})()
"""

_JS_ADVANCING  = """
(function(){
    var v=document.querySelector('video');
    if(!v) return false;
    // Just check the video element is playing something (ad or live — doesn't matter here).
    // Ad vs live is handled separately by _JS_IS_AD / _wait_ads_clear.
    if(!(v.currentTime > 0 && !v.paused && v.readyState >= 3)) return false;
    // On Twitch: reject VODs — we only want live streams.
    // VOD pages have a seekbar and no live badge; live pages have the live indicator.
    var onTwitch = location.hostname.includes('twitch.tv');
    if(onTwitch){
        var isVOD = location.href.includes('/videos/') || location.href.includes('/clip/');
        if(isVOD) return false;
        // channel-root--live or stream-title confirms this is a live channel
        var twitchLive = !!(document.querySelector('.channel-root--live, .live-time, [class*="channel-root--live"]'))
                      || !!(document.querySelector('p[data-a-target="stream-title"]'));
        if(!twitchLive) return false;
    }
    return true;
})()
"""

# ── helpers ───────────────────────────────────────────────────────────────────

def _run_make(*args, quiet=True):
    kw = dict(capture_output=True) if quiet else {}
    subprocess.run(["make"] + list(args), check=False, **kw)


def _set_speed(kbit: int, delay_ms: int = 20, loss_pct: float = 0.0):
    _run_make("slow",
              f"BANDWIDTH={kbit}kbit",
              f"DELAY={delay_ms}ms",
              f"PLR={loss_pct}%")


def _get_page(timeout: float = 40.0) -> CDPPage | None:
    if not _wait_for_cdp(timeout):
        return None
    ws = _get_page_ws_url() or _open_new_tab()
    return CDPPage(ws) if ws else None


def _navigate(page: CDPPage, url: str):
    page.call("Page.navigate", {"url": url})
    _wait_for_load(page, timeout=20)
    time.sleep(1.5)
    page.eval(_JS_CLICK_PLAY)


def _wait_video_plays(page: CDPPage, timeout: float = 45.0) -> bool:
    """Return True once a LIVE video is advancing (rejects VODs and non-live pages)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        page.eval(_JS_CLICK_PLAY)
        if page.eval(_JS_ADVANCING):
            # Check it's actually a live stream, not a VOD/ad
            status = page.eval(_JS_IS_LIVE)
            if isinstance(status, dict) and status.get("live"):
                return True
            elif isinstance(status, dict):
                reason = status.get("reason", "?")
                # VOD/clip — bail immediately, no point waiting
                if "vod" in reason or "finite-duration" in reason:
                    print(f"  ✗ not live ({reason})", flush=True)
                    return False
        time.sleep(2)
    return False


def _wait_ads_clear(page: CDPPage, timeout_s: float) -> bool:
    """Keep skipping ads; return True once none are showing."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        page.eval(_JS_SKIP)
        if not page.eval(_JS_IS_AD):
            time.sleep(3)           # confirm it stayed clear
            if not page.eval(_JS_IS_AD):
                return True
        time.sleep(2)
    return False


def _sample_quality(page: CDPPage, duration_s: float, platform: str = "youtube") -> tuple[list, int]:
    """Sample (w,h) tuples while no ad is showing. Returns (samples, total_polls)."""
    samples = []
    total   = 0
    deadline    = time.time() + duration_s
    next_live   = time.time() + LIVE_CLICK_S
    live_js     = _CLICK_LIVE_JS.get(platform, "null")
    while time.time() < deadline:
        if time.time() >= next_live:
            page.eval(live_js)
            next_live = time.time() + LIVE_CLICK_S
        page.eval(_JS_CLICK_PLAY)
        q = page.eval(_JS_QUALITY)
        if q and not q["ad"]:
            total += 1
            if q["w"] > 0:
                samples.append((q["w"], q["h"]))
        time.sleep(SAMPLE_EVERY_S)
    return samples, total


def _dominant_res(samples: list) -> tuple[int, int]:
    if not samples:
        return (0, 0)
    return Counter(samples).most_common(1)[0][0]


_JS_PAGE_OK = """
(function(){
    var t = (document.title || '').toLowerCase();
    // 404 / error pages
    if(t.includes('404') || t.includes('not found') || t.includes('unavailable')) return false;
    // YouTube channel page with no live stream (title is just the channel name, no video)
    // When /live redirects to a live stream the title contains the stream title (longer string)
    // When there is no live stream, it lands on the channel page: title = "ChannelName - YouTube"
    var isChannelPage = !!(document.querySelector('ytd-channel-renderer, #channel-header-container'))
                        && !document.querySelector('video');
    if(isChannelPage) return false;
    // YouTube error overlay
    if(document.querySelector('yt-formatted-string.ytd-background-promo-renderer')) return false;
    return true;
})()
"""

def _find_live(page: CDPPage, urls: list[str], platform: str = "youtube") -> bool:
    """Try each URL until we find one with an advancing live stream."""
    pick_js = _PICK_STREAM_JS.get(platform, "null")
    for url in urls:
        print(f"  → navigating {url}", flush=True)
        _navigate(page, url)
        if not page.eval(_JS_PAGE_OK):
            print(f"  ✗ page error/404", flush=True)
            continue
        # For directory pages, click into first stream card
        if pick_js.strip() != "null":
            clicked = page.eval(pick_js)
            if clicked:
                print(f"  → clicked stream: {str(clicked)[:60]}", flush=True)
                _wait_for_load(page, timeout=15)
                page.eval(_JS_CLICK_PLAY)
                time.sleep(2)
        if _wait_video_plays(page, timeout=60):
            print(f"  ✓ video playing", flush=True)
            return True
        print(f"  ✗ no stream", flush=True)
    return False


# ── core calibration loop ─────────────────────────────────────────────────────

def calibrate(platform: str, speeds: list[int], runs_per_speed: int = 1) -> list[dict]:
    """
    For each speed in `speeds`, run `runs_per_speed` measurement windows
    and return a list of result dicts.
    """
    urls = PLATFORM_URLS.get(platform, [])
    if not urls:
        sys.exit(f"Unknown platform: {platform}")

    _run_make("enable")

    page = _get_page(timeout=60)
    if page is None:
        sys.exit("Could not connect to Chrome CDP — is Chrome running with --remote-debugging-port=9222?")

    results = []

    for kbit in speeds:
        for run in range(runs_per_speed):
            print(f"\n[{kbit} kbit/s  run {run+1}/{runs_per_speed}]", flush=True)

            # On first run at each speed, navigate at full speed so the page loads
            # reliably, then throttle down to test speed before measuring.
            if run == 0:
                _set_speed(5000)   # fast load
                if not _find_live(page, urls, platform):
                    print("  ✗ could not find live stream — skipping", flush=True)
                    results.append({"kbit": kbit, "run": run, "w": 0, "h": 0, "samples": 0,
                                    "total_polls": 0, "stall_pct": 100.0, "ads_cleared": False})
                    continue
                print(f"  throttling to {kbit} kbit/s…", flush=True)
                _set_speed(kbit)   # now apply test speed
            else:
                _set_speed(kbit)

            print(f"  waiting for ads to clear (max {AD_TIMEOUT_S}s)…", flush=True)
            ads_ok = _wait_ads_clear(page, AD_TIMEOUT_S)
            if not ads_ok:
                print(f"  ⚠ ads still showing after {AD_TIMEOUT_S}s", flush=True)

            # Force max quality — ABR is very conservative on most platforms
            force_js = _FORCE_QUALITY_JS.get(platform, "null")
            if kbit >= FORCE_QUALITY_ABOVE_KBIT and force_js.strip() != "null":
                result = page.eval(force_js)
                print(f"  quality forced: {result}", flush=True)
                time.sleep(3)  # give player time to switch

            print(f"  measuring quality for {MEASURE_S}s…", flush=True)
            samples, total_polls = _sample_quality(page, MEASURE_S, platform)
            w, h = _dominant_res(samples)
            stall_pct = round(100 * (1 - len(samples) / total_polls), 1) if total_polls else 100.0

            print(f"  → dominant resolution: {w}x{h}  ({len(samples)}/{total_polls} samples, {stall_pct}% stalled)", flush=True)
            results.append({"kbit": kbit, "run": run, "w": w, "h": h,
                             "samples": len(samples), "total_polls": total_polls,
                             "stall_pct": stall_pct, "ads_cleared": ads_ok})

    _run_make("disable")
    return results


# ── report ────────────────────────────────────────────────────────────────────

def _res_label(w: int, h: int) -> str:
    if h == 0:     return "no-data"
    if h <= 144:   return "144p"
    if h <= 240:   return "240p"
    if h <= 360:   return "360p"
    if h <= 480:   return "480p"
    if h <= 720:   return "720p"
    if h <= 1080:  return "1080p"
    return f"{h}p"


def print_report(platform: str, results: list[dict]):
    print("\n" + "="*60)
    print(f"  CALIBRATION RESULTS — {platform.upper()}")
    print("="*60)
    print(f"  {'Speed':>8}  {'Resolution':>12}  {'Label':>6}  {'Samples':>7}  {'Stall%':>7}  Ads")
    print(f"  {'-'*8}  {'-'*12}  {'-'*6}  {'-'*7}  {'-'*7}  ---")
    for r in results:
        res = f"{r['w']}x{r['h']}"
        lbl = _res_label(r['w'], r['h'])
        ads = "ok" if r['ads_cleared'] else "stuck"
        sp  = f"{r.get('stall_pct', '?')}%"
        print(f"  {r['kbit']:>8}  {res:>12}  {lbl:>6}  {r['samples']:>7}  {sp:>7}  {ads}")
    print()


def suggest_scenarios(results: list[dict]):
    """Print a suggested SCENARIOS patch based on calibration data."""
    # Group by kbit → most common resolution
    from collections import defaultdict
    by_speed: dict[int, list] = defaultdict(list)
    for r in results:
        if r['samples'] > 0:
            by_speed[r['kbit']].append((r['w'], r['h']))

    print("# Suggested threshold observations (for tuning SCENARIOS in shaping.py):")
    for kbit in sorted(by_speed):
        w, h = _dominant_res(by_speed[kbit])
        print(f"#   {kbit:>6} kbit/s  →  {w}x{h}  ({_res_label(w, h)})")


def save_results(platform: str, results: list[dict]):
    out = Path(__file__).parent.parent / "data" / f"calibration_{platform}.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved → {out}")


def sanity_check(platform: str, ref_kbit: int = 2000) -> bool:
    """
    Quick check: can we land on a live video for this platform?
    Sets TC to ref_kbit, navigates, confirms video is advancing, samples
    resolution for 30s, prints a one-line verdict.  Returns True on success.
    """
    print(f"\n[sanity] {platform} @ {ref_kbit} kbit/s", flush=True)
    _run_make("enable")
    _set_speed(ref_kbit)

    page = _get_page(timeout=30)
    if page is None:
        print(f"[sanity] FAIL — Chrome not reachable", flush=True)
        return False

    urls = PLATFORM_URLS.get(platform, [])
    if not _find_live(page, urls, platform):
        print(f"[sanity] FAIL — could not find playing stream", flush=True)
        return False

    ads_ok = _wait_ads_clear(page, timeout_s=120)
    samples, total = _sample_quality(page, 30, platform)
    w, h = _dominant_res(samples)
    stall_pct = round(100 * (1 - len(samples) / total), 1) if total else 100.0
    status = "OK" if h > 0 else "STALLED"
    print(f"[sanity] {status}  res={w}x{h} ({_res_label(w,h)})  stall={stall_pct}%  ads={'cleared' if ads_ok else 'stuck'}", flush=True)
    return h > 0


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", default="youtube",
                    choices=list(PLATFORM_URLS))
    ap.add_argument("--speeds",
                    help="Comma-separated kbit/s list, e.g. 500,1000,2000")
    ap.add_argument("--speed", type=int,
                    help="Single speed to test (for quick checks)")
    ap.add_argument("--runs", type=int, default=1,
                    help="Measurement runs per speed (default 1)")
    ap.add_argument("--sanity", action="store_true",
                    help="Quick sanity check: verify live video plays, then exit")
    args = ap.parse_args()

    if args.sanity:
        ok = sanity_check(args.platform, ref_kbit=args.speed or 2000)
        sys.exit(0 if ok else 1)

    if args.speed:
        speeds = [args.speed]
    elif args.speeds:
        speeds = [int(s.strip()) for s in args.speeds.split(",")]
    else:
        speeds = DEFAULT_SPEEDS

    print(f"Platform : {args.platform}")
    print(f"Speeds   : {speeds} kbit/s")
    print(f"Runs/speed: {args.runs}")
    print(f"Ad timeout: {AD_TIMEOUT_S}s   Measure: {MEASURE_S}s")
    print()

    results = calibrate(args.platform, speeds, runs_per_speed=args.runs)
    print_report(args.platform, results)
    suggest_scenarios(results)
    save_results(args.platform, results)
