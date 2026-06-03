#!/usr/bin/env bash
# LV_DC setup script — run once on a new Linux machine.
# Must be run as a normal user (not root); will call sudo when needed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MAKEFILE="$ROOT/networkSim/Makefile"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}  $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC}  $*"; }
hdr()  { echo -e "\n${BOLD}==> $*${NC}"; }

ISSUES=()

# ── 1. OS check ──────────────────────────────────────────────────────────────
hdr "Checking OS"
if ! grep -qi 'ubuntu\|debian' /etc/os-release 2>/dev/null; then
    warn "Not Ubuntu/Debian — apt commands may not apply. Proceed carefully."
else
    ok "Ubuntu/Debian detected"
fi

# ── 2. System packages ───────────────────────────────────────────────────────
hdr "Installing system packages"
PKGS=(python3 python3-venv python3-pip tshark tcpdump wireshark-common
      iproute2 make curl)

MISSING_PKGS=()
for p in "${PKGS[@]}"; do
    dpkg -s "$p" &>/dev/null || MISSING_PKGS+=("$p")
done

if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo "Installing: ${MISSING_PKGS[*]}"
    sudo apt-get update -qq
    # tshark install prompts "should non-superusers capture packets?" — answer yes
    echo "wireshark-common wireshark-common/install-setuid boolean true" \
        | sudo debconf-set-selections
    sudo apt-get install -y "${MISSING_PKGS[@]}"
else
    ok "All system packages present"
fi

# Check Chrome / Chromium — install Chromium via apt if nothing is found
CHROME_BIN=$(which google-chrome chromium-browser chromium 2>/dev/null | head -1 || true)
if [ -z "$CHROME_BIN" ]; then
    echo "No Chrome or Chromium found — installing chromium-browser via apt..."
    sudo apt-get update -qq
    sudo apt-get install -y chromium-browser
    CHROME_BIN=$(which chromium-browser chromium 2>/dev/null | head -1 || true)
    if [ -z "$CHROME_BIN" ]; then
        err "Chromium install failed. Install manually:"
        warn "  Google Chrome:  https://www.google.com/chrome/"
        warn "  Chromium:       sudo apt install chromium-browser"
        ISSUES+=("Chrome/Chromium not installed — install manually")
    else
        ok "Installed Chromium: $CHROME_BIN"
    fi
else
    ok "Browser: $CHROME_BIN"
fi

# ── 3. tshark / wireshark group ──────────────────────────────────────────────
hdr "Configuring tshark permissions"
if ! groups "$USER" | grep -q wireshark; then
    echo "Adding $USER to wireshark group (re-login required to take effect)"
    sudo usermod -aG wireshark "$USER"
    warn "You must log out and back in (or run 'newgrp wireshark') before tshark works without sudo"
    ISSUES+=("Log out and back in so tshark group membership takes effect")
else
    ok "$USER is in the wireshark group"
fi

# ── 4. Python .venv ──────────────────────────────────────────────────────────
hdr "Setting up Python .venv"
VENV="$ROOT/.venv"

# Find python3 >= 3.10, preferring newer versions
PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
        if [ "$ver" = "True" ]; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    err "No Python >= 3.10 found. Install python3.10 or newer and re-run."
    exit 1
fi
ok "Using $PYTHON_BIN ($(${PYTHON_BIN} --version))"

if [ ! -d "$VENV" ] || ! "$VENV/bin/python3" -c "" &>/dev/null; then
    [ -d "$VENV" ] && { warn ".venv is broken — recreating"; rm -rf "$VENV"; }
    "$PYTHON_BIN" -m venv "$VENV"
    ok "Created $VENV"
else
    ok ".venv already exists and is healthy"
fi

echo "Installing Python packages..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    "network_core>=0.6.0" \
    scapy \
    "websocket-client>=1.0" \
    pandas \
    numpy
ok "Python packages installed"

# ── 5. current_data directory ────────────────────────────────────────────────
hdr "Creating working directories"
mkdir -p "$ROOT/current_data"
mkdir -p "$ROOT/data"
ok "current_data/ and data/ ready"

# ── 6. Network interface ─────────────────────────────────────────────────────
hdr "Checking network interface"
MAKEFILE_IFACE=$(grep '^IFACE\s*[?:]*=' "$MAKEFILE" | head -1 | sed 's/.*=\s*//' | tr -d ' ')
DEFAULT_IFACE=$(ip route 2>/dev/null | awk '/^default/{print $5; exit}')

if [ -z "$DEFAULT_IFACE" ]; then
    err "Cannot detect default network interface."
    ISSUES+=("Set IFACE in networkSim/Makefile to your active network interface")
else
    ok "Detected default interface: $DEFAULT_IFACE"
    if [ "$DEFAULT_IFACE" != "$MAKEFILE_IFACE" ]; then
        sed -i "s/^IFACE ?=.*/IFACE ?= $DEFAULT_IFACE/" "$MAKEFILE"
        ok "Updated Makefile IFACE: $MAKEFILE_IFACE → $DEFAULT_IFACE"
    else
        ok "Makefile IFACE matches ($MAKEFILE_IFACE)"
    fi
fi

# ── 7. IFB kernel module ─────────────────────────────────────────────────────
hdr "Checking ifb kernel module"
if sudo modprobe ifb 2>/dev/null; then
    sudo ip link add ifb0 type ifb 2>/dev/null || true
    sudo ip link delete ifb0 2>/dev/null || true
    ok "ifb module loadable"
else
    err "Cannot load ifb kernel module — traffic shaping will not work."
    warn "This usually means a VM/container with a restricted kernel."
    warn "On a bare-metal Ubuntu machine, install:  sudo apt install linux-modules-extra-$(uname -r)"
    ISSUES+=("ifb kernel module unavailable — TC shaping broken (likely a VM kernel)")
fi

# ── 8. Chrome extension — manual step ────────────────────────────────────────
hdr "Chrome extension (manual step)"
LABELING_DIR="$ROOT/labeling"
echo "The QoE labeling extension at $LABELING_DIR must be loaded once manually:"
echo "  1. Open Chrome → chrome://extensions"
echo "  2. Enable Developer Mode (top-right toggle)"
echo "  3. Click 'Load unpacked' → select:  $LABELING_DIR"
echo "  4. The extension persists in your Chrome profile after that."
ISSUES+=("Load Chrome extension manually once (see above)")

# ── 9. sudoers — write NOPASSWD entries so exp.py runs without sudo ───────────
hdr "Configuring passwordless sudo for network commands"

SUDOERS_FILE="/etc/sudoers.d/lv_dc"

# Resolve actual binary paths (they differ across distros/Ubuntu versions)
resolve_bin() {
    # Try sbin variants first, then fall back to `which`
    for p in "/usr/sbin/$1" "/sbin/$1" "/usr/bin/$1" "/bin/$1"; do
        [ -x "$p" ] && echo "$p" && return
    done
    command -v "$1" 2>/dev/null || echo "/usr/sbin/$1"
}

TC=$(resolve_bin tc)
IP=$(resolve_bin ip)
MODPROBE=$(resolve_bin modprobe)
TCPDUMP=$(resolve_bin tcpdump)
TSHARK=$(resolve_bin tshark)
KILL="/usr/bin/kill"
[ -x /bin/kill ] && KILL="/bin/kill"

SUDOERS_LINE="$USER ALL=(ALL) NOPASSWD: $TC, $IP, $MODPROBE, $TCPDUMP, $TSHARK, $KILL, /usr/bin/chown, /bin/chown, /usr/bin/chmod, /bin/chmod"

echo "$SUDOERS_LINE" | sudo tee "$SUDOERS_FILE" > /dev/null
sudo chmod 440 "$SUDOERS_FILE"

# Validate — visudo -c rejects broken sudoers files
if sudo visudo -c -f "$SUDOERS_FILE" &>/dev/null; then
    ok "Passwordless sudo configured ($SUDOERS_FILE)"
else
    warn "sudoers validation failed — removing $SUDOERS_FILE to stay safe"
    sudo rm -f "$SUDOERS_FILE"
    ISSUES+=("Passwordless sudo not configured — you may be prompted during collection")
fi

# ── 10. Summary ───────────────────────────────────────────────────────────────
hdr "Setup complete"
echo ""
if [ ${#ISSUES[@]} -eq 0 ]; then
    echo -e "${GREEN}Everything looks good — you can run:${NC}"
else
    echo -e "${YELLOW}Setup done with ${#ISSUES[@]} item(s) that need attention:${NC}"
    for i in "${!ISSUES[@]}"; do
        echo -e "  ${RED}[$((i+1))]${NC} ${ISSUES[$i]}"
    done
    echo ""
fi

echo "Run a collection session:"
echo "  cd $ROOT/networkSim"
echo "  python3 exp.py --platform youtube"
echo ""
echo "Or with make:"
echo "  cd $ROOT/networkSim && make run_network_sim"
