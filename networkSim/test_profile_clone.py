#!/usr/bin/env python3
"""The per-session profile clone is gone after the session (supervisor 2026-10-18; profile_clone.py): a fake session
clones a template under current_data/chrome_profiles, runs, and removes its clone after the check; the template and
anything outside the clones root are never touched.

    python3 test_profile_clone.py        (exit 0 = pass)
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_clone import remove_profile_clone  # noqa: E402


def fake_session(cur: Path, template: Path) -> Path:
    """What exp.py does with a clone: copy the template, use it (Chrome writes into it), check, then remove it."""
    prof = cur / "chrome_profiles" / "tiktok_ti_1_1792000000"
    prof.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template, prof, symlinks=True)
    (prof / "Default" / "Cookies").write_text("session cookies")       # what Chrome leaves behind
    (cur / "check.json").write_text("{}")                               # check_session ran
    assert remove_profile_clone(cur / "chrome_profiles", prof)
    return prof


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        cur = Path(td) / "current_data"
        tmpl = Path(td) / "profiles" / "tiktok"
        (tmpl / "Default").mkdir(parents=True)
        (tmpl / "Default" / "Preferences").write_text("{}")
        prof = fake_session(cur, tmpl)
        assert not prof.exists() and (tmpl / "Default" / "Preferences").exists()      # clone gone, template kept
        assert (cur / "chrome_profiles").is_dir()                                       # the root stays
        # refused: the template, the root itself, a nested dir, a missing dir, a symlink to a dir
        assert not remove_profile_clone(cur / "chrome_profiles", tmpl) and tmpl.exists()
        assert not remove_profile_clone(cur / "chrome_profiles", cur / "chrome_profiles")
        nested = cur / "chrome_profiles" / "a" / "b"
        nested.mkdir(parents=True)
        assert not remove_profile_clone(cur / "chrome_profiles", nested) and nested.exists()
        assert not remove_profile_clone(cur / "chrome_profiles", cur / "chrome_profiles" / "missing")
        link = cur / "chrome_profiles" / "link"
        link.symlink_to(tmpl, target_is_directory=True)
        assert not remove_profile_clone(cur / "chrome_profiles", link) and tmpl.exists()
    print("test_profile_clone: ok")
    return 0


def test_main():
    """The script's own checks under pytest too (levi t58 T-1: the apply gate runs both runners)."""
    assert main() in (0, None)


if __name__ == "__main__":
    sys.exit(main())
