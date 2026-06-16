"""
AES-GCM encryption for StratoPi LoRa UKHAS frames.
Keep in sync with repo root lora_crypto.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
from pathlib import Path

log = logging.getLogger("lora_crypto")

ENCRYPT_MARKER = "E"
_NONCE_LEN = 12

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except ImportError:
    AESGCM = None  # type: ignore
    _HAS_CRYPTO = False

_CRC_RE = re.compile(r"^\$\$(.+)\*([0-9A-Fa-f]{2})$")


def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(s: str) -> bytes:
    pad = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


def algorithm_for_key(key: bytes) -> str:
    if len(key) == 16:
        return "AES-128-GCM"
    if len(key) == 32:
        return "AES-256-GCM"
    raise ValueError(f"Key must be 16 or 32 bytes, got {len(key)}")


def parse_key_hex(key_hex: str) -> bytes:
    key_hex = (key_hex or "").strip().replace(" ", "")
    if not key_hex:
        raise ValueError("key_hex is empty")
    key = bytes.fromhex(key_hex)
    algorithm_for_key(key)
    return key


class LoRaCrypto:
    def __init__(self, key: bytes):
        if not _HAS_CRYPTO:
            raise RuntimeError(
                "cryptography package required — pip install cryptography"
            )
        self._key = key
        self._aes = AESGCM(key)
        self.algorithm = algorithm_for_key(key)

    def encrypt_body(self, plaintext_body: str) -> str:
        callsign = plaintext_body.split(",", 1)[0]
        nonce = os.urandom(_NONCE_LEN)
        aad = callsign.encode("ascii")
        ct = self._aes.encrypt(nonce, plaintext_body.encode("utf-8"), aad)
        return f"{callsign},{ENCRYPT_MARKER},{b64u_encode(nonce)},{b64u_encode(ct)}"

    def decrypt_body(self, envelope_body: str) -> str | None:
        parts = envelope_body.split(",")
        if len(parts) != 4 or parts[1] != ENCRYPT_MARKER:
            return None
        callsign, _, nonce_s, ct_s = parts
        try:
            nonce = b64u_decode(nonce_s)
            ct = b64u_decode(ct_s)
            plain = self._aes.decrypt(nonce, ct, callsign.encode("ascii"))
            return plain.decode("utf-8")
        except Exception:
            return None

    def seal_packet(self, packet: str) -> str:
        m = _CRC_RE.match(packet.strip())
        if not m:
            return packet
        body = m.group(1)
        enc = self.encrypt_body(body)
        return f"$${enc}*{_crc_xor(enc)}"

    def open_packet(self, packet: str) -> str | None:
        raw = packet.strip()
        m = _CRC_RE.match(raw)
        if not m:
            return None
        body, rx_crc = m.group(1), m.group(2).upper()
        if _crc_xor(body) != rx_crc:
            return None
        if body.split(",")[1:2] != [ENCRYPT_MARKER]:
            return raw
        inner = self.decrypt_body(body)
        if inner is None:
            return None
        return f"$${inner}*{_crc_xor(inner)}"


DEFAULT_CRYPTO_CONFIG = {
    "enabled": True,
    "key_hex": "",
}


def key_fingerprint(key_hex: str) -> str | None:
    key_hex = (key_hex or "").strip().replace(" ", "")
    if not key_hex:
        return None
    try:
        raw = bytes.fromhex(key_hex)
    except ValueError:
        return None
    return hashlib.sha256(raw).hexdigest()[:16]


def ensure_crypto_key(cfg: dict) -> tuple[dict, bool]:
    cfg = dict(cfg)
    if cfg.get("key_hex"):
        return cfg, False
    cfg["key_hex"] = secrets.token_hex(16)
    return cfg, True


def save_crypto_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_crypto_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CRYPTO_CONFIG)
    env_key = os.environ.get("STRATOPI_LORA_KEY_HEX", "").strip()
    if env_key:
        cfg["key_hex"] = env_key
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            cfg.update({k: data[k] for k in DEFAULT_CRYPTO_CONFIG if k in data})
            if "key_hex" in data and data["key_hex"]:
                cfg["key_hex"] = str(data["key_hex"]).strip()
    except Exception as e:
        log.warning("Crypto config load failed (%s)", e)
    cfg["enabled"] = bool(cfg.get("enabled"))
    return cfg


def crypto_from_config(cfg: dict) -> LoRaCrypto | None:
    if not cfg.get("enabled"):
        return None
    if not _HAS_CRYPTO:
        log.error("Encryption enabled but cryptography is not installed")
        return None
    try:
        key = parse_key_hex(str(cfg.get("key_hex", "")))
        return LoRaCrypto(key)
    except Exception as e:
        log.error("Invalid crypto key: %s", e)
        return None


def crypto_status(cfg: dict) -> dict:
    key_hex = str(cfg.get("key_hex", "")).replace(" ", "")
    key_len = 0
    if key_hex:
        try:
            key_len = len(bytes.fromhex(key_hex))
        except ValueError:
            key_len = 0
    algo = None
    if key_len in (16, 32):
        algo = algorithm_for_key(bytes.fromhex(key_hex))
    fp = key_fingerprint(key_hex) if key_len in (16, 32) else None
    return {
        "enabled": bool(cfg.get("enabled")),
        "key_configured": key_len in (16, 32),
        "key_fingerprint": fp,
        "algorithm": algo,
        "library_ok": _HAS_CRYPTO,
    }