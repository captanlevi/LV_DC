from .shaping import next_state, SCENARIOS
from .dataModels import NetStat
import random



def net_episode_generator(episode_length: int):
    intra_episode_mean = episode_length / 5

    while True:
        curr_time = 0
        state = "720"  # First state is always 720

        while curr_time < episode_length:
            scenario = SCENARIOS[state]

            # Sample duration
            dur = random.expovariate(1 / intra_episode_mean)

            # Make sure we don't go past the episode
            remaining_time = episode_length - curr_time
            dur = max(1, min(dur, remaining_time))

            yield NetStat(
                rate=scenario.sample_rate(),
                duration=dur,
                delay_ms=scenario.sample_delay(),
                loss_pct=scenario.sample_loss(),
            )

            curr_time += dur
            state = next_state(state)










