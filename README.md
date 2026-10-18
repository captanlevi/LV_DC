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
| IPv6 off | Old data is IPv4-only; `check_session` FAILs `ipv6_media`. Persist `net.ipv6.conf.all.disable_ipv6=1`, `net.ipv6.conf.default.disable_ipv6=1` **and** `net.ipv6.conf.<iface>.disable_ipv6=1` in `/etc/sysctl.d/` (chaos: `99-lvqoe-collection-noipv6.conf`). The interface line fails at boot, before Wi-Fi exists, so re-apply `sudo sysctl -p /etc/sysctl.d/<file>` after every boot / login. A **Wi-Fi reconnect** undoes the sysctl: NetworkManager re-applies the connection's `ipv6.method auto` (chaos 2026-10-18 02:20: four global v6 addresses, then tw_19 / tw_20 FAIL `ipv6_media`, tw_18 a capture gap, yt_34 aborted). So for the batch also run `sudo nmcli con modify <wifi-connection> ipv6.method disabled` (undo after: `ipv6.method auto`). orchestrate's preflight re-applies the sysctl and requires `ip -6 addr show scope global` empty before every session. |
| OS downloads off | `sudo systemctl stop unattended-upgrades apt-daily.timer apt-daily-upgrade.timer fwupd-refresh.timer`; `sudo snap refresh --hold`. `stop` does not survive a reboot: re-stop after every boot (chaos: the login hook, `ops/resume_after_login.sh`), or use `disable --now` for the batch window. Log both; undo after the batch. The preflight requires the timers inactive before every session. |
| data volume | `data/` may be a symlink to an external disk (chaos: exFAT USB HDD at `/media/<user>/<label>/LV_DC_v2_data`). exFAT has no chown (the capture writes through a root shell), is case-insensitive and allows no symlinks inside. Check `mountpoint /media/<user>/<label>` and free space before a batch; `meta.json` records the realpath, mount and fstype per session.. Deleting from the GUI on an exFAT volume moves files to `<vol>/.Trash-<uid>` on the **same** disk: no space is freed until the trash is emptied (2026-10-17: 234 GB sat there). |
| mount-path trap | after a manual `sudo mount`, the empty `/media/<user>/<label>` dir stays, so at the next login udisks mounts at `<label>1` and `data/` dangles. Fix: `sudo umount /media/<user>/<label>1; sudo rmdir` the stale dir(s); remount at `/media/<user>/<label>`. |
| launch environment = cookie store | Chrome picks gnome-keyring when `XDG_CURRENT_DESKTOP` / `DESKTOP_SESSION` are set (desktop session), and `basic` when they are not (ssh). Build the templates and run the batch from the **same** environment (chaos: both from ssh). A login-autostart relaunch must `env -i` to the ssh variable set, or the tt login cookie does not decrypt. |
| display | Geometry targets need 1920x1080 (`player:1280x720`, `player:537x880`); check with `xrandr`. Keep the page visible and the box awake: `gsettings set org.gnome.desktop.session idle-delay 0`, `…screensaver lock-enabled false`, `…screensaver idle-activation-enabled false`, `…settings-daemon.plugins.power sleep-inactive-ac-type nothing` (and `-battery-type`). Two modes: **screen on** (the operator watches playback): `xset s off; xset -dpms`, no `--dpms-off`; **screen off** (night): `xset s off; xset dpms force off`, `--dpms-off` in the batch, and `ops/keep_display_off.sh`. x11 shots work with the monitor dark (ti_23: every shot x11; blanks only at readyState < 3). |
| audio | mute the sink: `pactl set-sink-mute @DEFAULT_SINK@ 1` (templates and tests play sound; the batch Chrome has `--mute-audio`). |
| bytecode | `export PYTHONDONTWRITEBYTECODE=1`; `__pycache__` is untracked since 2026-10-17, so `meta.json` `lvdc_commit.dirty` stays false. |
| profile templates | §3 |

**Before every start (and after any reboot):** orchestrate enforces 1-3, the Chrome hold and a root-filesystem floor (`ROOT_MIN_FREE_GB`, 5 GB: per-session Chrome profile clones live on the root fs) before every session (`preflight.py`: exit 8 on failure, result in `data/_summaries/preflight_last.json` and `meta.json` `preflight`); 4-5 stay manual.
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
setsid nohup python -u orchestrate.py --batch youtube=100,twitch=80,tiktok=80 --dpms-off [--tt-room-rotation 300] \
    [--fixed-geometry] < /dev/null > ../batch_$(date +%F_%H%M).log 2>&1 &
```

The targets are floors (rushi): when a platform reaches its floor, it is raised by the same step and the batch goes on. Start the batch in the screen mode you need (§1 display).

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
5. Close yt chat, turn yt captions off (again whenever they come back; `captions_off` in ad_actions.jsonl), set the geometry (`geometry.py` rotation), and turn on twitch Video Stats (hidden). yt's rotation includes `theater:1920x1080` (the window, then yt's theater mode, so the player exceeds 720 device px; `player_pin.theater` records it) beside the small players.
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
- **Free space:** it stops below `MIN_FREE_GB` (25 GB) free (exit 4).
- **Pre-clean:** it undoes leftovers (shaping, tcpdump, Chrome) before every session.
- **Pausing a platform:** after 5 consecutive sessions without a PASS, or on a login wall. A `player_frozen` abort
  (exit 5), `room_ended` abort (exit 6) or `player_dead` abort (exit 7) is not counted toward the 5; a separate cap pauses the platform after 5
  consecutive such aborts.
- **Not-live yt channel:** skipped for 6 h; 2 not-live failures within 24 h put it out for 24 h after the latest. Read from the failures in `data/_failed`, so a restart keeps the skip (`yt_channels.not_live_skips`). Before each yt session a liveness probe (`yt_channels.yt_live_state` / `live_now`, a page fetch between sessions, outside capture) re-draws past a channel that is positively off air; fail open on errors; such pre-check skips go to `_summaries/yt_precheck_skips.jsonl` and are not strikes.
- **yt list maintenance (prereg Addendum P):** a channel with ≥ 3 not-live observations ≥ 6 h apart (start failures or pre-checks that found it off air) and 0 accepted sessions in a rolling 48 h leaves the list; the batch applies it at the pick and writes it to `data/_summaries/yt_list_leaves.jsonl` (kept out after the window), then it is committed in `yt_channels.py` (`LIST_CHANGES`, `REMOVED_CHANNELS`). A spare is promoted only after `yt_live_check.py` logs `isLiveNow` true at ≥ 3 checks ≥ 6 h apart spanning 24 h (`channel_checks/yt_live_checks.jsonl`, committed); `--needed --leaves <data>/_summaries/yt_list_leaves.jsonl` checks the spare of every type a leave left with < 2 owners. Every yt meta.json carries `channel_list` (version and every change). Each live-check row also logs `top_height`, the tallest quality the channel's `/live` page offers.
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
- `--tt-matrix` / `--tw-matrix` / `--yt-matrix`: `current` reproduces the old schedule. In `--batch` they apply only with `--matrix-rule fixed`.
- `--matrix-rule coverage` (the `--batch` default; prereg Addendum Q): each session's shaper matrix comes from the coverage snapshot of the accepted sessions' labels (`coverage_report.py`). The rule:
  - the target is the largest deficit to the floors: 5 sessions with at least 2 min of each class (tt tier classes count HEVC sessions only), the stall band, and 5 tt entries per state;
  - then the matrix: first one with fewer than 2 accepted sessions; otherwise the highest yield for the target; ties go to the first matrix id.
  - The record (snapshot, deficits, yields, choice, rule version) is written to meta.json `coverage`. Its `applied` field says whether the session ran that matrix.
  - `--coverage-allowed 'tiktok=TT-M|current,...'` narrows the allowed set (default: every defined matrix, including twitch `TW-L`, the low-state matrix of Addendum Q amendment 3).
  - tt entry states (amendment 3) count every checked session's entries, accepted or `_failed`, except not_live / login_wall / interrupted.
- Coverage report on demand: `python3 networkSim/coverage_report.py` writes `data/_summaries/coverage_<date>.json` and `.md`:
  - per class: the sessions and minutes;
  - the stall share against its band;
  - tt: entries per state and codec sessions;
  - the deficits;
  - per session: buffer p10 / median / p90 during play, and for twitch the latency mode and latency to broadcaster.
  The daily summary carries the same table.

**Exit codes:**

| process | code | meaning |
|---|---|---|
| exp.py | 0 | session ran; see its `check.md` |
| exp.py | 1 | start failed (channel not live, extension not ticking); finalised, FAIL |
| exp.py | 3 | login wall: orchestrate pauses that platform |
| exp.py | 6 | `room_ended` (tt): the room died mid-session: the rendered "LIVE has ended" banner with no player or no progress for 5 s, or no progress for the platform's T (addendum N, 720 s) with the interfaces idle; that clock stops during a stall label, for 30 s after one and in shaper states 1-4 (under shaping tt tears its player down and reconnects for 10-15 s after a stall: recorded as `room_discontinuities`, never a cut); finalised, FAIL, kept in `_failed/`; not a failure for the 5-fail pause; the next session picks another room |
| exp.py | 7 | `player_dead` (tt): readyState 0 (or no element) with no media appended or decoded for `PLAYER_DEAD_MAX_S` (60 s; supervisor 2026-10-17, ti_20 / ti_21), stall label and shaper state notwithstanding; a player that recovers inside it is a discontinuity. The first death re-enters the same room once (supervisor 2026-10-18; recorded as an entry with reason player_dead and the dead window; the dead interval stays labelled stall) and the session continues if playback is confirmed; a re-entry onto the ended banner ends the session as `room_ended` (the host ended); a second death, or a re-entry that does not play, ends it with exit 7. Not a failure, the room is re-picked. No reload anywhere else. meta.json `player_dead.dead_window`: the shaped downstream over the run (ingress IFB tx) against the shaper rate, `stream` alive (≥ 0.5: the player stopped consuming a live stream) or no_media |
| exp.py | 5 | `player_frozen`: the watchdog aborted a player frozen in a stall past T (addendum N); finalised, FAIL, kept in `_failed/`; not a failure for the 5-fail pause; the next session re-picks |
| exp.py | 130 | stopped by SIGINT / SIGTERM; finalised, FAIL, orchestrate stops |
| orchestrate.py | 4 | less than `MIN_FREE_GB` (25 GB) free on the data volume |
| orchestrate.py | 8 | the per-session preflight failed (`preflight.py`: IPv6, shaper, data volume, holds); result in `data/_summaries/preflight_last.json`; no session started |

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
- **tt stream regime and HEVC** (supervisor 2026-10-18): every tt session's meta.json `tt_stream` records the requested FLV renditions, their codec (h264 / hevc), the room's ladder per codec and the quality-button flag (`tt_stream.py`, from http_v2; check.md row). chaos Chrome 155 reported no HEVC to tt, so the sessions so far are H.264 on a one-rung ladder (`hd`), a recorded regime. **Template rebuild for HEVC** (after the flag set passes shagun's test on a throwaway profile; then the Makefile's CHROME_ARGS carry it): build a new tt template under the new flags as above (login, SMS step, 120 s room check), because the old template caches tt's probe result for 7 days (Local Storage `hevc_support_key_temporary`, `webcodecs_ability_key_temporary`); the room check must show `tt_stream.codec` hevc (an FLV suffix with a 5, e.g. `hd5`). Archive the old template.
- **`--empty-profile`** starts empty instead. It is a recorded deviation and check_session FAILs it.
- **Per-session clones are removed, not archived** (supervisor 2026-10-18): each session's clone (`current_data/chrome_profiles/<platform>_<session>_<t>`) is ephemeral runtime state holding login cookies, not a data product. The template is the reproducible state, and the TLS keys and logs live in the session dir. exp.py removes its clone at session end, after check_session has run (`profile_clone.py`: only a direct child of `chrome_profiles/`, never a template). Clones left from before (102 held 13 GB on chaos's root fs) are removed at a pause, with no Chrome running.

## 4. Pause / resume protocol

The on-box scripts for every step below are in `ops/` (`ops/README.md`; paths in `ops/env.sh`).

- **Boundary pause (the default, loses nothing):** `kill -STOP <orchestrate pid>`.
  - The running exp.py finishes its session normally (check included), and no new session starts.
  - Copy data, do the checkpoint, or hand the box to the user.
  - Then `kill -CONT <orchestrate pid>` to resume.
- **Stopping (abort):** send SIGINT or SIGTERM to the orchestrate process group, e.g. `kill -INT -<pgid>`.
  - The current session finalises: labels, pcap, meta and check.
  - It is archived as FAIL (`aborted: signal:…`) because it is incomplete.
  - orchestrate exits 130.
- **Kill patterns:** in any `pkill -f`, use the `[x]` form (e.g. `pkill -f "[r]emote-debugging-port=9222"`) or exact pids. Over ssh, a plain pattern also matches the ssh shell's own command line and kills it.
- **Code changes:** only at a pause. Never `git pull` under a running batch, because each new exp.py would load the new code mid-batch. A boundary pause (`SIGSTOP`) keeps the old code in memory, so to load new code:
  - at the boundary: `kill -TERM <orchestrate pid>; kill -CONT <orchestrate pid>` (the TERM is delivered on CONT, with no session running), then get the code, then rerun the same command; it resumes from the PASS sessions on disk;
  - **get code onto the box only while no capture runs**: at a pause, or by pushing a ref from the laptop over ssh (`git push <box>:<repo> <ref>:refs/heads/<name>`). A `git fetch` from GitHub during a session would be shaped and captured as background; there is no exclusion for it, by design (fetch is boundary-only);
  - **one boundary script per pause** (`ops/apply_at_boundary.sh`): two at once (one CONTs, the other TERMs) killed the next session's Chrome (tw_15, an operator artifact).
- **Waiting for a session to end under a paused orchestrate:** a finished exp.py is a zombie while its parent is stopped, so `kill -0 <pid>` stays true. Wait for the pid to exit **or** show state `Z` (`ps -o stat= -p <pid>`).
- **Copying data off the box:** tailscale (UDP, tailscaled's port) and ssh (TCP 22) bypass shaping and capture; `meta.json` `mgmt_exclusion` records this. A DERP-relayed tailscale link runs over TCP 443 and is not excluded, so check `tailscale status` shows `direct`. Without the exclusion, copy only between sessions.
- **Per-session verify and frames:** on the box, every PASS is verified as it lands: `verify_session.py --ingest` and `extract_frames.py` (`ops/verify_loop.sh`; chaos-built caches are QA only, never cited). The canonical caches are built on the laptop after the copy (LVQoE `verify_session`, the frozen-manifest rules).

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
| `player_dead` | tt: the longest run of ticks with no media (readyState 0; older ticks: 0x0 at playhead 0) after playback had been sustained ≥ `PLAYER_DEAD_MAX_S` (60 s); the live abort is browser.py's (exit 7) |
| (reported) `session_class` | levi 2026-10-17, first match wins: not_live > login_wall > interrupted > startup_failure > room_ended > player_dead > player_frozen > qa_fail > normal; only `normal` counts toward quotas. startup_failure: no ≥ 5 s run of playing ticks with an advancing playhead within 60 s of the stream entry (a tt start failure counts; a yt / twitch one is not_live). Not a startup_failure when the first shaped state inside that window was <= `SLOW_START_MAX_STATE` (2): the class stays as it otherwise reads and check.json `slow_start_shaped` is true (a covariate; the entry still counts as failed in the startup rates; supervisor 2026-10-18, yt_35) |
| (reported) `suspicious` | stream regime vs the expected table (`stream_regime.py`; check.json `stream_regime`, `suspicious`): `unexpected_codec` (tt not HEVC; twitch not H.264; yt not AV1 / VP9 / H.264); `single_rung_ladder` / `short_ladder` (< 3 rungs offered); `ladder_unknown` (no room page / master playlist / player response captured); `player_below_top` (yt: the smallest offered rung >= the player's device-px height is below the offered top). Never changes the verdict; orchestrate prints a `FLAG` line and a `flags.jsonl` row |
| (reported) `entries` | per entry (the stream entry, always unshaped; each tt room re-entry): shaper state at entry, the first shaped state after it, sustained within 60 s (`entries.py`; meta.json `entries`). batch_summary reports failed / entries per kind and entry state |
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
- **Settled.** 10 s cadence plus event shots, x11 player-rectangle grab. The 30 s fallback was withdrawn. yt on x11
  (shagun, 2026-10-18), dropped frames per tick near a cadence shot vs elsewhere:

| session | near cadence shot | elsewhere | x11 shots |
|---|---|---|---|
| yt_31 | 0.0 | 0.0 | all |
| yt_32 | 0.032 | 0.043 | 133 of 133 |

## 6. Session files (`data/<platform>/<abbrev>_N/`)

| file | content |
|---|---|
| `merged.pcap` | ingress + egress capture (management traffic excluded) |
| `ssl_keys.log` | TLS key log (kept) |
| `http_v2.json`, `h1_raw.json`, `h2_raw.pdml`, `h3_raw.pdml` | decoded HTTP logs and raw decoder outputs |
| `labels.csv` | extension ticks and events: v1 columns plus `buffered_ahead_s`, `playback_rate`, `duration_s`, `is_ad`, `ad_signals`, `src_changed`, `login_wall_text`, `login_wall`, `quality_countdown`, `paused`, `playhead_still_s` (from a99384f), `ready_state` (from 9a01fce) |
| `player_stats.jsonl` | ground truth only, never features: yt stats for nerds; twitch Video Stats |
| `net_labels.txt` | the shaper schedule (coverage / debug record, not a data covariate) |
| `meta.json` | provenance and covariates: commit, Chrome, extension, profile template + sha, shaper (matrix, cap, rule), channel / room, geometry, login state, room rotation, IPv6 sysctl, data volume, management exclusion, NTP, abort reason; geometry also carries `player_rect` (the largest visible video's bounding rect, viewport CSS px), `control_band` (yt / twitch control bar rect and whether it showed; tt none) and `shot` (the rect in device px and in screenshot px, with the shot scales; Addendum O amendment 2), at session start and after each tt room move |
| `compute_load.jsonl` | the box's compute load during the session (supervisor 2026-10-18; a covariate, never a feature): every 5 s the 1-min load average and the running non-collector compute jobs (`compute_load.JOB_PATTERNS`: verify / frames / M0 / M1 / baselines / heavy.sh); summary in `meta.json` `compute_load` |
| `motion.jsonl` | addendum O cross-check (ground truth, never a feature): per pair of 10 s shots, the player-crop downscaled grey MAD / 255, valid only when both ticks are play; summary in `check.md` |
| `shots/` | JPEGs every 10 s, plus one at every tier change and ad start; `index.jsonl` with tick, reason, crop, the player rect, the viewport (CSS px, dpr) and per-step timing. Grabbed from the X display (Pillow ImageGrab of the player rectangle), outside Chrome's renderer; CDP is the fallback (no X, or a blank grab), recorded per shot as `capture`. Capture effect: see §6.1 |
| `ad_actions.jsonl` | what the ad skipper did, and each play pressed on a self-paused player (`resume_after_self_pause`, `reason` `after_stall` / `no_stall`, `paused_s`) |
| `check.json`, `check.md` | the verdict |
