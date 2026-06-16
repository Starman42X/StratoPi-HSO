#!/usr/bin/env python3
"""
Temporary Pi TX settings to match an unconfigured (factory) E22 receiver.
Factory default ~433.125 MHz, air rate 2. Revert with deploy_lora_fix.py after
the PC module is configured via config_e22_com.py.
"""
import paramiko
import re
import time
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
content = Path(__file__).parent.joinpath('lora_tx.py').read_text(encoding='utf-8')

content = re.sub(r'E22_CHANNEL\s*=\s*\d+.*', 'E22_CHANNEL   = 23            # factory ~433.125 MHz', content, count=1)
content = re.sub(r'E22_AIR_RATE\s*=\s*0b\d+.*', 'E22_AIR_RATE  = 0b010         # factory air rate 2', content, count=1)
content = re.sub(r'FREQ_HZ\s*=\s*\d+.*', 'FREQ_HZ       = 433_125_000', content, count=1)
content = re.sub(r'TX_INTERVAL\s*=\s*\d+.*', 'TX_INTERVAL   = 8             # fast indoor test', content, count=1)

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('stratopi.local', username='louis', password='strato', timeout=15)
sftp = c.open_sftp()
with sftp.open('/home/louis/stratopi/lora_tx.py', 'w') as f:
    f.write(content.encode('utf-8'))
with sftp.open('/tmp/_cmd.sh', 'w') as f:
    f.write('#!/bin/bash\nsystemctl restart stratopi_lora.service\n')
c.exec_command('echo strato | sudo -S bash /tmp/_cmd.sh')
time.sleep(4)
_, o, _ = c.exec_command('journalctl -u stratopi_lora -n 3 --no-pager')
print(o.read().decode())
sftp.close()
c.close()