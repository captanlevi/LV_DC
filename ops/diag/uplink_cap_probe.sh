#!/bin/bash
# levi 02:48, the uplink-bound positive control's verification (diag; box idle only: no trio, no lv* links). Installs the
# trio's uplink policer (trio_slots.uplink_cap_cmds, the same command) on a bare ingress qdisc, downloads one object
# with curl, prints {cap_kbit, goodput_kbit, ratio, police counters} as one json line, and removes the qdisc. The
# management bypass is installed first, as in the trio, so ssh / tailscale are never policed.
# Usage: uplink_cap_probe.sh <cap kbit> [url]   (default url: a 100 MB object; LVDC_PROBE_URL overrides)
set -euo pipefail
. "$(dirname "$(readlink -f "$0")")/../env.sh"
CAP=${1:?cap kbit}; URL=${2:-${LVDC_PROBE_URL:-https://speed.cloudflare.com/__down?bytes=100000000}}
I=$LVDC_IFACE; NS=$LVDC_ROOT/networkSim; PY=${LVDC_PY:-$LVDC_ROOT/.venv/bin/python}
[ -z "$(ip -o link | grep -oE ' lv[io]_[^:@]+')" ] || { echo "lv* links present: a trio is up; not probing" >&2; exit 2; }
[ -z "$(tc qdisc show dev "$I" ingress)" ] || { echo "an ingress qdisc is already on $I; not probing" >&2; exit 2; }
CMDS=$(cd "$NS" && "$PY" -c "import sys, trio_slots as T; print('\n'.join(T._mgmt_filters(sys.argv[1], 'ffff:', 'action ok') + T.uplink_cap_cmds(sys.argv[1], int(sys.argv[2]))))" "$I" "$CAP")
cleanup() { sudo -n tc qdisc del dev "$I" ingress 2>/dev/null || true; }
trap cleanup EXIT
sudo -n tc qdisc add dev "$I" handle ffff: ingress
while IFS= read -r c; do sudo -n $c; done <<< "$CMDS"
BPS=$(curl -s -o /dev/null -w '%{speed_download}' --max-time 300 "$URL")
POL=$(tc -s filter show dev "$I" parent ffff: prio 6 | tr '\n' ' ' | grep -oE 'Sent [0-9]+ bytes [0-9]+ pkt \(dropped [0-9]+, overlimits [0-9]+' | head -1)
python3 -c "import json, sys; c, b = int(sys.argv[1]), float(sys.argv[2]); g = b * 8 / 1000; print(json.dumps({'cap_kbit': c, 'goodput_kbit': round(g, 1), 'ratio': round(g / c, 4), 'police': sys.argv[3], 'url': sys.argv[4]}))" "$CAP" "$BPS" "$POL" "$URL"
