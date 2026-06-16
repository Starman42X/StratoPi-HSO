#!/usr/bin/env python3
"""Fix Pi WiFi: use DHCP on Starlink so PC and Pi share the same subnet."""
import paramiko
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CONN = "netplan-wlan0-SpaceX Starlink"

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("stratopi.local", username="louis", password="strato", timeout=15)

script = f"""#!/bin/bash
set -e
echo '--- before ---'
hostname -I

echo '--- switch to DHCP ---'
nmcli connection modify "{CONN}" ipv4.method auto ipv4.addresses "" ipv4.gateway "" ipv4.dns ""
nmcli device reapply wlan0 2>/dev/null || {{
  nmcli connection down "{CONN}" || true
  sleep 2
  nmcli connection up "{CONN}"
}}
sleep 5

echo '--- after ---'
hostname -I
ip -4 addr show wlan0
curl -s -m 3 http://127.0.0.1:8080/api/health || true
echo
systemctl is-active stratopi
"""

sftp = c.open_sftp()
with sftp.open("/tmp/fix_dhcp.sh", "w") as f:
    f.write(script)
sftp.close()

_, o, e = c.exec_command("chmod +x /tmp/fix_dhcp.sh; echo strato | sudo -S bash /tmp/fix_dhcp.sh")
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err.strip():
    print("STDERR:", err)
c.close()