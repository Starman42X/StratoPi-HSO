"""Windows Wi‑Fi hotspot + map share — Tesla: http://192.168.137.1/  phones: .local or IP."""

from __future__ import annotations

import logging
import platform
import socket
import subprocess
import threading
import time
from typing import Any

log = logging.getLogger("gs.whereami")

_HOSTNAME = "whereami"
_VIEWER_PATH = "/whereami"
_mdns_service = None
_hotspot_state: dict[str, Any] = {
    "platform": platform.system(),
    "hostname": _HOSTNAME,
    "viewer_path": _VIEWER_PATH,
    "mdns_ok": False,
    "mdns_error": None,
    "mdns_ips": [],
    "hotspot_ok": False,
    "hotspot_error": None,
    "hotspot_ip": None,
    "urls": [],
    "viewer_urls": [],
    "tesla_url": None,
    "tesla_https_url": None,
    "ips": [],
}


def _powershell(script: str, timeout: int = 30) -> tuple[str, str, int]:
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stdout.strip(), proc.stderr.strip(), proc.returncode


def _windows_hotspot_on() -> bool:
    if platform.system() != "Windows":
        return False
    ps = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $profile) { 'OFF'; exit 0 }
$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
if ($mgr.TetheringOperationalState -eq 'On') { 'ON' } else { 'OFF' }
"""
    out, _, code = _powershell(ps, timeout=15)
    return code == 0 and out.strip().upper().endswith("ON")


def _hotspot_gateway_ip() -> str | None:
    if platform.system() != "Windows":
        return None
    ps = r"""
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.137.*' } |
  Select-Object -First 1 -ExpandProperty IPAddress)
if ($ip) { $ip } else { '' }
"""
    out, _, code = _powershell(ps, timeout=10)
    if code == 0 and out and out.strip():
        return out.strip().splitlines()[0].strip()
    return None


def _hotspot_adapter_info() -> dict[str, Any] | None:
    """Hotspot virtual adapter IP + interface index (needed for mDNS bind on Windows)."""
    if platform.system() != "Windows":
        return None
    ps = r"""
$row = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.137.*' } |
  Select-Object -First 1 IPAddress, InterfaceIndex, InterfaceAlias
if (-not $row) { exit 1 }
Write-Output ($row.IPAddress + '|' + $row.InterfaceIndex + '|' + $row.InterfaceAlias)
"""
    out, _, code = _powershell(ps, timeout=10)
    if code != 0 or not out:
        return None
    parts = out.strip().split("|")
    if len(parts) < 2:
        return None
    try:
        return {
            "ip": parts[0].strip(),
            "ifindex": int(parts[1].strip()),
            "alias": parts[2].strip() if len(parts) > 2 else "",
        }
    except ValueError:
        return None


def _primary_lan_ip() -> str | None:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


def _local_ips() -> list[str]:
    ips: list[str] = []
    seen: set[str] = set()

    def add(ip: str | None) -> None:
        if not ip or ip.startswith("127.") or ip in seen:
            return
        seen.add(ip)
        ips.append(ip)

    hotspot = _hotspot_gateway_ip()
    if hotspot:
        add(hotspot)

    if platform.system() == "Windows":
        ps = r"""
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } |
  ForEach-Object { $_.IPAddress }
"""
        out, _, code = _powershell(ps, timeout=12)
        if code == 0 and out:
            for line in out.splitlines():
                add(line.strip())

    primary = _primary_lan_ip()
    if primary:
        add(primary)

    return ips


def _mdns_publish_targets() -> tuple[list[str], Any, str | None]:
    """
    Return (A-record IPs, zeroconf interfaces, error hint).
    Hotspot clients must see only 192.168.137.1 — extra LAN IPs break .local on phones.
    """
    try:
        from zeroconf import InterfaceChoice
    except ImportError:
        return [], InterfaceChoice.All, "zeroconf not installed"

    adapter = _hotspot_adapter_info()
    hotspot_on = _windows_hotspot_on()

    if adapter and adapter.get("ip"):
        ip = str(adapter["ip"])
        log.debug("mDNS hotspot bind %s (ifindex %s)", ip, adapter.get("ifindex"))
        return [ip], [ip], None

    if hotspot_on:
        # Tethering up but gateway IP not assigned yet — wait for refresh loop.
        return [], [ip for ip in [_hotspot_gateway_ip()] if ip] or InterfaceChoice.All, "hotspot starting"

    primary = _primary_lan_ip()
    if primary:
        return [primary], InterfaceChoice.All, None

    ips = _local_ips()[:1]
    if ips:
        return ips, InterfaceChoice.All, None
    return [], InterfaceChoice.All, "no LAN IP"


def _build_urls(port: int) -> tuple[list[str], list[str], str | None, str | None]:
    ips = _local_ips()
    hotspot = _hotspot_gateway_ip() or "192.168.137.1"
    if hotspot not in ips:
        ips.insert(0, hotspot)

    viewer_urls: list[str] = []
    all_urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            viewer_urls.append(url)

    # Tesla HW4: HTTPS :443 first, then HTTP :80 — never :8080 or .local
    tesla_https = f"https://{hotspot}/"
    tesla_url = f"http://{hotspot}/"
    add(tesla_https)
    add(tesla_url)
    add(f"https://{hotspot}/whereami")
    add(f"http://{hotspot}/whereami")
    add(f"http://{hotspot}:{port}{_VIEWER_PATH}")
    add(f"http://{_HOSTNAME}.local:{port}{_VIEWER_PATH}")

    for ip in ips:
        if ip == hotspot:
            continue
        add(f"http://{ip}:{port}{_VIEWER_PATH}")

    for u in viewer_urls:
        all_urls.append(u)
    return all_urls, viewer_urls, tesla_url, tesla_https


def _stop_mdns() -> None:
    global _mdns_service
    if not _mdns_service:
        return
    zc, info = _mdns_service
    try:
        zc.unregister_service(info)
        zc.close()
    except Exception:
        pass
    _mdns_service = None


def _start_mdns(port: int) -> bool:
    global _mdns_service
    _stop_mdns()
    try:
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
    except ImportError:
        msg = "zeroconf not installed — pip install zeroconf for whereami.local"
        log.warning(msg)
        _hotspot_state["mdns_error"] = msg
        return False

    ips, interfaces, hint = _mdns_publish_targets()
    if not ips:
        _hotspot_state["mdns_ips"] = []
        _hotspot_state["mdns_error"] = hint or "no IP for mDNS"
        log.debug("mDNS deferred: %s", _hotspot_state["mdns_error"])
        return False

    addrs: list[bytes] = []
    for ip in ips:
        try:
            addrs.append(socket.inet_aton(ip))
        except OSError:
            pass
    if not addrs:
        _hotspot_state["mdns_error"] = "invalid mDNS addresses"
        return False

    try:
        zc = Zeroconf(ip_version=IPVersion.V4Only, interfaces=interfaces)
        info = ServiceInfo(
            "_http._tcp.local.",
            f"{_HOSTNAME}._http._tcp.local.",
            addresses=addrs,
            port=port,
            properties={"path": _VIEWER_PATH.encode("utf-8")},
            server=f"{_HOSTNAME}.local.",
        )
        zc.register_service(info, cooperating_responders=True)
        _mdns_service = (zc, info)
        _hotspot_state["mdns_ips"] = ips
        _hotspot_state["mdns_error"] = None
        log.info(
            "mDNS viewer on %s → http://%s.local:%d%s",
            ", ".join(ips),
            _HOSTNAME,
            port,
            _VIEWER_PATH,
        )
        return True
    except Exception as e:
        _hotspot_state["mdns_error"] = str(e)
        log.warning("mDNS registration failed: %s", e)
        return False


def _ensure_firewall_rules(port: int) -> None:
    if platform.system() != "Windows":
        return
    tcp_ports = sorted({port, 80, 443})
    tcp_rules = []
    for p in tcp_ports:
        tcp_rules.append(
            f"  @{{ Name='StratoPi TCP {p}'; Proto='TCP'; Port={p}; Remote=$null }},"
        )
        tcp_rules.append(
            f"  @{{ Name='StratoPi Hotspot TCP {p}'; Proto='TCP'; Port={p}; "
            f"Remote='192.168.137.0/24' }},"
        )
    tcp_block = "`n".join(tcp_rules)
    ps = f"""
$rules = @(
{tcp_block}
  @{{ Name='StratoPi mDNS UDP'; Proto='UDP'; Port=5353; Remote=$null }},
  @{{ Name='StratoPi mDNS hotspot'; Proto='UDP'; Port=5353; Remote='192.168.137.0/24' }}
)
foreach ($r in $rules) {{
  if (-not (Get-NetFirewallRule -DisplayName $r.Name -ErrorAction SilentlyContinue)) {{
    $params = @{{
      DisplayName = $r.Name
      Direction = 'Inbound'
      Action = 'Allow'
      Protocol = $r.Proto
      LocalPort = $r.Port
      Profile = 'Any'
      Enabled = 'True'
    }}
    if ($r.Remote) {{ $params.RemoteAddress = $r.Remote }}
    New-NetFirewallRule @params | Out-Null
  }}
}}
"""
    _powershell(ps, timeout=25)


def _ensure_hotspot_network_private() -> None:
    """Public profile blocks multicast — hotspot adapter should be Private."""
    if platform.system() != "Windows":
        return
    ps = r"""
$ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like '192.168.137.*' } |
  Select-Object -First 1 -ExpandProperty InterfaceAlias
if (-not $ip) { exit 0 }
Get-NetConnectionProfile -InterfaceAlias $ip -ErrorAction SilentlyContinue |
  Set-NetConnectionProfile -NetworkCategory Private -ErrorAction SilentlyContinue
"""
    _powershell(ps, timeout=15)


def _start_windows_hotspot() -> tuple[bool, str | None]:
    if platform.system() != "Windows":
        return False, "Hotspot only supported on Windows"
    ps = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  $netTask.Result
}
[Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime] | Out-Null
[Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime] | Out-Null
$profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
if (-not $profile) { Write-Output 'NO_INTERNET_PROFILE'; exit 2 }
$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
if ($mgr.TetheringOperationalState -eq 'On') { Write-Output 'ALREADY_ON'; exit 0 }
$r = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
if ($r.Status -ne 'Success') { Write-Output ('FAIL:' + $r.Status); exit 3 }
Write-Output 'STARTED'
"""
    out, err, code = _powershell(ps, timeout=45)
    if code == 0:
        return True, None
    detail = out or err or f"exit {code}"
    if "NO_INTERNET_PROFILE" in detail:
        return False, "No active internet — enable Wi‑Fi or Ethernet first"
    return False, detail


def _refresh_state(port: int) -> dict[str, Any]:
    urls, viewer_urls, tesla_url, tesla_https = _build_urls(port)
    _hotspot_state["ips"] = _local_ips()
    _hotspot_state["hotspot_ip"] = _hotspot_gateway_ip()
    _hotspot_state["urls"] = urls
    _hotspot_state["viewer_urls"] = viewer_urls
    _hotspot_state["tesla_url"] = tesla_url
    _hotspot_state["tesla_https_url"] = tesla_https
    return dict(_hotspot_state)


def _mdns_refresh_loop(port: int) -> None:
    """Re-register mDNS when hotspot adapter + 192.168.137.1 appear."""
    for attempt in range(18):
        time.sleep(5)
        _ensure_hotspot_network_private()
        if _start_mdns(port):
            _hotspot_state["mdns_ok"] = True
            st = _refresh_state(port)
            for u in st.get("viewer_urls", [])[:3]:
                log.info("Share map (hotspot): %s", u)
            return
        log.debug("mDNS retry %d/18: %s", attempt + 1, _hotspot_state.get("mdns_error"))
    log.warning(
        "mDNS never bound to hotspot — use IP fallback: http://192.168.137.1:%d%s",
        port,
        _VIEWER_PATH,
    )


def start_whereami_host(port: int, *, hotspot: bool = True) -> dict[str, Any]:
    """Start hotspot + mDNS; Tesla uses https://192.168.137.1/ (not :8080)."""
    if platform.system() == "Windows":
        _ensure_firewall_rules(port)

    if hotspot:
        ok, err = _start_windows_hotspot()
        _hotspot_state["hotspot_ok"] = ok
        _hotspot_state["hotspot_error"] = err
        if ok:
            log.info("Windows mobile hotspot enabled — phones use /whereami")
            _ensure_hotspot_network_private()
            threading.Thread(target=_mdns_refresh_loop, args=(port,), daemon=True).start()
        else:
            log.warning("Hotspot not started: %s", err)

    # Hotspot adapter often appears 2–8s after tether start.
    wait_s = 8 if _hotspot_state.get("hotspot_ok") else 0
    if wait_s:
        for _ in range(wait_s):
            if _hotspot_adapter_info():
                break
            time.sleep(1)
        _ensure_hotspot_network_private()

    _hotspot_state["mdns_ok"] = _start_mdns(port)
    st = _refresh_state(port)
    for u in st.get("viewer_urls", []):
        log.info("Map viewer: %s", u)
    if _hotspot_state.get("tesla_https_url"):
        log.info("Tesla browser: %s", _hotspot_state["tesla_https_url"])
    elif _hotspot_state.get("tesla_url"):
        log.info("Tesla browser: %s", _hotspot_state["tesla_url"])
    if not _hotspot_state["mdns_ok"] and _hotspot_state.get("hotspot_ok"):
        log.info(
            "mDNS pending — use http://%s/ for Tesla, or http://%s:%d%s for phones",
            _hotspot_state.get("hotspot_ip") or "192.168.137.1",
            _hotspot_state.get("hotspot_ip") or "192.168.137.1",
            port,
            _VIEWER_PATH,
        )
    return st


def get_network_status(port: int = 5001) -> dict[str, Any]:
    return _refresh_state(port)


def stop_whereami_host() -> None:
    _stop_mdns()