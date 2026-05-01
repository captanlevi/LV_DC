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



def next_state(curr: str, transition_dict : dict[str,dict[str,float]]) -> str:
    trans = transition_dict[curr]
    states = list(trans.keys())
    probs = list(trans.values())
    return random.choices(states, weights=probs, k=1)[0]


SCENARIOS = {
    "10": Scenario(
        rate_lim=(6000, 7500),
        delay_lim=(0, 50),
        loss_pct_lim=(0.0, 0.5),
    ),
    "9" : Scenario(
        rate_lim= (5000,6000),
        delay_lim= (0,50),
        loss_pct_lim= (0.0,.5)

    ),
    "8" : Scenario(
        rate_lim= (3000,4000),
        delay_lim= (0,50),
        loss_pct_lim= (0.0,.5)

    ),
    "7" : Scenario(
        rate_lim= (2000,3000),
        delay_lim= (0,50),
        loss_pct_lim= (0.0,.5)

    ),
    "6" : Scenario(
        rate_lim= (1500,2000),
        delay_lim= (0,80),
        loss_pct_lim= (0.0,.6)

    ),
    "5" : Scenario(
        rate_lim= (1000,1500),
        delay_lim= (0,80),
        loss_pct_lim= (0.0,.7)

    ),
    "4" : Scenario(
        rate_lim= (800,1000),
        delay_lim= (0,90),
        loss_pct_lim= (0.0,.8)

    ),
    "3" : Scenario(
        rate_lim= (500,800),
        delay_lim= (0,100),
        loss_pct_lim= (0.0,.8)

    ),
    "2" : Scenario(
        rate_lim= (400,500),
        delay_lim= (0,100),
        loss_pct_lim= (0.0,.8)

    ),
    "1" : Scenario(
        rate_lim= (180,400),
        delay_lim= (0,100),
        loss_pct_lim= (0.0,.8)

    )
}


YOUTUBE_TRANSITIONS = {
    "10": {"10": 0.4, "9": 0.3, "8": 0.2, "7": 0.1},

    "9":  {"10": 0.2, "9": 0.4, "8": 0.25, "7": 0.1, "6": 0.05},

    "8":  {"9": 0.2, "8": 0.4, "7": 0.25, "6": 0.1, "5": 0.05},

    "7":  {"8": 0.2, "7": 0.4, "6": 0.25, "5": 0.1, "4": 0.05},

    "6":  {"7": 0.2, "6": 0.4, "5": 0.25, "4": 0.1, "3": 0.05},

    "5":  {"6": 0.2, "5": 0.4, "4": 0.25, "3": 0.1, "2": 0.05},

    "4":  {"5": 0.2, "4": 0.4, "3": 0.25, "2": 0.1, "1": 0.05},

    "3":  {"4": 0.2, "3": 0.4, "2": 0.25, "1": 0.15},

    "2":  {"3": 0.2, "2": 0.4, "1": 0.4},

    "1":  {"2": 0.3, "1": 0.7},
}


BILIBILI_TRANSITIONS = {
    "10": {"10": 0.6, "9": 0.25, "8": 0.1, "7": 0.05},

    "9":  {"10": 0.3, "9": 0.4, "8": 0.2, "7": 0.1},

    "8":  {"9": 0.3, "8": 0.4, "7": 0.2, "6": 0.1},

    # 720p floor
    "7":  {"8": 0.3, "7": 0.4, "6": 0.2, "5": 0.1},

    "6":  {"7": 0.3, "6": 0.35, "5": 0.25, "8": 0.1},

    "5":  {"6": 0.4, "5": 0.35, "7": 0.25},

    # lowest meaningful state
    "4":  {"6": 0.5, "5": 0.3, "4": 0.2},

    # transient junk states (auto-escape)
    "3": {"5": 0.6, "6": 0.3, "3": 0.1},
    "2": {"5": 0.6, "6": 0.3, "2": 0.1},
    "1": {"5": 0.7, "6": 0.3},
}


TIKTOK_TRANSITIONS = {
    "10": {"10": 0.5, "9": 0.25, "8": 0.15, "7": 0.1},

    "9":  {"10": 0.25, "9": 0.35, "8": 0.2, "7": 0.15, "6": 0.05},

    "8":  {"9": 0.25, "8": 0.35, "7": 0.2, "6": 0.15, "5": 0.05},

    # mid band (very dynamic)
    "7":  {"8": 0.25, "7": 0.3, "6": 0.2, "5": 0.15, "9": 0.1},

    "6":  {"7": 0.25, "6": 0.3, "5": 0.2, "8": 0.15, "4": 0.1},

    "5":  {"6": 0.3, "5": 0.3, "7": 0.2, "4": 0.15, "8": 0.05},

    # 360p floor — strong bounce
    "4":  {"6": 0.4, "5": 0.25, "7": 0.15, "4": 0.2},

    # forbidden/stall states → instant recovery
    "3":  {"5": 0.6, "6": 0.3, "4": 0.1},
    "2":  {"5": 0.6, "6": 0.3, "4": 0.1},
    "1":  {"5": 0.7, "6": 0.3},
}



TWITCH_TRANSITIONS = {
    "10": {"10": 0.55, "9": 0.25, "8": 0.15, "7": 0.05},

    "9":  {"10": 0.25, "9": 0.4, "8": 0.2, "7": 0.1, "6": 0.05},

    "8":  {"9": 0.25, "8": 0.4, "7": 0.2, "6": 0.1, "5": 0.05},

    # core operating band (720p-ish)
    "7":  {"8": 0.2, "7": 0.4, "6": 0.2, "5": 0.1, "9": 0.1},

    "6":  {"7": 0.25, "6": 0.35, "5": 0.2, "8": 0.1, "4": 0.1},

    # fallback band (more sticky than TikTok)
    "5":  {"6": 0.3, "5": 0.35, "4": 0.2, "7": 0.1, "3": 0.05},

    "4":  {"5": 0.3, "4": 0.4, "6": 0.15, "3": 0.1, "2": 0.05},

    # low states — recover, but not instantly
    "3":  {"5": 0.35, "4": 0.25, "3": 0.25, "6": 0.1, "2": 0.05},

    "2":  {"4": 0.35, "3": 0.3, "2": 0.25, "5": 0.1},

    "1":  {"3": 0.4, "2": 0.3, "1": 0.2, "4": 0.1},
}



