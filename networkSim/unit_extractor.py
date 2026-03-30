import argparse
from network_core.http.httpExtract.parser import parse_tshark_http
from network_core.http.httpIO import load_ft_to_http_units, save_ft_to_http_units
import os
from network_core.dataModels import HttpUnit
from network_core.dataModels import FiveTuple


def get_http_units_from_tshark_logs(
    path: str, http_version: int, master_dict: dict[FiveTuple, list[HttpUnit]]
) -> dict[FiveTuple, list[HttpUnit]]:
    ft_to_stream_to_units = parse_tshark_http(path=path, http_version=http_version)
    for ft, stream_to_units in ft_to_stream_to_units.items():
        if ft not in master_dict and ft.rev_ft() not in master_dict:
            master_dict[ft] = []  # this is the stream dict keyed by stream_id

        if ft in master_dict:
            master_dict[ft].extend(stream_to_units.values())
        elif ft.rev_ft() in master_dict:
            master_dict[ft.rev_ft()].extend(stream_to_units.values())

    return master_dict


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse tshark http logs")
    parser.add_argument(
        "--path", type=str, help="Path to the tshark json logs to be parsed"
    )

    parser.add_argument(
        "--save_path", type=str, help="Path to save the parsed http units"
    )
    # In this path directory I will look for h1_raw.json, h2_raw.json, and h3_raw.json

    args = parser.parse_args()

    h1_path = os.path.join(args.path, "h1_raw.json")
    h2_path = os.path.join(args.path, "h2_raw.json")
    h3_path = os.path.join(args.path, "h3_raw.json")

    units = []

    try:
        master_dict: dict[FiveTuple, list[HttpUnit]] = dict()
        if os.path.exists(h1_path):
            units.extend(
                get_http_units_from_tshark_logs(
                    path=h1_path, http_version=1, master_dict=master_dict
                )
            )
        if os.path.exists(h2_path):
            units.extend(
                get_http_units_from_tshark_logs(
                    path=h2_path, http_version=2, master_dict=master_dict
                )
            )
        if os.path.exists(h3_path):
            units.extend(
                get_http_units_from_tshark_logs(
                    path=h3_path, http_version=3, master_dict=master_dict
                )
            )
    except Exception as e:
        print("Error parsing tshark logs")
        print(e)

    save_path = args.save_path

    save_ft_to_http_units(path=save_path, data=master_dict)
