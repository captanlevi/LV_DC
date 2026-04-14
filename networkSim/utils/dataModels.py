from dataclasses import dataclass


@dataclass
class NetLabel:
    timestamp: float
    speed: int
    state: str


@dataclass
class NetStat:
    rate: int  # kbit
    duration: float
    delay_ms: int
    loss_pct: float
    state: str
