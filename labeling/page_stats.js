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
  function readTwitch() {
    const rows = document.querySelectorAll('[data-a-target="player-overlay-video-stats-row"]');
    if (!rows.length) return null;
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
