// =====================
// Video QoE Collector (background.js)
// =====================

// On message
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.kind !== "video-events-batch") return;

  console.log("[BG][EVENT BATCH]", msg.payload.length);
  const incoming_event = msg.payload.map((evt) => ({
    ...evt,
    tabId: sender.tab?.id ?? null,
    url: sender.tab?.url ?? null,
  }));

  chrome.storage.local.get({ events: [] }, (data) => {
    const events = data.events.concat(incoming_event);
    // Persist to storage immediately
    chrome.storage.local.set({ events }, () => {
      sendResponse({ ok: true });
    });
  });

  return true; // keep sendResponse valid async, i need this in background scripts otherwise it breaks the function and does not wait.
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

  console.log;
  const rows = events.map((e) =>
    header
      .map((key) => {
        const val = e[key];
        if (val == null) return "";
        // Escape quotes for CSV safety
        return `"${String(val).replace(/"/g, '""')}"`;
      })
      .join(","),
  );

  return [header.join(","), ...rows].join("\n");
}

function exportCSV() {
  chrome.storage.local.get({ events: [] }, (events) => {
    const csv = eventsToCSV(events.events); // events is an object with key 'events'

    if (!csv) {
      console.log("[BG] No events to export");
      chrome.storage.local.remove("events", () => {
        console.log("[BG] Cleared stored events");
      });
      return;
    }

    const dataUrl = "data:text/csv;charset=utf-8," + encodeURIComponent(csv);

    chrome.downloads.download(
      {
        url: dataUrl,
        filename: `lv_qoe_${Date.now()}.csv`,
        saveAs: true,
      },
      () => {
        chrome.storage.local.remove("events", () => {
          console.log("[BG] Exported CSV and cleared stored events");
        });
      },
    );
  });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  console.log("[BG] Tab closed:", tabId);
  exportCSV();
});
