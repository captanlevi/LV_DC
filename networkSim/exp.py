import subprocess
import time
from dataclasses import asdict
import time
import json

import subprocess
import os
import signal

from utils.netStat import net_episode_generator
from utils.dataModels import NetLabel


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


def open_chrome_with_ssl_key_log() -> bool:
    try:
        run(["make", "start_chrome"])
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


def make_http_logs() -> bool:
    try:
        run(["make", "make_http_logs"])
        return True
    except Exception as e:
        print(e)
        return False


def process_http_logs() -> bool:
    try:
        run(["make", "process_http_logs"])
        return True
    except Exception as e:
        print(e)
        return False


def remove_ssl_and_http_logs() -> bool:
    try:
        run(["make", "remove_ssl_keys"])
        run(["make", "remove_http_logs"])
        return True
    except Exception as e:
        print(e)
        return False


def ossilate(
    episode_time_in_seconds: int,
    net_labels: list[NetLabel],
):

    for net_stat in net_episode_generator(episode_length=episode_time_in_seconds):
        print(f"Currently shaping {net_stat}")
        # run(["make", "slow", f"BANDWIDTH={net_stat.delay_ms}, kbit DELAY={net_stat.delay_ms}ms, PLR={net_stat.loss_pct}%"])
        run(
            [
                "make",
                "slow",
                f"BANDWIDTH={net_stat.rate}kbit",
                f"DELAY={net_stat.delay_ms}ms",
                f"PLR={net_stat.loss_pct}%",
            ]
        )
        ts = time.time()
        net_labels.append(NetLabel(timestamp=ts, speed=net_stat.rate))
        time.sleep(net_stat.duration)


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

    if open_chrome_with_ssl_key_log() == False:
        raise ValueError("Cannot open chrome with SSL key log enabled")
    try:
        # ossilate(stable_time_in_seconds=30, net_labels=net_labels)
        ossilate(episode_time_in_seconds=60, net_labels=net_labels)

    finally:
        # Need to stop capture before tearing down the network
        if is_capture and finalize_pcap() == False:
            raise ValueError("Cannot finalize pcap capture")
        if finalize_network() == False:
            raise ValueError("Could not finish removing the network setup")

        if make_http_logs() == False:
            print("Could not make http logs from the pcap")
        # Here run python script to parse the http logs and save the relevant data structure for training the model
        if process_http_logs() == False:
            print("Could not process http logs")

        if remove_ssl_and_http_logs() == False:
            print("Could not remove ssl and http logs")

        save_json(path="../current_data/net_labels.txt", data=net_labels)
        change_perm()
