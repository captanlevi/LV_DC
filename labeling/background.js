// =====================
// Video QoE Collector (background.js)
// =====================

// Track which tabs have active video collection so we only export when
// the right tab closes (not any unrelated tab).
const videoTabIds = new Set();

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
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
