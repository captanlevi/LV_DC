from .shaping import next_state, next_state_no_self, initial_state, SCENARIOS
from .dataModels import NetStat
import random

# States at or below this threshold are considered stall-risk.
# After MAX_CONSECUTIVE_LOW consecutive episodes here, force a jump to MIN_RECOVERY_STATE.
_LOW_STATE_THRESHOLD  = 3   # states "1", "2", "3"
_MAX_CONSECUTIVE_LOW  = 2   # allow at most 2 consecutive stall-risk episodes (~120 s)
_MIN_RECOVERY_STATE   = "4" # jump to at least this state after the cap


def net_episode_generator(
    episode_length: int,
    transition_dict: dict[str, dict[str, float]],
    forced_initial_state: str | None = None,
    max_episodes: int | None = None,
    max_consecutive_low: int = _MAX_CONSECUTIVE_LOW,
    initial_state_fn=None,
):
    """
    Yields NetStat objects one per bandwidth sample.
    Each episode is ~episode_length seconds in a single state; states
    transition via the Markov chain at episode boundaries.

    forced_initial_state: skip the weighted-random start and begin here.
    max_episodes: stop after this many state transitions (None = run forever).
    max_consecutive_low: low-state cap (t38: yt 6 under matrices A / B; every other config 2).
    initial_state_fn: start-state rule (t38: yt draws from the active matrix's stationary distribution).
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
            yield NetStat(
                rate=scenario.sample_rate(),
                duration=dur,
                delay_ms=scenario.sample_delay(),
                loss_pct=scenario.sample_loss(),
                state=state,
            )
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
):
    """Addendum Q am. 17's schedule families (utils/shaping.SCHEDULES): NetStat sub-steps of redraw_s seconds within
    episodes of exactly episode_length; at each boundary the next state is drawn with or without self-loops. The
    low-state cap is in seconds: consecutive time at states <= _LOW_STATE_THRESHOLD never exceeds low_cap_s; a low
    episode reaching it is cut there and held at _MIN_RECOVERY_STATE for the rest of the episode (the next draw is from
    that state's row). No sub-step is shorter than 1 s unless the episode or the cap ends it there."""
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
            yield NetStat(
                rate=scenario.sample_rate(),
                duration=dur,
                delay_ms=scenario.sample_delay(),
                loss_pct=scenario.sample_loss(),
                state=state,
            )
            t += dur
            low_s = low_s + dur if low else 0.0
        state = next_state(state, transition_dict=transition_dict) if self_loops else next_state_no_self(state, transition_dict)
        episode_count += 1
