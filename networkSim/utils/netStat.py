from .shaping import next_state, SCENARIOS
from .dataModels import NetStat
import random


def net_episode_generator(episode_length: int, transition_dict : dict[str, dict[str,float]]):
    min_state_time = episode_length / 5
    max_state_time = episode_length / 2
    state = "10"  # First state is always max internet
    while True:
        curr_time = 0
        run_inner = True
        while run_inner:

            scenario = SCENARIOS[state]

            # Sample duration
            dur = max(1, random.uniform(min_state_time, max_state_time))

            # Make sure we don't go past the episode
            remaining_time = episode_length - curr_time
            if dur > remaining_time:
                dur = max(1, remaining_time)
                run_inner = False
            if state == "stall":
                # in case we have a stall state, we do not continue it beyond one fraction.
                run_inner = False
            yield NetStat(
                rate=scenario.sample_rate(),
                duration=dur,
                delay_ms=scenario.sample_delay(),
                loss_pct=scenario.sample_loss(),
                state=state,
            )

            curr_time += dur
        state = next_state(state, transition_dict= transition_dict)
