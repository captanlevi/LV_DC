import math
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

    # State 11 (prereg d32a48ce, label diversity item 4; levi 18:14): twitch 1080p60 headroom, used only by TW-H. Its
    # rate_max reaches >= 1.25 x the highest 1080p variant BANDWIDTH in t58 twitch HLS masters (9147536 bps, tw_100078,
    # shagun's read sha 3c63b074113b: 48 sessions / 106 masters / 50 rows) = 11435 kbit; delay / loss as state 10
    "11": Scenario(rate_lim=(9000, 12000), delay_lim=(0, 20), loss_pct_lim=(0.0, 0.05)),
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


def get_higher_state(current_state : str):
    if current_state not in SCENARIOS:
        raise ValueError(f"state {current_state} not found in SCENARIOS")
    current_state_int = int(current_state)
    if current_state_int == len(SCENARIOS):
        # This is the highest so we just return this
        return current_state
    return str(current_state_int + 1)
    

def get_lower_state(current_state : str):
    if current_state not in SCENARIOS:
        raise ValueError(f"state {current_state} not found in SCENARIOS")
    current_state_int = int(current_state)
    if current_state_int == 1:
        # This is the lowest so we just return this
        return current_state
    return str(current_state_int - 1)




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


# t38 (LVQoE prereg collection_v2 Addendum A, rev 2; json t38_yt_lowtier_matrix_2026-10-16.json sha d0171456): yt
# low-tier targeting by long dwell in states 2-4. SCENARIOS rate ranges are unchanged (state semantics match the old
# data). Matrix A is the batch default, B the pre-declared fallback (switch rule in the prereg). Rows: from-state.
YOUTUBE_TRANSITIONS_A = {
    "1":  {"1": 0.60, "2": 0.30, "3": 0.10},
    "2":  {"1": 0.15, "2": 0.65, "3": 0.15, "4": 0.05},
    "3":  {"1": 0.05, "2": 0.15, "3": 0.65, "4": 0.15},
    "4":  {"2": 0.10, "3": 0.20, "4": 0.55, "5": 0.10, "6": 0.05},
    "5":  {"3": 0.10, "4": 0.25, "5": 0.30, "6": 0.25, "7": 0.10},
    "6":  {"4": 0.10, "5": 0.25, "6": 0.30, "7": 0.25, "8": 0.10},
    "7":  {"5": 0.10, "6": 0.25, "7": 0.30, "8": 0.25, "9": 0.10},
    "8":  {"6": 0.10, "7": 0.25, "8": 0.30, "9": 0.25, "10": 0.10},
    "9":  {"7": 0.10, "8": 0.25, "9": 0.40, "10": 0.25},
    "10": {"8": 0.15, "9": 0.40, "10": 0.45},
}
YOUTUBE_TRANSITIONS_B = {
    "1":  {"1": 0.60, "2": 0.30, "3": 0.10},
    "2":  {"1": 0.10, "2": 0.70, "3": 0.15, "4": 0.05},
    "3":  {"1": 0.05, "2": 0.15, "3": 0.70, "4": 0.10},
    "4":  {"2": 0.10, "3": 0.20, "4": 0.65, "5": 0.05},
    "5":  {"3": 0.10, "4": 0.25, "5": 0.35, "6": 0.20, "7": 0.10},
    "6":  {"4": 0.10, "5": 0.25, "6": 0.30, "7": 0.25, "8": 0.10},
    "7":  {"5": 0.10, "6": 0.25, "7": 0.30, "8": 0.25, "9": 0.10},
    "8":  {"6": 0.10, "7": 0.25, "8": 0.30, "9": 0.25, "10": 0.10},
    "9":  {"7": 0.10, "8": 0.25, "9": 0.40, "10": 0.25},
    "10": {"8": 0.15, "9": 0.40, "10": 0.45},
}
# LVQoE prereg Addendum Q am. 15 (6) (2026-10-19, levi at LVQoE 57cd519e; peter's proposal, design inputs only): yt-H,
# a top-heavy yt matrix for the Q_1080p target (stationary mass in 9-10 about 0.52). Literal from the amendment's table;
# cap 2; stationary start, band-restricted for a Q_1080p target (am. 15 (5)); drawn only for that target
# (coverage_report.MATRIX_TARGETS). The deep drops 9 -> 4 and 10 -> 3 are part of the table (levi).
YOUTUBE_TRANSITIONS_H = {
    "1":  {"1": 0.30, "2": 0.30, "3": 0.25, "4": 0.15},
    "2":  {"1": 0.10, "2": 0.30, "3": 0.30, "4": 0.20, "5": 0.10},
    "3":  {"2": 0.10, "3": 0.30, "4": 0.30, "5": 0.20, "6": 0.10},
    "4":  {"3": 0.10, "4": 0.30, "5": 0.30, "6": 0.20, "7": 0.10},
    "5":  {"4": 0.10, "5": 0.30, "6": 0.30, "7": 0.20, "8": 0.10},
    "6":  {"5": 0.10, "6": 0.30, "7": 0.30, "8": 0.20, "9": 0.10},
    "7":  {"5": 0.05, "6": 0.10, "7": 0.30, "8": 0.30, "9": 0.20, "10": 0.05},
    "8":  {"5": 0.05, "6": 0.10, "7": 0.15, "8": 0.30, "9": 0.25, "10": 0.15},
    "9":  {"4": 0.05, "7": 0.05, "8": 0.15, "9": 0.45, "10": 0.30},
    "10": {"3": 0.05, "8": 0.10, "9": 0.25, "10": 0.60},
}
# matrix id -> (transitions, low-state cap, initial-state rule). "current" = the pre-t38 yt config (old data).
YOUTUBE_MATRICES = {
    "current": (YOUTUBE_TRANSITIONS, 2, "linear"),
    "A": (YOUTUBE_TRANSITIONS_A, 6, "stationary"),
    "B": (YOUTUBE_TRANSITIONS_B, 6, "stationary"),
    "H": (YOUTUBE_TRANSITIONS_H, 2, "stationary"),
}


def stationary_distribution(transitions: dict[str, dict[str, float]]) -> dict[str, float]:
    """Power iteration: pi such that pi P = pi."""
    states = sorted(transitions.keys(), key=int)
    pi = {s: 1.0 / len(states) for s in states}
    for _ in range(6000):
        new = {s: 0.0 for s in states}
        for s in states:
            for t, prob in transitions[s].items():
                new[t] += pi[s] * prob
        pi = new
    return pi


def initial_state_stationary(transition_dict: dict[str, dict[str, float]]) -> str:
    """t38: the yt start state is drawn from the active matrix's stationary distribution."""
    pi = stationary_distribution(transition_dict)
    states = sorted(pi, key=int)
    return random.choices(states, weights=[pi[s] for s in states], k=1)[0]


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





# LVQoE prereg addendum M (2026-10-17, levi; applied at the next checkpoint): tt and twitch matrices with a floor of
# low-tier time. Literal from the addendum's table (rows = from-state); SCENARIOS rate ranges unchanged. Stationary
# start, orchestrate target = the stationary distribution, 23 episodes of 60 s; caps: tt 4, twitch 2.
TIKTOK_TRANSITIONS_M = {
    "1":  {"1": 0.45, "2": 0.30, "3": 0.25},
    "2":  {"1": 0.25, "2": 0.40, "3": 0.25, "4": 0.10},
    "3":  {"1": 0.10, "2": 0.10, "3": 0.50, "4": 0.20, "5": 0.10},
    "4":  {"2": 0.05, "3": 0.15, "4": 0.55, "5": 0.20, "6": 0.05},
    "5":  {"3": 0.05, "4": 0.20, "5": 0.55, "6": 0.15, "7": 0.05},
    "6":  {"4": 0.10, "5": 0.20, "6": 0.40, "7": 0.20, "8": 0.10},
    "7":  {"5": 0.10, "6": 0.20, "7": 0.40, "8": 0.20, "9": 0.10},
    "8":  {"6": 0.10, "7": 0.20, "8": 0.40, "9": 0.20, "10": 0.10},
    "9":  {"7": 0.10, "8": 0.20, "9": 0.45, "10": 0.25},
    "10": {"8": 0.15, "9": 0.40, "10": 0.45},
}
TWITCH_TRANSITIONS_M = {
    "1":  {"1": 0.45, "2": 0.40, "3": 0.15},
    "2":  {"1": 0.20, "2": 0.50, "3": 0.20, "4": 0.10},
    "3":  {"1": 0.10, "2": 0.20, "3": 0.40, "4": 0.20, "5": 0.10},
    "4":  {"2": 0.05, "3": 0.15, "4": 0.45, "5": 0.30, "6": 0.05},
    "5":  {"3": 0.05, "4": 0.30, "5": 0.45, "6": 0.15, "7": 0.05},
    "6":  {"4": 0.10, "5": 0.20, "6": 0.40, "7": 0.20, "8": 0.10},
    "7":  {"5": 0.10, "6": 0.20, "7": 0.40, "8": 0.20, "9": 0.10},
    "8":  {"6": 0.10, "7": 0.20, "8": 0.40, "9": 0.20, "10": 0.10},
    "9":  {"7": 0.05, "8": 0.15, "9": 0.50, "10": 0.30},
    "10": {"8": 0.10, "9": 0.45, "10": 0.45},
}
# LVQoE prereg Addendum Q amendment 3 (2026-10-18, levi; an addendum M amendment, defined before any steering yield):
# TW-L, a twitch low-state matrix for stall and low-tier coverage (not a model of real networks). Literal from the
# amendment's table; cap 2 as TW-M; stationary start (states 1-5); rows 7-10 return to 6.
TWITCH_TRANSITIONS_L = {
    "1":  {"1": 0.50, "2": 0.35, "3": 0.15},
    "2":  {"1": 0.30, "2": 0.45, "3": 0.20, "4": 0.05},
    "3":  {"1": 0.15, "2": 0.30, "3": 0.40, "4": 0.15},
    "4":  {"2": 0.20, "3": 0.35, "4": 0.35, "5": 0.10},
    "5":  {"3": 0.25, "4": 0.40, "5": 0.30, "6": 0.05},
    "6":  {"4": 0.40, "5": 0.40, "6": 0.20},
    "7":  {"6": 1.00},
    "8":  {"6": 1.00},
    "9":  {"6": 1.00},
    "10": {"6": 1.00},
}
# prereg d32a48ce item 4 (levi 18:14): TW-H, the twitch matrix whose band reaches 1080p. Rows 1-8 are TW-M's (the base
# matrix's shape); rows 9-10 move mass up into state 11 and 11 returns to 9-10, so the top band is reachable and left;
# state 11's stationary mass clears coverage_report's qualifying floor for twitch Q_1080p (band_map min_mass).
# Cap 2 and the stationary start as TW-M.
TWITCH_TRANSITIONS_H = {
    **{s: dict(r) for s, r in TWITCH_TRANSITIONS_M.items() if int(s) <= 8},
    "9":  {"7": 0.05, "8": 0.15, "9": 0.40, "10": 0.25, "11": 0.15},
    "10": {"8": 0.10, "9": 0.25, "10": 0.35, "11": 0.30},
    "11": {"9": 0.10, "10": 0.35, "11": 0.55},
}
TIKTOK_MATRICES = {
    "current": (TIKTOK_TRANSITIONS, 2, "linear"),
    "TT-M": (TIKTOK_TRANSITIONS_M, 4, "stationary"),
}
TWITCH_MATRICES = {
    "current": (TWITCH_TRANSITIONS, 2, "linear"),
    "TW-M": (TWITCH_TRANSITIONS_M, 2, "stationary"),
    "TW-L": (TWITCH_TRANSITIONS_L, 2, "stationary"),
    "TW-H": (TWITCH_TRANSITIONS_H, 2, "stationary"),
}
# per platform: matrix id -> (transitions, low-state cap, initial-state rule)
PLATFORM_MATRICES = {"youtube": YOUTUBE_MATRICES, "tiktok": TIKTOK_MATRICES, "twitch": TWITCH_MATRICES}


# LVQoE prereg Addendum Q am. 17 (2026-10-20, levi at LVQoE 67bc6b3e; low cap corrected for TT-M after it; parameters
# at LVQoE 3c409b93, supervisor for rushi): two schedule families on each platform's default stationary matrix, session
# time held. "@slow": 3 episodes of the session's time / 3 (yt 420 s, twitch / tt 460 s), the re-draw at base
# (12-30 s). "@fast": 12 s episodes (episodes = round(base episodes x BASE_EPISODE_S / 12)), re-draw at D/5-D/2. Both
# draw the next state without self-loops (renormalised). Every family's low-state cap is in seconds: the base matrix's
# cap x BASE_EPISODE_S (TW-M 120, A 360, TT-M 240); a low episode is cut at the cap and the rest of it held at
# netStat._MIN_RECOVERY_STATE. A family id is "<base>@<schedule>"; its tuple is the base's (transitions, cap in base
# episodes, start rule): the start rules are the base's (am. 15 (5) included).
BASE_EPISODE_S = 60
# J (levi 15:25): every auto session holds the fixed join state this long after playing before its schedule starts
# (exp._join_phase); orchestrate's window allowance includes it (levi 15:55)
JOIN_SETTLE_S = 60.0
SCHEDULES = {
    "slow": {"episodes": 3, "self_loops": False, "redraw_s": (12.0, 30.0)},
    "fast": {"episode_s": 12, "self_loops": False, "redraw_s": (2.4, 6.0)},
}
FAMILY_BASE = {"youtube": "A", "tiktok": "TT-M", "twitch": "TW-M"}
for _p, _table in PLATFORM_MATRICES.items():
    for _s in SCHEDULES:
        _table[f"{FAMILY_BASE[_p]}@{_s}"] = _table[FAMILY_BASE[_p]]


def schedule_of(matrix_id: str | None, cap_episodes: int | None = None, base_episodes: float | None = None,
                episode_s: float | None = None) -> dict:
    """A matrix id's schedule: "base" (BASE_EPISODE_S episodes, self-loops, the re-draw at D/5-D/2, the cap in episodes)
    or its family's (am. 17). cap_episodes: the matrix's cap in base episodes (its low_cap_s = cap x BASE_EPISODE_S);
    base_episodes: the platform's base episodes per session (EPISODES_V2), or the drawn session's minutes, which @slow's
    episode length divides; episode_s: @fast's drawn episode length (restart §2b), its re-draw at D/5-D/2."""
    base, _, fam = (matrix_id or "").partition("@")
    low_cap_s = cap_episodes * BASE_EPISODE_S if cap_episodes is not None else None
    if not fam:
        if episode_s is not None:
            raise ValueError(f"{matrix_id!r}: a drawn episode length is @fast's only (restart §2b)")
        return {"schedule": "base", "base_matrix": base or None, "episode_s": BASE_EPISODE_S, "self_loops": True,
                "redraw_s": [BASE_EPISODE_S / 5, BASE_EPISODE_S / 2], "low_cap_s": low_cap_s}
    if fam not in SCHEDULES:
        raise ValueError(f"{matrix_id!r}: no schedule {fam!r} (one of {sorted(SCHEDULES)})")
    s = SCHEDULES[fam]
    if "episodes" in s and base_episodes is None:
        raise ValueError(f"{matrix_id!r}: its episode length is the session's time / {s['episodes']}: base_episodes needed")
    if episode_s is not None:
        if "episode_s" not in s:
            raise ValueError(f"{matrix_id!r}: a drawn episode length is @fast's only (restart §2b)")
        return {"schedule": fam, "base_matrix": base, "episode_s": episode_s, "self_loops": s["self_loops"],
                "redraw_s": [episode_s / 5, episode_s / 2], "low_cap_s": low_cap_s}
    ep_s = s["episode_s"] if "episode_s" in s else base_episodes * BASE_EPISODE_S / s["episodes"]
    return {"schedule": fam, "base_matrix": base, "episode_s": ep_s, "self_loops": s["self_loops"],
            "redraw_s": list(s["redraw_s"]), "low_cap_s": low_cap_s}


def session_episodes(base_episodes: float, matrix_id: str | None, episode_s: float | None = None) -> int:
    """am. 17: the session time held: a family's episodes = round(base episodes x BASE_EPISODE_S / its episode_s).
    base_episodes may be a drawn session's minutes (restart §4; BASE_EPISODE_S is a minute); episode_s: @fast's drawn
    episode length (restart §2b)."""
    sc = schedule_of(matrix_id, base_episodes=base_episodes, episode_s=episode_s)
    return max(1, int(round(base_episodes * BASE_EPISODE_S / sc["episode_s"])))


# LVQoE restart_2026-10-21_prereg_levi.md §4 (rushi GO 2026-10-20): each trio session's length is drawn, U(10, 40) min,
# seeded, independent of everything; the episodes scale with it (base: round(minutes) episodes of 60 s; @slow: 3
# episodes of the session / 3; @fast: round(minutes x 5) episodes of 12 s). Single mode and a held state keep the base
# episodes (not of record).
SESSION_MINUTES = (10.0, 40.0)
SESSION_LEN_SEED = 0
SESSION_LEN_RULE = ("restart prereg §4: minutes = random.Random('<seed>:<key>').uniform(10, 40), key = <trio run>:<slot "
                    "tag>:<the slot's session index in the run>; episodes = shaping.session_episodes(minutes, matrix)")


def draw_session_minutes(key: str, seed: int = SESSION_LEN_SEED) -> dict:
    """A session's drawn length (restart §4), with its seed and key (meta.json shaper.session_length)."""
    m = random.Random(f"{seed}:{key}").uniform(*SESSION_MINUTES)
    return {"rule": SESSION_LEN_RULE, "seed": seed, "key": key, "range_min": list(SESSION_MINUTES), "minutes": round(m, 4)}


# restart prereg §2b (levi on brad's design check 00231): @fast's episode length is drawn per session, log-U(12, 48) s,
# seeded, recorded (it fills the change-frequency gap between base and @fast); its re-draw stays D/5-D/2. @slow keeps 3
# episodes, so its dwell scales with the drawn length.
FAST_EPISODE_S = (12.0, 48.0)
FAST_EPISODE_RULE = ("restart prereg §2b: @fast episode_s = exp(random.Random('<seed>:<key>:fast_episode_s').uniform("
                     "ln 12, ln 48)), the session-length key; re-draw at D/5-D/2; episodes = round(minutes x 60 / D)")


def draw_fast_episode_s(key: str, seed: int = SESSION_LEN_SEED) -> dict:
    """A session's @fast episode length (restart §2b), with its seed and key (meta.json shaper.session_length)."""
    lo, hi = FAST_EPISODE_S
    v = math.exp(random.Random(f"{seed}:{key}:fast_episode_s").uniform(math.log(lo), math.log(hi)))
    return {"rule": FAST_EPISODE_RULE, "seed": seed, "key": key, "range_s": list(FAST_EPISODE_S), "episode_s": round(v, 3)}


def next_state_no_self(curr: str, transition_dict: dict[str, dict[str, float]], rng=random) -> str:
    """@slow: the next state drawn from curr's row without its self-loop, renormalised (random.choices normalises)."""
    row = {s: p for s, p in transition_dict[curr].items() if s != curr and p > 0}
    if not row:
        return curr
    return rng.choices(list(row), weights=list(row.values()), k=1)[0]


def state_ids() -> list[str]:
    """Every shaper state id, ascending (SCENARIOS' keys): exp.py's --initial-state / --hold-state choices (w9, levi
    02:41: a hard-coded 1..10 refused TW-H's state 11 at argparse, so the twitch slot never ran)."""
    return sorted(SCENARIOS, key=int)


def start_state_refusal(state: str | None, transition: dict, matrix_id: str | None) -> str | None:
    """exp.py's refusal of a forced start state outside the session's matrix (its first transition would read a row the
    matrix does not have); None when there is no forced state or it is one of the matrix's states."""
    if state is None or state in transition:
        return None
    return f"--initial-state {state!r}: not a state of matrix {matrix_id or 'default'} ({sorted(transition, key=int)})"


def hold_schedule(state: str) -> dict:
    """exp.py --hold-state (t56 decode_stress, diag only): one state for the whole session. The transition holds it, and
    the low-state cap is lifted (levi gate 49d: with the platform's cap kept, a held state <= 3 was forced to state 4
    after the cap; yt 1 -> 4 at t+381): max_consecutive_low infinite, low_cap None (meta.json low_state_cap reads None)."""
    if state not in SCENARIOS:
        raise ValueError(f"--hold-state {state!r}: no such shaper state")
    return {"transition": {state: {state: 1.0}}, "matrix_id": f"hold:{state}", "low_cap": None,
            "max_consecutive_low": math.inf}
