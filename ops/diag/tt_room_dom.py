"""Read-only (supervisor 2026-10-18, room_mode): while a tt session runs, sample the room page's DOM markers every
30 s into <session>/tt_room_dom.jsonl, so a battle / match / multi-guest layout seen in the shots can be matched to
DOM markers before content.js gets a room_mode tick column. A second CDP client, Runtime.evaluate only; never drives
the page. Per sample: every data-e2e value present (counts), class tokens naming pk / battle / match / guest / multi /
cohost / link, and the number and sizes of visible <video> elements (a battle shows two streams).

Single mode: the latest unfinished data/tiktok session, on the batch Chrome's port (browser.CDP_BASE). Trio / N-slot
(supervisor 2026-10-19, after ti_100010's multi-guest grid: 0 of 9 trio tt sessions had the record): every tt session
the run's slots log shows running (a start without a later end / exit for its slot, as browser.sibling_streams reads
it), on its own slot's Chrome (meta.json concurrency.cdp_port, 9223 + k). Each sample records the port it read.
    python ops/diag/tt_room_dom.py        (detached; exits when no tt session runs for 10 min)"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402
import trio_slots as T  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "data"   # data/ resolves to the data volume
D = DATA / "tiktok"
SINGLE_BASE = B.CDP_BASE
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


def running_trio_tt(troot: Path) -> list[tuple[Path, int]]:
    """(session dir, CDP port) of every tt session the slots log shows running: per slot its latest start without a
    later end / exit; the port from the session's meta.json concurrency.cdp_port."""
    log = troot / T.SLOTS_LOG
    if not log.exists():
        return []
    cur: dict = {}
    for line in log.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event") == "start":
            cur[e.get("slot")] = (e.get("session"), e.get("platform"))
        elif e.get("event") in ("end", "exit"):
            cur.pop(e.get("slot"), None)
    out = []
    for name, platform in cur.values():
        sd = troot / "tiktok" / str(name)
        if platform != "tiktok" or not (sd / "meta.json").exists() or (sd / "check.md").exists():
            continue
        try:
            port = int((json.loads((sd / "meta.json").read_text()).get("concurrency") or {})["cdp_port"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        out.append((sd, port))
    return out


def sample(sd: Path, base: str, port: int | None) -> None:
    B.CDP_BASE = base                     # _get_page_ws_url reads the module's base
    page = B.CDPPage(B._get_page_ws_url())
    r = json.loads(page.eval(JS) or "{}")
    page.close()
    r["ts"], r["cdp_port"] = time.time(), port
    with open(sd / "tt_room_dom.jsonl", "a") as fh:
        fh.write(json.dumps(r) + "\n")


def main() -> None:
    idle_since = time.time()
    while time.time() - idle_since < 600:
        todo = [(s, SINGLE_BASE, None) for s in [live_session()] if s]
        todo += [(s, f"http://localhost:{p}", p) for s, p in running_trio_tt(DATA / T.TRIO_DIR)]
        if not todo:
            time.sleep(30)
            continue
        idle_since = time.time()
        for sd, base, port in todo:
            try:
                sample(sd, base, port)
            except Exception as e:  # a missed sample never matters
                print(f"{time.strftime('%T')} {sd.name} sample failed: {e!r}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
