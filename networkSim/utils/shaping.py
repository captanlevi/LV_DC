import random

"""

/*
Live Video Streaming Bandwidth Requirements (kbit/s)

Resolution | Min (kbit/s) | Recommended (kbit/s) | Safe 1.5x (kbit/s)
-----------|--------------|----------------------|-------------------
720p       | 1800         | 2500 - 5000          | 3750 - 7500
480p       | 700          | 1000 - 2500          | 1500 - 3750
360p       | 400          | 600 - 1000           | 900 - 1500
240p       | 250          | 300 - 700            | 450 - 1050
144p       | 80           | 100 - 300            | 150 - 450

Notes:
- Min = may work but high buffering risk
- Recommended = typical streaming quality
- Safe (1.5x) = helps avoid stalls due to fluctuations
- Live streaming is more sensitive than pre-recorded video
*/

"""


class Scenario:
    """
    rates is the speed in kbit
    1000 kbit == 1 mbit
    """

    def __init__(
        self,
        rate_lim: tuple[int, int],
        delay_lim: tuple[int, int],
        loss_pct_lim: tuple[float, float],
    ):
        """
        delay in ms
        """
        self.rate_min = rate_lim[0]
        self.rate_max = rate_lim[1]

        self.delay_min, self.delay_max = delay_lim
        self.loss_pct_min, self.loss_pct_max = loss_pct_lim

        assert self.rate_max >= self.rate_min and self.rate_min > 0

    def sample_rate(self) -> int:
        return random.randint(self.rate_min, self.rate_max)

    def sample_delay(self) -> int:
        return random.randint(self.delay_min, self.delay_max)

    def sample_loss(self) -> float:
        return round(random.uniform(a=self.loss_pct_min, b=self.loss_pct_max), 1)


SCENARIOS = {
    "720": Scenario(
        rate_lim=(2500, 7500),
        delay_lim=(20, 80),
        loss_pct_lim=(0.0, 0.5),
    ),
    "480": Scenario(
        rate_lim=(1000, 2000),
        delay_lim=(30, 120),
        loss_pct_lim=(0.0, 1.0),
    ),
    "360": Scenario(
        rate_lim=(800, 1200),
        delay_lim=(50, 150),
        loss_pct_lim=(0.0, 2.0),
    ),
    "240": Scenario(
        rate_lim=(400, 1000),
        delay_lim=(80, 200),
        loss_pct_lim=(0.5, 3.0),
    ),
    "144": Scenario(
        rate_lim=(180, 500),
        delay_lim=(100, 300),
        loss_pct_lim=(1.0, 5.0),
    ),
    "stall": Scenario(
        rate_lim=(130, 250),
        delay_lim=(200, 500),
        loss_pct_lim=(5.0, 8.0),
    ),
}


TRANSITIONS = {
    "720": {
        "720": 0.2,
        "480": 0.4,
        "360": 0.4,
    },
    "480": {
        "720": 0.4,
        "480": 0.2,
        "360": 0.2,
        "240": 0.2,
    },
    "360": {
        "480": 0.4,
        "360": 0.2,
        "240": 0.2,
        "144": 0.2,
    },
    "240": {
        "360": 0.3,
        "240": 0.3,
        "144": 0.3,
        "stall": 0.1,
    },
    "144": {
        "240": 0.5,
        "144": 0.2,
        "stall": 0.3,
    },
    "stall": {
        "144": 1,
        "stall": 0,
    },
}


def next_state(curr: str) -> str:
    transitions = TRANSITIONS[curr]
    states = list(transitions.keys())
    probs = list(transitions.values())
    return random.choices(states, weights=probs, k=1)[0]
