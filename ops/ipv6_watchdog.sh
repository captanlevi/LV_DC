#!/bin/bash
# IPv6 must stay off during collection (old data is IPv4-only; check_session FAILs ipv6_media). A Wi-Fi reconnect makes
# NetworkManager re-enable it (chaos 2026-10-18 02:20, three sessions lost). Every 30 s: any global IPv6 address ->
# re-apply the sysctl file and the interface key, log it. On the box, so it does not depend on the operator. Between
# sessions only; orchestrate's preflight does the same before every session. Stop it after the collection.
. "${OPS:-$(dirname "$(readlink -f "$0")")}/env.sh"
while true; do
  n=$(ip -6 addr show scope global | grep -c inet6)
  if [ "$n" != 0 ]; then
    sudo -n sysctl -q -p "$LVDC_NOIPV6_SYSCTL"
    sudo -n sysctl -q -w "net.ipv6.conf.$LVDC_IFACE.disable_ipv6=1"
    echo "$(date -Is) IPV6 WATCHDOG: $n global v6 addr(s) found -> sysctl re-applied, now $(ip -6 addr show scope global | grep -c inet6)" >> "$EVENTS"
  fi
  sleep 30
done
