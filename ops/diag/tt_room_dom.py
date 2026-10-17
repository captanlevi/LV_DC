"""Read-only (supervisor 2026-10-18, room_mode): while a tt session runs, sample the room page's DOM markers every
30 s into <session>/tt_room_dom.jsonl, so a battle / match / multi-guest layout seen in the shots can be matched to
DOM markers before content.js gets a room_mode tick column. A second CDP client, Runtime.evaluate only; never drives
the page. Per sample: every data-e2e value present (counts), class tokens naming pk / battle / match / guest / multi /
cohost / link, and the number and sizes of visible <video> elements (a battle shows two streams).
    python ops/diag/tt_room_dom.py        (detached; one session at a time; exits when no tt session runs for 10 min)"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402

D = Path(__file__).resolve().parents[2] / "data" / "tiktok"   # data/ resolves to the data volume
JS = r"""(function(){
  const e2e = {};
  document.querySelectorAll('[data-e2e]').forEach(el => { const k = el.getAttribute('data-e2e'); e2e[k] = (e2e[k] || 0) + 1; });
  const toks = {};
  document.querySelectorAll('[class]').forEach(el => String(el.className).split(/\s+/).forEach(t => {
    if (/pk|battle|match|guest|multi|cohost|linkmic|link-mic/i.test(t)) toks[t] = (toks[t] || 0) + 1; }));
  const vids = [...document.querySelectorAll('video')].filter(v => v.getClientRects().length)
    .map(v => { const r = v.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height), v.readyState]; });
  return JSON.stringify({url: location.href, e2e, class_tokens: toks, videos: vids});
})()"""


def live_session():
    ds = sorted((p for p in D.glob("ti_*") if (p / "meta.json").exists() and not (p / "check.md").exists()),
                key=lambda p: p.stat().st_mtime)
    return ds[-1] if ds else None


idle_since = time.time()
while time.time() - idle_since < 600:
    s = live_session()
    if not s:
        time.sleep(30)
        continue
    idle_since = time.time()
    try:
        page = B.CDPPage(B._get_page_ws_url())
        r = json.loads(page.eval(JS) or "{}")
        page.close()
        r["ts"] = time.time()
        with open(s / "tt_room_dom.jsonl", "a") as fh:
            fh.write(json.dumps(r) + "\n")
    except Exception as e:  # a missed sample never matters
        print(f"{time.strftime('%T')} sample failed: {e!r}", flush=True)
    time.sleep(30)
