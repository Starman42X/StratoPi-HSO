"""Reach Pi server on home Wi‑Fi, Pi hotspot, or mDNS — always probe all routes."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import socket
import time
import urllib.error
import urllib.request
from typing import Callable

# Force IPv4 — Windows prefers IPv6 for stratopi.local but Flask listens IPv4-only.
_IP_V4_FAMILY = socket.AF_INET

log = logging.getLogger("gs.pi")

CANONICAL_PI_URL = "http://stratopi.local:8080"

# Known Pi endpoints (hotspot gateway, home static IP, mDNS names)
PI_URL_CANDIDATES = (
    CANONICAL_PI_URL,
    "http://stratopi:8080",
    "http://192.168.4.1:8080",
    "http://10.42.0.1:8080",
    "http://raspberrypi.local:8080",
)

_PI_HOSTNAMES = ("stratopi.local", "stratopi", "raspberrypi.local")

_CACHE_TTL_S = 25
_cache: dict = {"url": None, "ts": 0.0, "failures": 0}


def _local_network_hints() -> list[str]:
    """Prefer Pi URL matching the network we are currently on."""
    hints: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        if local_ip.startswith("192.168.4."):
            hints.append("http://192.168.4.1:8080")
        elif local_ip.startswith("10.42."):
            hints.append("http://10.42.0.1:8080")
        for ip in _resolve_hostnames_ipv4():
            hints.append(f"http://{ip}:8080")
    except OSError:
        pass
    return hints


def _resolve_hostnames_ipv4() -> list[str]:
    """mDNS A records only — avoids broken IPv6 reachability on Windows."""
    ips: list[str] = []
    seen: set[str] = set()
    for host in _PI_HOSTNAMES:
        try:
            for info in socket.getaddrinfo(
                host, 8080, _IP_V4_FAMILY, socket.SOCK_STREAM,
            ):
                ip = info[4][0]
                if ip not in seen:
                    seen.add(ip)
                    ips.append(ip)
        except OSError:
            continue
    return ips


def pi_url_candidates(primary: str | None) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def add(u: str | None) -> None:
        if not u:
            return
        u = u.rstrip("/")
        if u in seen:
            return
        seen.add(u)
        urls.append(u)

    cached = _cache.get("url")
    if cached and _cache.get("failures", 0) < 2:
        add(cached)
    for u in _local_network_hints():
        add(u)
    add(primary)
    add(CANONICAL_PI_URL)
    for ip in _resolve_hostnames_ipv4():
        add(f"http://{ip}:8080")
    for u in PI_URL_CANDIDATES:
        add(u)
    return urls


def _probe_url(base: str, timeout: float) -> str | None:
    """Health check over IPv4 (Flask on Pi is 0.0.0.0:8080 only)."""
    path = "/api/health"
    try:
        if "://" in base:
            scheme, rest = base.split("://", 1)
            host_port = rest.split("/", 1)[0]
            if ":" in host_port:
                host, port_s = host_port.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = host_port, 8080
            ips: list[str] = []
            if host.replace(".", "").isdigit() or host.count(".") == 3:
                ips = [host]
            else:
                ips = _resolve_hostnames_ipv4() or []
                try:
                    for info in socket.getaddrinfo(
                        host, port, _IP_V4_FAMILY, socket.SOCK_STREAM,
                    ):
                        ip = info[4][0]
                        if ip not in ips:
                            ips.append(ip)
                except OSError:
                    pass
            for ip in ips:
                if _probe_ipv4_health(ip, port, timeout):
                    return f"http://{ip}:{port}"
            return None
    except Exception:
        pass
    url = f"{base}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {}
            if data.get("ok"):
                return base
    except Exception:
        return None
    return None


def _probe_ipv4_health(ip: str, port: int, timeout: float) -> bool:
    body = (
        f"GET /api/health HTTP/1.1\r\nHost: {ip}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii")
    try:
        with socket.create_connection((ip, port), timeout=timeout) as sock:
            sock.sendall(body)
            sock.settimeout(timeout)
            data = b""
            while len(data) < 512:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\r\n\r\n" in data and (
                    b'"ok"' in data or b" 200 " in data.split(b"\r\n", 1)[0]
                ):
                    break
            status = data.split(b"\r\n", 1)[0] if data else b""
            return b" 200 " in status
    except OSError:
        pass
    return False


def discover_pi_url(
    primary: str | None = None,
    *,
    timeout: float = 2.5,
    force: bool = False,
) -> str | None:
    """Try all Pi URLs in parallel; return first that responds."""
    now = time.time()
    if (
        not force
        and _cache.get("url")
        and _cache.get("failures", 0) == 0
        and now - _cache.get("ts", 0) < _CACHE_TTL_S
    ):
        return _cache["url"]

    candidates = pi_url_candidates(primary)
    found: str | None = None
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(8, len(candidates)),
    ) as pool:
        futures = {
            pool.submit(_probe_url, base, timeout): base
            for base in candidates
        }
        for fut in concurrent.futures.as_completed(futures):
            result = fut.result()
            if result:
                found = result
                break
        for fut in futures:
            fut.cancel()

    if found:
        _cache.update(url=found, ts=now, failures=0)
        log.info("Pi discovered @ %s", found)
    else:
        _cache["failures"] = _cache.get("failures", 0) + 1
        _cache["ts"] = now
        if _cache["failures"] >= 2:
            _cache["url"] = None
        log.debug("Pi discovery failed (%d candidates)", len(candidates))
    return found


def pi_request(
    base_urls: list[str],
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    timeout: int = 6,
    on_url_resolved: Callable[[str], None] | None = None,
    primary: str | None = None,
    _allow_rediscover: bool = True,
) -> tuple[dict | None, dict | None, str | None]:
    """
    Resolve Pi via parallel discovery, then call API.
    Re-discovers automatically when the cached URL stops working.
    """
    base = discover_pi_url(primary or (base_urls[0] if base_urls else None))
    if not base:
        return None, {"error": "Pi unreachable on all networks (try Pi hotspot Strato-HSO)"}, None

    data_payload = None
    headers: dict[str, str] = {}
    if body is not None:
        data_payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    url = f"{base}{path}"
    req = urllib.request.Request(
        url, data=data_payload, headers=headers, method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            data = json.loads(raw) if raw else {}
            if on_url_resolved:
                on_url_resolved(base)
            _cache.update(url=base, ts=time.time(), failures=0)
            return data, None, base
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode())
        except Exception:
            err = {"error": str(e)}
        _cache["failures"] = _cache.get("failures", 0) + 1
        return None, err, base
    except Exception as e:
        _cache["failures"] = _cache.get("failures", 0) + 1
        _cache["url"] = None
        if _allow_rediscover:
            base2 = discover_pi_url(force=True)
            if base2 and base2 != base:
                return pi_request(
                    pi_url_candidates(base2),
                    path,
                    method=method,
                    body=body,
                    timeout=timeout,
                    on_url_resolved=on_url_resolved,
                    primary=base2,
                    _allow_rediscover=False,
                )
        return None, {"error": str(e)}, None


def pi_link_status() -> dict:
    return {
        "canonical": CANONICAL_PI_URL,
        "cached_url": _cache.get("url"),
        "cache_age_s": round(time.time() - _cache.get("ts", 0), 1) if _cache.get("ts") else None,
        "candidates": pi_url_candidates(_cache.get("url")),
        "local_hints": _local_network_hints(),
    }