import argparse
import os
import shutil
from network_core.http.httpExtract.parser import parse_tshark_http
from network_core.http.httpExtract.pdml_parser import parse_pdml_http_from_pcap
from network_core.http.httpIO import save_ft_to_http_units
from network_core.dataModels import HttpUnit, FiveTuple

TSHARK = shutil.which("tshark") or "tshark"
ERROR_MARKER = "http_v2_error.json"     # levi 06:29: written instead of http_v2.json when the extraction fails


def _merge_into(ft_to_stream_to_units, master_dict):
    for ft, stream_to_units in ft_to_stream_to_units.items():
        if ft not in master_dict and ft.rev_ft() not in master_dict:
            master_dict[ft] = []
        k = ft if ft in master_dict else ft.rev_ft()
        master_dict[k].extend(stream_to_units.values())
    return master_dict



def parse_pdml_zst(path: str, http_version: int) -> dict:
    """A zstd-compressed PDML (h2_raw.pdml.zst / h3_raw.pdml.zst) parsed as the plain file is: zstd -dc's stdout is
    the parser's byte stream (network_core's parse_pdml_http reads a binary stream the same way)."""
    import subprocess
    from network_core.http.httpExtract.pdml_parser import _parse_pdml_stream
    proc = subprocess.Popen(["zstd", "-dc", path], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        return _parse_pdml_stream(proc.stdout, http_version)
    finally:
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(f"zstd -dc {path} failed (rc {proc.returncode})")

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
            # File mode: read pre-generated PDML files; a .pdml.zst (compressed in place after the derived outputs exist,
            # supervisor 2026-10-19; the compressor is on the gate2-* line, not v3) is read through `zstd -dc`, the same bytes
            for ver, fname in [(2, "h2_raw.pdml"), (3, "h3_raw.pdml")]:
                p = os.path.join(args.path, fname)
                if os.path.exists(p):
                    from network_core.http.httpExtract.pdml_parser import parse_pdml_http
                    _merge_into(parse_pdml_http(p, http_version=ver), master_dict)
                elif os.path.exists(p + ".zst"):
                    _merge_into(parse_pdml_zst(p + ".zst", ver), master_dict)

    except Exception as e:
        # levi 06:29 (WC2 tw_100001: an exception here wrote "{}" with rc 0 and nothing flagged it): fail closed. No
        # http_v2.json is written; the error marker (ERROR_MARKER beside it) holds the traceback; rc 3 fails the make
        # target, so exp.py logs it and the session goes on (http_v2 is re-derivable from the kept pcap + keylog)
        import json
        import time
        import traceback
        tb = traceback.format_exc()
        print("Error parsing tshark logs: no http_v2.json written")
        print(tb)
        marker = os.path.join(os.path.dirname(os.path.abspath(args.save_path)), ERROR_MARKER)
        with open(marker, "w") as fh:
            json.dump({"error": repr(e)[:500], "traceback": tb[-4000:], "ts": time.time(),
                       "save_path": args.save_path, "pcap": args.pcap}, fh, indent=1)
        raise SystemExit(3)

    save_ft_to_http_units(path=args.save_path, data=master_dict)
