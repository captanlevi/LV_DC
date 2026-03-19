from .shaping import next_state, SCENARIOS
from .dataModels import NetStat
import random



def net_episode_generator(episode_length: int):
    min_state_time = episode_length / 5
    max_state_time = episode_length / 2
    state = "720"  # First state is always 720
    while True:
        curr_time = 0
        
        run_inner = True
        while run_inner:
            
            scenario = SCENARIOS[state]

            # Sample duration
            dur = max(1,random.uniform(min_state_time, max_state_time))

            # Make sure we don't go past the episode
            remaining_time = episode_length - curr_time
            if dur > remaining_time:
                dur = max(1,remaining_time)
                run_inner = False
            yield NetStat(
                rate=scenario.sample_rate(),
                duration=dur,
                delay_ms=scenario.sample_delay(),
                loss_pct=scenario.sample_loss(),
                state= state
            )

            curr_time += dur
        state = next_state(state)










