(() => {
  if (window.__lvqoeMse) return;
  window.__lvqoeMse = true;
  const inst = performance.now();
  let first = null, firstSent = false, instSent = false;
  const BLOCK_S = %(block_s)d, MAX = %(max)d, R2 = %(r2)d;
  const on = () => !(BLOCK_S > 0) || Math.floor(Date.now() / (BLOCK_S * 1000)) %% 2 === 1;
  let recs = [], over = 0, next = 0;
  const ids = new WeakMap();
  const id = (sb) => { let i = ids.get(sb); if (i === undefined) { i = ++next; ids.set(sb, i); } return i; };
  const push = (r) => { if (!on()) return; if (recs.length >= MAX) { over += 1; return; } r.t = performance.now(); recs.push(r); };
  const wrap = (proto, name, before, after) => {
    const orig = proto && proto[name];
    if (typeof orig !== "function") return;
    proto[name] = function (...a) {
      if (before) { try { before(this, a); } catch (e) {} }
      const ret = orig.apply(this, a);
      if (after) { try { after(this, a, ret); } catch (e) {} }
      return ret;
    };
  };
  // t100 R2 (rushi 14:35, levi's budget; MSE_WRAP_R2): on app, the tso in force and the head's first time: fMP4 the first
  // traf's [track id, tfdt] (tf), WebM a Cluster's Timecode (ctc); init (fMP4 moov: [tid, timescale, handler]) and tcs
  // (WebM TimecodeScale, first 4 KB of an EBML header append) are state records, every block. Only the boxes / elements at
  // the head are read, never the media; a fault leaves tf null (r2err) and never reaches the page.
  const state = (r) => { if (recs.length >= MAX) { over += 1; return; } r.t = performance.now(); recs.push(r); };
  const u8 = (b) => b instanceof ArrayBuffer ? new Uint8Array(b) : (b && b.buffer ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength) : null);
  const vint = (b, i, keep) => { const x = b[i]; let n = 1; while (n <= 8 && !(x & (0x80 >> (n - 1)))) n++; if (n > 8 || i + n > b.length) return null;
    let v = keep ? x : x & (0xff >> n); for (let k = 1; k < n; k++) v = v * 256 + b[i + k]; return [v, n]; };
  const uint = (b, i, n) => { let v = 0; for (let k = 0; k < n && i + k < b.length; k++) v = v * 256 + b[i + k]; return v; };
  const ebml = (b, i, want, lim) => { let k = 0; while (i < Math.min(b.length, lim) && k++ < 8) { const d = vint(b, i, true); if (!d) return null;
      const z = vint(b, i + d[1]); if (!z) return null; const at = i + d[1] + z[1]; if (d[0] === want) return uint(b, at, z[0]); i = at + z[0]; } return null; };
  const r2 = (sb, b0, r) => { try {
    r.tso = sb.timestampOffset; const b = u8(b0); if (!b || b.length < 8) return;
    if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) { const lim = Math.min(b.length, 4096);
      for (let i = 0; i + 3 < lim; i++) if (b[i] === 0x2a && b[i + 1] === 0xd7 && b[i + 2] === 0xb1) { const z = vint(b, i + 3);
        if (z) state({ op: "tcs", sb: id(sb), v: uint(b, i + 3 + z[1], z[0]) }); break; }
      return; }
    if (b[0] === 0x1f && b[1] === 0x43 && b[2] === 0xb6 && b[3] === 0x75) { const z = vint(b, 4); if (z) { const v = ebml(b, 4 + z[1], 0xe7, 4096); if (v !== null) r.ctc = v; } return; }
    const dv = new DataView(b.buffer, b.byteOffset, b.byteLength), T = (o) => String.fromCharCode(b[o + 4], b[o + 5], b[o + 6], b[o + 7]);
    const each = (s, e, f) => { let o = s, n = 0; while (o + 8 <= e && n++ < 64) { const z = dv.getUint32(o); if (z < 8 || o + z > e) return; if (f(T(o), o + 8, o + z) === false) return; o += z; } };
    each(0, b.length, (t, bb, be) => {
      if (t === "moov") { const tr = []; each(bb, be, (t2, b2, e2) => { if (t2 !== "trak") return; const x = [null, null, null];
          each(b2, e2, (t3, b3, e3) => { if (t3 === "tkhd") x[0] = dv.getUint32(b3 + (b[b3] === 1 ? 20 : 12));
            if (t3 === "mdia") each(b3, e3, (t4, b4) => { if (t4 === "mdhd") x[1] = dv.getUint32(b4 + (b[b4] === 1 ? 20 : 12));
              if (t4 === "hdlr") x[2] = T(b4 + 4); }); });
          tr.push(x); });
        state({ op: "init", sb: id(sb), tr }); }
      if (t === "moof") { r.tf = null; each(bb, be, (t2, b2, e2) => { if (t2 !== "traf") return; let tid = null, bdt = null;
          each(b2, e2, (t3, b3) => { if (t3 === "tfhd") tid = dv.getUint32(b3 + 4);
            if (t3 === "tfdt") bdt = b[b3] === 1 ? dv.getUint32(b3 + 4) * 4294967296 + dv.getUint32(b3 + 8) : dv.getUint32(b3 + 4); });
          r.tf = [tid, bdt]; return false; });
        return false; } });
  } catch (e) { r.tf = null; r.r2err = 1; } };
  const ended = (sb) => () => { try { const b = sb.buffered, n = b.length;
    push({ op: "end", sb: id(sb), bend: n ? b.end(n - 1) : null, nr: n }); } catch (e) {} };
  if (typeof MediaSource !== "undefined") {
    wrap(MediaSource.prototype, "addSourceBuffer", null, (ms, a, sb) => {
      if (first === null) first = { t: performance.now(), mime: String(a[0]).slice(0, 160) };
      push({ op: "add", sb: id(sb), mime: String(a[0]).slice(0, 160) });
      sb.addEventListener("updateend", ended(sb));
    });
  }
  if (typeof SourceBuffer !== "undefined") {
    const p = SourceBuffer.prototype;
    wrap(p, "appendBuffer", (sb, a) => { const r = { op: "app", sb: id(sb), bytes: a[0] ? a[0].byteLength : null };
      if (R2) r2(sb, a[0], r);
      push(r); });
    wrap(p, "remove", (sb, a) => push({ op: "rm", sb: id(sb), s: a[0], e: a[1] }));
    wrap(p, "changeType", (sb, a) => push({ op: "ct", sb: id(sb), mime: String(a[0]).slice(0, 160) }));
  }
  setInterval(() => {
    if (!recs.length && instSent && (first === null || firstSent)) return;
    const batch = recs; recs = [];
    instSent = true;
    firstSent = first !== null;
    try { window.postMessage({ source: "lvqoe-mse", recs: batch, over, inst, first }, "*"); } catch (e) {}
  }, 1000);
})();