from .shaping import next_state, next_state_no_self, initial_state, SCENARIOS
from .dataModels import NetStat
import math
import random

# States at or below this threshold are considered stall-risk.
# After MAX_CONSECUTIVE_LOW consecutive episodes here, force a jump to MIN_RECOVERY_STATE.
_LOW_STATE_THRESHOLD  = 3   # states "1", "2", "3"
_MAX_CONSECUTIVE_LOW  = 2   # allow at most 2 consecutive stall-risk episodes (~120 s)
_MIN_RECOVERY_STATE   = "4" # jump to at least this state after the cap

# Restart prereg 2026-10-21 §2 (levi, on brad's code facts 00228; the walk in linear rate, levi's ruling on brad 00231).
# (a) Within each sub-step the rate follows a reflecting random walk in linear rate inside the state's band, from the
# sub-step's re-drawn rate, one step every WALK_STEP_S; a step is placed only while at least WALK_LAST_MIN_S of the
# sub-step remains after it. Its stationary law is uniform on the band, the re-draw's own, so the walk keeps the band
# mean. sigma per state = WALK_SIGMA_PER_BAND x (rate_max - rate_min), calibrated so the walk as run (reflected, from
# the uniform re-draw) over the reference sub-step (WALK_REF_STEPS steps: the base / @slow re-draw's mean of 21 s)
# spans WALK_SPAN_SHARE of the band in expectation (walk_simulation.calibrate_sigma, seeded bisection; the walk on the
# band is scale-free, so one constant serves every state). Unreflected, Spitzer's E[range of an n-step Gaussian walk]
# = sigma sqrt(2/pi) sum_{k=1..n} k^-1/2 would give 0.1248; the walls compress that walk's span to about 0.42.
# (b) One factor per session, log-U(DL_FACTOR_LO, DL_FACTOR_HI), multiplies the re-drawn delay and loss (one factor for
# both, independent of state). Both draw from their own generators, seeded per session (session_shaping), never from
# the global random: the state sequence and the re-draws are the same with or without them.
WALK_RULE = "restart 2026-10-21 §2 (a): reflecting random walk in linear rate in the state's band from the sub-step's re-draw"
WALK_STEP_S = 2.0
WALK_LAST_MIN_S = 1.0
WALK_SPAN_SHARE = 0.5
WALK_REF_STEPS = 10
WALK_SIGMA_PER_BAND = 0.159
DL_FACTOR_RULE = "restart 2026-10-21 §2 (b): one factor per session, log-U(0.5, 2), on the re-drawn delay and loss"
DL_FACTOR_LO, DL_FACTOR_HI = 0.5, 2.0
SHAPING_SEED = 0


def walk_sigma(state: str) -> float:
    """The walk's step sigma in kbit for the state."""
    sc = SCENARIOS[state]
    return WALK_SIGMA_PER_BAND * (sc.rate_max - sc.rate_min)


def reflect(x: float, lo: float, hi: float) -> float:
    """x folded back into [lo, hi] at both walls."""
    if hi <= lo:
        return lo
    while x < lo or x > hi:
        x = 2 * lo - x if x < lo else 2 * hi - x
    return x


def walk_step(x: float, state: str, rng: random.Random) -> float:
    """One step of the walk from rate x (kbit, unrounded) in the state's band."""
    sc = SCENARIOS[state]
    return reflect(x + rng.gauss(0.0, walk_sigma(state)), sc.rate_min, sc.rate_max)


def draw_dl_factor(rng: random.Random) -> float:
    return round(math.exp(rng.uniform(math.log(DL_FACTOR_LO), math.log(DL_FACTOR_HI))), 6)


def session_shaping(session_name: str) -> tuple[random.Random, dict]:
    """§2's per-session generators, seeded by the session's name: (the walk's generator, the record for meta.json
    shaper: the walk's rule and sigma per state, the delay / loss factor and both seeds)."""
    walk_seed, dl_seed = f"walk:{SHAPING_SEED}:{session_name}", f"dl:{SHAPING_SEED}:{session_name}"
    rec = {"walk": {"rule": WALK_RULE, "space": "linear", "step_s": WALK_STEP_S, "last_min_s": WALK_LAST_MIN_S,
                    "span_share": WALK_SPAN_SHARE, "ref_steps": WALK_REF_STEPS,
                    "sigma_per_band": WALK_SIGMA_PER_BAND,
                    "sigma_kbit_by_state": {s: round(walk_sigma(s), 3) for s in sorted(SCENARIOS, key=int)},
                    "seed": walk_seed},
           "dl_factor": {"rule": DL_FACTOR_RULE, "lo": DL_FACTOR_LO, "hi": DL_FACTOR_HI,
                         "value": draw_dl_factor(random.Random(dl_seed)), "seed": dl_seed}}
    return random.Random(walk_seed), rec


def substep_changes(state: str, rate: int, dur: float, delay_ms: int, loss_pct: float,
                    walk_rng: random.Random | None = None, dl_factor: float = 1.0) -> list[NetStat]:
    """One sub-step's tc changes: its re-draw at 0 (walk_step 0), then, with walk_rng, the walk's steps (§2 (a); the
    set rate is the walk's position rounded to the kbit). Delay and loss are the re-draw's x dl_factor (§2 (b)) for the
    whole sub-step. Without walk_rng and with factor 1 this is the single change the generators yielded before §2."""
    starts = [0.0]
    if walk_rng is not None:
        while dur - (starts[-1] + WALK_STEP_S) >= WALK_LAST_MIN_S - 1e-9:
            starts.append(starts[-1] + WALK_STEP_S)
    d, loss = int(round(delay_ms * dl_factor)), round(loss_pct * dl_factor, 3)
    x, out = float(rate), []
    for k, t in enumerate(starts):
        if k:
            x = walk_step(x, state, walk_rng)
        end = starts[k + 1] if k + 1 < len(starts) else dur
        out.append(NetStat(rate=int(round(x)), duration=end - t, delay_ms=d, loss_pct=loss, state=state,
                           redraw_kbit=rate, redraw_delay_ms=delay_ms, redraw_loss_pct=loss_pct, walk_step=k))
    return out


# v3 W-A (levi 17:1x, the instrumentation A/B: "the arms differ ONLY in the instrument flag"): one recorded schedule replayed
# identically in every round. States 1 -> 10 -> 1, each held REPLAY_STEP_S; rate / delay / loss and the walk come from
# generators seeded by the replay seed alone (never the session name, never the module's random), so every session given
# the same seed and step gets the same tc changes at the same session times. Dry only (orchestrate refuses it with
# --trio-record).
REPLAY_STATES = tuple(str(i) for i in range(1, 11)) + tuple(str(i) for i in range(9, 0, -1))
REPLAY_STEP_S = 79.0          # 19 steps x 79 s = 1501 s, the W-A session's 25 min
REPLAY_RULE = "v3 W-A: states 1->10->1, REPLAY_STEP_S each, rate / delay / loss / walk seeded by the replay seed only"


def replay_record(seed: str, step_s: float = REPLAY_STEP_S) -> dict:
    return {"rule": REPLAY_RULE, "seed": seed, "step_s": float(step_s), "states": list(REPLAY_STATES),
            "session_s": round(len(REPLAY_STATES) * float(step_s), 3)}


def replay_generator(episode_length=None, transition_dict=None, forced_initial_state=None, max_episodes=None, *,
                     replay_seed: str, step_s: float = REPLAY_STEP_S, **_ignored):
    """The replayed schedule's tc changes (substep_changes per state: its draw, then its walk steps); the shaper's other
    options (the session's walk generator, delay / loss factor, low cap, start rule) are ignored: the schedule is fixed."""
    rng = random.Random(f"replay:{replay_seed}")
    walk = random.Random(f"walk:replay:{replay_seed}")
    for state in REPLAY_STATES:
        sc = SCENARIOS[state]
        rate = rng.randint(sc.rate_min, sc.rate_max)
        delay = rng.randint(sc.delay_min, sc.delay_max)
        loss = round(rng.uniform(sc.loss_pct_min, sc.loss_pct_max), 1)
        yield from substep_changes(state, rate, float(step_s), delay, loss, walk, 1.0)


def net_episode_generator(
    episode_length: int,
    transition_dict: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    max_consecutive_low: int = _MAX_CONSECUTIVE_LOW,
    initial_state_fn=None,
    walk_rng: random.Random | None = None,
    dl_factor: float = 1.0,
):
    """
    Yields NetStat objects one per tc change (each sub-step's re-draw, then its walk steps: substep_changes).
    Each episode is ~episode_length seconds in a single state; states
    transition via the Markov chain at episode boundaries.

    forced_initial_state: skip the weighted-random start and begin here.
    max_episodes: stop after this many state transitions (None = run forever).
    max_consecutive_low: low-state cap (t38: yt 6 under matrices A / B; every other config 2).
    initial_state_fn: start-state rule (t38: yt draws from the active matrix's stationary distribution).
    walk_rng / dl_factor: restart §2's walk generator and per-session delay / loss factor (None / 1: no walk, no factor).
    """
    min_state_time = episode_length / 5
    max_state_time = episode_length / 2
    state = forced_initial_state if forced_initial_state else (initial_state_fn or initial_state)(transition_dict)
    episode_count = 0
    consecutive_low = 0
    while max_episodes is None or episode_count < max_episodes:
        curr_time = 0
        run_inner = True
        while run_inner:
            scenario = SCENARIOS[state]
            dur = max(1, random.uniform(min_state_time, max_state_time))
            remaining_time = episode_length - curr_time
            if dur > remaining_time:
                dur = max(1, remaining_time)
                run_inner = False
            rate, delay, loss = scenario.sample_rate(), scenario.sample_delay(), scenario.sample_loss()
            yield from substep_changes(state, rate, dur, delay, loss, walk_rng, dl_factor)
            curr_time += dur

        # Track consecutive stall-risk episodes and force recovery if needed
        if int(state) <= _LOW_STATE_THRESHOLD:
            consecutive_low += 1
        else:
            consecutive_low = 0

        next_s = next_state(state, transition_dict=transition_dict)
        if consecutive_low >= max_consecutive_low and int(next_s) <= _LOW_STATE_THRESHOLD:
            next_s = _MIN_RECOVERY_STATE
            consecutive_low = 0

        state = next_s
        episode_count += 1


def family_episode_generator(
    episode_length: float,
    transition_dict: dict[str, dict[str, float]],
    redraw_s: tuple[float, float],
    low_cap_s: float,
    self_loops: bool = True,
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    initial_state_fn=None,
    walk_rng: random.Random | None = None,
    dl_factor: float = 1.0,
):
    """Addendum Q am. 17's schedule families (utils/shaping.SCHEDULES): NetStat sub-steps of redraw_s seconds within
    episodes of exactly episode_length; at each boundary the next state is drawn with or without self-loops. The
    low-state cap is in seconds: consecutive time at states <= _LOW_STATE_THRESHOLD never exceeds low_cap_s; a low
    episode reaching it is cut there and held at _MIN_RECOVERY_STATE for the rest of the episode (the next draw is from
    that state's row). No sub-step is shorter than 1 s unless the episode or the cap ends it there. Each sub-step yields
    its re-draw and then restart §2's walk steps (substep_changes; walk_rng / dl_factor as net_episode_generator's)."""
    lo, hi = redraw_s
    state = forced_initial_state if forced_initial_state else (initial_state_fn or initial_state)(transition_dict)
    episode_count, low_s = 0, 0.0
    while max_episodes is None or episode_count < max_episodes:
        t = 0.0
        while episode_length - t > 1e-6:
            low = int(state) <= _LOW_STATE_THRESHOLD
            if low and low_cap_s - low_s <= 1e-6:
                state, low, low_s = _MIN_RECOVERY_STATE, False, 0.0
            limit = episode_length - t
            if low:
                limit = min(limit, low_cap_s - low_s)
            dur = max(1.0, random.uniform(lo, hi))
            if limit - dur < 1.0:
                dur = limit
            scenario = SCENARIOS[state]
            rate, delay, loss = scenario.sample_rate(), scenario.sample_delay(), scenario.sample_loss()
            yield from substep_changes(state, rate, dur, delay, loss, walk_rng, dl_factor)
            t += dur
            low_s = low_s + dur if low else 0.0
        state = next_state(state, transition_dict=transition_dict) if self_loops else next_state_no_self(state, transition_dict)
        episode_count += 1
