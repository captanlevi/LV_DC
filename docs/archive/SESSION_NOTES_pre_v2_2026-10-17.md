# Data Collection Session Notes

## Known Good YouTube Live Channels (for calibration / exp.py)
Priority order — higher = more reliable, higher max quality:
1. `https://www.youtube.com/@BBCNews/live`       — 24/7, up to 1080p
2. `https://www.youtube.com/@BloombergLive/live` — 24/7, up to 1080p
3. `https://www.youtube.com/@AlJazeeraEnglish/live` — 24/7, up to 1080p
4. `https://www.youtube.com/@EuroNews/live`      — 24/7, up to 720p
5. `https://www.youtube.com/@SkyNews/live`       — sometimes goes offline (404)
6. `https://www.youtube.com/@NASAtelevision/live`— 24/7 but caps at ~480p
7. `https://www.youtube.com/@ABCNews/live`       — caps at 480p regardless of bandwidth

## YouTube Ad Detection — Lessons Learned
- `#movie_player.ad-showing` CSS class is NOT always set during ads (YouTube rotates implementations)
- Reliable signal: `video.duration` — live streams = `Infinity`, ads = finite (15–30 s)
- Belt-and-suspenders: check all three: `ad-showing` class + `.ytp-ad-preview-container` + `isFinite(duration)`
- `_JS_ADVANCING` must NOT require `!isFinite(duration)` — pages that start with ads have finite
  duration, and we need to detect the page loaded before we can skip the ad.
  Separation of concerns: _JS_ADVANCING = "video is playing anything"; _JS_IS_AD = "is it an ad"

## Calibration Results — YouTube / Sky News (2026-05-20, first run)
NOTE: Results at 1800+ kbit/s are UNRELIABLE — ads were not detected, 480p frames were
counted as stream resolution. Re-run needed at those speeds.

Confirmed reliable (ads < 1800 kbit/s are short enough to clear naturally):
| Speed      | Resolution | Notes                        |
|------------|------------|------------------------------|
| 250 kbit/s | 144p       | confirmed ×2                 |
| 450 kbit/s | 144p       | confirmed ×2                 |
| 650 kbit/s | 240p–360p  | borderline, both seen        |
| 900 kbit/s | 360p       | confirmed ×2                 |
| 1200 kbit/s| 360p       | confirmed ×2                 |
| 1800+ kbit/s| RERUN NEEDED | ad frames counted as stream |

## Current SCENARIOS (shaping.py) — to be updated after re-run
States 1–3 need stall-state cap logic (netStat.py already has max 2 consecutive low episodes).

## Platform-Specific Notes

### Twitch
- Ad-skip: do NOT reload page (kills stream + retriggers ad). Mute video and wait instead.
- Directory page (`/directory/all/live`) → click first card JS is fragile (Twitch rotates class names)
- Fallback channels added: twitchrivals, esl_csgo, riotgames, nasa

### TikTok
- No pre-roll ads on live streams
- Stream picker JS uses multiple selector strategies + offsetParent visibility check

### Bilibili
- Has pre-roll ads with countdown skip button (`.bilibili-player-ipad-ad-skip`)
- Navigate to `live.bilibili.com/` then click room card

## YouTube Quality Forcing (important for calibration at high speeds)
YouTube's ABR algorithm is very conservative — it will stay at 480p even at 8 Mbit/s.
Solution: after ads clear, call `movie_player.setPlaybackQualityRange('hd1080','hd1080')` via CDP.
- `FORCE_QUALITY_ABOVE_KBIT = 1500` in calibrate.py controls the threshold
- `_JS_FORCE_QUALITY` tries hd2160→hd1440→hd1080→hd720 in order (picks best available)
- After forcing, wait 3s for the player to switch before sampling

## Common Failure Modes
1. **404 on YouTube channel** — Sky News occasionally goes offline. Try BBC/Bloomberg first.
2. **Calibration measures ads instead of stream** — check `video.duration`; live = Infinity
3. **`_wait_video_plays` rejects all URLs** — if `_JS_ADVANCING` is too strict (e.g. requires
   Infinity duration), it rejects ad-playing pages. Fix: only check `currentTime/paused/readyState`.
4. **sudo permission issues** — `kill_chrome` make target may fail if Chrome launched by different user.
   Use `sudo make kill_chrome` or `pkill -f remote-debugging-port=9222`.
5. **Python stdout buffering** — always use `python3 -u` flag when redirecting to log files.
6. **Module import errors** — exp.py / calibrate.py must run from inside `networkSim/` dir (bare imports).

## Data Collection Commands
```bash
# Start fresh session
cd /home/captanlevi/Desktop/LV_DC/networkSim
sudo make enable && sudo make start_chrome

# Calibrate a platform (gets speed→quality thresholds)
python3 -u calibrate.py --platform youtube --runs 2

# Quick sanity check (is the live video actually playing?)
python3 -u calibrate.py --platform youtube --sanity
python3 -u calibrate.py --platform twitch --sanity

# Run a data collection session
bash run_session.sh youtube 20

# Kill Chrome
sudo make kill_chrome
```
