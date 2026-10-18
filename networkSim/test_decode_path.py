#!/usr/bin/env python3
"""decode_path (supervisor 2026-10-18): tt's VA-API HEVC flag set reaches start_chrome as make variables, the extra
disabled feature joins the one --disable-features list, yt / twitch keep today's args, and Chrome's GPU report is
summarised. The Makefile is checked with make -n (nothing launches).

    python3 test_decode_path.py        (exit 0 = pass)
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import decode_path as D  # noqa: E402


def dry(vars_):
    r = subprocess.run(["make", "-n", "-s", "start_chrome", "CHROME_BIN=/usr/bin/google-chrome", "CHROME_PROFILE=/tmp/x",
                        "SESSION_DIR=/tmp/y", *vars_], cwd=HERE, capture_output=True, text=True)
    return r.stdout + r.stderr


def main():
    tt = D.make_vars("tiktok")
    assert tt == ["CHROME_ENABLE_FEATURES=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,PlatformHEVCDecoderSupport",
                  "CHROME_DISABLE_FEATURES_EXTRA=UseChromeOSDirectVideoDecoder",
                  "CHROME_EXTRA_ARGS=--ignore-gpu-blocklist --render-node-override=/dev/dri/renderD128",
                  "CHROME_ENV=LIBVA_DRIVER_NAME=iHD"], tt
    assert D.make_vars("youtube") == [] and D.make_vars("twitch") == [] and D.spec("twitch")["name"] == "default"
    if shutil.which("make"):
        out = dry(tt)
        dis = re.findall(r"--disable-features=(\S+)", out)
        assert len(set(dis)) == 1 and dis[0].endswith(",UseChromeOSDirectVideoDecoder") and "OptimizationHints" in dis[0], dis
        assert "--enable-features=VaapiVideoDecoder,VaapiVideoDecodeLinuxGL,PlatformHEVCDecoderSupport" in out
        assert "--render-node-override=/dev/dri/renderD128" in out and "LIBVA_DRIVER_NAME=iHD" in out
        base = dry([])
        assert "--enable-features" not in base and "LIBVA" not in base and "UseChromeOSDirectVideoDecoder" not in base
    info = {"gpu": {"featureStatus": {"video_decode": "enabled"}, "auxAttributes": {"glRenderer": "Mesa Intel"},
                    "videoDecoding": [{"profile": "h264 main"}, {"profile": "hevc main"}]}}
    assert D.gpu_summary(info) == {"video_decode": "enabled", "decode_profiles": ["h264 main", "hevc main"], "hevc": True,
                                   "gl_renderer": "Mesa Intel"}
    assert D.gpu_summary(None) == {"error": "no SystemInfo"}
    # chaos 2026-10-18: SystemInfo listed no profiles while HEVC played -> the page probe answers, then the played codec
    empty = D.gpu_summary({"gpu": {"featureStatus": {"video_decode": "enabled"}, "videoDecoding": []}})
    assert D.hevc_decision(empty, {"mse_hvc1": True, "webcodecs_hvc1": True}) == \
        {"hevc": True, "source": "page_probe", "probes": {"mse_hvc1": True, "webcodecs_hvc1": True}}
    assert D.hevc_decision(D.gpu_summary(info), {"mse_hvc1": False}) == {"hevc": True, "source": "systeminfo_profiles"}
    assert D.hevc_decision(empty, None, "hevc") == {"hevc": True, "source": "played_codec"}
    assert D.hevc_decision(empty, {"error": "x"}, None) == {"hevc": None, "source": "none"}
    import tempfile
    from profile_clone import template_note_of
    with tempfile.TemporaryDirectory() as d:
        assert template_note_of(Path(d)) is None
        (Path(d) / "TEMPLATE_NOTE.txt").write_text("rebuilt 2026-10-18 option b (HEVC)")
        assert template_note_of(Path(d)) == "rebuilt 2026-10-18 option b (HEVC)" and template_note_of(None) is None
    print("test_decode_path: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
