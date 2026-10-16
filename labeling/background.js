// =====================
// Video QoE Collector (background.js)
// =====================

// Track which tabs have active video collection so we only export when
// the right tab closes (not any unrelated tab).
const videoTabIds = new Set();

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
