Archived strays (peter, code-health audit 2026-10-24, E4): kept for history, not used by the collector.
- not_for_students.ipynb: a notebook at the repo root that nothing references
- run_session.sh: the pre-v2 single-session runner (docs/archive/SESSION_NOTES_pre_v2_2026-10-17.md)
- tt_room_dom/ (peter, boundary after counted window 1; levi 00106 / 00108 "retire or fix, simplify first"): ops/diag/tt_room_dom.py
  (the 30 s DOM / NetListener room-mode sampler), its test and fixture. It wrote nothing in any v3 window (it read
  <its checkout>/data, never the round's data root; shagun WCJb verdict 1f804b9c) and nothing but era_report's flag read it.
  Room mode stays a join-time stratum (browser.py's room/enter parse at join); tt room fields are never model inputs.
