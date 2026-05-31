#!/usr/bin/env python3
"""
Quick smoke-test for the browser automation layer.
Launches a Chrome instance with the debug port, navigates to a YouTube live
stream, confirms the video is playing, runs the ad-watcher for 30 seconds,
then exits cleanly.

Run from networkSim/:
    python3 test_browser.py [--platform youtube|twitch|tiktok|bilibili]
"""
import argparse
import subprocess
import time
import os
import signal
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "current_data"
SSL_KEY_LOG_FILE = DATA_DIR / "ssl_keys.log"
CHROME_PROFILE = DATA_DIR / "chrome_profile"
CHROME_BIN = "/usr/bin/google-chrome"


def launch_chrome() -> subprocess.Popen:
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SSLKEYLOGFILE": str(SSL_KEY_LOG_FILE)}
    proc = subprocess.Popen(
        [
            CHROME_BIN,
            "--no-sandbox",
            f"--user-data-dir={CHROME_PROFILE}",
            "--remote-debugging-port=9222",
            "--remote-allow-origins=*",
            "--disable-features=ChromeWhatsNewUI",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--no-first-run",
            "--disable-default-apps",
            "about:blank",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    print(f"Chrome PID: {proc.pid}")
    return proc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default="youtube",
                        choices=["youtube", "twitch", "tiktok", "bilibili"])
    parser.add_argument("--watch-secs", type=int, default=30,
                        help="Seconds to watch after video starts playing (default 30)")
    args = parser.parse_args()

    print(f"=== Browser automation test: {args.platform} ===\n")

    chrome = launch_chrome()
    print("Waiting 3 s for Chrome to initialise...")
    time.sleep(3)

    from browser import BrowserSession

    session = BrowserSession(args.platform)
    try:
        session.start()
        print(f"\nVideo is playing. Watching for {args.watch_secs} s...")
        time.sleep(args.watch_secs)
        print("\nTest complete — stopping session.")
    except RuntimeError as e:
        print(f"\nFAILED: {e}")
    finally:
        session.stop()
        print("Killing Chrome...")
        try:
            os.killpg(chrome.pid, signal.SIGTERM)
        except Exception:
            chrome.terminate()
        chrome.wait(timeout=5)
        print("Done.")


if __name__ == "__main__":
    main()
