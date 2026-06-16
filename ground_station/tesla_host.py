"""Tesla in-car browser — port 80/443 on hotspot IP, firewall, self-signed TLS."""

from __future__ import annotations

import datetime
import ipaddress
import logging
import platform
import socket
import subprocess
import threading
from pathlib import Path
from typing import Any

from bundle_paths import app_dir

log = logging.getLogger("gs.tesla")

HOTSPOT_GW = "192.168.137.1"
HOTSPOT_NET = "192.168.137.0/24"
_TESLA_PORTS: tuple[int, ...] = (80, 443)
_servers: list[Any] = []


def _powershell(script: str, timeout: int = 25) -> tuple[str, str, int]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def ensure_tesla_firewall(ports: tuple[int, ...] = _TESLA_PORTS) -> bool:
    """Allow hotspot clients (Tesla) to reach the PC — needs Administrator."""
    if platform.system() != "Windows":
        return True
    port_list = ",".join(str(p) for p in ports)
    ps = f"""
$ports = '{port_list}'.Split(',')
$ok = $true
foreach ($p in $ports) {{
  $name = "StratoPi Tesla TCP $p"
  $existing = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
  if (-not $existing) {{
    try {{
      New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $p -RemoteAddress '{HOTSPOT_NET}' -Profile Any -Enabled True `
        -ErrorAction Stop | Out-Null
    }} catch {{
      netsh advfirewall firewall add rule name="$name" dir=in action=allow `
        protocol=TCP localport=$p remoteip={HOTSPOT_NET} | Out-Null
    }}
  }}
  $name2 = "StratoPi Tesla TCP $p (all)"
  if (-not (Get-NetFirewallRule -DisplayName $name2 -ErrorAction SilentlyContinue)) {{
    try {{
      New-NetFirewallRule -DisplayName $name2 -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $p -Profile Any -Enabled True -ErrorAction Stop | Out-Null
    }} catch {{ $ok = $false }}
  }}
}}
# Hotspot adapter must be Private (Public blocks inbound multicast/TCP)
$alias = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object {{ $_.IPAddress -like '192.168.137.*' }} |
  Select-Object -First 1 -ExpandProperty InterfaceAlias)
if ($alias) {{
  Get-NetConnectionProfile -InterfaceAlias $alias -ErrorAction SilentlyContinue |
    Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
}}
if ($ok) {{ 'OK' }} else {{ 'PARTIAL' }}
"""
    out, err, code = _powershell(ps, timeout=30)
    if "OK" in out:
        log.info("Firewall open for Tesla on port(s) %s from %s", port_list, HOTSPOT_NET)
        return True
    log.warning("Tesla firewall setup incomplete (%s) — run as Administrator", err or out)
    return False


def _ssl_cert_paths() -> tuple[Path, Path]:
    d = app_dir()
    return d / "tesla_hotspot.crt", d / "tesla_hotspot.key"


def ensure_ssl_cert() -> tuple[Path, Path] | None:
    """Self-signed cert for https://192.168.137.1/ (Tesla HW4 often blocks plain HTTP)."""
    cert_path, key_path = _ssl_cert_paths()
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        log.warning("cryptography missing — HTTPS viewer disabled")
        return None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [
        x509.DNSName("localhost"),
        x509.DNSName("whereami.local"),
        x509.IPAddress(ipaddress.IPv4Address(HOTSPOT_GW)),
    ]
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOTSPOT_GW)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    log.info("Generated Tesla HTTPS cert: %s", cert_path)
    return cert_path, key_path


def _ssl_context():
    paths = ensure_ssl_cert()
    if not paths:
        return None
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(paths[0], paths[1])
    return ctx


def start_tesla_servers(app, primary_port: int) -> dict[str, Any]:
    """
    Listen on 80 + 443 for Tesla (8080 is blocked in-car).
    Returns {tesla_url, tesla_https_url, ports_ok, firewall_ok}.
    """
    from werkzeug.serving import make_server

    global _servers
    _servers = []
    result: dict[str, Any] = {
        "tesla_url": f"http://{HOTSPOT_GW}/",
        "tesla_https_url": f"https://{HOTSPOT_GW}/",
        "ports_ok": [],
        "ports_failed": [],
        "firewall_ok": ensure_tesla_firewall(_TESLA_PORTS),
    }
    ssl_ctx = _ssl_context()

    for port in _TESLA_PORTS:
        if port == primary_port:
            continue
        kwargs: dict[str, Any] = {"app": app, "threaded": True}
        if port == 443:
            if not ssl_ctx:
                result["ports_failed"].append(port)
                continue
            kwargs["ssl_context"] = ssl_ctx
        try:
            srv = make_server("0.0.0.0", port, **kwargs)
            threading.Thread(
                target=srv.serve_forever,
                name=f"tesla-http-{port}",
                daemon=True,
            ).start()
            _servers.append(srv)
            result["ports_ok"].append(port)
            if port == 80:
                log.info("Tesla map (HTTP):  http://%s/", HOTSPOT_GW)
            elif port == 443:
                log.info("Tesla map (HTTPS): https://%s/  (use if HTTP blocked)", HOTSPOT_GW)
        except OSError as e:
            result["ports_failed"].append(port)
            if port == 80:
                log.warning("Port 80 failed (%s) — run as Administrator", e)
            elif port == 443:
                log.warning("Port 443 failed (%s) — run as Administrator", e)

    if 80 not in result["ports_ok"] and 443 not in result["ports_ok"]:
        log.error(
            "Tesla viewer ports not listening — right-click run_ground_control_tesla.ps1 "
            "-> Run as administrator"
        )
    return result


def tesla_urls() -> list[str]:
    """URLs to try in Tesla browser, best first."""
    return [
        f"https://{HOTSPOT_GW}/",
        f"http://{HOTSPOT_GW}/",
        f"https://{HOTSPOT_GW}/whereami",
        f"http://{HOTSPOT_GW}/whereami",
    ]