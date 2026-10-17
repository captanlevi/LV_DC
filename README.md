# LV_DC: live-video QoE data collection (collection v2)

LV_DC collects one live-video session at a time:
- it plays a live stream in Chrome with a labeling extension;
- it shapes the network through a Markov schedule of bandwidth states;
- it captures the packets, plus the TLS keys and the decoded HTTP logs;
- it writes a `check.md` verdict per session.

The data feeds LVQoE (prediction of resolution and stalls from encrypted traffic).

This README is the operator's reference. History and lessons are in `SESSION_NOTES.md`. Older notes are in `docs/archive/`.

## 1. One-time setup on the collection box

| step | command / rule |
|---|---|
| Python env | `bash setup.sh` (creates `.venv`; `network_core` comes from PyPI) |
| Chrome | branded `google-chrome-stable`, version held for the whole batch: `sudo apt-mark hold google-chrome-stable` (unhold after). The version is recorded in every `meta.json`. |
| IPv6 off | `sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1 net.ipv6.conf.default.disable_ipv6=1` (persist in `/etc/sysctl.d/`). Check that `ip -6 addr show scope global` is empty. Old data is IPv4-only; `check_session` FAILs `ipv6_media`. |
| OS downloads off | `sudo systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer`; `sudo snap refresh --hold`. Log both; undo after the batch. |
| data volume | `data/` may be a symlink to an external disk (chaos: exFAT USB HDD at `/media/<user>/<label>/LV_DC_v2_data`). exFAT has no chown (the capture writes through a root shell), is case-insensitive and allows no symlinks inside. Check `mountpoint /media/<user>/<label>` and free space before a batch; `meta.json` records the realpath, mount and fstype per session. |
| mount-path trap | after a manual `sudo mount`, the empty `/media/<user>/<label>` dir stays, so at the next login udisks mounts at `<label>1` and `data/` dangles. Fix: `sudo umount /media/<user>/<label>1; sudo rmdir` the stale dir(s); remount at `/media/<user>/<label>`. |
| IPv6 off across reboots | persist `net.ipv6.conf.all.disable_ipv6=1`, `net.ipv6.conf.default.disable_ipv6=1` **and** `net.ipv6.conf.<iface>.disable_ipv6=1` in `/etc/sysctl.d/`. The interface line fails at boot, before Wi-Fi exists, so re-apply `sudo sysctl -p /etc/sysctl.d/<file>` after every boot / login. |
| OS downloads across reboots | `systemctl stop` does not survive a reboot. For the batch window, run `sudo systemctl disable --now unattended-upgrades apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer`; re-enable after. |
| launch environment = cookie store | Chrome picks gnome-keyring when `XDG_CURRENT_DESKTOP` / `DESKTOP_SESSION` are set (desktop session), and `basic` when they are not (ssh). Build the templates and run the batch from the **same** environment (chaos: both from ssh). A login-autostart relaunch must `env -i` to the ssh variable set, or the tt login cookie does not decrypt. |
| display | Geometry targets need 1920x1080 (`player:1280x720`, `player:537x880`); check with `xrandr`. Keep the page visible and the box awake: `gsettings set org.gnome.desktop.session idle-delay 0`, `…screensaver lock-enabled false`, `…screensaver idle-activation-enabled false`, `…settings-daemon.plugins.power sleep-inactive-ac-type nothing` (and `-battery-type`). Then `xset s off; xset dpms force off`, plus `--dpms-off` in the batch. |
| audio | mute the sink: `pactl set-sink-mute @DEFAULT_SINK@ 1` (templates and tests play sound; the batch Chrome has `--mute-audio`). |
| bytecode | `export PYTHONDONTWRITEBYTECODE=1`; `__pycache__` is untracked since 2026-10-17, so `meta.json` `lvdc_commit.dirty` stays false. |
| profile templates | §3 |

**Before every start (and after any reboot):**
1. The data volume is mounted at the expected path, with enough free space.
2. `ip -6 addr show scope global` is empty.
3. The OS timers are inactive.
4. `tailscale status` shows the laptop link `direct`.
5. `xrandr` shows 1920x1080, and the display settings above are applied.

## 2. Running the batch

From `networkSim/` (bare imports), detached and logged to a file. The shaper also throttles an operator's ssh, and a
foreground ssh session can drop mid-episode. A non-tty stdin is handled.

```bash
export PYTHONDONTWRITEBYTECODE=1
setsid nohup python -u orchestrate.py --batch youtube=30,twitch=15,tiktok=15 [--dpms-off] [--tt-room-rotation 300] \
    [--fixed-geometry] < /dev/null > ../batch_$(date +%F_%H%M).log 2>&1 &
```

**What one session does:**
1. Clone the platform's template profile into a fresh Chrome profile. The template path and its sha go to `meta.json`.
2. Start shaping and capture, then launch Chrome with the session's TLS key log.
3. Load the labeling extension over CDP and navigate to the stream:
   - yt: the channel rotation in `yt_channels.py`;
   - twitch: the category rotation in `tw_categories.py` (addendum O: talk / fast gaming / slow gaming / esports /
     music-creative / IRL, least-collected first; category and type in `meta.json`), the directory's first live card;
     esports is a fixed list of league channels (riotgames, lck, lec, eslcs, valorant), drawn only when one is live
     (checked between sessions), else skipped for that session and logged in the daily summary;
     in a directory, channels of the last 10 twitch sessions are skipped and the first other card is taken
     (candidates, exclusions and the pick in `meta.json` `tw_pick`);
   - tt: a room from `/live`, chosen by `browser.py` (2026-10-17):
     - rooms of the last 10 tt sessions are excluded;
     - cards with a LIVE badge and ≥ 100 viewers come first, most viewers first;
     - play is clicked, then after a 5 s autoplay grace each tried room must play within 15 s (readyState ≥ 3, playhead
       advancing; tt plays through a MediaStream `srcObject`, so no currentSrc / buffered / decoded bytes), then pass a 60 s
       pre-roll (playhead advancing ≥ 30 s, no dead signal) before the shaper starts;
     - when `/live` renders as the LIVE feed (no room links), the old first-card path is used and recorded (`fallback`);
     - up to 5 rooms; candidates, tries and the choice go to `meta.json` `tt_room_choice`.
4. Fail fast if the extension's content script is not ticking within 15 s.
5. Close yt chat, set the geometry (`geometry.py` rotation), and turn on twitch Video Stats (hidden).
   - A drawn player size larger than the screen can give is clamped to the largest it gives: `player_pin.clamped_to`;
     `ok` then means the clamped size was reached, and `ok` false is a real failure.
   - `video_stats` in `meta.json` says whether the overlay is hidden (`on hidden`) or not (`on VISIBLE`).
6. Run the shaper for the episodes:

   | platform | episodes | matrix | low-state cap |
   |---|---|---|---|
   | yt | 21 | A | 6 |
   | tt | 23 | TT-M | 4 |
   | twitch | 23 | TW-M | 2 |

7. Export labels, player stats and screenshots, stop the capture, decode the HTTP logs, and write `meta.json`.
8. Run `check_session.py` (§5). A FAIL is moved to `data/_failed/<platform>/` and never deleted.

**What the batch loop does:**
- **Next platform:** a platform absent from the last `STALE_AFTER_SESSIONS` (4) sessions goes first; otherwise the one furthest behind its PASS target, but at most `MAX_CONSECUTIVE_SAME_PLATFORM` (2) sessions in a row on one platform. Both rules read the recent sessions from disk, so they survive a restart.
- **Resume:** PASS sessions are recounted from disk at every start.
- **Free space:** it stops below 10 % free (exit 4).
- **Pre-clean:** it undoes leftovers (shaping, tcpdump, Chrome) before every session.
- **Pausing a platform:** after 5 consecutive sessions without a PASS, or on a login wall. A `player_frozen` abort
  (exit 5) or `room_ended` abort (exit 6) is not counted toward the 5; a separate cap pauses the platform after 5
  consecutive such aborts.
- **Not-live yt channel:** skipped for 6 h; 2 not-live failures within 24 h put it out for 24 h after the latest. Read from the failures in `data/_failed`, so a restart keeps the skip (`yt_channels.not_live_skips`).
- **Daily summary:** `data/_summaries/summary_<day>.md / .json` after every session:
  - PASS / FAIL by reason;
  - background shares and hosts;
  - class mix;
  - geometry realised / drawn;
  - sizes, disk and data volume;
  - yt rotation.

**Options:**
- `--dpms-off`: blank the monitor again after each window resize.
- `--tt-room-rotation 300`: the prereg's pre-declared tt fallback, a new room every 5 min. Only when the checkpoint QA says so.
- `--fixed-geometry`: keep each platform's pinned player size instead of the rotation.
- `--tt-matrix` / `--tw-matrix` / `--yt-matrix`: `current` reproduces the old schedule.

**Exit codes:**

| process | code | meaning |
|---|---|---|
| exp.py | 0 | session ran; see its `check.md` |
| exp.py | 1 | start failed (channel not live, extension not ticking); finalised, FAIL |
| exp.py | 3 | login wall: orchestrate pauses that platform |
| exp.py | 6 | `room_ended` (tt): the room died mid-session: the rendered "LIVE has ended" banner with no player or no progress for 5 s, or no progress for the platform's T (addendum N, 720 s) with the interfaces idle; that clock stops during a stall label, for 30 s after one and in shaper states 1-4 (under shaping tt tears its player down and reconnects for 10-15 s after a stall: recorded as `room_discontinuities`, never a cut); finalised, FAIL, kept in `_failed/`; not a failure for the 5-fail pause; the next session picks another room |
| exp.py | 5 | `player_frozen`: the watchdog aborted a player frozen in a stall past T (addendum N); finalised, FAIL, kept in `_failed/`; not a failure for the 5-fail pause; the next session re-picks |
| exp.py | 130 | stopped by SIGINT / SIGTERM; finalised, FAIL, orchestrate stops |
| orchestrate.py | 4 | less than 10 % free space on the data volume |

## 3. Template profiles

- **Location:** one per platform at `<LV_DC>/profiles/<platform>/` (or `$LVDC_PROFILE_TEMPLATES/<platform>`). `profiles/` is gitignored because it holds login cookies.
- **Build:**
  - Use the same user Chrome is launched as (`$SUDO_USER` under sudo), with the default password store. Otherwise the cookies do not decrypt in the clone.
  - Run `google-chrome --user-data-dir=<LV_DC>/profiles/<platform> --no-first-run`, then log in where needed (tt), accept consent prompts, and quit.
- **tt login:** a fresh profile's TikTok login hits "Verify it's really you" (SMS).
  - The account owner does the SMS step by hand at the box's screen. Automated clicking does not trigger the SMS, and bot-evasion clicking is off-limits.
  - Then run a 120 s room check: 0 `quality_countdown` ticks in its `check.md` before the template is used.
- **Never open Chrome on a template during a batch.** Every session records the template's sha, and a change shows.
- **Not cloned:** locks, HTTP and service-worker caches (every session starts cold), crash dumps, session restore, other extensions. Chrome's Safe Browsing database is kept.
- **`--empty-profile`** starts empty instead. It is a recorded deviation and check_session FAILs it.

## 4. Pause / resume protocol

- **Boundary pause (the default, loses nothing):** `kill -STOP <orchestrate pid>`.
  - The running exp.py finishes its session normally (check included), and no new session starts.
  - Copy data, do the checkpoint, or hand the box to the user.
  - Then `kill -CONT <orchestrate pid>` to resume.
- **Stopping (abort):** send SIGINT or SIGTERM to the orchestrate process group, e.g. `kill -INT -<pgid>`.
  - The current session finalises: labels, pcap, meta and check.
  - It is archived as FAIL (`aborted: signal:…`) because it is incomplete.
  - orchestrate exits 130.
- **Kill patterns:** in any `pkill -f`, use the `[x]` form (e.g. `pkill -f "[r]emote-debugging-port=9222"`) or exact pids. Over ssh, a plain pattern also matches the ssh shell's own command line and kills it.
- **Code changes:** only at a pause. Never `git pull` under a running batch, because each new exp.py would load the new code mid-batch. Pull, then rerun the same command; it resumes from the PASS sessions on disk.
- **Copying data off the box:** tailscale (UDP, tailscaled's port) and ssh (TCP 22) bypass shaping and capture; `meta.json` `mgmt_exclusion` records this. A DERP-relayed tailscale link runs over TCP 443 and is not excluded, so check `tailscale status` shows `direct`. Without the exclusion, copy only between sessions.
- **After copying a session to LVQoE:** run `verify_session` there once it lands (LVQoE C2, in progress). It is the t41 checks as a permanent step. Until then: `scripts/analysis/t41_ingest_verify.py --sessions chaos/<platform>/<session>`.

## 5. Guards: `check_session.py`

It writes `check.json` and a one-screen `check.md` per session.

**FAIL reasons (any one fails the session):**

| reason | rule |
|---|---|
| `login_wall` | the platform's logged-out wall stopped playback; the session was aborted |
| `aborted: …` | the session was stopped (signal, start error) |
| `tt_logged_out_cap` | tt's logged-out quality countdown was on screen |
| `no pcap / no packets` | capture missing or empty |
| `ssl_keys.log missing or empty` | v2 keeps the TLS keys |
| `capture gap` | a minute with zero client packets while the shaper ran |
| `no res_meta ticks` / tick presence < `0.95` | the extension's 1 Hz tick |
| `current_time advancing` < `0.8` | of playing, non-ad ticks (it says nothing about stall ticks) |
| `player_frozen` | addendum N: the longest stall-only frozen-playhead run (stall label on, non-ad, playhead within 1 s of the run's first tick) > T: yt `180` s, twitch `660` s, tt `720` s (`1.2` × the old data's max, rounded up to the minute; `FROZEN_PLAYHEAD_MAX_S`). browser.py's watchdog aborts at the same T on the same measure (content.js `data-lvqoe-frozen-s`); `test_frozen_run.py` checks the two agree |
| (reported) `paused_while_playing_share` | playing-labelled non-ad ticks whose element was paused (raw `paused` column); suspicious above `0.05` (levi; every pause is a self-pause and the collector presses play: at once after a stall, otherwise after the pause held > `3` s on a non-ad element with readyState ≥ `3`; it should read about 0) |
| `latched_stall` (I10) | more than `0.5` of within-stall ticks (previous tick also in stall) with the playhead advancing at `0.8`–`1.5` s/s, scored with ≥ `10` such ticks (levi's `i10_stall_advance.py`) |
| `labels.csv lacks the v2 columns` | old extension loaded |
| `usable` < `0.6` | non-ad play or stall time over the session length |
| `ipv6_media` | any selected media bytes on IPv6 flows |
| `exogenous background` > `0.05` | Chrome's own / OS / unnamed flows over the shaper window's downstream (platform first-party traffic is reported, not enforced) |
| `screenshots` | fewer than half the expected cadence shots |
| `meta.json incomplete` | a required field is missing (commit, Chrome, shaper, matrix + cap, channel handle / type on yt, login state on tt, profile template, NTP, …) |

**Reported, never a FAIL:**
- **Labels and ads:** ad ticks per signal and ad spans; stall share; tier mix and changes; yt 144 / 240 / 360p shares.
- **Twitch swaps:** src_changed at tier changes, and swap-only ad spans.
- **tt rooms:** room per tick, room changes, FLV tokens per room, login state / room rotation.
- **Background traffic:**
  - all-background and per-category shares;
  - the top background hosts, and the top unnamed servers when the unknown share is above `0.02`;
  - 60 s windows with exogenous > `0.25` of their downstream.
- **IPv6:** media and all-downstream shares.
- **Shaper QA:** binds per episode, and dwell in seconds / episodes with the longest low-state run beside the cap.
- **Captures:** screenshots by reason; player_stats records.
- **Geometry:** drawn vs realised.

## 6.1 Screenshot capture and dropped frames (measured; settled 2026-10-17)

- **Measure.** `shot_effects.py` (also a check.md row) compares dropped frames per tick within 1 s of a **cadence**
  shot with the rest of the session.
- **The confound, resolved.** Event shots fire at tier changes and ad starts, which drop frames by themselves, so an
  early version that pooled cadence and event shots read as a capture effect. Split by shot type, cadence shots on
  twitch show no excess under any capture (tw_6 / tw_9 / tw_10 / tw_11). The one real effect was the scaled CDP clip
  (yt_10), which made Chrome emulate device metrics on every shot; it was removed.
- **Settled.** 10 s cadence plus event shots, x11 player-rectangle grab. The 30 s fallback was withdrawn. The yt check
  on x11 is pending its first session.

## 6. Session files (`data/<platform>/<abbrev>_N/`)

| file | content |
|---|---|
| `merged.pcap` | ingress + egress capture (management traffic excluded) |
| `ssl_keys.log` | TLS key log (kept) |
| `http_v2.json`, `h1_raw.json`, `h2_raw.pdml`, `h3_raw.pdml` | decoded HTTP logs and raw decoder outputs |
| `labels.csv` | extension ticks and events: v1 columns plus `buffered_ahead_s`, `playback_rate`, `duration_s`, `is_ad`, `ad_signals`, `src_changed`, `login_wall_text`, `login_wall`, `quality_countdown` |
| `player_stats.jsonl` | ground truth only, never features: yt stats for nerds; twitch Video Stats |
| `net_labels.txt` | the shaper schedule (coverage / debug record, not a data covariate) |
| `meta.json` | provenance and covariates: commit, Chrome, extension, profile template + sha, shaper (matrix, cap, rule), channel / room, geometry, login state, room rotation, IPv6 sysctl, data volume, management exclusion, NTP, abort reason |
| `motion.jsonl` | addendum O cross-check (ground truth, never a feature): per pair of 10 s shots, the player-crop downscaled grey MAD / 255, valid only when both ticks are play; summary in `check.md` |
| `shots/` | JPEGs every 10 s, plus one at every tier change and ad start; `index.jsonl` with tick, reason, crop and per-step timing. Grabbed from the X display (Pillow ImageGrab of the player rectangle), outside Chrome's renderer; CDP is the fallback (no X, or a blank grab), recorded per shot as `capture`. Capture effect: see §6.1 |
| `ad_actions.jsonl` | what the ad skipper did, and each play pressed on a self-paused player (`resume_after_self_pause`, `reason` `after_stall` / `no_stall`, `paused_s`) |
| `check.json`, `check.md` | the verdict |
