# Configurable parameters
# PLZ keep paths ABSOLUTE no RELATIVE SHIT
IFACE ?= wlp0s20f3
# t53 (--concurrent): a trio slot overrides these; every default is single mode's value (make -n golden:
# test_make_single_mode.py)
IFB_INGRESS ?= ifb0
IFB_EGRESS ?= ifb1
EGRESS_CLASS ?= 1:10
CDP_PORT ?= 9222
SLOT_SUFFIX ?=
CHROME_PREFIX ?=
# a slot's tcpdump stderr (its kernel drop counters, meta.json concurrency.capture_drops); single mode: make's stderr
CAPTURE_ERR_IN ?=
CAPTURE_ERR_OUT ?=
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

# t37 (supervisor): our own management traffic is neither shaped nor captured: tailscale's WireGuard UDP (tailscaled's
# listening port, read at make time, default 41641) and ssh (TCP 22), both directions, IPv4 (IPv6 is off on chaos).
# The 100.x tailscale addresses exist only inside the tunnel; on IFACE the tunnel is this UDP port. A DERP-relayed
# tunnel runs over TCP 443 and cannot be told apart by port: exp.py records `tailscale status` in meta.json.
TS_UDP_PORT := $(shell sudo -n ss -ulpnH 2>/dev/null | awk '/tailscaled/ {n=split($$4,a,":"); print a[n]; exit}')
MGMT_UDP_PORT ?= $(if $(TS_UDP_PORT),$(TS_UDP_PORT),41641)
MGMT_TCP_PORT ?= 22
CAPTURE_BPF = not (udp port $(MGMT_UDP_PORT) or tcp port $(MGMT_TCP_PORT))
# t58: a trio slot's staged capture passes TCPDUMP_Z=-Z root (trio_slots.capture_vars: staging on the exFAT data volume);
# single mode leaves it empty, its commands unchanged
TCPDUMP_Z ?=
# t57 tw_100023 (levi 2026-10-20: lvi_tw2's ingress dropped 381 of 669029 packets in the kernel under N = 5): every
# session capture's kernel buffer is 64 MiB (-B, KiB; trio_slots.TCPDUMP_B_KIB, the DNS capture's too; meta.json
# capture.tcpdump_buffer_kib). -B changes no captured byte; it only keeps the ring from overflowing
TCPDUMP_B ?= 65536

# v2 (LVQoE t35, rushi 2026-10-16): the TLS key log is KEPT, one per session, so every session can be re-decoded
# offline (HTTP/3 and SABR request bodies included). remove_ssl_keys / remove_http_logs are no longer called.
SSL_KEY_LOG_FILE = $(SESSION_DIR)/ssl_keys.log

.PHONY: enable disable start-capture stop-capture merge-capture clean-captures mgmt-exclusion

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
	# Management traffic first (prio 1): accepted as is, never redirected to IFB_INGRESS (not shaped, not captured)
	sudo tc filter add dev $(IFACE) parent ffff: protocol ip prio 1 u32 \
		match ip protocol 17 0xff match ip sport $(MGMT_UDP_PORT) 0xffff action ok
	sudo tc filter add dev $(IFACE) parent ffff: protocol ip prio 1 u32 \
		match ip protocol 17 0xff match ip dport $(MGMT_UDP_PORT) 0xffff action ok
	sudo tc filter add dev $(IFACE) parent ffff: protocol ip prio 1 u32 \
		match ip protocol 6 0xff match ip sport $(MGMT_TCP_PORT) 0xffff action ok
	sudo tc filter add dev $(IFACE) parent ffff: protocol ip prio 1 u32 \
		match ip protocol 6 0xff match ip dport $(MGMT_TCP_PORT) 0xffff action ok
	# Catch ALL other ingress traffic and send to IFB_INGRESS
	sudo tc filter add dev $(IFACE) parent ffff: prio 10 \
		matchall action mirred egress redirect dev $(IFB_INGRESS)

	sudo tc qdisc add dev $(IFB_INGRESS) root handle 1: htb default 10

	@echo "📤 Shaping and mirroring outgoing traffic (All Protocols)..."
	sudo tc qdisc add dev $(IFACE) root handle 1: htb default 10
	# Management traffic first (prio 1): classified into the unshaped class 1:20; classification stops there, so the
	# mirror below never sees it (not captured)
	sudo tc class add dev $(IFACE) parent 1: classid 1:20 htb rate 1000mbit
	sudo tc filter add dev $(IFACE) parent 1: protocol ip prio 1 u32 \
		match ip protocol 17 0xff match ip sport $(MGMT_UDP_PORT) 0xffff flowid 1:20
	sudo tc filter add dev $(IFACE) parent 1: protocol ip prio 1 u32 \
		match ip protocol 17 0xff match ip dport $(MGMT_UDP_PORT) 0xffff flowid 1:20
	sudo tc filter add dev $(IFACE) parent 1: protocol ip prio 1 u32 \
		match ip protocol 6 0xff match ip sport $(MGMT_TCP_PORT) 0xffff flowid 1:20
	sudo tc filter add dev $(IFACE) parent 1: protocol ip prio 1 u32 \
		match ip protocol 6 0xff match ip dport $(MGMT_TCP_PORT) 0xffff flowid 1:20
	# Catch ALL other egress traffic and mirror to IFB_EGRESS for capture
	sudo tc filter add dev $(IFACE) parent 1: prio 10 \
		matchall action mirred egress mirror dev $(IFB_EGRESS)

	@echo "✅ Shaping ENABLED on $(IFACE) for ALL traffic (TCP/UDP/IPv4/IPv6)"

# printed by exp.py into meta.json (mgmt_exclusion)
mgmt-exclusion:
	@echo '{"udp_port": $(MGMT_UDP_PORT), "udp_port_source": "$(if $(TS_UDP_PORT),tailscaled ss,default 41641)", "tcp_port": $(MGMT_TCP_PORT), "capture_bpf": "$(CAPTURE_BPF)", "method": "tc prio-1 u32 bypass (ingress action ok, egress class 1:20 unshaped) + tcpdump BPF"}'

slow:
	@echo "📉 Applying bandwidth limit and delay..."
	# Ingress — add first, fall back to change if already exists
	@sudo tc class add dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH) 2>/dev/null || \
		sudo tc class change dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc qdisc add dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR) 2>/dev/null || \
		sudo tc qdisc change dev $(IFB_INGRESS) parent 1:10 netem delay $(DELAY) loss $(PLR)
	# Egress
	@sudo tc class add dev $(IFACE) parent 1: classid $(EGRESS_CLASS) htb rate $(BANDWIDTH) 2>/dev/null || \
		sudo tc class change dev $(IFACE) parent 1: classid $(EGRESS_CLASS) htb rate $(BANDWIDTH)
	@sudo tc qdisc add dev $(IFACE) parent $(EGRESS_CLASS) netem delay $(DELAY) loss $(PLR) 2>/dev/null || \
		sudo tc qdisc change dev $(IFACE) parent $(EGRESS_CLASS) netem delay $(DELAY) loss $(PLR)
	@echo "✅ Network shaping applied: $(BANDWIDTH), $(DELAY), loss $(PLR)"

# restart §2 (levi on 80af314): a walk step changes only the HTB rate, both directions; netem (delay, loss) changes only at
# the sub-step's re-draw (make slow), where it is constant anyway. The classes exist (a walk step never comes first).
rate:
	@sudo tc class change dev $(IFB_INGRESS) parent 1: classid 1:10 htb rate $(BANDWIDTH)
	@sudo tc class change dev $(IFACE) parent 1: classid $(EGRESS_CLASS) htb rate $(BANDWIDTH)


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
	@# v2 (t37, chaos): the session dir can be on exFAT, where `tcpdump -w FILE` fails ("Couldn't change ownership of
	@# savefile") and writes only the header. A root shell opens the file and tcpdump writes to stdout, so tcpdump never
	@# opens or chowns it; `exec` makes tcpdump sudo's direct child, so stop-capture's SIGINT reaches it via sudo.
	@sudo sh -c 'exec tcpdump $(TCPDUMP_Z) -B $(TCPDUMP_B) -i $(IFB_INGRESS) -U -w - "$(CAPTURE_BPF)" > "$(IN_FILE)"$(if $(CAPTURE_ERR_IN), 2> "$(CAPTURE_ERR_IN)",)' & echo $$! > $(CAPTURE_DIR)/$(IFB_INGRESS).pid
	@sudo sh -c 'exec tcpdump $(TCPDUMP_Z) -B $(TCPDUMP_B) -i $(IFB_EGRESS) -U -w - "$(CAPTURE_BPF)" > "$(OUT_FILE)"$(if $(CAPTURE_ERR_OUT), 2> "$(CAPTURE_ERR_OUT)",)' & echo $$! > $(CAPTURE_DIR)/$(IFB_EGRESS).pid
	@echo "✅ Capture started"




stop-capture:
	@echo "🛑 Stopping capture..."

	@if [ -f $(CAPTURE_DIR)/$(IFB_INGRESS).pid ]; then \
		PID=`cat $(CAPTURE_DIR)/$(IFB_INGRESS).pid`; \
		sudo kill -INT $$PID; \
		for i in $$(seq 50); do sudo kill -0 $$PID 2>/dev/null || break; sleep 0.2; done; \
		rm $(CAPTURE_DIR)/$(IFB_INGRESS).pid; \
	fi

	@if [ -f $(CAPTURE_DIR)/$(IFB_EGRESS).pid ]; then \
		PID=`cat $(CAPTURE_DIR)/$(IFB_EGRESS).pid`; \
		sudo kill -INT $$PID; \
		for i in $$(seq 50); do sudo kill -0 $$PID 2>/dev/null || break; sleep 0.2; done; \
		rm $(CAPTURE_DIR)/$(IFB_EGRESS).pid; \
	fi

	@# (the loop above waits up to 10 s for tcpdump to flush and exit; `sudo wait` never waited: wait is a shell builtin)
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
# Separate profile so collection Chrome/Chromium doesn't share state with your daily browser.
# v2: a FRESH profile per session (exp.py passes CHROME_PROFILE); no cache, cookies or state carry over, and no
# user extensions, so ours is the only non-built-in one. Branded Chrome >= 137 ignores --load-extension; browser.py
# then loads the extension over CDP (--enable-unsafe-extension-debugging). Do NOT add --disable-extensions-except:
# on branded Chrome it keeps the CDP-loaded extension's content script from running (laptop test 2026-10-16).
CHROME_PROFILE ?= $(CAPTURE_DIR)/chrome_profile

CHROME_LOG := $(CAPTURE_DIR)/chrome$(SLOT_SUFFIX).log
# v2: optional fixed window size "W,H" (exp.py passes the platform's; the player size is recorded in meta.json)
WINDOW_SIZE ?=
WINDOW_FLAG := $(if $(WINDOW_SIZE),--window-size=$(WINDOW_SIZE),)

# one --disable-features list (Chrome honours only the last one). Optimization Guide: its on-device model download
# ignores --disable-background-networking / --disable-component-update and took 41-42 MB in the first ~70 s of a
# session inside the shaped link (chaos captest3; laptop: 42.3 MB with the old list, 0.0 MB with this one, t37)
# MediaRouter / GlobalMediaControlsCastStartStop (levi 13:41, a named T* defect): Chrome's Cast discovery opened :8009
# connections to LAN devices in every yt / tw session of record (shagun's era count, json 98f84545e13f); one list for
# every launch (single mode, every slot, tt's merged list), so the three platforms' Chromes are identical in it
CHROME_DISABLE_FEATURES = ChromeWhatsNewUI,OptimizationGuideModelDownloading,OptimizationHintsFetching,OptimizationTargetPrediction,OptimizationHints,OptimizationGuideOnDeviceModel,MediaRouter,GlobalMediaControlsCastStartStop
comma := ,
# per-platform decode flags (decode_path.py; exp.py passes them for tt's HEVC path, 2026-10-18): extra features to
# disable join the one list above; features to enable, extra args and env assignments before the binary
CHROME_DISABLE_FEATURES_EXTRA ?=
CHROME_ENABLE_FEATURES ?=
CHROME_EXTRA_ARGS ?=
CHROME_ENV ?=
DISABLE_FEATURES_ALL := $(CHROME_DISABLE_FEATURES)$(if $(CHROME_DISABLE_FEATURES_EXTRA),$(comma)$(CHROME_DISABLE_FEATURES_EXTRA),)
ENABLE_FEATURES_FLAG := $(if $(CHROME_ENABLE_FEATURES),--enable-features=$(CHROME_ENABLE_FEATURES),)

start_chrome:
	@echo "🚀 Starting Chrome with remote debugging..."
	@LAUNCH_USER=$${SUDO_USER:-$${USER}}; \
	mkdir -p $(CHROME_PROFILE); \
	chown -R $$LAUNCH_USER $(CHROME_PROFILE) 2>/dev/null || true; \
	CHROME_ARGS="--no-sandbox --user-data-dir=$(CHROME_PROFILE) --remote-debugging-port=$(CDP_PORT) --remote-allow-origins=* --disable-features=$(DISABLE_FEATURES_ALL) $(ENABLE_FEATURES_FLAG) $(CHROME_EXTRA_ARGS) --no-first-run --disable-default-apps --autoplay-policy=no-user-gesture-required --mute-audio --enable-unsafe-extension-debugging --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-background-timer-throttling --disable-background-networking --disable-component-update --disable-sync --disable-domain-reliability --no-default-browser-check $(WINDOW_FLAG) --load-extension=$(LABELING_DIR) about:blank"; \
	if [ "$$LAUNCH_USER" = "$$(id -un)" ]; then \
		$(if $(CHROME_PREFIX),$(CHROME_PREFIX) ,)env SSLKEYLOGFILE=$(SSL_KEY_LOG_FILE) $(CHROME_ENV) $(CHROME_BIN) $$CHROME_ARGS > $(CHROME_LOG) 2>&1 & \
	else \
		$(if $(CHROME_PREFIX),$(CHROME_PREFIX) ,)sudo -u $$LAUNCH_USER env SSLKEYLOGFILE=$(SSL_KEY_LOG_FILE) $(CHROME_ENV) $(CHROME_BIN) $$CHROME_ARGS > $(CHROME_LOG) 2>&1 & \
	fi; \
	echo $$! > $(CAPTURE_DIR)/chrome$(SLOT_SUFFIX).pid
	@echo "✅ Chrome launched (debug port $(CDP_PORT), log → $(CHROME_LOG))"

# kill_chrome keeps the stderr log (levi 12:19 (f)): exp.py copies it into the session after this; start_chrome's >
# truncates it at the next launch
kill_chrome:
	@echo "🛑 Killing collection Chrome..."
	@# [r]: the pattern must not match this recipe's own shell, whose command line contains it (make: *** Killed)
	@pkill -9 -f "[r]emote-debugging-port=$(CDP_PORT)" 2>/dev/null || true
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		pgrep -f "[r]emote-debugging-port=$(CDP_PORT)" > /dev/null 2>&1 || break; \
		sleep 1; \
	done
	@rm -f "$(CHROME_PROFILE)/Default/Sessions" \
	        "$(CHROME_PROFILE)/Default/Last Session" \
	        "$(CHROME_PROFILE)/Default/Current Session" \
	        "$(CHROME_PROFILE)/Default/Last Tabs" \
	        "$(CHROME_PROFILE)/Default/Current Tabs" \
	        "$(CHROME_PROFILE)/Default/Last Browser" 2>/dev/null || true
	@rm -f $(CAPTURE_DIR)/chrome$(SLOT_SUFFIX).pid
	@echo "✅ Chrome killed, session files cleared"

# v2: NOT called by exp.py any more (the keys are kept per session); left for manual use.
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
	-e http.content_encoding > $(SESSION_DIR)/h1_raw.json


# v2: raw decoder outputs stay in the session dir (h1_raw.json, h2_raw.pdml, h3_raw.pdml)
SAVE_PDML ?= 1

process_http_logs:
	@echo "📊 Processing HTTP logs into structured format..."
	@if [ -d "$(VENV_DIR)" ]; then \
		bash -c "source $(VENV_DIR)/bin/activate && python3 $(MAKEFILE_DIR)/unit_extractor.py \
			--path $(SESSION_DIR) \
			--save_path $(HTTP_LOG_FILE) \
			--pcap $(MERGED_FILE) \
			--ssl_keys $(SSL_KEY_LOG_FILE) \
			$(if $(filter 1,$(SAVE_PDML)),--save_pdml,)" && \
		echo "✅ HTTP logs processed and saved"; \
	else \
		python3 $(MAKEFILE_DIR)/unit_extractor.py \
			--path $(SESSION_DIR) \
			--save_path $(HTTP_LOG_FILE) \
			--pcap $(MERGED_FILE) \
			--ssl_keys $(SSL_KEY_LOG_FILE) \
			$(if $(filter 1,$(SAVE_PDML)),--save_pdml,) && \
		echo "✅ HTTP logs processed and saved" || \
		echo "⚠️  unit_extractor skipped — network_core not installed (raw JSON kept in $(CAPTURE_DIR))"; \
	fi


# v2: NOT called by exp.py any more (raw decoder outputs stay in the session dir); left for manual use.
remove_http_logs:
	@echo "🧹 Removing existing HTTP log files..."
	@rm -f $(CAPTURE_DIR)/h1_raw.json
	@echo "✅ HTTP log files removed"