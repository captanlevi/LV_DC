import subprocess
import time
from dataclasses import dataclass, asdict
import time
import json

import subprocess
import os
import signal
import random


@dataclass
class NetLabel:
    timestamp: float
    speed: int


def run_(cmd):
    print(f"> {cmd}")
    subprocess.run(cmd, shell=True)


def run(args):
    proc = subprocess.Popen(args, preexec_fn=os.setsid)  # NEW process group
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n🛑 Ctrl+C received — killing subprocess group")
        os.killpg(proc.pid, signal.SIGTERM)
        raise


def initialize_network() -> bool:
    try:
        run(["make", "enable"])
        return True
    except Exception as e:
        print(e)
        return False


def finalize_network() -> bool:
    try:
        run(["make", "disable"])
        return True
    except Exception as e:
        print(e)
        return False


def initialize_pcap() -> bool:
    try:
        run(["make", "start-capture"])
        return True
    except Exception as e:
        print(e)
        return False


def finalize_pcap() -> bool:
    try:
        run(["make", "stop-capture"])
        run(["make", "merge-capture"])
        return True
    except Exception as e:
        print(e)
        return False


def change_perm() -> bool:
    try:
        run(["make", "change_perm"])
        return True
    except Exception as e:
        print(e)
        return False


def ossilate(
    stable_time_in_seconds: int,
    net_labels: list[NetLabel],
    rates=[5000, 1000, 500, 200, 100],
):

    index = 0
    while True:
        index = index % len(rates)
        rate = rates[index]
        run(["make", "slow", f"BANDWIDTH={rate}kbit"])
        ts = time.time()
        net_labels.append(NetLabel(timestamp=ts, speed=rate))
        time.sleep(stable_time_in_seconds)
        index += 1


def oscillate_balanced(net_labels, episode_length=180):
    """
    Generate balanced network scenarios with minimal interruptions.
    """
    scenarios = ["high", "medium", "low", "stall", "oscillation"]
    last_rate = None  # Track last applied bandwidth

    while True:
        scenario = random.choice(scenarios)
        print(f"\n🎬 Scenario: {scenario}")

        start_time = time.time()

        if scenario == "high":
            rates = [4000, 5000]
            duration_fn = lambda: random.expovariate(1 / 40)

        elif scenario == "medium":
            rates = [800, 1000, 1500]
            duration_fn = lambda: random.expovariate(1 / 30)

        elif scenario == "low":
            rates = [100, 200, 300]
            duration_fn = lambda: random.expovariate(1 / 25)

        elif scenario == "stall":
            # occasional drop to very low bandwidth
            def stall_pattern():
                if random.random() < 0.5:
                    return random.choice([50, 100])  # stall trigger
                return random.choice([2000, 4000])

            duration_fn = lambda: random.uniform(5, 20)

        elif scenario == "oscillation":
            rates = [100, 500, 1000, 5000]
            duration_fn = lambda: random.uniform(5, 15)

        # ---- run scenario ----
        while time.time() - start_time < episode_length:

            # choose rate based on scenario
            if scenario == "stall":
                rate = stall_pattern()
            else:
                rate = random.choice(rates)

            # Only apply network shaping if rate changed
            if rate != last_rate:
                run(["make", "slow", f"BANDWIDTH={rate}kbit"])
                last_rate = rate

            net_labels.append(NetLabel(timestamp=time.time(), speed=rate))

            time.sleep(duration_fn())


def save_json(path: str, data: list[NetLabel]):
    save_data = [asdict(obj) for obj in data]
    with open(path, "w") as f:
        for item in save_data:
            json.dump(item, f)
            f.write("\n")


if __name__ == "__main__":

    is_capture: bool = True

    net_labels = []
    if initialize_network() == False:
        raise ValueError("Cannot initialize the network for collection")

    # PCAP is always done after the network initialization
    if is_capture and initialize_pcap() == False:
        raise ValueError("Cannot initialize pcap capture")

    try:
        # ossilate(stable_time_in_seconds=30, net_labels=net_labels)
        oscillate_balanced(net_labels=net_labels, episode_length=120)

    finally:
        # Need to stop capture before tearing down the network
        if is_capture and finalize_pcap() == False:
            raise ValueError("Cannot finalize pcap capture")
        if finalize_network() == False:
            raise ValueError("Could not finish removing the network setup")

        save_json(path="../current_data/net_labels.txt", data=net_labels)
        change_perm()
