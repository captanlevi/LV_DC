# FROZEN_COLLECTOR.md: the collector's frozen behaviour (living document)

**What this is.** Every collector behaviour a reading depends on, with its value, the code, what pins it, and the prereg clause behind it. The prereg is `docs/analysis/design/collection_v2_prereg_levi_2026-10-16.md` in LVQoE ("Q am. 6" = Addendum Q amendment 6).

**Who does what:**
- peter fills the rows from the code.
- levi checks each row against the clause it cites. A mismatch is a finding, not an edit.
- Started 2026-10-18 at lvdc `1bcdc5e` (the tt media instrumentation), on lvdc-v2 `7a248b3`. levi's first audit is applied: the clause fixes, F1 fixed, F2 pinned, and the unpinned values now under `test_frozen_constants.py` / `test_check_schema.py`.

**Conventions:**
- Paths are under `networkSim/` unless shown otherwise.
- Status values:
  - **frozen**: in force and pinned;
  - **unpinned**: in force, no test asserts the value;
  - **pending**: on a branch, not yet on lvdc-v2;
  - **not built**: a clause with no code yet;
  - **finding**: something levi should rule on.
- Tests are plain scripts. `ops/apply_at_boundary.sh` runs every `test_*.py` except the live `test_browser.py`, and a failure leaves the batch paused.

## 1. Session classes and precedence

| behaviour | value / rule | code (file:symbol) | pinned by | prereg clause | status |
|---|---|---|---|---|---|
| precedence | not_live > login_wall > interrupted > startup_failure > room_ended > player_dead > player_dead_recovered > player_frozen > qa_fail > normal; first match wins | entries.py:CLASS_PRECEDENCE, session_class | test_player_dead.py:test_precedence, test_frozen_constants.py | Q am. 12 (classes as built), Q am. 4 / am. 6 (recovered) | frozen |
| not_live | abort_reason holds `NOT_LIVE_MARK` ("Failed to navigate to a live"), on yt / twitch | entries.py:session_class | test_precedence, test_slow_start.py | Q am. 12 | frozen |
| login_wall | abort_reason == "login_wall" | entries.py:session_class | test_precedence | Q am. 12 | frozen |
| interrupted | abort_reason starts with "signal" / "interrupted" | entries.py:session_class | test_precedence | Q am. 12 | frozen |
| startup_failure | a start failure on tt, or the stream entry neither `sustained_60s` nor `slow_start_shaped` | entries.py:session_class, slow_start_shaped | test_precedence, test_tt_start_failure_entry, test_slow_start.py | Q am. 12 | frozen |
| room_ended | abort_reason == "room_ended" (banner, or rule (b) of TTRoomWatch) | entries.py:session_class; browser.py:TTRoomWatch | test_precedence, test_tt_room.py | Q am. 12 | frozen |
| player_dead | abort_reason == "player_dead", or the longest non-recovered dead run ≥ T (tt 60 s) | entries.py:session_class, dead_over_t (`>=`) | test_precedence, test_entries_and_class, test_ti37_recovered_reads_normal, test_class_switches_and_boundaries | Q am. 12, am. 4 | frozen |
| player_dead_recovered | returned only when `RECOVERED_DEAD_CLASS != "normal"` (it is "normal", ruling (a)) | entries.py:RECOVERED_DEAD_CLASS | test_ti37 (flips the constant) | Q am. 4 | frozen |
| player_frozen | abort_reason == "player_frozen" (watchdog at T, or the stuck rule's censor), or the longest non-recovered frozen run > T | entries.py:session_class, frozen_over_t (`>`) | test_precedence, test_stuck.py:test_check_recovered_and_censored, test_class_switches_and_boundaries | Q am. 12, N, Q am. 6, am. 7 (1) | frozen |
| player_frozen_recovered | returned only when `RECOVERED_FROZEN_CLASS != "normal"` (it is "normal"); after player_frozen in the precedence | entries.py:RECOVERED_FROZEN_CLASS | test_class_switches_and_boundaries (flips each switch alone) | Q am. 6, am. 12 | frozen (F1 fix) |
| qa_fail / normal | verdict != PASS / otherwise | entries.py:session_class | test_precedence | Q am. 12; Q (acceptance = QA verdicts) | frozen |
| recovered dead | the dead run holding an outcome-continue re-entry's t_nav (±`RECOVERY_SLACK_S` 2.0 s) is excluded and reported as `recovered_dead_s` + window; one per session | entries.py:dead_player, RECOVERY_SLACK_S | test_ti37_recovered_reads_normal | Q am. 4 | frozen |
| recovered frozen | the frozen run holding a continue re-entry's t_nav (2.0 s before the run to 5.0 s after) is excluded and reported as `recovered_frozen_s`, window, post-recovery window, rung, shaper; only with no dead recovery | check_session.py:recovered_frozen, label_checks | test_stuck.py:test_check_recovered_and_censored | Q am. 6 | frozen; the revocation in label_checks (a frozen continue re-entry revokes a dead recovery) is pinned by test_check_schema.py:test_dead_recovery_revoked_by_a_frozen_continue |
| censored | set only by the stuck rule's second T2, or a re-entry that does not play: `{reason: second_freeze \| freeze_after_recovery \| reentry_not_playing, t, ...}` plus abort player_frozen. The backstop watchdog at T sets player_frozen without a censored record | browser.py:_stuck_tick; stuck.py:action | test_stuck.py:test_stuck_tick | Q am. 6, am. 11 | frozen. The checkpoint-list status is LVQoE's (am. 11) |
| F1 (fixed) | session_class took one `recovered` flag for both kinds; now `recovered_dead` / `recovered_frozen`, each with its own switch | entries.py:session_class; check_session.py:main | test_class_switches_and_boundaries | Q am. 4 / am. 6 | fixed (levi's audit) |
| F2 (kept) | dead fails at `≥ T`, frozen above `T`, as intended; one helper each, used by the class and the verdict | entries.py:dead_over_t, frozen_over_t; check_session.py:verdict | test_class_switches_and_boundaries (dead: T−1 tick passes, T fails; frozen: T passes, T+1 tick fails; class and verdict) | Q am. 12 | frozen |

## 2. Thresholds

| behaviour | value / rule | code | pinned by | prereg clause | status |
|---|---|---|---|---|---|
| T table (backstop) | yt `180` s, twitch `660` s, tt `720` s; FAIL on > T; the watchdog aborts at T | check_session.py:FROZEN_PLAYHEAD_MAX_S; browser.py:_ad_watcher_loop | test_frozen_constants.py | N, Q am. 7 (2) | frozen |
| tt dead T | `60` s (readyState 0 / no element with no media appended or decoded) | check_session.py:PLAYER_DEAD_MAX_S; browser.py:TTRoomWatch | test_player_dead.py:test_recovery_is_a_discontinuity (59 s does not fire, 62 s fires), test_ti20_replay, test_ti32 | Q am. 4 (tt dead rule, supervisor 2026-10-17) | frozen |
| frozen-run measure | after the first move of > `1.0` s (FROZEN_TOL_S), a run opens at the first non-ad tick whose step is not in (`0.2`, `3.0`] s, whatever the label. It ends only after `10` consecutive advancing ticks (the end is where that advance began) or at an ad tick. A tick without current_time keeps it open | labeling/content.js:FROZEN_TOL_S / FROZEN_ADVANCE_TICKS / FROZEN_ADV_MIN_S / FROZEN_ADV_MAX_S; check_session.py:FROZEN_ADVANCE_TICKS, FROZEN_ADV_STEP_S, frozen_runs, frozen_playhead_run | test_frozen_run.py (JS = Python; needs node, else SKIP), test_stuck.py:test_tw1_tw27 (tw_27 longest > 660; tw_1 player_frozen), test_frozen_constants.py (both sets) | Q am. 7 (1), am. 12 ("did not advance" = a step outside (0.2, 3] s, a skip > 3 s included) | frozen |
| T2 | `90` s, its clock running only while the shaper is ≥ `1.5` × the lowest rung **read from the stream** (never the fallback; one exception: twitch's `ladder_160p_transcode`, `230` kbit, when the player's quality menu lists 160p and the master is unread; the menu, which reading opens and closes, is read only inside a frozen run ≥ `60` s (`LADDER_PROBE_FROZEN_S`), once per run, a second attempt only after a raised read (`LADDER_PROBE_ATTEMPTS` `2`), each read's open / close in meta `rung_probes` and the session log; cadence shots within `3` s of a probe are flagged in check.json `shots.menu_overlay`, which motion_gt / R-O skip); start = max(run start, start of that stretch); a run still on the fallback at T2_S logs `t2_skipped_rung_unknown` (meta `t2_skipped`) | stuck.py:T2_S, RUNG_MARGIN, above_since, t2_clock | test_stuck.py:test_t2_cases, test_stuck_tick | Q am. 6, am. 7 (3) | frozen (exact values: test_frozen_constants.py) |
| stuck-rule action | first T2: one re-entry of the same channel / room, which must play within `60` s; second T2, or no resume: censor | stuck.py:action, REENTRY_CONFIRM_S; browser.py:_reenter_frozen, _reenter_room (tt: TT_REENTRY_CONFIRM_S 60) | test_stuck_tick; test_reenter_room_records_an_entry (tt 60 s) | Q am. 6 | frozen (REENTRY_CONFIRM_S / TT_REENTRY_CONFIRM_S: test_frozen_constants.py) |
| one recovery per session | `RECOVERIES_PER_SESSION = 1` across player_dead and frozen re-entries, under one lock (`_recovery_lock`; used = dead + frozen re-entries) | stuck.py:RECOVERIES_PER_SESSION; browser.py:_recovery_lock | test_stuck_tick (freeze_after_recovery); test_one_reentry_then_the_session_ends | Q am. 4, am. 6 | frozen |
| tt dead re-entry | `PLAYER_DEAD_REENTRIES = 1`; the re-entry is a shaped entry | browser.py:PLAYER_DEAD_REENTRIES, _reenter_room, dead_action | test_one_reentry_then_the_session_ends, test_ti33_reentry_window | Q am. 4 | frozen |
| dead_window verdict (recorded only) | alive iff shaped rx ≥ `0.5` × min(shaper rate, shaped rx over the `30` s before the dead clock while the player progressed on ≥ `0.8` of ticks); else no_media. Detection is unchanged | browser.py:dead_window, PLAYER_DEAD_ALIVE_SHARE, PLAYING_REF_S, TTRoomWatch.playing_kbps | test_player_dead.py:test_ti43_dead_window_reference, test_ti32, test_frozen_constants.py | none (diagnostic; supervisor 2026-10-18) | pending `1bcdc5e` / `c826984` (levi: OK to ff) |
| lowest rung | twitch: HLS master's lowest BANDWIDTH (MasterListener, auto-attached to the player's workers, where twitch fetches it). yt: lowest video + lowest audio (player response). tt: `vbitrate` for the codec (room sdk_params). Fallbacks twitch `230`, yt `130`, tt `1800` kbit; source recorded | stuck.py:rung_from_master, rung_from_yt_formats, rung_from_tt_sdk, rung_record, FALLBACK_RUNG_KBIT; browser.py:_current_rung | test_stuck.py:test_rungs | Q am. 7 (3) | frozen (fallbacks: test_frozen_constants.py) |
| the entry rule | a censored stall whose frozen window starts within `60` s of an entry fails that entry (`sustained_60s` false, `censored_stall` true) | entries.py:compute_entries, SUSTAIN_WINDOW_S | test_stuck.py:test_entries | Q am. 6 | frozen |
| entry fields | kind (stream / room_reentry / reentry), t, url, rotation_ok, reason, dead_window, outcome, shaper_at_entry {state, rate_kbit}, first_shaped_after, sustained_60s (a `5` s run within `60` s), sustained_at, start_failed, censored_stall | entries.py:compute_entries, SUSTAIN_RUN_S | test_player_dead.py (test_rates_and_reentry, test_entries_and_class), test_slow_start.py | Q am. 4 (per-entry startup rate) | frozen |
| slow start | an unsustained stream entry whose first shaped state in the window is ≤ `2` is slow_start_shaped, not startup_failure | entries.py:slow_start_shaped, SLOW_START_MAX_STATE | test_slow_start.py, test_frozen_constants.py | Q am. 12 (it counts as not sustained in the startup rate) | frozen |

## 3. Recorded fields

| field | content | writer | pinned by | prereg clause | status |
|---|---|---|---|---|---|
| meta shaper | matrix_id, low_state_cap, initial_state_rule, forced_initial_state | exp.py → session_meta.start | check_session meta_check requires matrix_id + low_state_cap | M, Q | frozen (the writers are untested) |
| meta coverage | the snapshot itself, rule version, allowed set, deficits, chosen matrix, target, yields, why, taken_at, applied. There is no separate snapshot id: the snapshot is embedded | coverage_report.choose, meta_record; exp.py | test_coverage.py | Q | frozen |
| meta lowest_rung | {kbit, source, detail, t} | browser.py:_current_rung → exp.py | test_stuck.py | Q am. 7 (3) | frozen |
| meta decode_path | spec (name, flags, env) + gpu (SystemInfo at launch) + page (MSE / WebCodecs hvc1) + hevc / source / probes (the decision) | decode_path.py; browser.py; exp.py | test_decode_path.py | Q am. 8 | frozen (decision fields from `cdc6204`) |
| meta template_note | the template's TEMPLATE_NOTE.txt only | profile_clone.template_note_of | test_decode_path.py | Q am. 8 (regime boundary) | frozen |
| meta tt_stream | the codec, requested suffixes, ladder per codec, quality button (tt only) | tt_stream.py → exp.py | test_tt_stream.py | Q am. 2 / am. 8 | frozen |
| meta player_dead / player_dead_reentries | reason, at, sample, shaper_state, room, dead_s, dead_window; re-entries: t_nav, t_enter, ok, t_playing, confirm, outcome. New: `media` | browser.py → exp.py | test_player_dead.py | Q am. 4 | frozen; `media` pending `1bcdc5e` |
| meta frozen_reentries / censored | the stuck rule's re-entries and its censor record | browser.py:_stuck_tick → exp.py | test_stuck.py | Q am. 6 | frozen |
| recovered_dead_s / recovered_frozen_s | check-time only (check.json labels), with windows; not in meta | check_session.py:label_checks | test_ti37, test_check_recovered_and_censored | Q am. 4 / am. 6 | frozen |
| meta compute_load | Sampler summary (1-min load, running compute jobs every 5 s; compute_load.jsonl) | compute_load.py → exp.py | test_compute_load.py | ck1 spec (`checkpoint1_rerun_spec_brad_2026-10-18.md`) §4 "Compute on the collecting box" | frozen |
| media_events.jsonl + player_dead.media | tt: CDP Media domain per player (decoder, resolution, events, errors, error / warning / info messages); at a dead trigger each `<video>`'s error / state, Chrome's video_decode, the listener snapshot. Recording only, no flag | browser.py:MediaListener, _media_snapshot | test_player_dead.py:test_media_listener_records | none (diagnostic, supervisor 2026-10-18, ti_43) | pending `1bcdc5e` / `c826984` (snapshot capped at 2 s, `snapshot_s`; levi: OK to ff) |
| entry_quality | yt / twitch, `--entry-quality` (default off): {rule, t_entry, selected, menu_available, auto_restored, options, entry_auto_state, choice, t_pick, t_effect, t_auto, verify, auto_clicks, settle_trace, t_settle_s, reason}; a re-entry's `setup.entry_quality` {was_auto, auto_restored, verify}; check.json `entry_quality` with `qa_fail` (a restore not verified from state → FAIL) | entry_quality.py:entry_pick, reentry_check, verify_auto, t_settle; browser.py:_entry_quality_pick, _after_reentry_setup; check_session.py:entry_quality_summary | test_entry_quality.py, test_check_schema.py, test_frozen_constants.py | Q am. 1, am. 10 | pending (flag off until the laptop check and the 3-per-platform chaos pilot) |
| session counter | `_summaries/session_counter_<platform>.json`; never reuses a removed session's name | session_names.py | test_session_names.py | (supervisor 2026-10-18) | frozen |
| check.json | verdict, session_class, entries, reasons, thresholds, labels (incl. recovered_*), traffic, shots, ad_actions, matrix, low_state_cap, stream_regime, suspicious, censored, lowest_rung, decode_path, decode_hevc (decision with the played codec), template_note. tt_stream is in check.md only | check_session.py:main | test_check_schema.py:test_check_json_schema (keys, scalar types, labels keys; on a ti_37-built session) | read by LVQoE ingest | frozen |

## 4. Matrices and the coverage rule

| behaviour | value / rule | code | pinned by | prereg clause | status |
|---|---|---|---|---|---|
| tt matrices | `current` (cap 2, linear start), `TT-M` (cap 4, stationary start) | utils/shaping.py:TIKTOK_MATRICES | test_frozen_constants.py | M | frozen |
| twitch matrices | `current` (2, linear), `TW-M` (2, stationary), `TW-L` (2, stationary) | utils/shaping.py:TWITCH_MATRICES, TWITCH_TRANSITIONS_L | test_coverage.py (TW-L), test_frozen_constants.py (all) | M, Q am. 3 | frozen |
| yt matrices | `current` (2, linear), `A` (6, stationary), `B` (6, stationary) | utils/shaping.py:YOUTUBE_MATRICES; utils/netStat.py (yt cap 6 under A / B, t38) | test_frozen_constants.py | A | frozen |
| initial state | linear (weights 1..n) or the stationary distribution, per matrix; `--initial-state` overrides | utils/shaping.py:initial_state, initial_state_stationary; exp.py; utils/netStat.py (`--initial-state`) | test_frozen_constants.py (the rule per matrix) | M | frozen |
| low-state cap | after `cap` consecutive episodes in states ≤ 3, the next is forced to state 4 | utils/netStat.py (_LOW_STATE_THRESHOLD 3, _MAX_CONSECUTIVE_LOW, _MIN_RECOVERY_STATE "4") | test_coverage.py (TW-L), test_frozen_constants.py | M, Q am. 3 | frozen |
| episodes | `60` s each; yt `21`, tt `23`, twitch `23` | exp.py (episode_time_in_seconds); orchestrate.py:EPISODES_V2 | test_frozen_constants.py | collection prereg §1, the session-length row (round(current-era median ÷ 60)); M (23 × 60 s) | frozen |
| geometry rotation | per platform a fixed list, the least-collected entry next (list order breaks ties); yt: player 853x480, player 1280x720, window 1920x1080, theater 1920x1080, theater 1920x1080 at device scale `1.5` (`@1.5`: `--force-device-scale-factor`, layout only; the requested scale and the page's devicePixelRatio in player_pin) | geometry.py:GEOMETRY_CHOICES, next_geometry, device_scale, chrome_args; exp.py:open_chrome_with_ssl_key_log; browser.py (player_pin) | test_geometry_rect.py:test_device_scale_variant, test_frozen_constants.py | Q am. 2 (player size is a covariate) | frozen |
| steering | `MATRIX_RULE = "coverage"`; allowed = every defined matrix (`COVERAGE_ALLOWED` empty); fixed fallback ids A / TT-M / TW-M | orchestrate.py:MATRIX_RULE, COVERAGE_ALLOWED, MATRIX_IDS, session_matrix | test_coverage.py | Q, Q am. 3 (1) | frozen |
| the rule | largest deficit first; any matrix with < `2` sessions explored; then highest yield (tier: mean minutes; STALL: mean onsets; tt entry state: stationary probability); ties to the lowest id; no deficit → DEFAULT_MATRIX | coverage_report.pick_matrix, EXPLORE_MIN, DEFAULT_MATRIX, RULE_VERSION `coverage-v2` | test_coverage.py | Q, Q am. 3 (2) / (5), am. 5 | frozen |
| floors | `5` sessions with ≥ `2` min of every tier class; STALL: `5` sessions with ≥ `3` onsets; tt: each shaper state at ≥ `5` room entries (every recorded entry except not_live / login_wall / interrupted); pooled stall band reported | coverage_report.FLOOR_SESSIONS, FLOOR_MINUTES, STALL_FLOOR_ONSETS, TT_ENTRY_FLOOR | test_coverage.py | Q, Q am. 3 (4), am. 5 | frozen |
| entry-tier rotation | `ENTRY_RULE`: of the offered tiers, the largest class deficit on the matrix's own coverage snapshot; ties to the higher; no deficit → the top; < `2` resolution options → no-op, outside the rotation; the pick waits ≤ `60` s from the stream entry for ad-free content (else none, reason ad) and holds ≤ `15` s until the picked height plays | entry_quality.py:choose_tier, AD_WAIT_S, PICK_HOLD_MAX_S, MIN_OPTIONS | test_entry_quality.py, test_frozen_constants.py | Q am. 1 / am. 10 | pending (flag off) |

## 5. Fixtures as goldens

Run with `cd networkSim && for t in test_*.py; do [ "$t" = test_browser.py ] \|\| ../.venv/bin/python "$t"; done`. That is the same set as apply_at_boundary.sh.

| fixture | pins | test |
|---|---|---|
| fixtures/tt_room_html_2026-10-17.html | the ended-banner phrase on real pages | test_tt_room.py:test_ended_phrase_on_real_pages |
| fixtures/tt_ti12_src_drops.json | a reconnect is a discontinuity, not a cut | test_tt_room.py:test_ti12_reconnects_are_discontinuities |
| fixtures/tt_ti20_session_2026-10-17.json, tt_ti20_ticks_2026-10-17.json | player_dead at 60 s; entries and class | test_player_dead.py:test_ti20_replay, test_entries_and_class |
| fixtures/tt_ti32_player_dead_2026-10-18.json | dead after the element's end with the stream alive | test_player_dead.py:test_ti32 |
| fixtures/tt_ti33_reentry_2026-10-18.json | the tt re-entry window (60 s) | test_player_dead.py:test_ti33_reentry_window |
| fixtures/tt_ti37_recovered_2026-10-18.json | a recovered death reads normal | test_player_dead.py:test_ti37_recovered_reads_normal |
| fixtures/tw_player_dom_self_pause_2026-10-17.html, tw_player_state_self_pause_2026-10-17.json | the self-pause resume on a real twitch DOM | test_resume.py:test_real_twitch_self_pause_fixture |
| fixtures/tw1_frozen_2026-10-18.json | tw_1 player_frozen under the am. 7 measure | test_stuck.py:test_tw1_tw27 |
| fixtures/tw27_frozen_2026-10-18.json | tw_27's run > 660 s; recovered / censored at check | test_stuck.py:test_tw1_tw27, test_check_recovered_and_censored |

| fixtures/frozen_constants.json | every experiment-defining constant: the T table, the frozen measure (Python + content.js), the stuck rule, the tt dead rule and its diagnostics, the classes and their switches, the entry rule, the QA checks, the matrices (tables, caps, initial-state rules), the low-state window, the episodes, the coverage rule and floors, the decode path. A change edits the json in the same commit (`--write`) with a change-log row | test_frozen_constants.py |
| fixtures/tt_ti37_recovered_2026-10-18.json (again) | check.json's keys and types; the one-recovery revocation | test_check_schema.py |

No behaviour in §1–4 is unpinned now.

## 6. Change log

"First session" means the first session per platform whose recorded `lvdc_commit` contains the change. Changes are applied at boundaries, so commit time is not used.

| date | change | code sha | first session (tt / twitch / yt) | prereg clause | signed |
|---|---|---|---|---|---|
| 2026-10-18 | coverage fixes; tt_stream regime record; yt theater 1920x1080 (the one behaviour change: the Q am. 2 player-size covariate, player_pin.theater) | `8141759` | ti_40 / tw_27 / yt_39 | Q am. 2 | levi |
| 2026-10-18 | coverage steering (snapshot → matrix) | `c13b3e6` | ti_40 / tw_27 / yt_39 | Q | levi (Q am. 3) |
| 2026-10-18 | first-session fault flags | `c3c5dbe` | ti_40 / tw_27 / yt_39 | Q am. 3 (3) | levi |
| 2026-10-18 | TW-L; tt entry count | `75640a8` | ti_40 / tw_27 / yt_39 | Q am. 3 | levi |
| 2026-10-18 | tt recovered death reads normal | `c9f465e` | ti_40 / tw_28 / yt_39 | Q am. 4 | levi |
| 2026-10-18 | monotonic session counter | `1a97037` | ti_40 / tw_28 / yt_39 | — (fault fix) | — |
| 2026-10-18 | am. 5–6 (stall onsets, stuck rule) + frozen-measure fix | `8eec432` | ti_40 / tw_29 / yt_40 | Q am. 5, am. 6, am. 7 (1) | levi |
| 2026-10-18 | T2 clock only at ≥ 1.5 × lowest rung | `bd0231b` | ti_40 / tw_29 / yt_40 | Q am. 7 (3) | levi |
| 2026-10-18 | tt VA-API HEVC flag set (yt / twitch unchanged) | `62f2e1b` | ti_40 / tw_29 / yt_40 | Q am. 8 | levi |
| 2026-10-18 | **HEVC regime boundary**: the tt template rebuild (TEMPLATE_NOTE: verified by room check `tt_hevc_roomcheck_20261018_1337`) | template, not code | last H.264: ti_39 (11:33); first HEVC: ti_40 (13:42) | Q am. 8 | levi |
| 2026-10-18 | decode decision recorded with its source (page probe on chaos); template_note in meta | `cdc6204` | ti_41 / tw_29 / yt_40. ti_40 predates it: `decode_path_unrecorded`, regime from the played codec (levi's ruling) | Q am. 8 | levi |
| 2026-10-18 | ops: verify / frames loop from one LVQoE worktree (no collector behaviour) | `c77f524` | ti_41 / tw_32 / yt_41 | — (ops) | supervisor |
| 2026-10-18 | tt media-internals recorder; dead_window reference (recorded verdict only); the snapshot capped at 2 s | `1bcdc5e`, `c826984` | pending (fills when it lands) | none (diagnostic) | levi (OK to ff) |
| 2026-10-18 | yt geometry `theater:1920x1080@1.5` added to the rotation (device scale 1.5, layout only: shagun's audit, yt_39–41's theater player stayed 699 device px tall and never played above 720p, so the 1080p floor could not fill); the DPR recorded in player_pin; verified on its first session by offered vs played top quality | `b311277` | pending: the first yt session drawing it | Q am. 2 | — |
| 2026-10-18 | stuck rule: no T2 on the fallback rung (the docstring's rule, now in code; tw_34 was censored at state 4 against twitch's 230 kbit fallback); the one exception `ladder_160p_transcode`; `t2_skipped_rung_unknown`; MasterListener auto-attaches the player's workers (no master captured on tw_29–34; laptop check: captured from a worker) | `88cbade` | pending: the first twitch session after it must record `hls_master_lowest_bandwidth` | Q am. 6, am. 7 (3) | — |
| 2026-10-18 | F1: RECOVERED_FROZEN_CLASS and split recovered flags (no effect at "normal"); F2 boundaries single-sourced; constants golden; check.json schema test; revocation fixture test | `16400a4`, `3b654bf` | pending | Q am. 12, am. 4, am. 6 | — |
| 2026-10-18 | the entry-quality exception behind `--entry-quality` (default off; no behaviour change with it off; check.json gains `entry_quality`, null when off) | `7415ed0` (+ `51f8e29`, the DPR-1.5 shot test) | pending; with the flag on, the pilot's first sessions | Q am. 1, am. 10 | — |
| 2026-10-18 | ops: censored candidates verified in the verify loop (shagun's current_data/verify_censored_loop.sh folded into ops/verify_loop.sh, in the autostart path; no collector behaviour): a _failed session with meta.json abort_reason player_frozen / player_dead gets LVQoE verify_session --ingest --until <its final run's onset> (checkpoint_session_list --verify-until), once per loop start, events `verify-censored`; needs LVQOE_LOOP_DIR at an LVQoE commit with --verify-until (else logged off) | this commit | — (ops) | Q am. 11 | supervisor |
| 2026-10-18 | yt geometry `fullscreen:1920x1080` replaces `theater:1920x1080@1.5` in the rotation (t37; yt_43: at scale 1.5 the CSS viewport shrinks to 1235x702 on the 1920x1080 panel, the theater player is 853x480 CSS = 1280x720 device px, the same 720 cap; @1.5 sessions stay valid at their recorded geometry): window W x H, then the player's own fullscreen by the `f` key (a CDP key event; else a CDP click on its button), the pointer parked over the picture so the controls auto-hide; player_pin.fullscreen records the state and the page's fullscreenchange trace; in fullscreen the X11 shot is the whole screen in device px (index `fullscreen: true`, crop `player`: the image is the player) | this commit | pending: the first yt session drawing it (does fullscreen hold on chaos; laptop: the `f` key enters, macOS drops a background app's fullscreen after about 1 s) | Q am. 2 | supervisor |
| 2026-10-18 | page_stats.js logs the yt player's getAvailableQualityLevels() per tick (a getter; no setter anywhere); stream_regime takes the yt ladder from it when http_v2 carries no player response (from yt_41 the /live page is an app shell and no youtubei player exchange is in http_v2: yt_41-43 read ladder_unknown), with ladder_source and ladder_why; the fullscreen player height counts as the theater one did | this commit | pending: the next yt session | none (diagnostic flags only) | supervisor |
| 2026-10-18 | X11 shot origin calibration (supervisor t37 on yt_43 / tw_35 / tw_36: the window-metric model put the region about 20 device px below the viewport): once per session, after the geometry and before the shot loop and the schedule, one CDP viewport capture (no clip, no scale) matched against an X11 grab of the model viewport +- SHOT_CALIB_MARGIN_PX (64) over the page outside the <video> rect, coarse at 1/SHOT_CALIB_SCALE (4), refined at full size; accepted at residual <= SHOT_CALIB_MAX_RESIDUAL (8.0 grey) and gap >= SHOT_CALIB_MIN_GAP (4.0), then the offset moves every X11 region (DOM / CDP rects untouched); else the model stands, shot_origin_uncalibrated in meta and every index row; meta.geometry.shot.origin records offset, residual, gap, method, timing; each index row carries origin_offset. Thresholds set on the synthetic pair (test_shots), not yet on a chaos screen | this commit | pending: the first session after it (residual / gap / offset reported) | Q am. 2 (shots) | supervisor |
| 2026-10-19 | check_session tick presence is phase-free (fault fix; t37, shagun): over the session window (the shaper's first label to meta ended_at; the supervisor: a sampler that stops early must not shrink its own denominator; check.json labels.tick_window records it), with the window's edges as ticks, a second is covered unless a gap longer than CHECKS.tick_gap_s (1.5 s) spans it (the time beyond 1 s counts); int(t - t0) bins lost a second whenever jitter moved a tick sitting near an integer offset across the boundary. Re-read on all 111 checked chaos sessions (labels.csv, net_labels, meta; read only): every one has a window, ticks start 7-96 s before the shaper and end 0.2-2.7 s before ended_at; 2 verdicts change, both false FAILs with no other reason (0 gaps > 1.5 s, every empty bin single and matched by a double bin): _failed/twitch/tw_38 0.934 -> 1.0, _failed/youtube/yt_18 0.788 -> 1.0; lowest new presence 0.972 (ti_14) | this commit | the two re-checked | none (fault fix) | levi |
| 2026-10-19 | check.json labels.entry_tick_presence (reported only, never the verdict; levi): the same phase-free rule over the stream entry's seconds before the shaper starts (the first stream entry's t to the session window's start), for the per-entry startup readings; entries are now computed before the label checks (same values) | this commit | n/a (a new reported field) | none | levi |

## 7. Change procedure

- A behaviour change names its prereg clause before code. A change with no clause is a fault fix only, stated as such.
- Fixtures are updated in the same commit.
- Each change gets a change-log row naming its first affected session per platform, by `lvdc_commit`.
- levi signs each row.
