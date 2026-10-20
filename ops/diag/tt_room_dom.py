"""Read-only (supervisor 2026-10-18, room_mode): while a tt session runs, sample the room page's DOM markers every
30 s into <session>/tt_room_dom.jsonl, so a battle / match / multi-guest layout seen in the shots can be matched to
DOM markers before content.js gets a room_mode tick column. A second CDP client, Runtime.evaluate only; never drives
the page. Per sample: every data-e2e value present (counts), class tokens naming pk / battle / match / guest / multi /
cohost / link, and the number and sizes of visible <video> elements (a battle shows two streams).

Single mode: the latest unfinished data/tiktok session, on the batch Chrome's port (browser.CDP_BASE). Trio / N-slot
(supervisor 2026-10-19, after ti_100010's multi-guest grid: 0 of 9 trio tt sessions had the record): every tt session
the run's slots log shows running (a start without a later end / exit for its slot, as browser.sibling_streams reads
it), on its own slot's Chrome (meta.json concurrency.cdp_port, 9223 + k). Each sample records the port it read.
Layout signal (boundary bundle item 1; levi / supervisor 03:54, design 00267): the DOM carries no layout (the guest grid
is composited into the one stream), so a second, persistent and passive CDP client per tt page (NetListener) enables the
Network domain with bounded buffers and reads the page's own traffic, adding no request and changing nothing:
  - each `webcast/room/enter` JSON response: one row {"kind": "room_enter", ...ROOM_ENTER_FIELDS} (fixed fields only;
    no user ids, no chat, no raw body stored);
  - each `webcast/im/fetch` response and websocket frame: the occurrences of the fixed MSG_NAMES message-type names,
    counted into the next 30 s row as "msg_counts" (names only, no message content).
The layout per 30 s row is derived reader-side and validated against a blind screenshot read before any use. A listener
failure is logged and never touches the session. Layout is a covariate, never a model input (as `environment`).
    python ops/diag/tt_room_dom.py        (detached; exits when no tt session runs for 10 min)
    python ops/diag/tt_room_dom.py --no-net   (the DOM sample only: the diag-off arm of the CPU / RSS check)"""
import base64
import json
import sys
import threading
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


ROOM_ENTER_URL = "/webcast/room/enter/"
IM_FETCH_URL = "/webcast/im/fetch/"
MSG_NAMES = ("WebcastLinkMicBattle", "WebcastLinkMicArmies", "WebcastLinkLayerMessage", "WebcastLinkMicMethod",
             "WebcastLinkMessage", "WebcastLinkmicBattleTaskMessage", "WebcastCohostLayer")
NET_BUFFER = {"maxTotalBufferSize": 2 * 1024 * 1024, "maxResourceBufferSize": 512 * 1024}   # bounded (supervisor 03:54)


def _get(d, *path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def room_enter_fields(body: dict) -> dict:
    """The fixed room/enter fields (design 00267): battle state, linked guests / cohosts, the layout codes, pk status."""
    d = body.get("data") if isinstance(body.get("data"), dict) else {}
    return {"room_id": str(d.get("id_str") or d.get("id") or "") or None,
            "battle_status": _get(d, "link_mic", "battle_settings", "battle_status"),
            "battle_id": None if _get(d, "link_mic", "battle_settings", "battle_id") is None
            else str(_get(d, "link_mic", "battle_settings", "battle_id")),
            "n_linked_users": len(_get(d, "link_mic", "linked_user_list") or []),
            "n_cohost_linked": len(_get(d, "social_interaction", "cohost", "linked_users") or []),
            "multi_guest_enable": _get(d, "link_mic", "multi_guest_enable"),
            "linkmic_layout": d.get("linkmic_layout"), "room_layout": d.get("room_layout"),
            "live_type_linkmic": d.get("live_type_linkmic"),
            "pk_status": _get(d, "decisions", "server_features", "pk_status")}


def count_names(data: bytes) -> dict:
    """Occurrences of each MSG_NAMES name in a message payload (protobuf: the type names are plain bytes)."""
    return {n: data.count(n.encode()) for n in MSG_NAMES if data.count(n.encode())}


class NetListener(threading.Thread):
    """The passive Network listener on one tt page (its own CDP websocket; never the session's client)."""

    def __init__(self, sd: Path, ws_url: str, port: int | None, connect=None):
        super().__init__(daemon=True, name=f"tt-net-{sd.name}")
        self.sd, self.ws_url, self.port = sd, ws_url, port
        self._connect = connect or self._ws_connect
        self.counts: dict = {}
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.pending: dict = {}           # requestId -> "enter" | "im"
        self.errors = 0
        self._seq = 1000

    @staticmethod
    def _ws_connect(url):
        import websocket
        ws = websocket.WebSocket()
        ws.settimeout(1)
        ws.connect(url)
        return ws

    def _send(self, ws, method, params=None):
        self._seq += 1
        ws.send(json.dumps({"id": self._seq, "method": method, "params": params or {}}))
        return self._seq

    def take_counts(self) -> dict:
        with self.lock:
            c, self.counts = self.counts, {}
        return c

    def _add(self, c: dict) -> None:
        with self.lock:
            for k, v in c.items():
                self.counts[k] = self.counts.get(k, 0) + v

    def handle(self, ws, msg: dict, bodies: dict) -> None:
        """One CDP message: a matched response's id is remembered, its body fetched once loaded, then read."""
        m, p = msg.get("method"), msg.get("params") or {}
        if m == "Network.responseReceived":
            url = (p.get("response") or {}).get("url") or ""
            if ROOM_ENTER_URL in url:
                self.pending[p.get("requestId")] = "enter"
            elif IM_FETCH_URL in url:
                self.pending[p.get("requestId")] = "im"
        elif m == "Network.loadingFinished" and p.get("requestId") in self.pending:
            rid = p["requestId"]
            bodies[self._send(ws, "Network.getResponseBody", {"requestId": rid})] = (rid, self.pending.pop(rid))
        elif m == "Network.loadingFailed":
            self.pending.pop(p.get("requestId"), None)
        elif m == "Network.webSocketFrameReceived":
            payload = (p.get("response") or {}).get("payloadData") or ""
            op = (p.get("response") or {}).get("opcode")
            data = base64.b64decode(payload) if op == 2 else payload.encode()
            self._add(count_names(data))
        elif "id" in msg and msg["id"] in bodies:
            rid, kind = bodies.pop(msg["id"])
            r = msg.get("result") or {}
            raw = r.get("body") or ""
            data = base64.b64decode(raw) if r.get("base64Encoded") else raw.encode()
            if kind == "enter":
                try:
                    row = {"kind": "room_enter", "ts": time.time(), "cdp_port": self.port,
                           **room_enter_fields(json.loads(data.decode("utf-8", "replace")))}
                except ValueError:
                    row = {"kind": "room_enter", "ts": time.time(), "cdp_port": self.port, "unparsed": True}
                with open(self.sd / "tt_room_dom.jsonl", "a") as fh:
                    fh.write(json.dumps(row) + "\n")
            else:
                self._add(count_names(data))

    def run(self) -> None:
        try:
            ws = self._connect(self.ws_url)
            self._send(ws, "Network.enable", NET_BUFFER)
        except Exception as e:
            print(f"{time.strftime('%T')} {self.sd.name} net listener failed to start: {e!r}", flush=True)
            self.errors += 1
            return
        bodies: dict = {}
        while not self.stop_ev.is_set():
            try:
                raw = ws.recv()
            except Exception as e:       # a timeout (no event this second) or a closed page
                if "timed out" in str(e).lower() or type(e).__name__.endswith("TimeoutException"):
                    continue
                print(f"{time.strftime('%T')} {self.sd.name} net listener stopped: {e!r}", flush=True)
                self.errors += 1
                break
            try:
                self.handle(ws, json.loads(raw), bodies)
            except Exception as e:       # a bad event never stops the listener
                self.errors += 1
                if self.errors <= 5:
                    print(f"{time.strftime('%T')} {self.sd.name} net event skipped: {e!r}", flush=True)
        try:
            ws.close()
        except Exception:
            pass


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


def sample(sd: Path, base: str, port: int | None, listener: "NetListener | None" = None) -> None:
    B.CDP_BASE = base                     # _get_page_ws_url reads the module's base
    page = B.CDPPage(B._get_page_ws_url())
    r = json.loads(page.eval(JS) or "{}")
    page.close()
    r["ts"], r["cdp_port"] = time.time(), port
    if listener is not None:              # the 30 s row carries the message-type counts since the last one
        r["msg_counts"], r["net_listener"] = listener.take_counts(), {"alive": listener.is_alive(),
                                                                      "errors": listener.errors}
    with open(sd / "tt_room_dom.jsonl", "a") as fh:
        fh.write(json.dumps(r) + "\n")


def main(argv=None) -> None:
    net = "--no-net" not in (sys.argv[1:] if argv is None else argv)
    listeners: dict = {}
    idle_since = time.time()
    while time.time() - idle_since < 600:
        todo = [(s, SINGLE_BASE, None) for s in [live_session()] if s]
        todo += [(s, f"http://localhost:{p}", p) for s, p in running_trio_tt(DATA / T.TRIO_DIR)]
        for key in [k for k in listeners if k not in {(str(s), p) for s, _, p in todo}]:
            listeners.pop(key).stop_ev.set()          # the session ended: its listener stops
        if not todo:
            time.sleep(30)
            continue
        idle_since = time.time()
        for sd, base, port in todo:
            key = (str(sd), port)
            if net and key not in listeners:
                try:
                    B.CDP_BASE = base
                    listeners[key] = NetListener(sd, B._get_page_ws_url(), port)
                    listeners[key].start()
                except Exception as e:    # never touches the session
                    print(f"{time.strftime('%T')} {sd.name} net listener not started: {e!r}", flush=True)
            try:
                sample(sd, base, port, listeners.get(key))
            except Exception as e:  # a missed sample never matters
                print(f"{time.strftime('%T')} {sd.name} sample failed: {e!r}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
