"""One-off (supervisor 2026-10-17): the first time a twitch session self-pauses in a low shaper state (1-4), save the
player container outerHTML + the video state into that session dir (player_dom_self_pause.html / .json) as a real-page
fixture for peter. Read-only second CDP client on the batch Chrome (Runtime.evaluate only). Exits after one save.
    python ops/diag/tw_selfpause_dom.py   (detached; logs to stdout; produced networkSim/fixtures/tw_player_*)"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402

D = Path(__file__).resolve().parents[2] / "data" / "twitch"   # data/ resolves to the data volume
JS = r"""(function(){const v=document.querySelector('video');
const c=(v&&(v.closest('.video-player__container')||v.closest('.video-player')))||document.querySelector('.video-player__container');
const ad=!!document.querySelector('[data-a-target="video-ad-label"],[data-a-target="video-ad-countdown"]');
return JSON.stringify({paused:v?v.paused:null, ended:v?v.ended:null, rs:v?v.readyState:null, ct:v?v.currentTime:null,
 ad:ad, lvqoe:Object.assign({}, document.documentElement.dataset), html:c?c.outerHTML:null});})()"""


def live_session():
    ds = sorted((p for p in D.glob("tw_*") if (p / "meta.json").exists() and not (p / "check.md").exists()),
                key=lambda p: p.stat().st_mtime)
    return ds[-1] if ds else None


def state(s):
    """Live shaper state: net_labels.txt is only written at session end, so map the live ingress htb rate (ifb0 class
    1:10) onto the session's shaper scenarios (meta.json shaper.scenarios rate_min..rate_max, kbit)."""
    import re
    import subprocess
    try:
        out = subprocess.run(["tc", "class", "show", "dev", "ifb0"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"class htb 1:10 .*? rate (\d+(?:\.\d+)?)([KMG]?)bit", out)
        kbit = float(m.group(1)) * {"": 1e-3, "K": 1, "M": 1e3, "G": 1e6}[m.group(2)]
        sc = json.loads((s / "meta.json").read_text())["shaper"]["scenarios"]
        hits = [int(k) for k, v in sc.items() if v["rate_min"] <= kbit <= v["rate_max"]]
        return min(hits) if hits else None
    except Exception:
        return None


while True:
    s = live_session()
    if not s:
        time.sleep(20)
        continue
    try:
        page = B.CDPPage(B._get_page_ws_url())
    except Exception:
        time.sleep(10)
        continue
    print(f"{time.strftime('%T')} watching {s.name}", flush=True)
    streak = 0
    while not (s / "check.md").exists() and live_session() == s:
        try:
            r = json.loads(page.eval(JS) or "{}")
        except Exception:
            break
        st = state(s)
        low_pause = r.get("paused") and not r.get("ended") and not r.get("ad") and st is not None and st <= 4
        streak = streak + 1 if low_pause else 0
        if streak >= 3 and r.get("html"):
            (s / "player_dom_self_pause.html").write_text(r.pop("html"))
            r.update({"shaper_state": st, "saved_at": time.time(), "by": "shagun tw_selfpause_dom.py (one-off)"})
            (s / "player_dom_self_pause.json").write_text(json.dumps(r, indent=1))
            print(f"{time.strftime('%T')} saved {s}", flush=True)
            sys.exit(0)
        time.sleep(2)
    try:
        page.close()
    except Exception:
        pass
    time.sleep(10)
