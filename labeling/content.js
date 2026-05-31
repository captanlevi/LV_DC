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
  v.addEventListener("loadedmetadata", () => emit("meta"));

  // Use only event-based stall detection — the readyState interval below
  // is intentionally removed to avoid double-counting the same buffer event.
  v.addEventListener("playing", () => {
    if (inStall) {
      inStall = false;
      emit("start");
    }
  });
  v.addEventListener("waiting", () => {
    if (!inStall) {
      inStall = true;
      emit("stall");
    }
  });

  v.addEventListener("resize", () => emit("resize"));
  v.addEventListener("ended", () => emit("end"));
  v.addEventListener("pause", () => {
    if (!v.ended) emit("pause");
  });

  // Catch cases where video is already playing when we attach
  if (!v.paused && !v.ended) {
    emit("start");
  }
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
      const v = document.querySelector("video");
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
const existingVideo = document.querySelector("video");
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
