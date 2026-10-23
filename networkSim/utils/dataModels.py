from dataclasses import dataclass


@dataclass
class NetLabel:
    """One net_labels.txt row per shaper change (a tc change). speed / delay_ms / loss_pct are the realised values set
    on tc; state is the nominal state; redraw_* are the sub-step's re-draw before the walk and the session's delay /
    loss factor; walk_step is 0 at the re-draw, k at the walk's k-th step (restart prereg 2026-10-21 §2). Rows written
    before §2 carry timestamp, speed and state only."""
    timestamp: float
    speed: int
    state: str
    delay_ms: int | None = None
    loss_pct: float | None = None
    redraw_kbit: int | None = None
    redraw_delay_ms: int | None = None
    redraw_loss_pct: float | None = None
    walk_step: int | None = None
    apply_start: float | None = None    # v3 A9 (t67): the apply began (before `make slow` / `make rate`); timestamp = it returned
    apply: str | None = None            # "slow" (delay / loss / rate re-set) or "rate" (a walk step: the HTB rate only)


@dataclass
class NetStat:
    """One tc change: rate / delay_ms / loss_pct realised (the walk's rate, the delay and loss x the session factor),
    state nominal; redraw_* the sub-step's re-draw; walk_step 0 at the re-draw, k at the walk's k-th step."""
    rate: int  # kbit
    duration: float
    delay_ms: int
    loss_pct: float
    state: str
    redraw_kbit: int | None = None
    redraw_delay_ms: int | None = None
    redraw_loss_pct: float | None = None
    walk_step: int = 0
