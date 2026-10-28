// =====================
// Video QoE Collector (content.js)
// =====================

let video = null;
let event_id = 0;
let lastTick = null;           // v3 A5: the latest res_meta tick {id, ts}, stamped onto every page-stats row
let lastEventType = null;
const eventBuffer = [];
const MAX_BUFFER_SIZE = 1000;
const FLUSH_INTERVAL_MS = 1000;
const RESOLUTION_POLL_INTERVAL_MS = 1000;
let flushInProgress = false;
// boundary after window 1 (codex item 1, levi 00106): one id per content-script instance, so a reload or a second frame is
// a new event_id sequence, not a gap; the flush drops only the ids the background acked (never by count), and an event
// the overflow shifts out unacked is counted, cumulative per instance, on every later batch (dropped_n / _first / _last)
const CS_ID = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
let inflightMaxId = -1;            // the in-flight batch's last event_id (-1: none in flight)
const shiftedInFlight = [];        // ids the overflow shifted out while their batch was in flight (lost only if unacked)
const evDropped = { n: 0, first_id: null, last_id: null };
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
// WA2 r1 ti_100001 (levi 00:28 / 00:30, sensor_v3.4's frozen class): the frozen PICTURE run, beside the stall-only run.
// A tick pair is frozen when both ticks are not paused, not an ad, with a media clock and a decoded-frames counter, the
// clock advanced (> 0) and totalVideoFrames advanced by <= FROZEN_FRAMES_MAX; a RESET pair (the counter or the clock
// going back: a new player) BRIDGES, as sensor_v3.4 (brad / levi 00:38): neither frozen nor breaking. The run is the first
// frozen pair's first tick to the last frozen pair's last tick. lvqoeFrozenS is the UNION run of this and the stall-only
// run (overlapping or abutting merged: check_session.frozen_runs / _union_runs, the same)
const FROZEN_FRAMES_MAX = 1;
let picT0 = null;
let picLast = null;
let picPrev = null;
let unionT0 = null;
let unionEnd = null;
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
// (v3 A14: the presented frame's height changes, rvfcStart; without A1, consecutive non-zero res_meta video_h differ)
// and every ad start (is_ad false -> true)
let shotReqN = 0;
const shotTrig = { frame: 0, tick: 0, ad_start: 0, stall_onset: 0, start: 0 };   // levi 21:19: tier-change shots by trigger (pf = frame), ad starts
// WA2 r1 (levi 00:52 / 00:53): 69 stall_onset shots in one tt churn period (a new player, so a new stall, every ~1 s).
// Event shots of the same reason are >= SHOT_EVENT_MIN_GAP_S apart; a suppressed request is counted (side_counters
// shot_triggers_suppressed), never silent; the 60 s cadence (browser.py) is untouched
const SHOT_EVENT_MIN_GAP_S = 10;
const shotLastAt = {};
const shotSuppressed = {};
let shotLastH = null;
let shotLastAd = false;
function requestShot(reason, eventId, pf = null) {
  const nowMs = performance.now();
  if (shotLastAt[reason] !== undefined && nowMs - shotLastAt[reason] < SHOT_EVENT_MIN_GAP_S * 1000) {
    shotSuppressed[reason] = (shotSuppressed[reason] || 0) + 1;
    return;
  }
  shotLastAt[reason] = nowMs;
  shotReqN += 1;
  if (reason === "tier_change") shotTrig[pf === null ? "tick" : "frame"] += 1;
  else if (reason in shotTrig) shotTrig[reason] += 1;
  document.documentElement.dataset.lvqoeShotReq = `${shotReqN}|${reason}|${eventId}` + (pf === null ? "" : `|pf=${pf}`);
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

// Which ad signals fire now. is_ad = any of these but duration_finite alone (isAdFrom). A <video> swap is NOT an ad signal on its own (twitch swaps the
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

// levi 00176 (2c): every page this collector opens is a LIVE stream (a yt channel's /live, a twitch channel, a tt
// /live room), where a short finite duration is also what a young stream's DVR window looks like; so duration_finite
// alone never marks an ad: is_ad needs a platform signal. duration_finite stays in ad_signals (recorded).
function isAdFrom(sig) {
  return sig.some((s) => s !== "duration_finite");
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

function bufferedRanges(v) {
  // v3 A6 (t67): every buffered range at the tick, "start:end;..." in media seconds (3 dp), beside buffered_ahead_s;
  // null when the element exposes none (tt: a MediaStream source, probe 00093), never an empty string
  if (!v || !v.buffered || v.buffered.length === 0) return null;
  const out = [];
  for (let i = 0; i < v.buffered.length; i++) out.push(v.buffered.start(i).toFixed(3) + ":" + v.buffered.end(i).toFixed(3));
  return out.join(";");
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
const sideBuffers = { player_events: [], frames: [], ad_edges: [] };
const sideDropped = { player_events: 0, frames: 0, ad_edges: 0 };   // brad 18:17: records lost to the SIDE_MAX cap
const sideFailed = { player_events: 0, frames: 0, ad_edges: 0 };    // batches the extension did not store (re-queued)
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
  for (const ev of ["durationchange", "loadedmetadata", "emptied"]) {
    v.addEventListener(ev, () => { if (v === video) queueAdCheck(ev); });   // v3 A3: duration_finite flips here
  }
}
// v3 A1 (t67; rushi-approved, levi 17:41): requestVideoFrameCallback aggregates in the side stream "frames", never
// per frame: (a) each change of the presented frame's dims, with that first frame's presentationTime,
// expectedDisplayTime, mediaTime and presentedFrames; (b) once per 1 s tick (rvfcTick, from the res_meta timer, so it
// still records while nothing is presented): presentedFrames, decoded (totalVideoFrames) and dropped side by side, the
// last frame's mediaTime / presentationTime and the callbacks since the last tick; (c) the first presented frame after
// an attach or a src_key change (tt's per-stream anchor: a MediaStream's mediaTime restarts per stream). The callback
// only reads metadata.
// t91 (rushi 2026-10-28 17:40 / 20:27; levi 17:4x / 18:11, boundary 3): the per-frame log, every presented frame with
// every VideoFrameCallbackMetadata field. lvqoe_config.frame_log ({block_s, t0}, browser.py from LVQOE_FRAME_LOG; absent:
// off, no field, the tick row as before) turns it on. The rVFC callback only stores the frame's numbers into a
// preallocated Float64Array (FL_COLS per row, an absent field NaN; past FRAME_LOG_MAX rows in one tick a frame is counted,
// over, never kept). Once per tick, off the callback, the rows go into the tick row's field fl as one columnar batch of
// integers: times in us (pt / edt / ct / rt from ms, mt / pd from s), each column delta-coded or offset from pt
// (FL_CODE), a column with no value in the tick left out, a value null where that frame lacked it. inexact counts values
// that are not whole us (rounded); keys is Object.keys of the last frame's metadata when it differs from the last one
// sent, so a field the browser adds shows. block_s > 0 alternates OFF / ON blocks of block_s s on the epoch clock (t0 0:
// arm = floor(unix s / block_s) odd; levi 21:0x, so page and host samplers switch together), fixed per tick. pf per row
// shows a frame skipped between callbacks. t93 (levi 00124; brad W1 / W2 / W5 / W6), the sweep's page rows, every one ON
// ticks only: the callback's own now (column now); per tick to / wall / hid, q (getVideoPlaybackQuality's
// corruptedVideoFrames / creationTime / totalFrameDelay where the browser has them), wk (the webkit decode counters where
// present), lt (long tasks since the last tick: count, max ms; null where unsupported), conn (navigator.connection), mse
// (the page-world MSE wrap's overflow, browser.py MSE_WRAP_JS, yt); page events (visibility, focus / blur, fullscreen,
// freeze / resume, pagehide / pageshow) into player_events as lvqoe_page. Absent stays absent, never 0.
const FRAME_LOG_MAX = 1200;
// column: [metadata field, scale to an integer, code: "d" delta from the previous value, "p" offset from the row's pt, "r" raw]
const FL_CODE = { el: [null, 1, "d"], pt: ["presentationTime", 1e3, "d"], edt: ["expectedDisplayTime", 1e3, "p"],
                  mt: ["mediaTime", 1e6, "d"], pf: ["presentedFrames", 1, "d"], w: ["width", 1, "d"], h: ["height", 1, "d"],
                  pd: ["processingDuration", 1e6, "r"], ct: ["captureTime", 1e3, "p"], rt: ["receiveTime", 1e3, "p"],
                  rtp: ["rtpTimestamp", 1, "d"], now: [null, 1e3, "p"] };
const FL_COLS = Object.keys(FL_CODE);
const FL_N = FL_COLS.length;
const frameBuf = new Float64Array(FRAME_LOG_MAX * FL_N);
let frameLogCfg = null;
let frameLogOn = false;
let frameN = 0;
let frameOver = 0;
let frameSeq = 0;
let frameKeys = null;
function frameLogArm() {
  const b = frameLogCfg.block_s;
  return !(b > 0) || Math.floor((Date.now() - frameLogCfg.t0) / (b * 1000)) % 2 === 1;
}
let ltN = 0;
let ltMax = 0;
let ltOk = false;
let mseOver = null;
let mseInst = null;
let framePushErr = 0;                      // frames whose push faulted in this tick (fl.push_err)
let frameLogErr = null;                     // a frame-log fault: recorded in fl, never thrown into the tick or the config
function frameLogConfigure(c) {
  if (!c || typeof c !== "object") return;
  frameLogCfg = { block_s: Number(c.block_s) || 0, t0: Number.isFinite(Number(c.t0)) ? Number(c.t0) : 0 };
  frameLogOn = frameLogArm();
  try { frameLogListen(); } catch (e) { frameLogErr = "listen: " + String(e).slice(0, 160); }
}
function frameLogListen() {
  try {                                     // t93 row 8: long tasks, counted in ON ticks only
    if (typeof PerformanceObserver === "function" && (PerformanceObserver.supportedEntryTypes || []).includes("longtask")) {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) { ltN += 1; if (e.duration > ltMax) ltMax = e.duration; }   // SCRATCH t91 G7: every tick
      }).observe({ type: "longtask" });
      ltOk = true;
    }
  } catch (e) { ltOk = false; }
  const pageEvent = (kind, extra) => {      // t93 rows 7 / 23 (brad W6): the page's state at its change, ON ticks only
    if (!frameLogOn || !collecting) return;
    const pn = performance.now();
    side("player_events", { ev: "lvqoe_page", kind, ts: performance.timeOrigin + pn, pn, wall: Date.now(),
                            hid: document.hidden, vis: document.visibilityState, top: window === window.top, ...(extra || {}) });
  };
  document.addEventListener("visibilitychange", () => pageEvent("visibilitychange"));
  window.addEventListener("focus", () => pageEvent("focus"));
  window.addEventListener("blur", () => pageEvent("blur"));
  document.addEventListener("fullscreenchange", () => {
    const fe = document.fullscreenElement;
    pageEvent("fullscreenchange", { fs: !!fe, fs_video: !!(fe && video && (fe === video || (fe.contains && fe.contains(video)))) });
  });
  document.addEventListener("freeze", () => pageEvent("freeze"));
  document.addEventListener("resume", () => pageEvent("resume"));
  window.addEventListener("pagehide", (e) => pageEvent("pagehide", { persisted: !!e.persisted }));
  window.addEventListener("pageshow", (e) => pageEvent("pageshow", { persisted: !!e.persisted }));
}
function presentKeys(o, keys) {               // only the keys the object has: absent stays absent, never 0
  const r = {};
  if (o) for (const [k, src] of keys) if (src in o && o[src] !== undefined) r[k] = o[src];
  return r;
}
function frameLogPush(el, now, md) {      // the callback's only work: numbers into the preallocated buffer
  if (frameN >= FRAME_LOG_MAX) { frameOver += 1; return; }
  const o = frameN * FL_N;
  try {                                     // levi 23:51: a fault is counted (fl.push_err, fl.err), the row not kept
    frameBuf[o] = el;
    frameBuf[o + 1] = md.presentationTime;
    frameBuf[o + 2] = md.expectedDisplayTime;
    frameBuf[o + 3] = md.mediaTime;
    frameBuf[o + 4] = md.presentedFrames;
    frameBuf[o + 5] = md.width;
    frameBuf[o + 6] = md.height;
    frameBuf[o + 7] = md.processingDuration;
    frameBuf[o + 8] = md.captureTime;
    frameBuf[o + 9] = md.receiveTime;
    frameBuf[o + 10] = md.rtpTimestamp;
    frameBuf[o + 11] = now;
    frameN += 1;
  } catch (e) {
    framePushErr += 1;
    frameLogErr = "push: " + String(e).slice(0, 160);
  }
}
function frameLogEncode(n) {              // {cols, inexact}: the columnar integer batch of the tick's n rows
  const cols = {};
  let inexact = 0;
  const q = (v, scale) => {
    const x = Math.round(v * scale);
    if (x / scale !== v) inexact += 1;
    return x;
  };
  for (let j = 0; j < FL_N; j++) {
    const [, scale, code] = FL_CODE[FL_COLS[j]];
    const out = new Array(n);
    let any = false, prev = 0;
    for (let i = 0; i < n; i++) {
      const v = frameBuf[i * FL_N + j];
      if (v !== v) { out[i] = null; continue; }          // NaN: the frame lacked the field
      any = true;
      const x = q(v, scale);
      if (code === "d") { out[i] = x - prev; prev = x; }
      else if (code === "p") out[i] = x - Math.round(frameBuf[i * FL_N + 1] * 1e3);   // pt in us
      else out[i] = x;
    }
    if (any) cols[FL_COLS[j]] = out;
  }
  return { cols, inexact };
}
function frameLogField(md, q, v) {        // the tick row's fl, then the next tick's arm; a fault is fl.err, never a throw
  if (!frameLogCfg) return {};
  try {
    return frameLogBuild(md, q, v);
  } catch (e) {
    frameN = 0;
    return { fl: { cs: CS_ID, seq: frameSeq++, err: String(e).slice(0, 200) } };
  }
}
function frameLogBuild(md, q, v) {
  const n = frameN;
  const { cols, inexact } = frameLogEncode(n);
  const fl = { cs: CS_ID, seq: frameSeq++, on: frameLogOn, n, over: frameOver, inexact, c: cols };
  if (frameLogOn) {                         // t93: the tick's page readings, ON ticks only
    fl.to = performance.timeOrigin;
    fl.wall = Date.now();
    fl.hid = document.hidden;
    fl.q = presentKeys(q, [["corrupted", "corruptedVideoFrames"], ["creation", "creationTime"], ["tfd", "totalFrameDelay"]]);
    fl.wk = presentKeys(v, [["dfc", "webkitDecodedFrameCount"], ["drc", "webkitDroppedFrameCount"],
                        ["vb", "webkitVideoDecodedByteCount"], ["ab", "webkitAudioDecodedByteCount"]]);
    fl.lt = ltOk ? { n: ltN, max_ms: Math.round(ltMax * 10) / 10 } : null;
    fl.conn = presentKeys(typeof navigator !== "undefined" ? navigator.connection : null, [["dl", "downlink"], ["rtt", "rtt"], ["et", "effectiveType"], ["sd", "saveData"]]);
    if (mseOver !== null) fl.mse_over = mseOver;
    if (mseInst !== null) fl.mse_inst = mseInst;
    if (frameLogErr !== null) fl.err = frameLogErr;
    if (framePushErr) fl.push_err = framePushErr;
    if (md) {
      const keys = Object.keys(md).sort().join(",");
      if (keys !== frameKeys) { fl.keys = keys; frameKeys = keys; }
    }
  }
  if (!frameLogOn && ltOk) fl.lt_off = { n: ltN, max_ms: Math.round(ltMax * 10) / 10 };   // SCRATCH t91 G7 (dev only, never frozen)
  ltN = 0;
  ltMax = 0;
  framePushErr = 0;
  frameN = 0;
  frameLogOn = frameLogArm();
  return { fl };
}
// levi 18:33: ONE flag for the whole v3 JS instrument set (A1 callbacks, A2 listeners, A3 observer, A7 box): the gate's
// instrumentation A/B. lvqoe_config.instruments === false (written by browser.py, LVQOE_INSTRUMENTS=0) starts none of
// them; nothing starts before the config is read (brad 18:17), and when it says on, the current element is started then.
// rVFC callbacks that still run while off are counted (off_arm_cb; the A/B asserts 0).
let v3On = true;
let v3Ready = false;
let offArmCb = 0;
function v3Start(v, reason, prev) {
  if (!v3Ready || !v3On || !v) return;
  observePlayerEvents(v);
  playerEvent(v, "lvqoe_attach", { reason, prev_el: elId(prev) });
  rvfcStart(v);
  adObserve(v);
}
function v3Configured(on) {
  v3On = on;
  v3Ready = true;
  if (v3On && video) v3Start(video, "config", null);
  announceInstruments();
}
let src3Polls = 0;                        // page_stats.js's yt player polls since the last tick (0 in the off arm)
function announceInstruments() {          // brad 18:45: the page world's poller starts only on this message
  if (!(v3Ready && v3On && window === window.top)) return;
  try {
    window.postMessage({ source: "lvqoe-instruments", on: true }, "*");
  } catch (e) { /* no page world to start: source 3 stays silent (counted 0) */ }
}
try {
  if (chrome.storage && chrome.storage.local) {
    chrome.storage.local.get({ lvqoe_config: {} }, (d) => {
      const cfg = (d && d.lvqoe_config) || {};
      const on = !(cfg.instruments === false || cfg.rvfc === false);
      frameLogConfigure(on ? cfg.frame_log : null);   // t91: before the callbacks start
      v3Configured(on);
    });
  } else {
    v3Ready = true;
  }
} catch (e) {
  v3Ready = true;                         // no storage: on
}
const rvfcState = new WeakMap();
const RVFC_FRESH_MS = 2000;
function rvfcStart(v) {
  if (!v3Ready || !v3On || !v || typeof v.requestVideoFrameCallback !== "function" || rvfcState.has(v)) return;
  const st = { w: null, h: null, src: null, n: 0, last: null };
  rvfcState.set(v, st);
  const cb = (now, md) => {
    if (!v3On) {
      offArmCb += 1;
      rvfcState.delete(v);
      return;
    }
    if (!collecting) return;
    try {                                   // levi 23:51: a throw in the body never ends the chain (re-request in finally)
      st.n += 1;
      st.last = md;
      st.lastAt = now;                         // A14: the res_meta trigger stands down only while frames are fresh
      const src = srcKey(v);
      const base = { ts: performance.timeOrigin + now, el: elId(v), src, pt: md.presentationTime ?? null,
                     edt: md.expectedDisplayTime ?? null, mt: md.mediaTime ?? null, pf: md.presentedFrames ?? null,
                     w: md.width ?? null, h: md.height ?? null };
      if (frameLogOn) frameLogPush(base.el, now, md);
      if (src !== st.src) {
        side("frames", { k: "first_frame", ...base, prev_src: st.src });
        st.src = src;
      }
      if (md.width !== st.w || md.height !== st.h) {
        side("frames", { k: "dims", ...base, prev_w: st.w, prev_h: st.h });
        // v3 A14 (t67): the tier-change shot fires on the presented frame whose height changed (A1), not on the next
        // res_meta tick; the request carries the tick it follows (A5) and that frame's presentedFrames
        // brad 21:19: a new element's first frame compares against the last tick's height (shotLastH), so a tier change
        // carried by an element swap (tt per-stream elements, a yt / tw replace) still gets its shot
        const prevH = st.h !== null ? st.h : shotLastH;
        if (v === video && prevH !== null && (md.height || 0) > 0 && md.height !== prevH && window === window.top) {
          requestShot("tier_change", lastTick ? lastTick.id : null, md.presentedFrames ?? null);
        }
        st.w = md.width;
        st.h = md.height;
      }
    } finally {
      if (v === video || v.isConnected) v.requestVideoFrameCallback(cb);
      else rvfcState.delete(v);
    }
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
                   ct: video.currentTime ?? null, box: v3On ? playerBox(video) : null, top: window === window.top,
                   instruments: v3On && v3Ready, rvfc: v3On && !!st, off_arm_cb: offArmCb,
                   ad_cb: adStats.cb, ad_checks: adStats.runs, ad_ms: Math.round(adStats.ms * 1000) / 1000,
                   src3_polls: src3Polls,
                   side_dropped: { ...sideDropped }, side_failed: { ...sideFailed }, ...frameLogField(md, q, video) });
  if (st) st.n = 0;
  adStats.cb = adStats.runs = adStats.ms = 0;
  src3Polls = 0;
  // levi 18:26: the counters also go to one small storage key each tick (top frame), so export records their final
  // values in meta.json ext_storage even when the last flush fails after the last tick
  if (window === window.top) {
    try {
      chrome.storage.local.set({ side_counters: { side_dropped: sideDropped, side_failed: sideFailed, off_arm_cb: offArmCb,
                                                  shot_triggers: shotTrig, shot_triggers_suppressed: shotSuppressed,
                                                  at: performance.timeOrigin + performance.now() } });
    } catch (e) { /* counted in the tick rows */ }
  }
}

// v3 A3 (t67; brad 3, prereg dcb64415): ad state at the change, not at the next 1 s tick. A MutationObserver on the
// attached element's player container (6 ancestors up) and the element's duration / metadata events re-evaluate the
// tick's own adSignals() (one definition: the tick's is_ad / ad_signals are unchanged); each change of the signal set
// is an ad_edges record with both clocks. One check per mutation batch.
let adObs = null;
let adLast = null;
let adQueued = false;
const adStats = { cb: 0, runs: 0, ms: 0 };   // brad 18:33: observer callbacks, checks and their main-thread ms, per tick
function adCheck(reason) {
  adQueued = false;
  if (!collecting || !video || !v3On) return;
  const t0 = performance.now();
  const sig = adSignals(video);
  adStats.runs += 1;
  adStats.ms += performance.now() - t0;
  const key = sig.join("|");
  if (adLast !== null && key === adLast) return;
  const pn = performance.now();
  side("ad_edges", { source: "dom", ts: performance.timeOrigin + pn, pn, wall: Date.now(), is_ad: isAdFrom(sig),
                     signals: key, prev: adLast, reason, el: elId(video), ct: video.currentTime ?? null });
  adLast = key;
}
function queueAdCheck(reason) {
  if (adQueued) return;
  adQueued = true;
  (typeof queueMicrotask === "function" ? queueMicrotask : (f) => f())(() => adCheck(reason));
}
function adObserve(v) {
  if (typeof MutationObserver !== "function" || !v) return;
  if (adObs) adObs.disconnect();
  let root = v;
  for (let i = 0; i < 6 && root.parentElement; i++) root = root.parentElement;
  let depth = 0;
  for (let e = v; e !== root; e = e.parentElement) depth += 1;
  adObs = new MutationObserver(() => { adStats.cb += 1; queueAdCheck("dom"); });
  adObs.observe(root, { subtree: true, childList: true, attributes: true,
                        attributeFilter: ["class", "style", "hidden", "data-a-target"] });
  // brad 18:33: the root, once per attach, so the A-window can check it holds what adSignals() reads
  const mp = document.getElementById ? document.getElementById("movie_player") : null;
  playerEvent(v, "lvqoe_ad_root", { root_tag: root.tagName || null, root_id: root.id || null,
                                    root_cls: String(root.className || "").slice(0, 80), depth,
                                    has_movie_player: !!(mp && root.contains && root.contains(mp)) });
  queueAdCheck("attach");
}

// v3 A7 (t67; brad 5, c14): the player box at the tick: the element's rect, its part inside the viewport (CSS px) and
// the DPR, so device px and the visible fraction are on the record per tick, not once per session.
function playerBox(v) {
  if (!v || typeof v.getBoundingClientRect !== "function") return null;
  const r = v.getBoundingClientRect();
  const vw = window.innerWidth ?? null, vh = window.innerHeight ?? null;
  const x0 = Math.max(0, r.left), y0 = Math.max(0, r.top);
  const x1 = vw === null ? r.right : Math.min(vw, r.right), y1 = vh === null ? r.bottom : Math.min(vh, r.bottom);
  const w = Math.max(0, x1 - x0), h = Math.max(0, y1 - y0);
  const area = r.width * r.height;
  return { x: x0, y: y0, w, h, el_w: r.width, el_h: r.height, vw, vh, dpr: window.devicePixelRatio || 1,
           frac: area > 0 ? Math.round((w * h / area) * 1e4) / 1e4 : 0 };
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
    cs: CS_ID,
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
    buffered_ranges: bufferedRanges(video),   // v3 A6
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
  evt.is_ad = isAdFrom(sig);
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
  // v3 (levi 16:57): event shots at the STALL onset and at playback start (the 60 s cadence and the A1 dims trigger do
  // the rest); top frame only, as every shot request
  if (window === window.top && (type === "stall" || type === "video_start")) {
    requestShot(type === "stall" ? "stall_onset" : "start", evt.event_id);
  }
  if (type === "res_meta") {
    lastTick = { id: evt.event_id, ts: evt.ts };
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
    const tf = evt.total_frames;
    const picOk = !evt.paused && !evt.is_ad && ct !== null && ct !== undefined && isFinite(ct)
                  && tf !== null && tf !== undefined && isFinite(tf);
    let picFrozen = false, picReset = false;
    if (picOk && picPrev !== null) {
      picReset = ct < picPrev.ct || tf < picPrev.tf;
      picFrozen = !picReset && ct - picPrev.ct > 0 && tf - picPrev.tf <= FROZEN_FRAMES_MAX;
    }
    if (picFrozen) {
      if (picT0 === null) picT0 = picPrev.ts;
      picLast = evt.ts;
    } else if (!picReset) {
      picT0 = null;
      picLast = null;
    }                                             // a reset pair: bridged, the open run neither grows nor ends
    picPrev = picOk ? { ts: evt.ts, ct, tf } : null;
    // the union of the open runs (stall: frozenT0 .. this tick; picture: picT0 .. picLast), a chain kept while a run
    // that starts at or before the chain's end is open (the same merge as check_session._union_runs)
    const segs = [];
    if (frozenT0 !== null) segs.push([frozenT0, evt.ts]);
    if (picT0 !== null) segs.push([picT0, picLast]);
    if (segs.length === 0) {
      unionT0 = null;
      unionEnd = null;
    } else {
      const s0 = Math.min(...segs.map(s => s[0])), e0 = Math.max(...segs.map(s => s[1]));
      if (unionT0 === null || s0 > unionEnd) {     // a new chain
        unionT0 = s0;
        unionEnd = e0;
      } else {
        unionT0 = Math.min(unionT0, s0);
        unionEnd = Math.max(unionEnd, e0);
      }
    }
    if (window === window.top) {
      document.documentElement.dataset.lvqoeFrozenS = String(unionT0 === null ? 0 : (unionEnd - unionT0) / 1000);
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
      // v3 A14: with A1 running on this element the frame callback fires the tier-change shot; the res_meta trigger
      // stays only where rVFC is not running (the instruments-off arm, no requestVideoFrameCallback)
      // brad 21:19: rVFC does not fire for an element that is not rendered (hidden, not composited); the tick trigger
      // stands down only while this element's frames arrived within RVFC_FRESH_MS
      const rst = v3On && v3Ready && video ? rvfcState.get(video) : null;
      const rvfcFresh = !!(rst && rst.lastAt !== undefined && performance.now() - rst.lastAt <= RVFC_FRESH_MS);
      if (shotLastH !== null && h !== shotLastH && !rvfcFresh) {
        requestShot("tier_change", evt.event_id);
      }
      shotLastH = h;
    }
    if (evt.is_ad && !shotLastAd) requestShot("ad_start", evt.event_id);
    shotLastAd = !!evt.is_ad;
  }

  while (eventBuffer.length > MAX_BUFFER_SIZE) {
    const old = eventBuffer.shift();
    if (old.event_id <= inflightMaxId) shiftedInFlight.push(old.event_id);
    else recordDrop(old.event_id);
  }

  tryFlush();
  console.log("[YT][EVENT]", evt);
}

function recordDrop(id) {
  evDropped.n += 1;
  evDropped.first_id = evDropped.first_id === null ? id : Math.min(evDropped.first_id, id);
  evDropped.last_id = evDropped.last_id === null ? id : Math.max(evDropped.last_id, id);
}

function tryFlush() {
  if (flushInProgress || eventBuffer.length === 0) return;
  flushInProgress = true;

  const batch = eventBuffer.slice();
  const batchMax = batch[batch.length - 1].event_id;
  inflightMaxId = batchMax;

  try {
    chrome.runtime.sendMessage(
      { kind: "video-events-batch", payload: batch, cs: CS_ID, dropped: evDropped.n ? { ...evDropped } : null },
      (resp) => {
        const ok = !chrome.runtime.lastError && resp?.ok;
        flushSettled(ok ? (typeof resp.acked_max_id === "number" ? resp.acked_max_id : batchMax) : null);
      },
    );
  } catch (e) {
    console.log("[YT][ERROR] Message failed", e);
    flushSettled(null);
  }
}

// acked: the highest event_id the background stored (null: the batch was not stored). Drop by id, never by count.
function flushSettled(acked) {
  flushInProgress = false;
  inflightMaxId = -1;
  for (const id of shiftedInFlight.splice(0)) if (acked === null || id > acked) recordDrop(id);
  if (acked === null) return;
  const keep = eventBuffer.filter((e) => e.event_id > acked);
  eventBuffer.length = 0;
  eventBuffer.push(...keep);
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

// t83 (6), W1 tw_100113 +1615.5: Twitch's 340x191 channel preview played beside the self-paused main player; the
// playing-first pick attached it, and its video_start cleared the page's stall label. A rendered element whose box is
// under PICK_SMALL_FRAC of the largest rendered one's is never picked while that one exists; each pick it changed is a
// player_events record (lvqoe_pick_small_skipped, once per element).
const PICK_SMALL_FRAC = 0.25;
const pickSkipLogged = new WeakSet();
function pickBestVideo() {
  // Prefer the element that is actually playing over the first one in DOM
  // order — after an element swap the dead one often remains in the DOM.
  const candidates = [...document.querySelectorAll("video")];
  const area = (x) => (x.clientWidth || 0) * (x.clientHeight || 0);
  const maxArea = Math.max(0, ...candidates.map(area));
  const ok = candidates.filter((x) => !(area(x) > 0 && area(x) < PICK_SMALL_FRAC * maxArea));
  const pick = ok.find((x) => !x.paused && !x.ended) ?? ok[0] ?? null;
  const old = candidates.find((x) => !x.paused && !x.ended) ?? candidates[0] ?? null;
  if (old && old !== pick && !pickSkipLogged.has(old)) {
    pickSkipLogged.add(old);
    playerEvent(old, "lvqoe_pick_small_skipped", { area: area(old), max_area: maxArea, picked_el: elId(pick) });
  }
  return pick;
}

function attachVideo(v, reason = "unknown") {
  if (video === v) return;

  const prev = video;
  video = v;
  v3Start(v, reason, prev);               // v3 A1 / A2 / A3 (one flag), before video_start
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
  if (e.source === window && e.data && e.data.source === "lvqoe-instruments-query") {
    announceInstruments();
    return;
  }
  if (e.source === window && e.data && e.data.source === "lvqoe-flush") {   // browser.py's final flush before export
    tryFlush(); flushSide(); flushStats();
    return;
  }
  if (e.source === window && e.data && e.data.source === "lvqoe-mse") {   // t93 rows 9 / 20 (yt): browser.py MSE_WRAP_JS
    if (collecting && frameLogCfg && Array.isArray(e.data.recs)) {
      for (const r of e.data.recs) side("frames", { k: "mse", ...r });
      mseOver = Number(e.data.over) || 0;
      mseInst = { inst: e.data.inst ?? null, first: e.data.first ?? null };   // levi 22:44 (1): install vs first add
    }
    return;
  }
  if (e.source === window && e.data && e.data.source === "lvqoe-ad-src-polls") {
    src3Polls += Number(e.data.polls) || 0;
    return;
  }
  if (e.source === window && e.data && e.data.source === "lvqoe-ad-src" && collecting && v3On && v3Ready) {
    const { kind, ts, pn, wall, video_id, player_state } = e.data;   // v3 A3 source 3 (page_stats.js, yt player API)
    side("ad_edges", { source: kind, ts, pn, wall, video_id, player_state, el: elId(video) });
    return;
  }
  if (e.source !== window || !e.data || e.data.source !== "lvqoe-page-stats") return;
  if (!collecting) return;
  // v3 A5 (t67): both clocks and the content.js tick the row follows (the playing rendition joins the tick by id)
  statsBuffer.push({ ts: e.data.ts, wall: Date.now(), tick_id: lastTick ? lastTick.id : null,
                     tick_ts: lastTick ? lastTick.ts : null, stats: e.data.stats, url: location.href });
  if (statsBuffer.length > 200) statsBuffer.shift();
});
// boundary after window 1: a batch the background did not store goes back to the buffer (it was dropped silently before)
function flushStats() {
  if (statsBuffer.length === 0) return;
  const batch = statsBuffer.splice(0, statsBuffer.length);
  try {
    chrome.runtime.sendMessage({ kind: "player-stats-batch", payload: batch }, (resp) => {
      if (chrome.runtime.lastError || !resp?.ok) statsBuffer.unshift(...batch);
    });
  } catch (e) {
    statsBuffer.unshift(...batch);
  }
}
setInterval(flushStats, FLUSH_INTERVAL_MS);

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
  if (v3On && v3Ready) adCheck("tick");   // brad 18:33: an ad edge is never later than the tick
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
