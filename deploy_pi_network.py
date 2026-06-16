#!/usr/bin/env python3
"""Deploy Pi network fixes (mDNS stratopi.local + hotspot reachability)."""
import paramiko
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
SETUP = ROOT / "pi_network_setup.sh"
WIFI_FB = ROOT / "wifi_fallback.sh"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
for host in ("stratopi.local", "stratopi", "192.168.178.210"):
    try:
        c.connect(host, username="louis", password="strato", timeout=12)
        print(f"Connected via {host}")
        break
    except Exception as e:
        print(f"  {host}: {e}")
else:
    raise SystemExit("Could not SSH to Pi")

sftp = c.open_sftp()
with sftp.open("/home/louis/stratopi/pi_network_setup.sh", "w") as f:
    f.write(SETUP.read_bytes())
with sftp.open("/home/louis/stratopi/wifi_fallback.sh", "w") as f:
    f.write(WIFI_FB.read_bytes())
sftp.close()

_, o, e = c.exec_command(
    "chmod +x /home/louis/stratopi/pi_network_setup.sh /home/louis/stratopi/wifi_fallback.sh; "
    "echo strato | sudo -S bash /home/louis/stratopi/pi_network_setup.sh; "
    "echo strato | sudo -S systemctl restart stratopi.service 2>/dev/null || true; "
    "sleep 2; "
    "curl -s http://127.0.0.1:8080/api/health; echo; "
    "hostname -I; "
    "getent hosts stratopi.local"
)
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:", err)
c.close()
print("Pi network deploy complete.")