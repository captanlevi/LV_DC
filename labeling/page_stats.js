// =====================
// Video QoE Collector: page-world player stats (page_stats.js): yt stats for nerds, twitch Video Stats; LVQoE t35 / t37 / G3
// =====================
// Runs in the page's MAIN world (content scripts cannot reach #movie_player's API). Read-only: getters only, never
// any quality setter (no setPlaybackQualityRange / setPlaybackQuality anywhere in the collection path). Posts to the
// isolated content script, which stores these apart from the label events (G3 quarantine: player-reported resolution
// and itag are ground truth, never features).
(function () {
  const POLL_MS = 1000;
  // twitch (t37): the player's own Video Stats overlay (settings > Advanced > Video Stats; browser.py turns it on and
  // hides it with CSS so screenshots stay clean; hidden, it still updates): one {row name: value} per poll
  // keep the overlay hidden on every poll: a one-off style rule did not hold on chaos (tw_2: small text top-left in
  // every shot); inline !important on the overlay container (found from its rows) survives re-renders
  function hideTwitchOverlay(rows) {
    if (!document.getElementById("lvqoe-hide-video-stats") && document.head) {
      const st = document.createElement("style");
      st.id = "lvqoe-hide-video-stats";
      st.textContent = '[data-a-target="player-overlay-video-stats"]{opacity:0 !important;pointer-events:none !important}';
      document.head.appendChild(st);
    }
    const box = rows[0].closest('[data-a-target="player-overlay-video-stats"]') || rows[0].closest("table");
    for (let e = box, i = 0; e && i < 3; e = e.parentElement, i++) {
      if (e.style && e.style.getPropertyValue("opacity") !== "0") {
        e.style.setProperty("opacity", "0", "important");
        e.style.setProperty("pointer-events", "none", "important");
      }
      if (e.getAttribute && e.getAttribute("data-a-target") === "player-overlay-video-stats") break;
    }
  }
  function readTwitch() {
    const rows = document.querySelectorAll('[data-a-target="player-overlay-video-stats-row"]');
    if (!rows.length) return null;
    hideTwitchOverlay(rows);
    const vs = {};
    rows.forEach((r) => {
      const c = r.querySelectorAll("td");
      if (c.length >= 2) vs[(c[0].innerText || "").trim()] = (c[1].innerText || "").trim();
    });
    return { twitch_video_stats: vs };
  }
  function read() {
    if (location.hostname.includes("twitch.tv")) return readTwitch();
    const p = document.getElementById("movie_player");
    if (!p) return null;
    const out = {};
    try { if (typeof p.getStatsForNerds === "function") out.nerds = p.getStatsForNerds(); } catch (e) { out.nerds_error = String(e); }
    try { if (typeof p.getPlaybackQuality === "function") out.playback_quality = p.getPlaybackQuality(); } catch (e) {}
    try { if (typeof p.getVideoLoadedFraction === "function") out.loaded_fraction = p.getVideoLoadedFraction(); } catch (e) {}
    try { if (typeof p.getAdState === "function") out.ad_state = p.getAdState(); } catch (e) {}
    return out;
  }
  setInterval(() => {
    const stats = read();
    if (stats) window.postMessage({ source: "lvqoe-page-stats", ts: performance.timeOrigin + performance.now(), stats }, "*");
  }, POLL_MS);
})();
