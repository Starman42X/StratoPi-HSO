#!/usr/bin/env python3
"""Deploy flight lora_tx.py (CH24 / SF12 / 35s) and restart stratopi_lora."""
import paramiko
import re
import time
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

ROOT = Path(__file__).parent
LOCAL = ROOT / 'lora_tx.py'
DIAG = ROOT / 'lora_diag.json'
RADIO = ROOT / 'lora_radio.json'
E22 = ROOT / 'e22_regs.py'
LORA_CMD = ROOT / 'lora_cmd.py'
LORA_CRYPTO = ROOT / 'lora_crypto.py'
CRYPTO_JSON = ROOT / 'lora_crypto.json'
content = LOCAL.read_text(encoding='utf-8')

content = re.sub(
    r'E22_CHANNEL\s*=\s*\d+.*',
    'E22_CHANNEL   = 24            # measured center ~434.105 MHz',
    content,
    count=1,
)
content = re.sub(
    r'E22_AIR_RATE\s*=\s*0b\d+.*',
    'E22_AIR_RATE  = 0b011         # =3 -> SF12/BW125, the robust narrow HAB mode',
    content,
    count=1,
)
content = re.sub(
    r'FREQ_HZ\s*=\s*\d+.*',
    'FREQ_HZ       = 434_105_000   # measured emission center (for logging)',
    content,
    count=1,
)
content = re.sub(
    r'TX_INTERVAL\s*=\s*\d+.*',
    'TX_INTERVAL   = 35            # s — SF12/BW125 ~9% duty (DE limit 10%)',
    content,
    count=1,
)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('stratopi.local', username='louis', password='strato', timeout=15)
sftp = c.open_sftp()

with sftp.open('/home/louis/stratopi/lora_tx.py', 'w') as f:
    f.write(content.encode('utf-8'))
for local, remote in (
    (DIAG, '/home/louis/stratopi/lora_diag.json'),
    (RADIO, '/home/louis/stratopi/lora_radio.json'),
    (E22, '/home/louis/stratopi/e22_regs.py'),
    (LORA_CMD, '/home/louis/stratopi/lora_cmd.py'),
    (LORA_CRYPTO, '/home/louis/stratopi/lora_crypto.py'),
    (CRYPTO_JSON, '/home/louis/stratopi/lora_crypto.json'),
):
    if local.exists():
        with sftp.open(remote, 'w') as f:
            f.write(local.read_bytes())
print('Deployed flight lora_tx.py (CH24, air=3, economy diag)')

with sftp.open('/tmp/_cmd.sh', 'w') as f:
    f.write(
        '#!/bin/bash\n'
        '/home/louis/stratopi/venv/bin/pip install -q cryptography\n'
        'systemctl enable stratopi_lora.service\n'
        'systemctl restart stratopi_lora.service\n'
    )
c.exec_command('echo strato | sudo -S bash /tmp/_cmd.sh')
time.sleep(4)
_, o, _ = c.exec_command(
    'systemctl is-active stratopi_lora.service; journalctl -u stratopi_lora -n 5 --no-pager')
print(o.read().decode())
sftp.close()
c.close()