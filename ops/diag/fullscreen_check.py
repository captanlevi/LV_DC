#!/usr/bin/env python3
"""Live check of the fullscreen hot-fix (t37) on a throwaway Chrome, never the batch Chrome (port 9222).

Hot-fix 4 (supervisor, yt_46: the check passed on a hand-launched Chrome without --no-sandbox while the batch failed
on that flag's infobar): the check launches its own Chrome through the collector's `make start_chrome` with the
collector's own variables (geometry.start_chrome_make_vars for yt's fullscreen rotation entry, as exp.py builds them;
exp.configure_chrome_download_dir on the profile), so its arguments are the batch's, except CDP_PORT (9333) and
SLOT_SUFFIX (its pid / log names); the profile is an empty /tmp one (no login is needed on yt) and the key log goes to
a /tmp session dir. The Chrome command line is recorded (make -n) and the Chrome is killed by its own port at the end.

    # from networkSim's directory, with the collector's venv, on the X display
    python3 ../ops/diag/fullscreen_check.py [--port 9333] [--url https://www.youtube.com/@LofiGirl/live]

Prints, as JSON: the launch (geometry entry, make variables, Chrome command line), the window and player fullscreen
record (window_fullscreen + yt_fullscreen: the player's own fullscreen, the chat hidden, player_is_screen must be
true), the shot-origin calibration (calibrate_shot_origin: fullscreen_screen must be true, offset [0, 0]), and the X11
shot region the loop would use; saves that region's grab to /tmp/fscheck_region.jpg for the eye check."""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

NSIM = Path(__file__).resolve().parents[2] / "networkSim"
sys.path.insert(0, str(NSIM))
import browser as B  # noqa: E402
import geometry  # noqa: E402


def launch_vars(port: int, session_dir: Path, profile: Path) -> tuple[str, list[str]]:
    """yt's fullscreen rotation entry and the collector's `make start_chrome` variables for it, on `port`."""
    import exp as E                                      # the collector's own WINDOW_SIZES
    spec = next(g for g in geometry.GEOMETRY_CHOICES["youtube"] if g.startswith("fullscreen"))
    return spec, (geometry.start_chrome_make_vars(session_dir, profile, E.WINDOW_SIZES.get("youtube"), "youtube", spec)
                  + [f"CDP_PORT={port}", "SLOT_SUFFIX=_fscheck"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9333)
    ap.add_argument("--url", default="https://www.youtube.com/@LofiGirl/live")
    a = ap.parse_args()
    if a.port == 9222:
        raise SystemExit("never the batch Chrome (9222): use a throwaway Chrome on 9333 / 9334")
    import os
    # over ssh there is no DISPLAY and Chrome never comes up (shagun 2026-10-19): the collection display, as ops/env.sh
    os.environ.setdefault("DISPLAY", ":0")
    os.environ.setdefault("XAUTHORITY", f"/run/user/{os.getuid()}/gdm/Xauthority")
    import exp as E
    session_dir, profile = Path("/tmp/fscheck_session"), Path(f"/tmp/fscheck_profile_{int(time.time())}")
    session_dir.mkdir(parents=True, exist_ok=True)
    profile.mkdir(parents=True)
    E.configure_chrome_download_dir(session_dir, profile)
    spec, mv = launch_vars(a.port, session_dir, profile)
    cmd = subprocess.run(["make", "-s", "-n", "start_chrome"] + mv, cwd=NSIM, capture_output=True, text=True).stdout
    launch = {"geometry": spec, "make_vars": mv, "chrome_command": cmd.strip()[-1500:],
              "display": os.environ.get("DISPLAY"),
              "test_type": "--test-type" in cmd, "no_sandbox": "--no-sandbox" in cmd}
    subprocess.run(["make", "-s", "start_chrome"] + mv, cwd=NSIM)
    try:
        return check(a, launch)
    finally:
        subprocess.run(["make", "-s", "kill_chrome", f"CHROME_PROFILE={profile}", f"CDP_PORT={a.port}", "SLOT_SUFFIX=_fscheck"],
                       cwd=NSIM)


def check(a, launch: dict) -> int:
    B.CDP_BASE = f"http://localhost:{a.port}"
    if not B._wait_for_cdp(30):
        print(json.dumps({"launch": launch, "verdict": "FAIL", "why": "the launched Chrome's CDP did not come up"}, indent=1))
        return 1
    targets = json.loads(urllib.request.urlopen(f"{B.CDP_BASE}/json/list", timeout=5).read())
    t = next(x for x in targets if x.get("type") == "page")
    page = B.CDPPage(t["webSocketDebuggerUrl"])
    page.navigate(a.url)
    for _ in range(60):
        time.sleep(1)
        st = json.loads(page.eval("JSON.stringify({t: (document.querySelector('#movie_player video')||{}).currentTime||0})") or "{}")
        if st.get("t", 0) > 3:
            break
    out = {"launch": launch, "playing_t": st.get("t")}
    out["fullscreen"] = B.yt_fullscreen(page, target_id=t["id"])
    out["origin"] = B.calibrate_shot_origin(page)
    try:                                                 # the window's state at the end (CDP; X11 fullscreen = this)
        wid = B._browser_call("Browser.getWindowForTarget", {"targetId": t["id"]}).get("windowId")
        out["window_at_end"] = B._browser_call("Browser.getWindowBounds", {"windowId": wid}).get("bounds")
    except Exception as e:
        out["window_at_end"] = {"error": repr(e)}
    out["fullscreen_trace"] = json.loads(page.eval("JSON.stringify(window.__lvqoeFs || null)") or "null")
    pr = json.loads(page.eval(B._PLAYER_RECT_JS) or "{}")
    fs = B._fullscreen_on_screen(pr) if out["origin"].get("fullscreen_screen") else None
    vp = B._viewport_on_screen(json.loads(page.eval(B._VIEWPORT_JS) or "{}"))
    region = B._player_on_screen(fs, pr.get("player")) or fs if fs else vp
    out["shot_region"] = {"region": region, "source": "screen (calibrated)" if fs else "window model"}
    if region:
        data = B._grab_x11(region)
        if data:
            Path("/tmp/fscheck_region.jpg").write_bytes(data)
            out["shot_region"]["saved"] = "/tmp/fscheck_region.jpg"
    ok = bool((out["fullscreen"].get("window") or {}).get("ok")) and out["fullscreen"].get("on_screen") \
        and out["fullscreen"].get("player_is_screen") and out["origin"].get("fullscreen_screen")
    out["verdict"] = "PASS" if ok else "FAIL"
    print(json.dumps(out, indent=1, default=str))
    page.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
