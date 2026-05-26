import argparse
import os
import shutil
from network_core.http.httpExtract.parser import parse_tshark_http
from network_core.http.httpExtract.pdml_parser import parse_pdml_http_from_pcap
from network_core.http.httpIO import save_ft_to_http_units
from network_core.dataModels import HttpUnit, FiveTuple

TSHARK = shutil.which("tshark") or "tshark"


def _merge_into(ft_to_stream_to_units, master_dict):
    for ft, stream_to_units in ft_to_stream_to_units.items():
        if ft not in master_dict and ft.rev_ft() not in master_dict:
            master_dict[ft] = []
        k = ft if ft in master_dict else ft.rev_ft()
        master_dict[k].extend(stream_to_units.values())
    return master_dict


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse tshark http logs")
    parser.add_argument("--path",      type=str, help="Directory containing capture files")
    parser.add_argument("--save_path", type=str, help="Path to save the parsed http units")
    parser.add_argument("--pcap",      type=str, help="Path to merged.pcap (enables streaming mode for H2/H3)")
    parser.add_argument("--ssl_keys",  type=str, help="Path to SSL key log file (required with --pcap)")
    parser.add_argument("--save_pdml", action="store_true", help="Save intermediate PDML files alongside http.json for inspection")
    args = parser.parse_args()

    h1_path = os.path.join(args.path, "h1_raw.json")

    try:
        master_dict: dict[FiveTuple, list[HttpUnit]] = {}

        if os.path.exists(h1_path):
            _merge_into(parse_tshark_http(h1_path, http_version=1), master_dict)

        if args.pcap and args.ssl_keys:
            if args.save_pdml:
                # Save PDML to disk first, then parse — files remain for inspection
                import subprocess
                for ver, fname, y_flag, extra in [
                    (2, "h2_raw.pdml", "http2", []),
                    (3, "h3_raw.pdml", "http3", ["-d", "udp.port==443,quic"]),
                ]:
                    pdml_path = os.path.join(args.path, fname)
                    cmd = [TSHARK, "-r", args.pcap, "-o", f"tls.keylog_file:{args.ssl_keys}"] \
                        + extra + ["-Y", f"http{ver}", "-T", "pdml"]
                    with open(pdml_path, "wb") as out:
                        subprocess.run(cmd, stdout=out, stderr=subprocess.DEVNULL)
                    from network_core.http.httpExtract.pdml_parser import parse_pdml_http
                    _merge_into(parse_pdml_http(pdml_path, http_version=ver), master_dict)
                    print(f"Saved {pdml_path}")
            else:
                # Streaming mode: pipe tshark PDML directly — no intermediate file
                _merge_into(parse_pdml_http_from_pcap(args.pcap, args.ssl_keys, http_version=2, tshark_bin=TSHARK), master_dict)
                _merge_into(parse_pdml_http_from_pcap(args.pcap, args.ssl_keys, http_version=3, tshark_bin=TSHARK), master_dict)
        else:
            # File mode: read pre-generated PDML files
            for ver, fname in [(2, "h2_raw.pdml"), (3, "h3_raw.pdml")]:
                p = os.path.join(args.path, fname)
                if os.path.exists(p):
                    from network_core.http.httpExtract.pdml_parser import parse_pdml_http
                    _merge_into(parse_pdml_http(p, http_version=ver), master_dict)

    except Exception as e:
        import traceback
        print("Error parsing tshark logs")
        traceback.print_exc()

    save_ft_to_http_units(path=args.save_path, data=master_dict)
