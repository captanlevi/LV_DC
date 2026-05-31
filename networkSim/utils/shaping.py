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



def next_state(curr: str, transition_dict: dict[str, dict[str, float]]) -> str:
    trans = transition_dict[curr]
    states = list(trans.keys())
    probs = list(trans.values())
    return random.choices(states, weights=probs, k=1)[0]


def initial_state(transition_dict: dict[str, dict[str, float]]) -> str:
    """
    Random weighted start state — upper half of the bandwidth range gets
    higher weight so sessions cover high-quality and recovery trajectories,
    not just the downgrade-from-peak path that always starting at '10' creates.
    """
    all_states = sorted(transition_dict.keys(), key=lambda s: int(s))
    n = len(all_states)
    # Linear weights: highest state gets weight n, lowest gets weight 1
    weights = list(range(1, n + 1))
    return random.choices(all_states, weights=weights, k=1)[0]


SCENARIOS = {
    # Calibration ground truth (YouTube/SkyNews, 2026-05-21):
    #   ≤450 kbit/s → 144p
    #   650 kbit/s  → 240–360p (borderline)
    #   900–1200    → stable 360p
    #   ≥1800       → 1080p (with quality forcing)

    # State 10: rock-solid 1080p, headroom to spare
    "10": Scenario(rate_lim=(6000, 9000), delay_lim=(0, 20),  loss_pct_lim=(0.0, 0.05)),
    # State 9: clean 1080p
    "9":  Scenario(rate_lim=(4000, 6000), delay_lim=(0, 30),  loss_pct_lim=(0.0, 0.1)),
    # State 8: comfortable 1080p
    "8":  Scenario(rate_lim=(2500, 4000), delay_lim=(0, 40),  loss_pct_lim=(0.0, 0.15)),
    # State 7: 1080p tight margin — momentary drops can cause brief stalls
    "7":  Scenario(rate_lim=(1800, 2500), delay_lim=(0, 60),  loss_pct_lim=(0.0, 0.2)),
    # State 6: transition zone — 360p ABR, could be nudged to 1080p
    "6":  Scenario(rate_lim=(1400, 1800), delay_lim=(0, 70),  loss_pct_lim=(0.0, 0.2)),
    # State 5: stable 360p upper end
    "5":  Scenario(rate_lim=(1000, 1400), delay_lim=(0, 80),  loss_pct_lim=(0.0, 0.25)),
    # State 4: stable 360p  (calibrated: 900–1200 kbit/s → solid 360p)
    "4":  Scenario(rate_lim=(700,  1000), delay_lim=(0, 90),  loss_pct_lim=(0.0, 0.3)),
    # State 3: 240p/360p borderline, stalls possible (calibrated: 650 kbit/s borderline)
    "3":  Scenario(rate_lim=(450,  700),  delay_lim=(0, 110), loss_pct_lim=(0.0, 0.4)),
    # State 2: 240p with regular stalls (calibrated: ≤450 kbit/s → 144p/stall)
    "2":  Scenario(rate_lim=(280,  450),  delay_lim=(0, 130), loss_pct_lim=(0.0, 0.5)),
    # State 1: 144p / near-stall (calibrated: ≤450 kbit/s confirmed 144p)
    "1":  Scenario(rate_lim=(150,  280),  delay_lim=(0, 150), loss_pct_lim=(0.0, 0.5)),
}


YOUTUBE_TRANSITIONS = {
    # Target stationary: ~40% in 1080p (7-10), ~40% in 360p (4-6), ~20% stall-risk (1-3)
    # 1080p zone: states 7-10 (≥1800 kbit/s)
    "10": {"10": 0.40, "9": 0.30, "8": 0.20, "7": 0.10},
    "9":  {"10": 0.25, "9": 0.35, "8": 0.20, "7": 0.15, "6": 0.05},
    "8":  {"10": 0.10, "9": 0.20, "8": 0.35, "7": 0.20, "6": 0.15},
    "7":  {"9": 0.15,  "8": 0.20, "7": 0.30, "6": 0.20, "5": 0.15},
    # 360p transition zone: states 4-6 (700–1800 kbit/s)
    "6":  {"7": 0.25,  "6": 0.30, "8": 0.10, "5": 0.20, "4": 0.15},
    "5":  {"6": 0.25,  "5": 0.30, "7": 0.10, "4": 0.20, "3": 0.15},
    "4":  {"5": 0.30,  "4": 0.35, "6": 0.15, "3": 0.15, "2": 0.05},
    # Stall-risk zone: states 1-3 (150–700 kbit/s) — upward bias, cap enforced in netStat.py
    "3":  {"4": 0.35,  "5": 0.25, "3": 0.20, "2": 0.15, "6": 0.05},
    "2":  {"3": 0.35,  "4": 0.30, "2": 0.20, "5": 0.10, "1": 0.05},
    "1":  {"2": 0.45,  "3": 0.30, "1": 0.15, "4": 0.10},
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
    # Calibration note (2026-05-21): TikTok live does NOT do traditional ABR.
    # Streamers broadcast at a fixed source resolution; the player buffers/stalls
    # instead of downgrading quality. Stall probability is the key QoE signal.
    # Resolution seen depends on the individual streamer, not bandwidth state.
    # States 1-3 are stall-risk; transitions push strongly back to mid-band.
    "10": {"10": 0.5, "9": 0.25, "8": 0.15, "7": 0.1},
    "9":  {"10": 0.25, "9": 0.35, "8": 0.2, "7": 0.15, "6": 0.05},
    "8":  {"9": 0.25, "8": 0.35, "7": 0.2, "6": 0.15, "5": 0.05},
    # mid band (very dynamic)
    "7":  {"8": 0.25, "7": 0.3, "6": 0.2, "5": 0.15, "9": 0.1},
    "6":  {"7": 0.25, "6": 0.3, "5": 0.2, "8": 0.15, "4": 0.1},
    "5":  {"6": 0.3, "5": 0.3, "7": 0.2, "4": 0.15, "8": 0.05},
    # stall-risk floor — strong upward bounce
    "4":  {"6": 0.4, "5": 0.25, "7": 0.15, "4": 0.2},
    "3":  {"5": 0.6, "6": 0.3, "4": 0.1},
    "2":  {"5": 0.6, "6": 0.3, "4": 0.1},
    "1":  {"5": 0.7, "6": 0.3},
}



TWITCH_TRANSITIONS = {
    # Calibration ground truth (2026-05-21):
    #   450-650 kbit/s  → 240p
    #   900-1200 kbit/s → 360p
    #   1800-3500       → 480p  (ABR natural ceiling without quality-forcing)
    #   5500            → 720p
    #   8000            → 1080p
    # 1080p zone: states 9-10 (≥5500 kbit/s)
    "10": {"10": 0.55, "9": 0.25, "8": 0.15, "7": 0.05},
    "9":  {"10": 0.25, "9": 0.4, "8": 0.2, "7": 0.1, "6": 0.05},
    # 720p zone: state 8 (2500-5500 kbit/s), 480p ABR floor
    "8":  {"9": 0.25, "8": 0.4, "7": 0.2, "6": 0.1, "5": 0.05},
    "7":  {"8": 0.2, "7": 0.4, "6": 0.2, "5": 0.1, "9": 0.1},
    # 480p zone: states 6-7 (1400-2500 kbit/s)
    "6":  {"7": 0.25, "6": 0.35, "5": 0.2, "8": 0.1, "4": 0.1},
    # 360p zone: states 4-5 (700-1400 kbit/s)
    "5":  {"6": 0.3, "5": 0.35, "4": 0.2, "7": 0.1, "3": 0.05},
    "4":  {"5": 0.3, "4": 0.4, "6": 0.15, "3": 0.1, "2": 0.05},
    # Low/stall zone: states 1-3 (≤700 kbit/s)
    "3":  {"5": 0.35, "4": 0.25, "3": 0.25, "6": 0.1, "2": 0.05},
    "2":  {"4": 0.35, "3": 0.3, "2": 0.25, "5": 0.1},
    "1":  {"3": 0.4, "2": 0.3, "1": 0.2, "4": 0.1},
}



