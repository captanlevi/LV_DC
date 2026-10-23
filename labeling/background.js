// =====================
// Video QoE Collector (background.js)
// =====================

// Track which tabs have active video collection so we only export when
// the right tab closes (not any unrelated tab).
const videoTabIds = new Set();
let sideSeq = 0;

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // v2 (LVQoE t35): yt player stats are ground truth (G3 quarantine), stored under their own key and exported to
  // player_stats.jsonl, never merged into the label events.
  if (msg.kind === "player-stats-batch") {
    const tabId = sender.tab?.id ?? null;
    const incoming = msg.payload.map((r) => ({ ...r, tabId }));
    chrome.storage.local.get({ player_stats: [] }, (data) => {
      chrome.storage.local.set({ player_stats: data.player_stats.concat(incoming) }, () => sendResponse({ ok: true }));
    });
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

  const incoming = msg.payload.map((evt) => ({
    ...evt,
    tabId,
    url: sender.tab?.url ?? null,
  }));

  chrome.storage.local.get({ events: [] }, (data) => {
    const events = data.events.concat(incoming);
    chrome.storage.local.set({ events }, () => {
      sendResponse({ ok: true });
    });
  });

  return true; // keep channel open for async sendResponse
});

chrome.tabs.onRemoved.addListener((tabId) => {
  videoTabIds.delete(tabId);
});
