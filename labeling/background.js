// =====================
// Video QoE Collector (background.js)
// =====================

// Track which tabs have active video collection so we only export when
// the right tab closes (not any unrelated tab).
const videoTabIds = new Set();
let sideSeq = 0;

// boundary after window 1 (codex item 1, levi 00106): every read-modify-write of a storage array (events, player_stats)
// goes through ONE serialized chain. Unqueued, two frames' get / get / set / set lost the first set's batch (all_frames:
// the tab's main frame and its iframes each run content.js). A batch is acked only after its set resolved, with the
// highest event_id stored, and the sender drops by that id.
let appendQueue = Promise.resolve();
const storedMax = new Map();       // "<tabId>|<frameId>|<cs>" -> the highest event_id stored (a resent batch is not doubled)

function enqueue(job) {
  appendQueue = appendQueue.then(job).catch((e) => console.log("[BG] append failed", e));
  return appendQueue;
}

// rows(existing) -> the rows to append; resolves with the storage error or null
function appendTo(key, rows) {
  return new Promise((resolve) => {
    chrome.storage.local.get({ [key]: [] }, (data) => {
      const getErr = chrome.runtime.lastError;
      if (getErr) return resolve(getErr);
      const add = rows(data[key]);
      chrome.storage.local.set({ [key]: data[key].concat(add) }, () => resolve(chrome.runtime.lastError || null));
    });
  });
}

const errText = (err) => (err ? String(err.message || err) : null);

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // v2 (LVQoE t35): yt player stats are ground truth (G3 quarantine), stored under their own key and exported to
  // player_stats.jsonl, never merged into the label events.
  if (msg.kind === "player-stats-batch") {
    const tabId = sender.tab?.id ?? null;
    const incoming = msg.payload.map((r) => ({ ...r, tabId }));
    enqueue(() => appendTo("player_stats", () => incoming)
      .then((err) => sendResponse({ ok: !err, err: errText(err) })));
    return true;
  }
  // v3 (t67 A2): a side stream (player_events, ...). Each batch under its own key (side:<stream>:<ms>:<seq>), so a flush
  // never rewrites the stream (the events array is read and rewritten whole on every flush); browser.py's export joins
  // the keys of a stream in key order into <stream>.jsonl.
  if (msg.kind === "side-batch") {
    const tabId = sender.tab?.id ?? null;
    const key = `side:${msg.stream}:${String(Date.now()).padStart(15, "0")}:${String(sideSeq++).padStart(9, "0")}`;
    chrome.storage.local.set({ [key]: msg.payload.map((r) => ({ ...r, tabId })) }, () => {
      const err = chrome.runtime.lastError;       // brad 18:17: a failed write is not acked, so the sender re-queues it
      sendResponse({ ok: !err, err: err ? String(err.message || err) : null });
    });
    return true;
  }
  if (msg.kind !== "video-events-batch") return;

  const tabId = sender.tab?.id ?? null;

  // Register this tab as a video tab on first event
  if (tabId !== null) {
    videoTabIds.add(tabId);
  }

  const frameId = sender.frameId ?? null;
  const documentId = sender.documentId ?? null;
  const d = msg.dropped;           // the sender's cumulative overflow drops (recorded, not silent)
  const incoming = msg.payload.map((evt) => ({
    ...evt,
    tabId,
    url: sender.tab?.url ?? null,
    frameId,
    documentId,
    ...(d ? { dropped_n: d.n, dropped_first_id: d.first_id, dropped_last_id: d.last_id } : {}),
  }));
  const ackedMax = msg.payload.reduce((m, e) => Math.max(m, e.event_id), -1);
  const cs = msg.cs ?? null;
  const key = `${tabId}|${frameId}|${cs}`;

  enqueue(() => {
    let after = -1;
    return appendTo("events", (existing) => {
      if (cs === null) return incoming;
      if (!storedMax.has(key)) {   // first batch since this worker started: the stored max from storage
        storedMax.set(key, existing.reduce((m, e) => (e.tabId === tabId && e.frameId === frameId && e.cs === cs
          ? Math.max(m, e.event_id) : m), -1));
      }
      after = storedMax.get(key);
      return incoming.filter((e) => e.event_id > after);
    }).then((err) => {
      if (!err && cs !== null) storedMax.set(key, Math.max(after, ackedMax));
      sendResponse({ ok: !err, acked_max_id: err ? null : ackedMax, err: errText(err) });
    });
  });

  return true; // keep channel open for async sendResponse
});

chrome.tabs.onRemoved.addListener((tabId) => {
  videoTabIds.delete(tabId);
});
