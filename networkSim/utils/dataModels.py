from dataclasses import dataclass


@dataclass
class NetLabel:
    """One net_labels.txt row per shaper change (a tc change). speed / delay_ms / loss_pct are the realised values set
    on tc; state is the nominal state; redraw_* are the sub-step's re-draw before the walk and the session's delay /
    loss factor; walk_step is 0 at the re-draw, k at the walk's k-th step (restart prereg 2026-10-21 §2). Rows written
    before §2 carry timestamp, speed and state only."""
    timestamp: float
    speed: int
    state: str | None
    delay_ms: int | None = None
    loss_pct: float | None = None
    redraw_kbit: int | None = None
    redraw_delay_ms: int | None = None
    redraw_loss_pct: float | None = None
    walk_step: int | None = None
    # §9 trace regime (levi efb5e4cc), written only on a trace session's rows (TRACE_FIELDS: exp.save_json drops them
    # when None, so a state session's net_labels.txt is byte-identical): state is None there and the low-state hold
    # keys on rate_band_state; scheduled_t is the change's scheduled time (timestamp: applied), for the apply-lag gate
    regime: str | None = None
    rate_band_state: str | None = None
    trace_source: str | None = None
    trace_id: str | None = None
    trace_offset_s: int | None = None
    trace_kbit: float | None = None
    clip: str | None = None
    scheduled_t: float | None = None


TRACE_FIELDS = ("regime", "rate_band_state", "trace_source", "trace_id", "trace_offset_s", "trace_kbit", "clip",
                "scheduled_t")


def net_label_row(label: "NetLabel") -> dict:
    """net_labels.txt's row for a label: the trace fields only where set (a state session's rows are unchanged)."""
    from dataclasses import asdict
    return {k: v for k, v in asdict(label).items() if not (k in TRACE_FIELDS and v is None)}


def trace_label_fields(net_stat: "NetStat", scheduled: float) -> dict:
    """The NetLabel fields a change adds: a trace change's regime, band state, source position and scheduled time;
    a state change none."""
    if not net_stat.regime:
        return {}
    return {k: getattr(net_stat, k) for k in ("regime", "rate_band_state", "trace_source", "trace_id", "trace_offset_s",
                                               "trace_kbit", "clip")} | {"scheduled_t": scheduled}


@dataclass
class NetStat:
    """One tc change: rate / delay_ms / loss_pct realised (the walk's rate, the delay and loss x the session factor),
    state nominal; redraw_* the sub-step's re-draw; walk_step 0 at the re-draw, k at the walk's k-th step."""
    rate: int  # kbit
    duration: float
    delay_ms: int
    loss_pct: float
    state: str | None
    redraw_kbit: int | None = None
    redraw_delay_ms: int | None = None
    redraw_loss_pct: float | None = None
    walk_step: int = 0
    regime: str | None = None             # §9 trace regime: "trace" (state None, rate_band_state the hold's key)
    rate_band_state: str | None = None
    trace_source: str | None = None
    trace_id: str | None = None
    trace_offset_s: int | None = None
    trace_kbit: float | None = None
    clip: str | None = None
