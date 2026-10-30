#!/usr/bin/env python3
"""yt-only boundary, fast gate G0 (levi 00139): the scripts this tree injects are 7809b21c's, byte for byte, except the yt
MSE wrap with its new rows on. Renders, at the base sha (git show into a temp dir) and at this tree:
  - every module-level str of browser.py whose name ends in _JS (templates as they are);
  - the per-session renders: worker_anchor_js(block_s) (tw / tt), the yt MSE wrap with both new constants off (the base:
    MSE_WRAP_JS % its keys) and on (reported, expected to differ), for block_s 0 / 60;
  - every file of the extension directory the collector loads (labeling/, at the repo root);
and prints one line per item: name, base sha256[:12], tip sha256[:12], equal. Exit 0 only when every item but the yt-on
render is equal and `git diff --stat <base>` names nothing beyond browser.py, the MSE wrap tests, their fixtures and
this script.
    python3 g0_rendered_scripts.py [base_sha]        (default 7809b21c)
"""
import hashlib
import importlib.util
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALLOWED = {"networkSim/browser.py", "networkSim/test_mse_wrap.py", "networkSim/test_mse_wrap_webm.py",
           "networkSim/g0_rendered_scripts.py", "networkSim/fixtures/mse_wrap_js_7809b21c.js",
           "networkSim/fixtures/yt_key_twin.json"}
EXT_DIRS = ("labeling",)                    # repo root: browser.LABELING_DIR, the content.js extension


def sha(s) -> str:
    return hashlib.sha256(s if isinstance(s, bytes) else s.encode()).hexdigest()[:12]


def load(path: Path, name: str):
    sys.modules.setdefault("websocket", types.ModuleType("websocket"))
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    finally:
        sys.path.pop(0)


def renders(m, tip: bool) -> dict:
    out = {k: v for k, v in vars(m).items() if k.endswith("_JS") and isinstance(v, str) and not k.startswith("MSE_WRAP_YT")}
    for bs in (0, 60):
        out[f"worker_anchor_js({bs})"] = m.worker_anchor_js(bs)
        kw = {"block_s": bs, "max": m.FRAME_LOG_MSE_MAX, "r2": int(m.MSE_WRAP_R2)}
        out[f"yt mse wrap off ({bs})"] = (m.mse_wrap_js(bs, webm=False, ykey=False) if tip else m.MSE_WRAP_JS % kw)
        if tip:
            out[f"yt mse wrap ON ({bs}) [expected to differ]"] = m.mse_wrap_js(bs)
    return out


def ext_files(root: Path, ref: str | None) -> dict:
    out = {}                                    # root: the repo root
    for d in EXT_DIRS:
        if ref is None:
            for p in sorted((root / d).rglob("*")) if (root / d).is_dir() else []:
                if p.is_file():
                    out[f"{d}/{p.relative_to(root / d)}"] = p.read_bytes()
        else:
            ls = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", ref, d],
                                capture_output=True, text=True).stdout.split()
            for f in ls:
                out[f] = subprocess.run(["git", "-C", str(root), "show", f"{ref}:{f}"], capture_output=True).stdout
    return out


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "7809b21c"
    names = subprocess.run(["git", "-C", str(HERE.parent), "diff", "--name-only", base], capture_output=True, text=True).stdout.split()
    extra = sorted(set(names) - ALLOWED)
    with tempfile.TemporaryDirectory() as t:
        arc = subprocess.run(["git", "-C", str(HERE.parent), "archive", base], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", t], input=arc, check=True)          # the base's tree (browser.py reads labeling/)
        old = renders(load(Path(t) / "networkSim" / "browser.py", "browser_base"), tip=False)
    new = renders(load(HERE / "browser.py", "browser_tip"), tip=True)
    oe, ne = ext_files(HERE.parent, base), ext_files(HERE.parent, None)
    bad = 0
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        eq = a == b
        exp = "[expected to differ]" in k
        bad += 0 if (eq or exp) else 1
        print(f"{k:48s} {sha(a) if a is not None else '-':12s} {sha(b) if b is not None else '-':12s} "
              f"{'equal' if eq else ('differs (expected)' if exp else 'DIFFERS')}")
    for k in sorted(set(oe) | set(ne)):
        eq = oe.get(k) == ne.get(k)
        bad += 0 if eq else 1
        if not eq:
            print(f"{k:48s} DIFFERS")
    print(f"extension files: {len(ne)} at tip, {sum(oe.get(k) == v for k, v in ne.items())} equal to {base}")
    print(f"diff --name-only {base}: {names}; outside the allowed set: {extra or 'none'}")
    ok = bad == 0 and not extra
    print("G0:", "PASS" if ok else f"FAIL ({bad} differing, {len(extra)} files outside the set)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
