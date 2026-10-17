# ops/: the on-box operator layer (shagun, chaos 2026-10-17/18; committed for t43)

The scripts the README's pause / resume, reboot and verify steps assume, so another box can run the batch from the
repo alone. Every script sources `env.sh`: box-specific paths and names are variables there, with chaos's values as
defaults (override in the environment or in `ops/env.local.sh`, gitignored). Each logs to
`data/_summaries/resume_events.txt` (`verify_loop.sh`: `verify_events.txt`). Flags live in `current_data/`
(gitignored): `BATCH_GO` (relaunch after a reboot), `RESUME_HOLD` (resume is a no-op), `SCREEN_OFF` (night mode), and
`batch_spec` (the batch targets, written by `apply_at_boundary.sh`; nothing edits a tracked file, so
`meta.json` `lvdc_commit.dirty` stays false).

| script | what it does | invoked by |
|---|---|---|
| `run_batch.sh` | the batch wrapper: data volume mounted and 0 global IPv6, else abort; logs lvdc / Chrome / screen; `orchestrate.py --batch <spec> $LVDC_BATCH_OPTS`; on exit `make disable` + `make kill_chrome` | `resume_batch.sh`, `resume_after_login.sh` (ssh-identical env, so the cookie store matches) |
| `pause_batch.sh [now]` | boundary pause: removes `BATCH_GO`, SIGSTOPs orchestrate; `now` also TERMs the running exp.py (archived as user_pause); waits for the session to end (zombie-aware), undoes the shaping, logs "box free" | operator, `apply_at_boundary.sh` |
| `resume_batch.sh` | re-applies IPv6-off, stops the OS timers, checks the mount, starts the guards, touches `BATCH_GO`, SIGCONTs orchestrate or relaunches the wrapper; a no-op while `RESUME_HOLD` exists | operator, boundary scripts |
| `resume_after_login.sh` | after a reboot and graphical login: waits for the data disk, fixes the mount-path trap, IPv6-off, timers, mute, guards; relaunches iff `BATCH_GO` | the autostart entry (`install_autostart.sh`; `--remove` after the batch) |
| `apply_at_boundary.sh [ref] [spec]` | ONE script per code change: pause, TERM + CONT the stopped orchestrate, `git merge --ff-only <ref>`, every `test_*.py` but the live `test_browser`, optional new spec, resume; a failed test leaves the batch paused | operator |
| `apply_during_pause.sh "<trigger>" [ref]` | merge + tests while another job holds the pause; **never alongside another chain that resumes** (tw_15) | operator |
| `verify_loop.sh` | every 60 s: each PASS without `verify.json` gets LVQoE `verify_session.py --ingest` (QA only), without `frames.jsonl` gets `extract_frames.py` (nice 10) | started once; restart if it dies |
| `ipv6_watchdog.sh` | every 30 s: any global IPv6 -> re-apply the sysctl file and the interface key (the preflight does it per session too) | the resume scripts |
| `keep_display_off.sh` | every 60 s `xset dpms force off` while the monitor is on (screen-off mode) | the resume scripts, only with `SCREEN_OFF` |
| `guard_stall_contradiction.sh` | interim: a PASS with stall share > 0.5 and advancing > 0.5 is re-archived (superseded by player_frozen / I10; kept while the batch runs) | the resume scripts |
| `diag/tt_video_state.py`, `diag/tw_selfpause_dom.py` | read-only second CDP client (Runtime.evaluate only): every video's state; the twitch self-pause DOM capture behind `networkSim/fixtures/tw_player_*` | one-off |

**Rules learned on chaos:**
- Anything safety-critical runs on the box (watchdog, preflight), not in an operator's session: the 2026-10-18 02:20
  IPv6 re-enable cost three sessions while the operator's checks were hourly and off-box.
- One boundary script at a time. Two chains, one CONTing and one TERMing, killed the next session's Chrome (tw_15).
- Code reaches the box over ssh (`git push <box>:<repo> <ref>:refs/heads/<name>`) or at a pause, never by a fetch
  while a session captures.

**Not here:** the operator's hourly health check and 6-hourly self-audit are session crons on the operator's side, not
on the box.
