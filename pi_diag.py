#!/usr/bin/env python3
"""Quick Pi diagnostics for GPS + LoRa."""
import paramiko
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

SCRIPT = r"""
echo '=== SERVICES ==='
systemctl is-active stratopi_lora stratopi 2>/dev/null
echo '=== LORA LOG (last 15) ==='
journalctl -u stratopi_lora -n 15 --no-pager
echo '=== STRATOPI LOG (gps lines) ==='
journalctl -u stratopi -n 30 --no-pager 2>/dev/null | grep -iE 'gps|ttyAMA|uart|NMEA|error' || echo '(no gps log lines)'
echo '=== GPS FIX FILE ==='
cat /tmp/gps_fix.json 2>/dev/null || echo missing
echo '=== LORA STATUS ==='
cat /tmp/lora_status.json 2>/dev/null || echo missing
echo '=== UART DEVICES ==='
ls -la /dev/ttyAMA* 2>/dev/null
echo '=== CONFIG.TXT uart ==='
grep -hE 'enable_uart|dtoverlay=uart|gpio13|miniuart' /boot/firmware/config.txt /boot/config.txt 2>/dev/null
echo '=== GPS RAW (3s) ==='
timeout 3 dd if=/dev/ttyAMA5 bs=1 count=300 2>/dev/null | strings | head -5 || echo 'NO DATA on ttyAMA5'
echo '=== WHO HAS UART ==='
fuser -v /dev/ttyAMA0 /dev/ttyAMA5 2>&1 || true
echo '=== PROCS ==='
ps aux | grep -E 'lora_tx|server.py' | grep -v grep
echo '=== LORA SERVICE STATUS ==='
systemctl status stratopi_lora --no-pager 2>&1 | head -20
echo '=== LORA JOURNAL TODAY ==='
journalctl -u stratopi_lora --since today --no-pager 2>/dev/null | tail -30
echo '=== GPS API ==='
curl -s http://127.0.0.1:8080/api/gps 2>/dev/null || curl -s http://127.0.0.1/api/gps 2>/dev/null
echo
echo '=== GPS BAUD PROBE (stop server briefly) ==='
sudo systemctl stop stratopi.service
sleep 1
for b in 9600 115200 38400 57600; do
  echo -n "baud $b: "
  timeout 2 stty -F /dev/ttyAMA5 $b raw -echo 2>/dev/null && timeout 2 dd if=/dev/ttyAMA5 bs=1 count=120 2>/dev/null | strings | head -1 || echo '(none)'
done
sudo systemctl start stratopi.service
"""

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('stratopi.local', username='louis', password='strato', timeout=15)
_, o, e = c.exec_command(SCRIPT, timeout=30)
print(o.read().decode())
err = e.read().decode()
if err:
    print('STDERR:', err)
c.close()