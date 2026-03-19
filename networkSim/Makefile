# Configurable parameters
# PLZ keep paths ABSOLUTE no RELATIVE SHIT
IFACE = wlp0s20f3
IFB_INGRESS = ifb0
IFB_EGRESS = ifb1
BANDWIDTH = 500kbit
DELAY = 0ms
PLR = 0%

MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CAPTURE_DIR := $(realpath $(MAKEFILE_DIR)/../current_data)
#CAPTURE_DIR = /home/captanlevi/Desktop/Live/data
IN_FILE = $(CAPTURE_DIR)/incoming.pcap
OUT_FILE = $(CAPTURE_DIR)/outgoing.pcap
MERGED_FILE = $(CAPTURE_DIR)/merged.pcap
NETWORK_LOG_FILE = $(CAPTURE_DIR)/net_labels.txt
.PHONY: enable disable start-capture stop-capture merge-capture clean-captures

enable:
	@echo "🔧 Loading ifb modules and creating interfaces..."
	sudo modprobe ifb
	sudo ip link add $(IFB_INGRESS) type ifb || true
	sudo ip link set $(IFB_INGRESS) up
	sudo ip link add $(IFB_EGRESS) type ifb || true
	sudo ip link set $(IFB_EGRESS) up

	@echo "🧹 Cleaning up previous tc rules..."
	sudo tc qdisc del dev $(IFACE) root || true
	sudo tc qdisc del dev $(IFACE) ingress || true
	sudo tc qdisc del dev $(IFB_INGRESS) root || true
	sudo tc qdisc del dev $(IFB_EGRESS) root || true

	@echo "📥 Shaping incoming traffic (All Protocols)..."
	sudo tc qdisc add dev $(IFACE) handle ffff: ingress
	# Catch ALL ingress traffic and send to IFB_INGRESS
	sudo tc filter add dev $(IFACE) parent ffff: \
		matchall action mirred egress redirect dev $(IFB_INGRESS)

	sudo tc qdisc add dev $(IFB_INGRESS) root handle 1: htb default 10

	@echo "📤 Shaping and mirroring outgoing traffic (All Protocols)..."
	sudo tc qdisc add dev $(IFACE) root handle 1: htb default 10
	# Catch ALL egress traffic and mirror to IFB_EGRESS for capture
	sudo tc filter add dev $(IFACE) parent 1: \
		matchall action mirred egress mirror dev $(IFB_EGRESS)

	@echo "✅ Shaping ENABLED on $(IFACE) for ALL traffic (TCP/UDP/IPv4/IPv6)"

slow:
	@echo "📉 Applying bandwidth limit and delay..."
	# Ingress
	@sudo tc class change dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH) || \
		sudo tc class add dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc qdisc change dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR) || \
		sudo tc qdisc add dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR)

	# Egress
	@sudo tc class change dev $(IFACE) parent 1: classid 1:10 htb rate $(BANDWIDTH) || \
		sudo tc class add dev $(IFACE) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc qdisc change dev $(IFACE) parent 1:10 netem delay $(DELAY) loss $(PLR) || \
		sudo tc qdisc add dev $(IFACE) parent 1:10 netem delay $(DELAY) loss $(PLR)

	@echo "✅ Network shaping applied: $(BANDWIDTH), $(DELAY), loss $(PLR)"


disable:
	@echo "🧹 Cleaning up traffic shaping and virtual interfaces..."
	sudo tc qdisc del dev $(IFACE) root || true
	sudo tc qdisc del dev $(IFACE) ingress || true
	sudo tc qdisc del dev $(IFB_INGRESS) root || true
	sudo tc qdisc del dev $(IFB_EGRESS) root || true
	sudo ip link set $(IFB_INGRESS) down || true
	sudo ip link delete $(IFB_INGRESS) type ifb || true
	sudo ip link set $(IFB_EGRESS) down || true
	sudo ip link delete $(IFB_EGRESS) type ifb || true
	@echo "✅ Traffic shaping DISABLED"

start-capture:
	@mkdir -p $(CAPTURE_DIR)
	@sudo tcpdump -i $(IFB_INGRESS) -U -w $(IN_FILE) & echo $$! > $(CAPTURE_DIR)/ifb0.pid
	@sudo tcpdump -i $(IFB_EGRESS) -U -w $(OUT_FILE) & echo $$! > $(CAPTURE_DIR)/ifb1.pid
	@echo "✅ Capture started"




stop-capture:
	@echo "🛑 Stopping capture..."

	@if [ -f $(CAPTURE_DIR)/ifb0.pid ]; then \
		PID=`cat $(CAPTURE_DIR)/ifb0.pid`; \
		sudo kill -INT $$PID; \
		sudo wait $$PID 2>/dev/null || true; \
		rm $(CAPTURE_DIR)/ifb0.pid; \
	fi

	@if [ -f $(CAPTURE_DIR)/ifb1.pid ]; then \
		PID=`cat $(CAPTURE_DIR)/ifb1.pid`; \
		sudo kill -INT $$PID; \
		sudo wait $$PID 2>/dev/null || true; \
		rm $(CAPTURE_DIR)/ifb1.pid; \
	fi

	@echo "✅ Capture fully stopped"



merge-capture:
	@echo "🔗 Merging incoming and outgoing captures..."
	@mergecap -w $(MERGED_FILE) $(IN_FILE) $(OUT_FILE)
	@echo "✅ Merged capture saved to $(MERGED_FILE)"
	@rm $(IN_FILE)
	@rm $(OUT_FILE)


run_network_sim:
	cd .. && python3 -m networkSim.exp

change_perm:
	OWNER=$${SUDO_USER:-$${USER}}; \
	[ -f $(MERGED_FILE) ] && sudo chown $$OWNER $(MERGED_FILE) && sudo chmod 644 $(MERGED_FILE); \
	[ -f $(NETWORK_LOG_FILE) ] && sudo chown $$OWNER $(NETWORK_LOG_FILE) && sudo chmod 644 $(NETWORK_LOG_FILE); \
	echo "Permissions updated for existing files"