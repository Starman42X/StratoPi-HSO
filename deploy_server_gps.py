#!/usr/bin/env python3
"""Deploy server.py GPS fix and restart stratopi."""
import paramiko
import time
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

root = Path(__file__).parent
content = root.joinpath('server.py').read_text(encoding='utf-8')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('stratopi.local', username='louis', password='strato', timeout=15)
sftp = c.open_sftp()
with sftp.open('/home/louis/stratopi/server.py', 'w') as f:
    f.write(content.encode('utf-8'))
e22 = root / 'e22_regs.py'
if e22.exists():
    with sftp.open('/home/louis/stratopi/e22_regs.py', 'w') as f:
        f.write(e22.read_bytes())
lora_crypto = root / 'lora_crypto.py'
if lora_crypto.exists():
    with sftp.open('/home/louis/stratopi/lora_crypto.py', 'w') as f:
        f.write(lora_crypto.read_bytes())
with sftp.open('/tmp/_cmd.sh', 'w') as f:
    f.write('#!/bin/bash\nsystemctl restart stratopi.service\n')
c.exec_command('echo strato | sudo -S bash /tmp/_cmd.sh')
time.sleep(3)
sftp.close()
c.close()
print('Deployed server.py')