(() => {
  if (window.__lvqoeMse) return;
  window.__lvqoeMse = true;
  const inst = performance.now();
  let first = null, firstSent = false, instSent = false;
  const BLOCK_S = %(block_s)d, MAX = %(max)d;
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
    wrap(p, "appendBuffer", (sb, a) => push({ op: "app", sb: id(sb), bytes: a[0] ? a[0].byteLength : null }));
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