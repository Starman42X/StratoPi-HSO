"""LoRa downlink command protocol (ground -> HAB)."""

from __future__ import annotations

import re

CALLSIGN_CMD = "STRATOPI_CMD"
CALLSIGN_ACK = "STRATOPI_ACK"


def _crc_xor(s: str) -> str:
    crc = 0
    for c in s:
        crc ^= ord(c)
    return f"{crc:02X}"


def build_cmd_apply(channel: int, air_rate: int, tx_power_dbm: int) -> str:
    body = f"{CALLSIGN_CMD},APPLY,{int(channel)},{int(air_rate)},{int(tx_power_dbm)}"
    return f"$${body}*{_crc_xor(body)}"


def build_ack_apply(channel: int, air_rate: int, tx_power_dbm: int) -> str:
    body = f"{CALLSIGN_ACK},APPLY,{int(channel)},{int(air_rate)},{int(tx_power_dbm)}"
    return f"$${body}*{_crc_xor(body)}"


def parse_cmd(raw: str) -> dict | None:
    raw = raw.strip()
    m = re.match(r"^\$\$(.+)\*([0-9A-Fa-f]{2})$", raw)
    if not m:
        return None
    body, rx_crc = m.group(1), m.group(2).upper()
    if _crc_xor(body) != rx_crc:
        return None
    parts = body.split(",")
    if len(parts) < 5:
        return None
    if parts[0] != CALLSIGN_CMD or parts[1] != "APPLY":
        return None
    try:
        return {
            "verb": "APPLY",
            "channel": int(parts[2]),
            "air_rate": int(parts[3]),
            "tx_power_dbm": int(parts[4]),
            "raw": raw,
        }
    except ValueError:
        return None


def parse_ack(raw: str) -> dict | None:
    raw = raw.strip()
    m = re.match(r"^\$\$(.+)\*([0-9A-Fa-f]{2})$", raw)
    if not m:
        return None
    body, rx_crc = m.group(1), m.group(2).upper()
    if _crc_xor(body) != rx_crc:
        return None
    parts = body.split(",")
    if len(parts) < 5:
        return None
    if parts[0] != CALLSIGN_ACK or parts[1] != "APPLY":
        return None
    try:
        return {
            "verb": "APPLY",
            "channel": int(parts[2]),
            "air_rate": int(parts[3]),
            "tx_power_dbm": int(parts[4]),
            "raw": raw,
        }
    except ValueError:
        return None