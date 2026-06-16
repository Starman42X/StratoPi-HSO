#!/usr/bin/env python3
"""Enable stratopi_lora, restart services, probe GPS NMEA."""
import paramiko
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

REMOTE = r"""
set -e
echo strato | sudo -S systemctl enable stratopi_lora.service
echo strato | sudo -S systemctl restart stratopi_lora.service
sleep 3
echo '=== LORA ==='
systemctl is-active stratopi_lora
journalctl -u stratopi_lora -n 5 --no-pager

echo strato | sudo -S systemctl stop stratopi.service
sleep 1
echo '=== GPS NMEA (10s, 115200 on GPIO13 / ttyAMA5) ==='
/home/louis/stratopi/venv/bin/python << 'PY'
import serial, time
try:
    s = serial.Serial('/dev/ttyAMA5', 115200, timeout=1)
except Exception as e:
    print('open fail:', e)
    raise SystemExit
t0 = time.time()
lines = []
while time.time() - t0 < 10:
    line = s.readline().decode('ascii', errors='replace').strip()
    if line.startswith('$'):
        lines.append(line)
        if len(lines) <= 15:
            print(line[:80])
print(f'--- total sentences: {len(lines)}')
gga = [l for l in lines if 'GGA' in l]
rmc = [l for l in lines if 'RMC' in l]
print(f'GGA: {len(gga)}  RMC: {len(rmc)}')
if gga:
    print('last GGA:', gga[-1][:90])
s.close()
PY

echo strato | sudo -S systemctl start stratopi.service
sleep 2
echo '=== GPS FIX ==='
cat /tmp/gps_fix.json 2>/dev/null || echo missing
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('stratopi.local', username='louis', password='strato', timeout=15)
_, o, e = c.exec_command(REMOTE, timeout=45)
print(o.read().decode())
err = e.read().decode()
if err:
    print('ERR:', err)
c.close()