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
PKGS=(python3.10 python3.10-venv python3-pip tshark tcpdump wireshark-common
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

# Check Chrome / Chromium
CHROME_BIN=$(which google-chrome chromium-browser chromium 2>/dev/null | head -1 || true)
if [ -z "$CHROME_BIN" ]; then
    err "No Chrome or Chromium found."
    warn "Install Google Chrome:  https://www.google.com/chrome/"
    warn "Or Chromium:            sudo apt install chromium-browser"
    ISSUES+=("Chrome/Chromium not installed")
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
if [ ! -d "$VENV" ]; then
    python3.10 -m venv "$VENV"
    ok "Created $VENV"
else
    ok ".venv already exists"
fi

echo "Installing Python packages..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    "network_core>=0.5.3" \
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
        warn "Makefile IFACE='$MAKEFILE_IFACE' but your default interface is '$DEFAULT_IFACE'"
        warn "Fix: edit networkSim/Makefile line 3  →  IFACE ?= $DEFAULT_IFACE"
        warn "  or pass it at runtime:  sudo make enable IFACE=$DEFAULT_IFACE"
        ISSUES+=("Makefile IFACE=$MAKEFILE_IFACE — change to IFACE ?= $DEFAULT_IFACE in networkSim/Makefile")
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

# ── 9. sudoers reminder ───────────────────────────────────────────────────────
hdr "Sudo requirements"
echo "The following commands are called with sudo during collection:"
echo "  modprobe ifb, ip link, tc, tcpdump, chown/chmod"
echo "If running in a CI/unattended environment, add a sudoers entry:"
echo "  $USER ALL=(ALL) NOPASSWD: /sbin/tc, /sbin/ip, /sbin/modprobe, /usr/sbin/tcpdump"

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
echo "  sudo ./../.venv/bin/python3 -u exp.py --platform youtube"
echo ""
echo "Or with make (sets up network shaping first):"
echo "  cd $ROOT/networkSim && sudo make enable && sudo make run_network_sim"
