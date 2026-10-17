"""Read-only: the state of every <video> on the batch Chrome's page, two reads 3 s apart (a second CDP client,
Runtime.evaluate only; never drives the page).    python ops/diag/tt_video_state.py"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "networkSim"))
import browser as B  # noqa: E402

JS = r"""JSON.stringify({url: location.href, ds: Object.assign({}, document.documentElement.dataset),
 videos: [...document.querySelectorAll('video')].map(v => ({w: v.videoWidth, h: v.videoHeight, ct: v.currentTime,
 paused: v.paused, rs: v.readyState, src: !!(v.currentSrc || v.srcObject), rect: (r => [Math.round(r.x), Math.round(r.y),
 Math.round(r.width), Math.round(r.height)])(v.getBoundingClientRect())}))})"""
p = B.CDPPage(B._get_page_ws_url())
a = json.loads(p.eval(JS)); time.sleep(3); b = json.loads(p.eval(JS))
print(a["url"]); print({k: v for k, v in b["ds"].items() if k.startswith("lvqoe")})
for x, y in zip(a["videos"], b["videos"]):
    print(y, "advanced", round(y["ct"] - x["ct"], 2))
p.close()
