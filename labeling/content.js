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
  };
}

function emit(type) {
  if (!collecting) return;
  if (type !== "res_meta" && type === lastEventType) return;
  if (type !== "res_meta") lastEventType = type;

  const evt = buildEvent(type);
  eventBuffer.push(evt);

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

  if (!isBuffering && inStall) {
    inStall = false;
    emit("start");
  }
}, RESOLUTION_POLL_INTERVAL_MS);
