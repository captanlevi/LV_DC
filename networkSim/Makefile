# Configurable parameters
# PLZ keep paths ABSOLUTE no RELATIVE SHIT
IFACE ?= wlp0s20f3
IFB_INGRESS = ifb0
IFB_EGRESS = ifb1
BANDWIDTH = 1000kbit
DELAY = 0ms
PLR = 0%

MAKEFILE_DIR := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))
CAPTURE_DIR := $(realpath $(MAKEFILE_DIR)/../current_data)
VENV_DIR := $(realpath $(MAKEFILE_DIR)/../.venv)
LABELING_DIR := $(abspath $(MAKEFILE_DIR)/../labeling)

# SESSION_DIR: per-run output directory, set by exp.py for each session.
# Falls back to CAPTURE_DIR so bare make calls still work.
SESSION_DIR ?= $(CAPTURE_DIR)

# Per-session output files live inside SESSION_DIR
IN_FILE        = $(SESSION_DIR)/incoming.pcap
OUT_FILE       = $(SESSION_DIR)/outgoing.pcap
MERGED_FILE    = $(SESSION_DIR)/merged.pcap
HTTP_LOG_FILE  = $(SESSION_DIR)/http_v2.json

# Temp/global files always in CAPTURE_DIR (deleted after use)
SSL_KEY_LOG_FILE = $(CAPTURE_DIR)/ssl_keys.log

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
	# Ingress — add first, fall back to change if already exists
	@sudo tc class add dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH) 2>/dev/null || \
		sudo tc class change dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc qdisc add dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR) 2>/dev/null || \
		sudo tc qdisc change dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR)
	# Egress
	@sudo tc class add dev $(IFACE) parent 1: classid 1:10 htb rate $(BANDWIDTH) 2>/dev/null || \
		sudo tc class change dev $(IFACE) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc qdisc add dev $(IFACE) parent 1:10 netem delay $(DELAY) loss $(PLR) 2>/dev/null || \
		sudo tc qdisc change dev $(IFACE) parent 1:10 netem delay $(DELAY) loss $(PLR)
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
	@mkdir -p $(SESSION_DIR)
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


PLATFORM ?= youtube

run_network_sim:
	python3 -u $(MAKEFILE_DIR)/exp.py --platform $(PLATFORM)

change_perm:
	OWNER=$${SUDO_USER:-$${USER}}; \
	sudo chown -R $$OWNER $(SESSION_DIR) 2>/dev/null || true; \
	sudo chmod -R u+rw $(SESSION_DIR) 2>/dev/null || true; \
	sudo chown $$OWNER $(dir $(SESSION_DIR)) 2>/dev/null || true; \
	sudo chown $$OWNER $(dir $(dir $(SESSION_DIR))) 2>/dev/null || true; \
	echo "Permissions updated for $(SESSION_DIR)"

# Auto-detect browser: prefer google-chrome, fall back to chromium-browser/chromium
CHROME_BIN ?= $(shell which google-chrome chromium-browser chromium 2>/dev/null | head -1)
# Separate profile so collection Chrome/Chromium doesn't share state with your daily browser
CHROME_PROFILE := $(CAPTURE_DIR)/chrome_profile

CHROME_LOG := $(CAPTURE_DIR)/chrome.log

start_chrome:
	@echo "🚀 Starting Chrome with remote debugging..."
	@LAUNCH_USER=$${SUDO_USER:-$${USER}}; \
	mkdir -p $(CHROME_PROFILE); \
	chown -R $$LAUNCH_USER $(CHROME_PROFILE) 2>/dev/null || true; \
	CHROME_ARGS="--no-sandbox --user-data-dir=$(CHROME_PROFILE) --remote-debugging-port=9222 --remote-allow-origins=* --disable-features=ChromeWhatsNewUI --no-first-run --disable-default-apps --autoplay-policy=no-user-gesture-required --mute-audio --load-extension=$(LABELING_DIR) about:blank"; \
	if [ "$$LAUNCH_USER" = "$$(id -un)" ]; then \
		env SSLKEYLOGFILE=$(SSL_KEY_LOG_FILE) $(CHROME_BIN) $$CHROME_ARGS > $(CHROME_LOG) 2>&1 & \
	else \
		sudo -u $$LAUNCH_USER env SSLKEYLOGFILE=$(SSL_KEY_LOG_FILE) $(CHROME_BIN) $$CHROME_ARGS > $(CHROME_LOG) 2>&1 & \
	fi; \
	echo $$! > $(CAPTURE_DIR)/chrome.pid
	@echo "✅ Chrome launched (debug port 9222, log → $(CHROME_LOG))"

kill_chrome:
	@echo "🛑 Killing collection Chrome..."
	@pkill -9 -f "remote-debugging-port=9222" 2>/dev/null || true
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		pgrep -f "remote-debugging-port=9222" > /dev/null 2>&1 || break; \
		sleep 1; \
	done
	@rm -f "$(CHROME_PROFILE)/Default/Sessions" \
	        "$(CHROME_PROFILE)/Default/Last Session" \
	        "$(CHROME_PROFILE)/Default/Current Session" \
	        "$(CHROME_PROFILE)/Default/Last Tabs" \
	        "$(CHROME_PROFILE)/Default/Current Tabs" \
	        "$(CHROME_PROFILE)/Default/Last Browser" 2>/dev/null || true
	@rm -f $(CAPTURE_DIR)/chrome.pid $(CAPTURE_DIR)/chrome.log
	@echo "✅ Chrome killed, session files cleared"

remove_ssl_keys:
	@echo "🧹 Removing existing SSL key log file..."
	@rm -f $(SSL_KEY_LOG_FILE)
	@echo "✅ SSL key log file removed"


make_http_logs:
	@tshark -r $(MERGED_FILE) \
	-o tls.keylog_file:$(SSL_KEY_LOG_FILE) \
	-Y "http" \
	-T json \
	-e frame.time_epoch \
	-e ip.src -e ip.dst -e tcp.srcport -e tcp.dstport \
	-e http.request.method \
	-e http.request.uri \
	-e http.response.code \
	-e http.content_type \
	-e http.file_data \
	-e http.transfer_encoding \
	-e http.content_encoding > $(CAPTURE_DIR)/h1_raw.json


SAVE_PDML ?= 0

process_http_logs:
	@echo "📊 Processing HTTP logs into structured format..."
	@if [ -d "$(VENV_DIR)" ]; then \
		bash -c "source $(VENV_DIR)/bin/activate && python3 $(MAKEFILE_DIR)/unit_extractor.py \
			--path $(CAPTURE_DIR) \
			--save_path $(HTTP_LOG_FILE) \
			--pcap $(MERGED_FILE) \
			--ssl_keys $(SSL_KEY_LOG_FILE) \
			$(if $(filter 1,$(SAVE_PDML)),--save_pdml,)" && \
		echo "✅ HTTP logs processed and saved"; \
	else \
		python3 $(MAKEFILE_DIR)/unit_extractor.py \
			--path $(CAPTURE_DIR) \
			--save_path $(HTTP_LOG_FILE) \
			--pcap $(MERGED_FILE) \
			--ssl_keys $(SSL_KEY_LOG_FILE) && \
		echo "✅ HTTP logs processed and saved" || \
		echo "⚠️  unit_extractor skipped — network_core not installed (raw JSON kept in $(CAPTURE_DIR))"; \
	fi


remove_http_logs:
	@echo "🧹 Removing existing HTTP log files..."
	@rm -f $(CAPTURE_DIR)/h1_raw.json
	@echo "✅ HTTP log files removed"