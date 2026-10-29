def _export_via_service_worker(session_dir: Path) -> bool:
    """
    Read QoE events from the extension's chrome.storage.local via CDP,
    write labels.csv, and clear the storage for the next run.
    Returns True on success.

    Boundary after window 1 (codex, levi 00106): every file (side streams, player_stats.jsonl, labels.csv) is written
    durably BEFORE the storage clear; an exception or a full disk on the way leaves storage uncleared (the start path
    of the next session clears it, so the record is lost only if nothing reads it first, never by our own clear).
    """
    try:
        with urllib.request.urlopen(f"{CDP_BASE}/json", timeout=5) as r:
            targets = json.loads(r.read())

        sw, tmp_tab = _ext_page()
        if sw is None:
            print("[browser] No extension context found (service worker idle and no extension page)", flush=True)
            _ext_export_record(session_dir, ok=False, reason="no_ext_context")
            return False

        events_json = sw.eval(
            "new Promise(r => chrome.storage.local.get({events:[]}, d => r(JSON.stringify(d.events))))",
            await_promise=True,
        )
        # yt player stats (G3 ground truth): a separate file, never merged into labels.csv
        stats_json = sw.eval(
            "new Promise(r => chrome.storage.local.get({player_stats:[]}, d => r(JSON.stringify(d.player_stats))))",
            await_promise=True,
        )
        # v3 (t67 A2): the side streams (player_events, ...), one key per batch, each to <stream>.jsonl
        side_json = sw.eval(
            "new Promise(r => chrome.storage.local.get(null, d => r(JSON.stringify(Object.fromEntries("
            "Object.entries(d).filter(([k]) => k.startsWith('side:')))))))",
            await_promise=True,
        )
        side = json.loads(side_json) if side_json else {}
        ext = {"side_keys": len(side)}             # brad / levi 18:17: the storage use at export (meta.json ext_storage)
        try:
            ext["bytes_in_use"] = sw.eval("new Promise(r => chrome.storage.local.getBytesInUse(null, r))", await_promise=True)
            # levi 18:26: the content script's final side_dropped / side_failed / off_arm_cb (the gate counters read meta)
            ext["counters"] = json.loads(sw.eval("new Promise(r => chrome.storage.local.get({side_counters: null}, "
                                                 "d => r(JSON.stringify(d.side_counters))))", await_promise=True) or "null")
        except Exception as exc:
            ext["error"] = repr(exc)[:200]
        try:
            import session_meta
            session_meta.write(session_dir, ext_storage=ext)
        except Exception as exc:
            print(f"[browser] ext_storage not recorded: {exc!r}", flush=True)
        # brad / levi 23:10-23:21: one map of side stream -> session file (A1 is rvfc_frames.jsonl), every name resolved
        # and every record written BEFORE storage is cleared; an unmapped stream goes to side_<stream>.jsonl, flagged
        import session_files
        unmapped = []
        for stream, recs in _side_streams(side).items():
            fname, mapped = session_files.side_file(stream)
            if not mapped:
                unmapped.append({"stream": stream, "file": fname, "records": len(recs)})
            _write_durable(session_dir / fname, "".join(json.dumps(rec) + "\n" for rec in recs))
            print(f"[browser] {stream} ({len(recs)} records) → {session_dir / fname}", flush=True)
        if unmapped:
            try:
                import session_meta
                session_meta.write(session_dir, side_streams_unmapped=unmapped)
            except Exception as exc:
                print(f"[browser] side_streams_unmapped not recorded: {exc!r} {unmapped}", flush=True)
        stats = json.loads(stats_json) if stats_json else []
        if stats:
            _write_durable(session_dir / "player_stats.jsonl", "".join(json.dumps(rec) + "\n" for rec in stats))
            print(f"[browser] Player stats ({len(stats)} records) → {session_dir / 'player_stats.jsonl'}", flush=True)

        def clear_storage():
            # Clear storage so the next run starts fresh: only after every file above / below is on disk
            sw.eval("new Promise(r => chrome.storage.local.remove(['events', 'player_stats', 'side_counters'].concat(%s), r))"
                    % json.dumps(sorted(side)), await_promise=True)
            sw.close()
            _close_target(tmp_tab)

        events: list[dict] = json.loads(events_json) if events_json else []
        if not events:
            print("[browser] Extension storage empty — no QoE events collected", flush=True)
            clear_storage()
            _ext_export_record(session_dir, ok=True, events=0)
            return False

        # v2 (LVQoE t35): new columns appended after the v1 ones (additive; v1 readers keep working)
        header = ["ts", "type", "video_w", "video_h", "client_w", "client_h",
                  "dropped", "current_time", "tabId", "url", "event_id",
                  "buffered_ahead_s", "playback_rate", "duration_s", "is_ad", "ad_signals", "src_changed",
                  "login_wall_text", "login_wall", "quality_countdown", "paused", "playhead_still_s", "ready_state",
                  "visibility", "total_frames",                        # t53 (am. 13 / gate-2 4a): additive
                  "el_id", "src_key",                                  # v3 A10 (t67): additive
                  "buffered_ranges",                                   # v3 A6 (t67): additive
                  # boundary after window 1 (codex item 1): the content-script instance, the sending frame / document,
                  # and the instance's cumulative overflow drops (empty when none): additive
                  "cs", "frameId", "documentId", "dropped_n", "dropped_first_id", "dropped_last_id"]
        rows = [",".join(
            '"{}"'.format(str(e.get(k, "")).replace('"', '""')) for k in header
        ) for e in events]
        csv_text = "\n".join([",".join(header)] + rows)

        dest = session_dir / "labels.csv"
        _write_durable(dest, csv_text)
        print(f"[browser] Extension labels ({len(events)} events) → {dest}", flush=True)
        print("[browser] labels.csv written before storage clear", flush=True)
        clear_storage()
        print("[browser] Extension storage cleared after export", flush=True)
        _ext_export_record(session_dir, ok=True, events=len(events))
        return True

    except Exception as exc:
        print(f"[browser] Extension CSV export error: {exc}", flush=True)
        _ext_export_record(session_dir, ok=False, error=repr(exc)[:200])
        return False
