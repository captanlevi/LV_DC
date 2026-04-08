// =====================
// YouTube Video QoE Collector (content.js)
// =====================

// for cnn https://edition.cnn.com/videos/fast/cnni-fast

let video = null;
let event_id = 0;
let lastEventType = null;
const eventBuffer = [];
const MAX_BUFFER_SIZE = 1000;
const FLUSH_INTERVAL_MS = 1000;
const RESOLUTION_POLL_INTERVAL_MS = 1000;
let flushInProgress = false;
let collecting = true; // After ending set this to false

/**
 * Build a self-contained event snapshot
 */
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
  };
}

/**
 * Emit event to background.js
 */
function emit(type) {
  // res_meta is a polling event so i treat it as such
  if (!collecting) return;
  if (type !== "res_meta" && type === lastEventType) return;
  if (type !== "res_meta") lastEventType = type;

  const evt = buildEvent(type);

  // Always buffer locally first
  eventBuffer.push(evt);

  // Prevent unbounded growth
  if (eventBuffer.length > MAX_BUFFER_SIZE) {
    eventBuffer.shift();
  }

  tryFlush();

  console.log("[YT][EVENT]", evt);
}

// Flushing function: send buffered events to background.js
// Took care to avoid concurrent flushes, so safe to call from multiple places in an async manner.
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
    // ignore 'Extension context invalidated'
    console.log("[YT][ERROR] Message failed", e);
    flushInProgress = false;
  }
}

/**
 * Attach media event listeners to a video element
 */
function observeVideo(v) {
  v.addEventListener("loadedmetadata", () => emit("meta"));
  v.addEventListener("playing", () => emit("start"));
  v.addEventListener("waiting", () => emit("stall"));
  v.addEventListener("resize", () => emit("resize"));
  v.addEventListener("ended", () => emit("end"));
  v.addEventListener("pause", () => {
    if (!v.ended) emit("pause");
  });

  // Catch cases where video is already playing
  if (!v.paused && !v.ended) {
    emit("start");
  }
}

/**
 * Attach only once per video element
 */
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
  lastEventType = null; // I will allow same event type for end, as this is unique and must survive shutdown.

  emit("end");
  tryFlush();

  if (video) {
    video = null;
  }

  observer.disconnect();
  console.log("[YT] Collection ended:", reason);
}

/**
 * MutationObserver: detects when YouTube inserts/replaces <video>
 */
const observer = new MutationObserver((mutations) => {
  for (const m of mutations) {
    if (m.addedNodes.length > 0) {
      const v = document.querySelector("video");
      if (v) {
        attachVideo(v);
        break;
      }
    }
  }
});

// Start observing the whole document
observer.observe(document.documentElement, {
  childList: true,
  subtree: true,
});

// Fallback: try once in case video already exists
const existingVideo = document.querySelector("video");
if (existingVideo) {
  attachVideo(existingVideo);
}

window.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    tryFlush();
  }
});

window.addEventListener("pagehide", () => {
  tryFlush();
});

setInterval(() => {
  tryFlush();
}, FLUSH_INTERVAL_MS);

setInterval(() => {
  if (!video) return;
  emit("res_meta");
}, RESOLUTION_POLL_INTERVAL_MS);
