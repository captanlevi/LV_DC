# Data collection session notes (still true as of collection v2, 2026-10-17)

The operator steps are in `README.md`. The pre-v2 notes are archived unchanged in
`docs/archive/SESSION_NOTES_pre_v2_2026-10-17.md`: old channel lists, calibration results, quality forcing, old commands.

## Rules

- **No quality forcing in the collection path.** Never `setPlaybackQualityRange` / `setPlaybackQuality` (the old calibration-only trick). The player's own ABR decides.
- **Run from `networkSim/`.** The scripts use bare imports. Redirect logs with `python3 -u`.

## Ads

- **yt:** `video.duration` is finite during an ad and `Infinity` on live, but a DVR window can be finite and long, so only < 600 s counts. `#movie_player.ad-showing` / `.ad-interrupting` and the ad DOM are extra signals.
- **Ad masking:** every signal is recorded per tick (`ad_signals`); LVQoE's sensor_v2 masks the spans.
- **yt skipper:** skip buttons inside the player only, never the page's "Skip navigation". It falls through to fast-forward after 2 tries with the ad still showing.
- **twitch:** stitched ad breaks, up to 2:00 when logged out. Never reload the page (it retriggers the ad); mute and wait. `tw_ad_dom` marks them.
- **tt:** no pre-roll on live.

## Platforms

- **yt:** close chat (`#close-button` in the live_chat iframe). Rotation channels: `yt_channels.py` (10 primary across 5 content types, 6 spares).
- **twitch:** chat stays open (pinned size `1274x717`).
  - Video Stats is turned on through the player menu and hidden by CSS; `page_stats.js` reads it every second.
  - The `<video>` element is swapped on quality changes (`src_changed`); a swap alone is not an ad.
- **tt:**
  - Pick a room from `/live`; the old data is on `/@user/live` room pages.
  - Logged out, a room page shows a 60 s quality countdown and then drops one rendition (`tt_logged_out_cap`). The template is logged in.
  - The FLV may come from `pull-*.tiktokcdn.com` or `*.fcdnstatic-intl.com`.
  - No buffered ranges, so `buffered_ahead_s` is null.

## Failure modes seen in the v2 pilots

| symptom | cause | fix in place |
|---|---|---|
| pcap header only | exFAT session dir: `tcpdump -w` cannot chown | root-shell redirect (`sh -c 'exec tcpdump … -w - > file'`) |
| session never ends under nohup | pending stdin read held the exit | daemon stdin reader |
| labels empty on a cloned profile | adopted another extension's service worker | always load ours, match its manifest name; fail fast without ticks |
| background 1.000 | IPv6 client address not counted | per-family client address; IPv6 now off |
| 41 MB per session from optimizationguide-pa | Chrome's model download | one merged `--disable-features` list |
| "No such target id" at start | reused a closing extension tab | ordinary tabs only, retried connect |
| login wall / quality countdown | logged-out profile | template clone per platform (README §3) |
