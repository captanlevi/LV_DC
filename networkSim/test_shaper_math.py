#!/usr/bin/env python3
"""readers/shaper_math.py is the one definition of the span arithmetic (levi 04:19, t64: two definitions break C1). No
other Python file in the tree defines its functions; the trace regime's traces/fidelity.py imports them when it rebases.
Plus the arithmetic on a hand-computed case: a 20 s backlogged run at 1000 kbit with a change to 500 kbit at 10 s.

    python3 test_shaper_math.py        (exit 0 = pass)
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from readers import shaper_math as M  # noqa: E402

NAMES = ("rate_at", "integral_kbit_s", "backlogged_spans", "ratio_gate", "session_changes", "session_samples")


def test_one_definition():
    pat = re.compile(r"^\s*def (" + "|".join(NAMES) + r")\(", re.M)
    hits = sorted(str(p.relative_to(HERE.parent)) for p in HERE.parent.rglob("*.py")
                  if ".venv" not in p.parts and not p.name.startswith("test_") and pat.search(p.read_text(errors="replace")))
    assert hits == ["networkSim/readers/shaper_math.py"], hits


def test_arithmetic():
    ch = [(0.0, 1000.0, 40.0), (10.0, 500.0, 40.0)]
    assert M.rate_at(ch, 5) == (1000.0, 40.0) and M.rate_at(ch, 10) == (500.0, 40.0) and M.rate_at(ch, -1) is None
    assert M.integral_kbit_s(ch, 0, 20) == 1000 * 10 + 500 * 10
    by = 0.0
    samples = []
    for t in range(21):
        samples.append({"t": float(t), "bytes": by, "backlog": 50000})
        by += (1000 if t < 10 else 500) * 1000 / 8
    sp = M.backlogged_spans(samples, ch)
    assert len(sp) == 1 and sp[0]["s"] == 20.0 and abs(sp[0]["ratio"] - 1) < 1e-9, sp
    assert M.ratio_gate(sp) == "pass" and M.ratio_gate([]) == "insufficient"
    assert M.session_changes([{"timestamp": 1.0, "speed": 300, "delay_ms": 20}, {"timestamp": 0.5, "speed": 200}]) == \
        [(0.5, 200.0, 0.0), (1.0, 300.0, 20.0)]
    assert M.session_samples([{"ts": 2.0, "in": {"bytes": 5, "backlog": 0}}, {"ts": 1.0, "in": {}}]) == \
        [{"t": 2.0, "bytes": 5, "backlog": 0}]


def main() -> int:
    test_one_definition()
    test_arithmetic()
    print("test_shaper_math: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
