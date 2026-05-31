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

function eventsToCSV(events) {
  if (!events.length) return "";

  const header = [
    "ts",
    "type",
    "video_w",
    "video_h",
    "client_w",
    "client_h",
    "dropped",
    "tabId",
    "url",
    "event_id",
  ];

  const rows = events.map((e) =>
    header
      .map((key) => {
        const val = e[key];
        if (val == null) return "";
        return `"${String(val).replace(/"/g, '""')}"`;
      })
      .join(","),
  );

  return [header.join(","), ...rows].join("\n");
}

function exportCSV() {
  chrome.storage.local.get({ events: [] }, (data) => {
    const csv = eventsToCSV(data.events);

    if (!csv) {
      console.log("[BG] No events to export");
      chrome.storage.local.remove("events");
      return;
    }

    const dataUrl = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);

    chrome.downloads.download(
      {
        url: dataUrl,
        filename: `lv_qoe_${Date.now()}.csv`,
        saveAs: false,
      },
      (downloadId) => {
        if (chrome.runtime.lastError || downloadId === undefined) {
          console.log("[BG] Download failed — keeping events in storage");
          return;
        }
        chrome.storage.local.remove("events", () => {
          console.log("[BG] Exported CSV and cleared stored events");
        });
      },
    );
  });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (!videoTabIds.has(tabId)) return; // ignore unrelated tab closes
  videoTabIds.delete(tabId);
  console.log("[BG] Video tab closed:", tabId);
  exportCSV();
});
