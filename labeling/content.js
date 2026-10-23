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
// check_session.frozen_runs() does (stall label on from a "stall" event to a "start" / "video_start" event; non-ad tick;
// playhead within FROZEN_TOL_S of the run's first tick; any other tick ends it). The 2026-10-18 measure (a run ending
// only at 10 consecutive advancing ticks) is reverted (levi 2026-10-19, counted round 1's tw_100007: it read a Twitch
// reset-and-play loop of ~2 s plays as one 90 s frozen run and aborted a heavy-stall session). The top frame publishes
// the length in seconds as <html data-lvqoe-frozen-s>; browser.py's watchdog (the platform's T) and the stuck-player
// rule (Addendum Q am. 6, T2 above the lowest rung) read it.
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

// v3 A2 / A10 (t67): the raw player-event stream and element identity. Every listed <video> event of an element we
// attached, never deduped (emit() drops a repeat type, so a second resize in play was lost and that switch was poll-timed,
// kb collector.tier.sampling_resize_poll_dedupe), each with both clocks and the element's state, in its own stream
// (player_events.jsonl), never in the label events. el_id: a stable per-element counter; src_key: what feeds the element
// (a blob URL's hash, the srcObject kind and identity -- MediaStream#n on tt, MediaSourceHandle#n on twitch -- or a
// URL hash, no query).
const PLAYER_EVENTS = ["resize", "waiting", "stalled", "playing", "play", "pause", "seeking", "seeked", "ratechange",
                       "emptied", "loadstart", "loadedmetadata", "loadeddata", "durationchange", "ended"];
const SIDE_MAX = 5000;
const elIds = new WeakMap();
let elNext = 0;
function elId(v) {
  if (!v) return null;
  if (!elIds.has(v)) elIds.set(v, ++elNext);
  return elIds.get(v);
}
function hash8(s) {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(16).padStart(8, "0");
}
const objIds = new WeakMap();
let objNext = 0;
function srcKey(v) {
  if (!v) return null;
  if (v.srcObject) {                      // its kind and identity: tt's MediaStream is replaced per stream
    const o = v.srcObject;
    if (!objIds.has(o)) objIds.set(o, ++objNext);
    return `${(o.constructor && o.constructor.name) || "srcObject"}#${objIds.get(o)}`;
  }
  const s = v.currentSrc || v.src || "";
  if (!s) return "";
  return s.startsWith("blob:") ? "blob:" + hash8(s) : "url:" + hash8(s.split("?")[0]);
}
const sideBuffers = { player_events: [], frames: [] };
const sideDropped = { player_events: 0, frames: 0 };   // brad 18:17: records lost to the SIDE_MAX cap, per stream
const sideFailed = { player_events: 0, frames: 0 };    // batches the extension did not store (re-queued), per stream
function side(stream, rec) {
  const buf = sideBuffers[stream];
  buf.push(rec);
  if (buf.length > SIDE_MAX) {
    buf.shift();
    sideDropped[stream] += 1;
  }
}
function playerEvent(v, ev, extra) {
  if (!collecting || !v) return;
  const pn = performance.now();
  side("player_events", {
    ev, ts: performance.timeOrigin + pn, pn, wall: Date.now(), el: elId(v), src: srcKey(v), attached: v === video,
    ct: v.currentTime ?? null, rs: v.readyState ?? null, ns: v.networkState ?? null, w: v.videoWidth ?? null,
    h: v.videoHeight ?? null, paused: !!v.paused, ended: !!v.ended, rate: v.playbackRate ?? null,
    buf: bufferedAhead(v), top: window === window.top, ...(extra || {}),
  });
}
const peObserved = new WeakSet();
function observePlayerEvents(v) {
  if (peObserved.has(v)) return;
  peObserved.add(v);
  for (const ev of PLAYER_EVENTS) v.addEventListener(ev, () => playerEvent(v, ev));
}
// v3 A1 (t67; rushi-approved, levi 17:41): requestVideoFrameCallback aggregates in the side stream "frames", never
// per frame: (a) each change of the presented frame's dims, with that first frame's presentationTime,
// expectedDisplayTime, mediaTime and presentedFrames; (b) once per 1 s tick (rvfcTick, from the res_meta timer, so it
// still records while nothing is presented): presentedFrames, decoded (totalVideoFrames) and dropped side by side, the
// last frame's mediaTime / presentationTime and the callbacks since the last tick; (c) the first presented frame after
// an attach or a src_key change (tt's per-stream anchor: a MediaStream's mediaTime restarts per stream). The callback
// only reads metadata. lvqoe_config.rvfc === false (written by browser.py for the gate's off arm) registers nothing.
// brad 18:17: no callback is registered before the config is read (an element attached at load would otherwise run
// the off arm's callbacks); callbacks that still run while off are counted (off_arm_cb, the A/B asserts 0).
let rvfcOn = true;
let rvfcReady = false;
let offArmCb = 0;
function rvfcConfigured(on) {
  rvfcOn = on;
  rvfcReady = true;
  if (rvfcOn && video) rvfcStart(video);
}
try {
  if (chrome.storage && chrome.storage.local) {
    chrome.storage.local.get({ lvqoe_config: {} }, (d) => rvfcConfigured(!(d && d.lvqoe_config && d.lvqoe_config.rvfc === false)));
  } else {
    rvfcReady = true;
  }
} catch (e) {
  rvfcReady = true;                       // no storage: on
}
const rvfcState = new WeakMap();
function rvfcStart(v) {
  if (!rvfcReady || !rvfcOn || !v || typeof v.requestVideoFrameCallback !== "function" || rvfcState.has(v)) return;
  const st = { w: null, h: null, src: null, n: 0, last: null };
  rvfcState.set(v, st);
  const cb = (now, md) => {
    if (!rvfcOn) {
      offArmCb += 1;
      rvfcState.delete(v);
      return;
    }
    if (!collecting) return;
    st.n += 1;
    st.last = md;
    const src = srcKey(v);
    const base = { ts: performance.timeOrigin + now, el: elId(v), src, pt: md.presentationTime ?? null,
                   edt: md.expectedDisplayTime ?? null, mt: md.mediaTime ?? null, pf: md.presentedFrames ?? null,
                   w: md.width ?? null, h: md.height ?? null };
    if (src !== st.src) {
      side("frames", { k: "first_frame", ...base, prev_src: st.src });
      st.src = src;
    }
    if (md.width !== st.w || md.height !== st.h) {
      side("frames", { k: "dims", ...base, prev_w: st.w, prev_h: st.h });
      st.w = md.width;
      st.h = md.height;
    }
    if (v === video || v.isConnected) v.requestVideoFrameCallback(cb);
    else rvfcState.delete(v);
  };
  v.requestVideoFrameCallback(cb);
}
function rvfcTick() {
  if (!collecting || !video) return;
  const st = rvfcState.get(video);
  const q = video.getVideoPlaybackQuality ? video.getVideoPlaybackQuality() : null;
  const md = st && st.last;
  side("frames", { k: "tick", ts: performance.timeOrigin + performance.now(), el: elId(video), src: srcKey(video),
                   tick: event_id - 1, cb: st ? st.n : null, pf: md ? md.presentedFrames ?? null : null,
                   mt: md ? md.mediaTime ?? null : null, pt: md ? md.presentationTime ?? null : null,
                   decoded: q ? q.totalVideoFrames : null, dropped: q ? q.droppedVideoFrames : null,
                   ct: video.currentTime ?? null, rvfc: rvfcOn && !!st, off_arm_cb: offArmCb,
                   side_dropped: { ...sideDropped }, side_failed: { ...sideFailed } });
  if (st) st.n = 0;
}

function flushSide() {
  for (const [stream, buf] of Object.entries(sideBuffers)) {
    if (buf.length === 0) continue;
    const batch = buf.splice(0, buf.length);
    try {
      chrome.runtime.sendMessage({ kind: "side-batch", stream, payload: batch }, (resp) => {
        if (chrome.runtime.lastError || !resp?.ok) {
          sideFailed[stream] += 1;
          buf.unshift(...batch);
          while (buf.length > SIDE_MAX) { buf.shift(); sideDropped[stream] += 1; }
        }
      });
    } catch (e) {
      buf.unshift(...batch);
    }
  }
}

function buildEvent(type) {
  return {
    event_id: event_id++,
    ts: performance.timeOrigin + performance.now(),
    type,
    video_w: video?.videoWidth ?? null,
    video_h: video?.videoHeight ?? null,
    el_id: elId(video),                 // v3 A10
    src_key: srcKey(video),
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
    ready_state: video ? video.readyState : null,   // raw QA (player_dead: 0 = no media at all)
    // t53 (am. 13, gate-2 item 4a, levi): additive columns, every mode. The page's visibility at the tick (an
    // overlapping-window layout must never hide an occluded window) and the rendered-frame counter (fps per tier).
    visibility: document.visibilityState ?? null,
    total_frames: video?.getVideoPlaybackQuality?.().totalVideoFrames ?? null,
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

function attachVideo(v, reason = "unknown") {
  if (video === v) return;

  const prev = video;
  video = v;
  observePlayerEvents(v);                 // v3 A2: before video_start, so the attach precedes the element's events
  rvfcStart(v);                           // v3 A1
  playerEvent(v, "lvqoe_attach", { reason, prev_el: elId(prev) });
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
        attachVideo(v, "dom_added");
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
  attachVideo(existingVideo, "initial");
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

window.addEventListener("pagehide", () => { tryFlush(); flushSide(); });

// Periodic flush in case message channel was busy
setInterval(tryFlush, FLUSH_INTERVAL_MS);
setInterval(flushSide, FLUSH_INTERVAL_MS);                 // v3: the side streams (player_events)

// Resolution polling — records current dimensions once per second
setInterval(() => {
  // Detachment recovery: if our element left the DOM (platform swapped the
  // player), re-attach to the live one instead of polling a dead reference.
  if (video && !video.isConnected) {
    const v = pickBestVideo();
    if (v && v !== video) {
      attachVideo(v, "detached");
    }
  }
  if (!video) return;
  emit("res_meta");
  rvfcTick();                             // v3 A1: the 1 Hz counters, joined to this tick's event_id
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
