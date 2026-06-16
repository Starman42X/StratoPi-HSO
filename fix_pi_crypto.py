#!/usr/bin/env python3
"""Install cryptography in Pi venv (used by stratopi_lora) and restart."""
import paramiko
import time
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect("stratopi.local", username="louis", password="strato", timeout=15)

script = """#!/bin/bash
set -e
VENV=/home/louis/stratopi/venv
echo '--- venv crypto before ---'
$VENV/bin/python -c 'import cryptography' 2>&1 || echo MISSING
$VENV/bin/pip install -q cryptography
$VENV/bin/python -c 'import cryptography; print("venv crypto", cryptography.__version__)'
cd /home/louis/stratopi
$VENV/bin/python -c "
from pathlib import Path
from lora_crypto import load_crypto_config, crypto_from_config
cfg = load_crypto_config(Path('lora_crypto.json'))
obj = crypto_from_config(cfg)
print('lora_crypto_ok', obj is not None, getattr(obj, 'algorithm', None))
"
systemctl restart stratopi_lora.service
sleep 6
journalctl -u stratopi_lora -n 10 --no-pager
cat /tmp/lora_status.json
"""

sftp = c.open_sftp()
with sftp.open("/tmp/fix_crypto.sh", "w") as f:
    f.write(script)
sftp.close()

_, o, e = c.exec_command("echo strato | sudo -S bash /tmp/fix_crypto.sh")
print(o.read().decode(errors="replace"))
err = e.read().decode(errors="replace")
if err:
    print("STDERR:", err)
c.close()