#!/bin/bash
set -e

echo "=== StratoPi HSO Install ==="

# System packages
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip python3-venv \
    ffmpeg v4l-utils \
    libcamera-apps \
    libcamera-tools

# App directory
APP_DIR="/home/louis/stratopi"
mkdir -p "$APP_DIR"
mkdir -p /home/louis/captures

# Copy app files
cp server.py "$APP_DIR/"
cp -r templates "$APP_DIR/"

# Python venv
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet flask

# Systemd service
sudo cp stratopi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stratopi
sudo systemctl restart stratopi

echo ""
echo "=== Done! ==="
echo "Web UI: http://$(hostname -I | awk '{print $1}'):8080"
echo "Service status: sudo systemctl status stratopi"
