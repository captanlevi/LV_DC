#!/bin/bash
# usage: sudo -n bash ops/diag/ifb_segmentation_check.sh   (then compare <case>_ifb.pcap with <case>_nic.pcap per case;
# pass = with the IFB flags off, both taps identical in counts / sizes / order). The standing check that the capture IFB
# shows wire packets for Chrome-like upstream (a multi-MSS TCP write, a UDP GSO send), which a bulk download cannot.
# P3 ops test (levi 00120), isolated in throwaway network namespaces (nothing in the root namespace, wlp0s20f3 or the
# live trio tree is touched): veth vA (the "NIC", features as chaos's wlp0s20f3: tso / gso / tx-udp-segmentation off)
# with a clsact egress mirror to IFB ifbT, as trio_slots mirrors egress to lvo_<tag>. Per case: a fresh TCP connection
# writing 4000 B in one send, and one UDP GSO send (UDP_SEGMENT 1200, 4800 B: QUIC's GSO), captured at once on ifbT
# and on vA -Q out (the tap after software segmentation, as tcpdump -Q out on wlp0s20f3).
set -u
OUT=${OUT:-/tmp/ifb_segmentation_check}; mkdir -p $OUT; rm -f $OUT/*
cleanup(){ ip netns del lvtA 2>/dev/null; ip netns del lvtB 2>/dev/null; }
trap cleanup EXIT
cleanup
ip netns add lvtA; ip netns add lvtB
ip link add vA netns lvtA type veth peer name vB netns lvtB
A="ip netns exec lvtA"; Bn="ip netns exec lvtB"
$A ip addr add 10.77.0.1/24 dev vA; $A ip link set vA up; $A ip link set lo up
$Bn ip addr add 10.77.0.2/24 dev vB; $Bn ip link set vB up; $Bn ip link set lo up
for f in tso gso tx-udp-segmentation gro; do $A ethtool -K vA $f off 2>/dev/null; done
$A ip link add ifbT type ifb; $A ip link set ifbT up
$A tc qdisc add dev vA clsact
$A tc filter add dev vA egress matchall action mirred egress mirror dev ifbT
# servers
$Bn python3 -c "
import socket, threading
t = socket.socket(); t.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); t.bind(('10.77.0.2', 9000)); t.listen(5)
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.bind(('10.77.0.2', 9001))
def tcp():
    while True:
        c, _ = t.accept()
        while c.recv(65536): pass
        c.close()
def udp():
    while True: u.recv(65536)
threading.Thread(target=tcp, daemon=True).start(); threading.Thread(target=udp, daemon=True).start()
import time; time.sleep(60)
" & SRV=$!
sleep 0.5
run_case(){
  local name=$1
  $A tcpdump -i ifbT -nn -U -w $OUT/${name}_ifb.pcap 2>$OUT/${name}_ifb.err & T1=$!
  $A tcpdump -i vA -Q out -nn -U -w $OUT/${name}_nic.pcap 2>$OUT/${name}_nic.err & T2=$!
  sleep 1.5
  $A python3 -c "
import socket, time
s = socket.create_connection(('10.77.0.2', 9000)); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
time.sleep(0.2); s.send(b'x' * 4000); time.sleep(0.3); s.close()
u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); u.setsockopt(17, 103, 1200)   # SOL_UDP, UDP_SEGMENT
u.sendto(b'y' * 4800, ('10.77.0.2', 9001)); time.sleep(0.3)
"
  sleep 1; kill -INT $T1 $T2; wait $T1 $T2 2>/dev/null
  $A ethtool -k ifbT | grep -E "^(tcp-segmentation-offload|generic-segmentation-offload|tx-udp-segmentation):" > $OUT/${name}_ifb_features.txt
}
run_case default
for f in tso gso tx-udp-segmentation; do $A ethtool -K ifbT $f off; done
run_case off
kill $SRV 2>/dev/null
chmod a+r $OUT/*
echo done
