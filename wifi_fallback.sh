#!/bin/bash
# Start a WiFi hotspot if no network connection is found within 5 seconds of boot.
# Hotspot SSID: Strato-HSO  Password: strato123
# Access the control panel at http://192.168.4.1:8080

SSID="Strato-HSO"
PASSWORD="strato123"

sleep 5

# Check if wlan0 is already connected to a network
if nmcli -t -f TYPE,STATE device | grep -q "wifi:connected"; then
    echo "[wifi_fallback] WiFi connected — no hotspot needed."
    exit 0
fi

echo "[wifi_fallback] No WiFi connection. Starting hotspot: $SSID"
nmcli device wifi hotspot ifname wlan0 ssid "$SSID" password "$PASSWORD"
sleep 2
systemctl reload avahi-daemon 2>/dev/null || true
echo "[wifi_fallback] Control panel:"
echo "  http://stratopi.local:8080"
echo "  http://192.168.4.1:8080"
curl -sf http://127.0.0.1:8080/api/health >/dev/null && echo "[wifi_fallback] server OK on :8080" \
  || echo "[wifi_fallback] WARN: stratopi.service not responding on :8080"
