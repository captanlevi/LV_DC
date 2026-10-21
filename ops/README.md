# ops/: the on-box operator layer (shagun, chaos 2026-10-17/18; committed for t43)

The scripts the README's pause / resume, reboot and verify steps assume, so another box can run the batch from the
repo alone. Every script sources `env.sh`: box-specific paths and names are variables there, with chaos's values as
defaults (override in the environment or in `ops/env.local.sh`, gitignored). Each logs to
`data/_summaries/resume_events.txt` (`verify_loop.sh`: `verify_events.txt`). Flags live in `current_data/`
(gitignored): `BATCH_GO` (relaunch after a reboot), `RESUME_HOLD` (resume is a no-op), `SCREEN_OFF` (night mode), and
`batch_spec` / `batch_opts` (the batch targets and options, e.g. `--dpms-off` at night and none by day, written by
`apply_at_boundary.sh`; nothing edits a tracked file, so
`meta.json` `lvdc_commit.dirty` stays false).

| script | what it does | invoked by |
|---|---|---|
| `run_batch.sh` | the batch wrapper: data volume mounted and 0 global IPv6, else abort; logs lvdc / Chrome / screen; `orchestrate.py --batch <spec> $LVDC_BATCH_OPTS`; on exit `make disable` + `make kill_chrome` | `resume_batch.sh`, `resume_after_login.sh` (ssh-identical env, so the cookie store matches) |
| `pause_batch.sh [now]` | boundary pause: removes `BATCH_GO`, SIGSTOPs orchestrate; `now` also TERMs the running exp.py (archived as user_pause); waits for the session to end (zombie-aware), undoes the shaping, logs "box free" | operator, `apply_at_boundary.sh` |
| `resume_batch.sh` | re-applies IPv6-off, stops the OS timers, checks the mount, starts the guards, touches `BATCH_GO`, SIGCONTs orchestrate or relaunches the wrapper; a no-op while `RESUME_HOLD` exists | operator, boundary scripts |
| `resume_after_login.sh` | after a reboot and graphical login: waits for the data disk, fixes the mount-path trap, IPv6-off, timers, mute; then waits for NTP sync (`LVDC_NTP_WAIT_S`, default 600 s; on a timeout the guards and the launch are withheld, fail closed: post_boot_check must then FAIL on the missing guards) before the guards; relaunches iff `BATCH_GO` and no `RESUME_HOLD` (levi 02:38) | the autostart entry (`install_autostart.sh`; `--remove` after the batch) |
| `apply_at_boundary.sh [ref] [spec] [opts]` | ONE script per code change: pause, TERM + CONT the stopped orchestrate, `git merge --ff-only <ref>`, every `test_*.py` but the live `test_browser`, optional new spec / options (`none` = screen on), resume; a failed test leaves the batch paused | operator |
| `apply_during_pause.sh "<trigger>" [ref]` | merge + tests while another job holds the pause; **never alongside another chain that resumes** (tw_15) | operator |
| `verify_loop.sh` | every 60 s: each PASS without `verify.json` gets LVQoE `verify_session.py --ingest` (QA only), without `frames.jsonl` gets `extract_frames.py`, both from the one LVQoE worktree `LVQOE_LOOP_DIR` (env.sh; default the loop worktree at master 010c47c), under `LVDC_COMPUTE_PIN` (nice 19, upper half of the CPUs); each censored candidate in `_failed` (meta.json `abort_reason` player_frozen / player_dead) gets `verify_session.py --ingest --until <its final run's onset>` once per loop start, events `verify-censored` (Q am. 11) | started once; restart if it dies |
| `ipv6_watchdog.sh` | every 30 s: any global IPv6 -> re-apply the sysctl file and the interface key (the preflight does it per session too) | the resume scripts |
| `diskstats_sampler.py` | 1 s /proc/diskstats deltas beside the box record (levi t56); devices by name or by mount point, resolved at start to the disk under it (default the data volume's mount and /), so a disk swap behind the mount path needs no edit | the round scripts (chaos current_data) |
| `keep_display_off.sh` | every 60 s `xset dpms force off` while the monitor is on (screen-off mode) | the resume scripts, only with `SCREEN_OFF` |
| `guard_stall_contradiction.sh` | interim: a PASS with stall share > 0.5 and advancing > 0.5 is re-archived (superseded by player_frozen / I10; kept while the batch runs) | the resume scripts |
| `diag/tt_video_state.py`, `diag/tw_selfpause_dom.py` | read-only second CDP client (Runtime.evaluate only): every video's state; the twitch self-pause DOM capture behind `networkSim/fixtures/tw_player_*` | one-off |

**Rules learned on chaos:**
- Anything safety-critical runs on the box (watchdog, preflight), not in an operator's session: the 2026-10-18 02:20
  IPv6 re-enable cost three sessions while the operator's checks were hourly and off-box.
- One boundary script at a time. Two chains, one CONTing and one TERMing, killed the next session's Chrome (tw_15).
- Code reaches the box over ssh (`git push <box>:<repo> <ref>:refs/heads/<name>`) or at a pause, never by a fetch
  while a session captures.

**Driver hold (2026-10-18):** the Intel VA-API driver (`intel-media-va-driver-non-free`, `libva2`) is apt-held like Chrome. Preflight refuses a session when either hold is gone (tt's HEVC decode depends on it). Versions are in `current_data/driver_upgrade/hold.log` on the box.

**Not here:** the operator's hourly health check and 6-hourly self-audit are session crons on the operator's side, not
on the box.
- Both read the per-session flags in `data/_summaries/flags.jsonl`. orchestrate appends a row after every session whose check
  raised a stream-regime flag; `new` marks a flag that platform had not raised before.
- The 6-hourly audit also runs `python3 networkSim/coverage_report.py` (supervisor 2026-10-18). It prints the coverage table and
  the `FLAG` lines:
  - a class with zero support after 5 accepted sessions;
  - each regime flag with its first and latest session.
- Report the first `FLAG`, not the twentieth.
