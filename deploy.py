#!/usr/bin/env python3
"""Deploy StratoPi HSO to the Raspberry Pi."""

import paramiko
import os
import stat
from pathlib import Path

HOST = "stratopi"
USER = "louis"
PASS = "strato"
REMOTE_APP = "/home/louis/stratopi"
LOCAL_BASE = Path(__file__).parent

def run(ssh, cmd, check=True):
    # Pipe sudo password via -S for any sudo commands
    if "sudo " in cmd:
        cmd = cmd.replace("sudo ", f"echo '{PASS}' | sudo -S ", 1)
    print(f"  $ {cmd[:80]}")
    stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=False)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc = stdout.channel.recv_exit_status()
    # Filter out the sudo password prompt from stderr
    err_lines = [l for l in err.splitlines() if "[sudo]" not in l and "password for" not in l.lower()]
    safe = lambda s: s.encode('ascii', errors='replace').decode('ascii')
    if out.strip():
        print("   ", safe(out.strip()[:200]))
    if err_lines:
        print("   STDERR:", safe("\n".join(err_lines)[:200]))
    if check and rc != 0:
        raise RuntimeError(f"Command failed (rc={rc}): {cmd}")
    return out, err, rc


def upload(sftp, local_path, remote_path):
    print(f"  upload {local_path.name} -> {remote_path}")
    sftp.put(str(local_path), remote_path)


def main():
    print(f"Connecting to {USER}@{HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASS, timeout=10)
    sftp = ssh.open_sftp()
    print("Connected.\n")

    # Create directories
    print("Creating directories...")
    run(ssh, f"mkdir -p {REMOTE_APP}/templates")
    run(ssh, "mkdir -p /home/louis/captures")

    # Upload app files
    print("\nUploading files...")
    upload(sftp, LOCAL_BASE / "server.py", f"{REMOTE_APP}/server.py")
    upload(sftp, LOCAL_BASE / "templates/index.html", f"{REMOTE_APP}/templates/index.html")
    upload(sftp, LOCAL_BASE / "stratopi.service", f"{REMOTE_APP}/stratopi.service")

    # Install system packages
    print("\nInstalling system packages (this may take a minute)...")
    run(ssh, "sudo apt-get update -qq", check=False)
    run(ssh, "sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip python3-venv ffmpeg v4l-utils libcamera-apps libcamera-tools 2>&1 | tail -5", check=False)

    # Python venv + flask
    print("\nSetting up Python venv...")
    run(ssh, f"python3 -m venv {REMOTE_APP}/venv")
    run(ssh, f"{REMOTE_APP}/venv/bin/pip install --quiet flask")

    # Install systemd service
    print("\nInstalling systemd service...")
    run(ssh, f"sudo cp {REMOTE_APP}/stratopi.service /etc/systemd/system/")
    run(ssh, "sudo systemctl daemon-reload")
    run(ssh, "sudo systemctl enable stratopi")
    run(ssh, "sudo systemctl restart stratopi")

    # Status check
    import time
    time.sleep(2)
    out, _, _ = run(ssh, "sudo systemctl is-active stratopi", check=False)
    ip_out, _, _ = run(ssh, "hostname -I | awk '{print $1}'", check=False)
    ip = ip_out.strip()

    sftp.close()
    ssh.close()

    status = out.strip()
    print(f"\n{'='*40}")
    if status == "active":
        print(f"[OK] Service running!")
        print(f"  Web UI: http://{ip}:8080")
        print(f"  Also try: http://stratopi:8080")
    else:
        print(f"[FAIL] Service status: {status}")
        print("  Check logs: sudo journalctl -u stratopi -n 30")
    print("="*40)


if __name__ == "__main__":
    main()
