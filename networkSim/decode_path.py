"""decode_path.py: the per-platform Chrome decode flags (supervisor 2026-10-18, after shagun's HEVC test on chaos: Intel
iHD 25.2.4, Chrome 155). tt gets the VA-API HEVC flag set, so TikTok's player sees HEVC support and serves its HEVC
ladder instead of the one-rung H.264 stream. yt and twitch keep today's args, so their decode path stays the regime of
the sessions so far. Every session records its decode path (this spec plus Chrome's own GPU video-decode report, read
once at launch over CDP SystemInfo.getInfo) in meta.json decode_path, as a covariate.

The flags reach the Makefile's start_chrome as make variables:
- CHROME_ENABLE_FEATURES;
- CHROME_DISABLE_FEATURES_EXTRA, merged into the one --disable-features list (Chrome honours only the last
  --disable-features);
- CHROME_EXTRA_ARGS;
- CHROME_ENV (env assignments before the binary).
"""
from __future__ import annotations

# shagun's passing set on chaos (variant C; variant A passed too): with no flags Chrome 155 reports no HEVC
HEVC_VAAPI = {
    "name": "hevc-vaapi-v1 (2026-10-18)",
    "enable_features": ["VaapiVideoDecoder", "VaapiVideoDecodeLinuxGL", "PlatformHEVCDecoderSupport"],
    "disable_features": ["UseChromeOSDirectVideoDecoder"],
    "args": ["--ignore-gpu-blocklist", "--render-node-override=/dev/dri/renderD128"],
    "env": {"LIBVA_DRIVER_NAME": "iHD"},
}
DEFAULT = {"name": "default", "enable_features": [], "disable_features": [], "args": [], "env": {}}
PLATFORM_DECODE = {"tiktok": HEVC_VAAPI}


def spec(platform: str) -> dict:
    return PLATFORM_DECODE.get(platform, DEFAULT)


def make_vars(platform: str) -> list[str]:
    """`make start_chrome` variable assignments for the platform's decode flags (none for the default)."""
    s = spec(platform)
    out = []
    if s["enable_features"]:
        out.append("CHROME_ENABLE_FEATURES=" + ",".join(s["enable_features"]))
    if s["disable_features"]:
        out.append("CHROME_DISABLE_FEATURES_EXTRA=" + ",".join(s["disable_features"]))
    if s["args"]:
        out.append("CHROME_EXTRA_ARGS=" + " ".join(s["args"]))
    if s["env"]:
        out.append("CHROME_ENV=" + " ".join(f"{k}={v}" for k, v in sorted(s["env"].items())))
    return out


def gpu_summary(info: dict | None) -> dict:
    """Chrome's video-decode report from SystemInfo.getInfo: the feature status and the decode profiles (HEVC or not)."""
    if not info:
        return {"error": "no SystemInfo"}
    gpu = info.get("gpu") or {}
    profs = [p.get("profile") for p in gpu.get("videoDecoding") or [] if p.get("profile")]
    return {"video_decode": (gpu.get("featureStatus") or {}).get("video_decode"),
            "decode_profiles": profs, "hevc": any("hevc" in p.lower() for p in profs),
            "gl_renderer": ((gpu.get("auxAttributes") or {}).get("glRenderer"))}


# what TikTok's own probe sees (WebCodecs) and MSE, from the page (no network traffic)
HEVC_PROBE_JS = """(async () => { try {
  const mse = typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported('video/mp4; codecs="hvc1.1.6.L120.90"');
  let wc = null;
  try { wc = typeof VideoDecoder !== 'undefined' ? (await VideoDecoder.isConfigSupported({codec: 'hvc1.1.6.L120.90'})).supported : null; } catch (e) {}
  return JSON.stringify({mse_hvc1: mse, webcodecs_hvc1: wc});
} catch (e) { return null; } })()"""
