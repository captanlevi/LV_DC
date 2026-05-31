from .shaping import next_state, initial_state, SCENARIOS
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
):
    """
    Yields NetStat objects one per bandwidth sample.
    Each episode is ~episode_length seconds in a single state; states
    transition via the Markov chain at episode boundaries.

    forced_initial_state: skip the weighted-random start and begin here.
    max_episodes: stop after this many state transitions (None = run forever).
    """
    min_state_time = episode_length / 5
    max_state_time = episode_length / 2
    state = forced_initial_state if forced_initial_state else initial_state(transition_dict)
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
        if consecutive_low >= _MAX_CONSECUTIVE_LOW and int(next_s) <= _LOW_STATE_THRESHOLD:
            next_s = _MIN_RECOVERY_STATE
            consecutive_low = 0

        state = next_s
        episode_count += 1
