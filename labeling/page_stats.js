// =====================
// Video QoE Collector: page-world yt player stats (page_stats.js), LVQoE t35 / G3
// =====================
// Runs in the page's MAIN world (content scripts cannot reach #movie_player's API). Read-only: getters only, never
// any quality setter (no setPlaybackQualityRange / setPlaybackQuality anywhere in the collection path). Posts to the
// isolated content script, which stores these apart from the label events (G3 quarantine: player-reported resolution
// and itag are ground truth, never features).
(function () {
  const POLL_MS = 1000;
  function read() {
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
