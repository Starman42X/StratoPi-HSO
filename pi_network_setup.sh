#!/bin/bash
# Make stratopi reachable at http://stratopi.local:8080 on home Wi‑Fi and Pi hotspot.
set -euo pipefail

echo "[stratopi-network] Configuring hostname + mDNS + firewall..."

sudo hostnamectl set-hostname stratopi 2>/dev/null || true

sudo mkdir -p /etc/avahi/avahi-daemon.conf.d
sudo tee /etc/avahi/avahi-daemon.conf.d/stratopi.conf >/dev/null <<'EOF'
[server]
host-name=stratopi
domain-name=local
use-ipv4=yes
use-ipv6=no
publish-addresses=yes
publish-hinfo=no
publish-workstation=no
allow-interfaces=wlan0,eth0,wlan1,ap0
deny-interfaces=lo
EOF

sudo systemctl enable avahi-daemon 2>/dev/null || true
sudo systemctl restart avahi-daemon 2>/dev/null || true

# Allow control panel on every interface (hotspot clients + LAN)
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow 8080/tcp comment 'StratoPi web UI' 2>/dev/null || true
fi

if command -v nft >/dev/null 2>&1; then
  sudo nft list table inet stratopi 2>/dev/null || \
    sudo nft add table inet stratopi 2>/dev/null || true
  sudo nft list chain inet stratopi input 2>/dev/null || \
    sudo nft 'add chain inet stratopi input { type filter hook input priority 0; policy accept; }' 2>/dev/null || true
  sudo nft add rule inet stratopi input tcp dport 8080 accept 2>/dev/null || true
fi

# NetworkManager dispatcher — refresh mDNS when link changes
sudo tee /etc/NetworkManager/dispatcher.d/99-stratopi-avahi >/dev/null <<'EOF'
#!/bin/bash
IF="$1"
STATUS="$2"
[ "$STATUS" = "up" ] || exit 0
case "$IF" in wlan*|eth*) systemctl reload avahi-daemon 2>/dev/null || true ;; esac
EOF
sudo chmod +x /etc/NetworkManager/dispatcher.d/99-stratopi-avahi

echo "[stratopi-network] Done. URLs:"
echo "  http://stratopi.local:8080"
hostname -I | awk '{for(i=1;i<=NF;i++) printf "  http://%s:8080\n", $i}'