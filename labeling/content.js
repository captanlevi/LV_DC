// =====================
// Video QoE Collector (content.js)
// =====================

let video = null;
let event_id = 0;
let lastEventType = null;
const eventBuffer = [];
const MAX_BUFFER_SIZE = 1000;
const FLUSH_INTERVAL_MS = 1000;
const RESOLUTION_POLL_INTERVAL_MS = 1000;
let flushInProgress = false;
let collecting = true;
let inStall = false;
// v2 (LVQoE t35): element / source identity, to report a swap on the first tick after it
let lastSrc = null;
let lastElement = null;
// addendum N (LVQoE, 2026-10-17): the stall-only frozen-playhead run, computed on the emitted stream exactly as
// check_session.frozen_playhead_run() does (stall label on from a "stall" event to a "start" / "video_start" event;
// non-ad tick; playhead within FROZEN_TOL_S of the run's first tick). The top frame publishes its length in seconds as
// <html data-lvqoe-frozen-s>; browser.py's watchdog aborts the session past the platform's T (check_session table).
const FROZEN_TOL_S = 1.0;
let labelStall = false;
// raw playhead state per tick (supervisor 2026-10-17, chaos tw_10: twitch pauses its player on a stall; the safety net's
// "start" at readyState >= 3 came while the element stayed paused, and 250 s of a frozen playhead read as playing).
// Label semantics stay with the label policy (levi); these columns only record what the element did.
let stillSinceTs = null;
let stillCt = null;
let frozenT0 = null;
let frozenCt0 = null;
// An ad's <video> has a short finite duration; live streams report Infinity. A DVR window can report a long finite
// duration, so only a finite duration below this bound counts as an ad signal (raw duration is recorded too).
const AD_MAX_DURATION_S = 600;

// Ad DOM markers per platform (they rotate: keep the list, add, never remove). Visible elements only.
const AD_SELECTORS = {
  youtube: [
    ".ytp-ad-player-overlay", ".ytp-ad-player-overlay-layout", ".ytp-ad-preview-container",
    ".ytp-ad-text", ".ytp-ad-simple-ad-badge", ".ytp-ad-badge", ".ytp-ad-duration-remaining",
    ".ytp-ad-skip-button", ".ytp-skip-ad-button", ".ytp-ad-skip-button-modern",
  ],
  twitch: [
    '[data-a-target="video-ad-label"]', '[data-a-target="video-ad-countdown"]', '[data-a-target="ad-banner"]',
    '[data-test-selector="ad-banner-default-text"]', '[data-test-selector="ad-banner"]', ".ads-manager__container",
    ".tw-ad",
  ],
};

// v2 (LVQoE t37, rushi 2026-10-16): the logged-out wall ("video lasts 10 seconds, log in to continue"). login_wall =
// a visible login prompt (in a dialog / modal / login node, or over the player) AND the playhead stalled for
// WALL_STALL_TICKS ticks after WALL_PLAYED_MIN_S..WALL_PLAYED_MAX_S of play since the page loaded. Text alone is not
// enough (login banners are common and harmless); a stall alone is a stall. login_wall_text reports the text alone.
const LOGIN_WALL_RE = /\b(log ?in|sign ?up|sign ?in)\b.{0,60}\b(continue|keep watching|watch)|\blasts? \d+ ?(s|sec|seconds)\b/i;
const LOGIN_WALL_NODES = '[role="dialog"], [aria-modal="true"], [class*="odal"], [data-e2e*="login"], [class*="ogin"]';
const WALL_STALL_TICKS = 3;
const WALL_PLAYED_MIN_S = 5;
const WALL_PLAYED_MAX_S = 30;
let playedS = 0;
let wallLastT = null;
let wallStallTicks = 0;

// visible text nodes anywhere in the page containing `phrase` (lower case); XPath is native, so a per-tick scan is cheap.
// The toasts sit outside the dialog / player-ancestor nodes (laptop run4: "Current video quality lasts for 7 s. Log in
// to continue your viewing experience." over the tt player, missed by the node scan)
function visibleTextHit(phrase) {
  const xp = `//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '${phrase}')]`;
  const r = document.evaluate(xp, document.body || document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
  for (let i = 0; i < r.snapshotLength; i++) if (visible(r.snapshotItem(i).parentElement)) return true;
  return false;
}

function loginWallText(v) {
  if (visibleTextHit("log in to continue") || visibleTextHit("log in to keep watching")) return true;
  const texts = [...document.querySelectorAll(LOGIN_WALL_NODES)].filter(visible).map(e => e.innerText || "");
  for (let e = v ? v.parentElement : null, i = 0; e && i < 4; e = e.parentElement, i++) texts.push(e.innerText || "");
  return texts.some(t => LOGIN_WALL_RE.test(t));
}

// event screenshots (t37): browser.py takes an extra screenshot when this request changes: at every tier change
// (consecutive non-zero res_meta video_h differ) and every ad start (is_ad false -> true)
let shotReqN = 0;
let shotLastH = null;
let shotLastAd = false;
function requestShot(reason, eventId) {
  shotReqN += 1;
  document.documentElement.dataset.lvqoeShotReq = `${shotReqN}|${reason}|${eventId}`;
}

// tt logged-out quality countdown (t37): "Current video quality lasts for N s. Log in to continue ..." on room pages;
// at 0 the source resets one rendition lower (laptop run3 720p -> 540p, run4 1280 -> 1024). check_session FAILs a
// session with any such tick (tt_logged_out_cap): the profile was not logged in.
function qualityCountdown() {
  return visibleTextHit("video quality lasts");
}

// once per res_meta tick: play time since load (positive playhead steps up to 2 s, so a source reset adds nothing)
// and the run of ticks with a frozen playhead
function updateWallClock(v) {
  const t = v ? v.currentTime : null;
  if (t !== null && wallLastT !== null) {
    const d = t - wallLastT;
    if (d > 0.05 && d <= 2) playedS += d;
    wallStallTicks = Math.abs(d) < 0.05 ? wallStallTicks + 1 : 0;
  }
  wallLastT = t;
}

function platformOf() {
  const h = location.hostname;
  if (h.includes("youtube.")) return "youtube";
  if (h.includes("twitch.")) return "twitch";
  if (h.includes("tiktok.")) return "tiktok";
  if (h.includes("bilibili.")) return "bilibili";
  return "other";
}

function visible(el) {
  return !!el && el.offsetParent !== null && el.getClientRects().length > 0;
}

// Which ad signals fire now. is_ad = any of these. A <video> swap is NOT an ad signal on its own (twitch swaps the
// element on quality changes too); it is reported separately as src_changed.
function adSignals(v) {
  const sig = [];
  if (v && isFinite(v.duration) && v.duration > 0 && v.duration < AD_MAX_DURATION_S) sig.push("duration_finite");
  const plat = platformOf();
  if (plat === "youtube") {
    const mp = document.getElementById("movie_player");
    if (mp && mp.classList.contains("ad-showing")) sig.push("yt_ad_showing");
    if (mp && mp.classList.contains("ad-interrupting")) sig.push("yt_ad_interrupting");
  }
  for (const sel of AD_SELECTORS[plat] || []) {
    if ([...document.querySelectorAll(sel)].some(visible)) {
      sig.push(plat === "youtube" ? "yt_ad_dom" : "tw_ad_dom");
      break;
    }
  }
  return sig;
}

function bufferedAhead(v) {
  // null = the element exposes no buffered ranges (TikTok's live player: every tick 0 in the laptop test while the
  // playhead advanced), so "unknown" is never confused with an empty buffer; 0 = playhead outside any range (starved)
  if (!v || !v.buffered || v.buffered.length === 0) return null;
  const t = v.currentTime;
  for (let i = 0; i < v.buffered.length; i++) {
    if (v.buffered.start(i) <= t + 0.05 && t <= v.buffered.end(i)) return v.buffered.end(i) - t;
  }
  return 0;
}

function srcChanged(v) {
  if (!v) return false;
  const src = v.currentSrc || v.src || null;
  const changed = (lastElement !== null && lastElement !== v) || (lastSrc !== null && src !== lastSrc);
  lastElement = v;
  lastSrc = src;
  return changed;
}

function buildEvent(type) {
  return {
    event_id: event_id++,
    ts: performance.timeOrigin + performance.now(),
    type,
    video_w: video?.videoWidth ?? null,
    video_h: video?.videoHeight ?? null,
    client_w: video?.clientWidth ?? null,
    client_h: video?.clientHeight ?? null,
    dropped: video?.getVideoPlaybackQuality?.().droppedVideoFrames ?? null,
    // Advancing playhead proves the element is really playing; a frozen
    // current_time makes stale res_meta ticks detectable in post-processing.
    current_time: video?.currentTime ?? null,
    // v2 (LVQoE t35): additive columns
    buffered_ahead_s: bufferedAhead(video),
    playback_rate: video?.playbackRate ?? null,
    duration_s: video ? (isFinite(video.duration) ? video.duration : "inf") : null,
    is_ad: null,
    ad_signals: null,
    src_changed: null,
    login_wall_text: null,
    login_wall: null,
    quality_countdown: null,
    paused: video ? video.paused : null,
    playhead_still_s: null,
  };
}

function withAdState(evt) {
  const sig = adSignals(video);
  evt.is_ad = sig.length > 0;
  evt.ad_signals = sig.join("|");
  evt.src_changed = srcChanged(video);
  if (evt.type === "res_meta") updateWallClock(video);
  evt.login_wall_text = loginWallText(video);
  evt.login_wall = evt.login_wall_text && wallStallTicks >= WALL_STALL_TICKS &&
    playedS >= WALL_PLAYED_MIN_S && playedS <= WALL_PLAYED_MAX_S;
  evt.quality_countdown = qualityCountdown();
  return evt;
}

function emit(type) {
  if (!collecting) return;
  if (type !== "res_meta" && type === lastEventType) return;
  if (type !== "res_meta") lastEventType = type;

  const evt = withAdState(buildEvent(type));
  eventBuffer.push(evt);
  if (type === "stall") labelStall = true;
  else if (type === "start" || type === "video_start") labelStall = false;
  if (type === "res_meta") {
    const ct = evt.current_time;
    if (ct === null || ct === undefined || stillCt === null || Math.abs(ct - stillCt) > 0.05) {
      stillCt = ct;
      stillSinceTs = evt.ts;
    }
    evt.playhead_still_s = stillSinceTs === null ? null : (evt.ts - stillSinceTs) / 1000;
    if (!labelStall || evt.is_ad || ct === null || ct === undefined || !isFinite(ct)) {
      frozenT0 = null;
    } else if (frozenT0 === null || Math.abs(ct - frozenCt0) > FROZEN_TOL_S) {
      frozenT0 = evt.ts;
      frozenCt0 = ct;
    }
    if (window === window.top) {
      document.documentElement.dataset.lvqoeFrozenS = String(frozenT0 === null ? 0 : (evt.ts - frozenT0) / 1000);
      // the stall label, for browser.py's tt dead-room watcher (a stall is never a dead room; player_frozen owns it)
      // and its self-pause resumer (after_stall)
      document.documentElement.dataset.lvqoeStall = labelStall ? "1" : "0";
      // the ad label, so the collector never presses play on an ad (browser.py SelfPauseResumer)
      document.documentElement.dataset.lvqoeAd = evt.is_ad ? "1" : "0";
    }
  }
  // Tick id visible to the page world (shared DOM), so screenshots can be stamped with the tick they follow.
  if (type === "res_meta" && window === window.top) {
    document.documentElement.dataset.lvqoeTick = String(evt.event_id);
    document.documentElement.dataset.lvqoeTickTs = String(evt.ts);
    if (evt.login_wall) document.documentElement.dataset.lvqoeWall = "1";   // browser.py aborts the session on it
    const h = evt.video_h || 0;
    if (h > 0) {
      if (shotLastH !== null && h !== shotLastH) requestShot("tier_change", evt.event_id);
      shotLastH = h;
    }
    if (evt.is_ad && !shotLastAd) requestShot("ad_start", evt.event_id);
    shotLastAd = !!evt.is_ad;
  }

  if (eventBuffer.length > MAX_BUFFER_SIZE) {
    eventBuffer.shift();
  }

  tryFlush();
  console.log("[YT][EVENT]", evt);
}

function tryFlush() {
  if (flushInProgress || eventBuffer.length === 0) return;
  flushInProgress = true;

  const batch = eventBuffer.slice();

  try {
    chrome.runtime.sendMessage(
      { kind: "video-events-batch", payload: batch },
      (resp) => {
        flushInProgress = false;
        if (chrome.runtime.lastError) return;
        if (!resp?.ok) return;
        eventBuffer.splice(0, batch.length);
      },
    );
  } catch (e) {
    console.log("[YT][ERROR] Message failed", e);
    flushInProgress = false;
  }
}

function observeVideo(v) {
  // Ignore events from elements we are no longer attached to — platforms
  // (Twitch especially) swap <video> elements on ads/quality changes, and a
  // discarded element can keep firing pause/ended after we move on.
  const guard = (fn) => () => {
    if (v !== video) return;
    fn();
  };

  v.addEventListener("loadedmetadata", guard(() => emit("meta")));

  // Emit "start" on every playing transition (not only stall recovery) so a
  // pause -> resume without rebuffering is visible in the labels. emit()
  // dedupes consecutive identical event types, so this cannot spam.
  v.addEventListener("playing", guard(() => {
    inStall = false;
    emit("start");
  }));
  v.addEventListener("waiting", guard(() => {
    if (!inStall) {
      inStall = true;
      emit("stall");
    }
  }));

  v.addEventListener("resize", guard(() => emit("resize")));
  v.addEventListener("ended", guard(() => emit("end")));
  v.addEventListener("pause", guard(() => {
    if (!v.ended) emit("pause");
  }));

  // Catch cases where video is already playing when we attach
  if (!v.paused && !v.ended) {
    emit("start");
  }
}

function pickBestVideo() {
  // Prefer the element that is actually playing over the first one in DOM
  // order — after an element swap the dead one often remains in the DOM.
  const candidates = [...document.querySelectorAll("video")];
  return candidates.find((x) => !x.paused && !x.ended) ?? candidates[0] ?? null;
}

function attachVideo(v) {
  if (video === v) return;

  video = v;
  lastEventType = null;
  // stall state belongs to the element: re-evaluate it on the new one (the safety net emits "stall" if it is
  // buffering), instead of carrying the old element's flag past the "video_start" that clears the label
  inStall = false;
  stillCt = null;
  stillSinceTs = null;

  console.log("[YT] Video detected");
  emit("video_start");
  observeVideo(video);
}

function endCollection(reason = "manual") {
  if (!collecting) return;

  collecting = false;
  lastEventType = null;

  emit("end");
  tryFlush();

  video = null;
  observer.disconnect();
  console.log("[YT] Collection ended:", reason);
}

// MutationObserver: detects when the platform inserts/replaces <video>
const observer = new MutationObserver((mutations) => {
  for (const m of mutations) {
    if (m.addedNodes.length > 0) {
      const v = pickBestVideo();
      if (v) {
        attachVideo(v);
        break;
      }
    }
  }
});

observer.observe(document.documentElement, {
  childList: true,
  subtree: true,
});

// Fallback: attach immediately if video already exists
const existingVideo = pickBestVideo();
if (existingVideo) {
  attachVideo(existingVideo);
}

// yt player stats (G3, ground truth only): page_stats.js runs in the page world and posts here; forwarded as a
// separate stream (stored apart from the label events, exported to player_stats.jsonl, never into labels.csv).
const statsBuffer = [];
window.addEventListener("message", (e) => {
  if (e.source !== window || !e.data || e.data.source !== "lvqoe-page-stats") return;
  if (!collecting) return;
  statsBuffer.push({ ts: e.data.ts, stats: e.data.stats, url: location.href });
  if (statsBuffer.length > 200) statsBuffer.shift();
});
setInterval(() => {
  if (statsBuffer.length === 0) return;
  const batch = statsBuffer.splice(0, statsBuffer.length);
  try {
    chrome.runtime.sendMessage({ kind: "player-stats-batch", payload: batch }, () => void chrome.runtime.lastError);
  } catch (e) {
    statsBuffer.unshift(...batch);
  }
}, FLUSH_INTERVAL_MS);

window.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") tryFlush();
});

window.addEventListener("pagehide", () => tryFlush());

// Periodic flush in case message channel was busy
setInterval(tryFlush, FLUSH_INTERVAL_MS);

// Resolution polling — records current dimensions once per second
setInterval(() => {
  // Detachment recovery: if our element left the DOM (platform swapped the
  // player), re-attach to the live one instead of polling a dead reference.
  if (video && !video.isConnected) {
    const v = pickBestVideo();
    if (v && v !== video) {
      attachVideo(v);
    }
  }
  if (!video) return;
  emit("res_meta");
}, RESOLUTION_POLL_INTERVAL_MS);

// Stall safety-net: catches buffering that doesn't fire a 'waiting' event
// (e.g. stream gaps on some platforms). Guards against double-emit with inStall flag.
setInterval(() => {
  if (!video) return;

  const isBuffering = video.readyState < 3;

  if (isBuffering && !inStall) {
    inStall = true;
    emit("stall");
  }

  // "start" means playback resumed: never while the element is paused (levi 2026-10-17, chaos tw_10: twitch pauses its
  // player on a stall and stayed paused; a start here labelled 250 s of a frozen playhead as playing). The collector
  // (browser.py) presses play on such a self-pause, and the element's own "playing" event then emits start.
  if (!isBuffering && inStall && !video.paused) {
    inStall = false;
    emit("start");
  }
}, RESOLUTION_POLL_INTERVAL_MS);
