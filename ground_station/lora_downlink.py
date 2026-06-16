"""Send LoRa config commands on the ground module's open serial port."""

from __future__ import annotations

import logging

import serial

from e22_regs import TX_POWER_DBM
from e22_serial import send_transparent
from lora_cmd import build_cmd_apply

try:
    from lora_crypto import LoRaCrypto
except ImportError:
    LoRaCrypto = None  # type: ignore

log = logging.getLogger("gs.downlink")

MAX_TX_DBM = 22


def send_config_ping(
    ser: serial.Serial,
    *,
    link_channel: int,
    link_air_rate: int,
    target_channel: int,
    target_air_rate: int,
    target_tx_dbm: int,
    tx_dbm: int = MAX_TX_DBM,
    repeats: int = 3,
    crypto: "LoRaCrypto | None" = None,
) -> dict:
    """
    Transparent TX on the shared COM port (caller holds _serial_lock).
    Module must already be configured for link_channel/air_rate and TX power
    (e.g. via config_e22_com.py --power 22 while gs_app is stopped).
    """
    if tx_dbm not in TX_POWER_DBM:
        raise ValueError(f"tx_dbm must be one of {TX_POWER_DBM}")
    if target_tx_dbm not in TX_POWER_DBM:
        raise ValueError(f"target_tx_power_dbm must be one of {TX_POWER_DBM}")

    packet = build_cmd_apply(target_channel, target_air_rate, target_tx_dbm)
    if crypto:
        packet = crypto.seal_packet(packet)
    sent = send_transparent(ser, packet, repeats=repeats, gap_s=0.55)

    log.info(
        "Downlink %d× on CH%d air=%d: %s",
        sent, link_channel, link_air_rate, packet,
    )

    return {
        "ok": sent > 0,
        "packet": packet,
        "repeats": sent,
        "tx_dbm": tx_dbm,
        "link_channel": link_channel,
        "link_air_rate": link_air_rate,
        "target_channel": target_channel,
        "target_air_rate": target_air_rate,
        "target_tx_dbm": target_tx_dbm,
        "message": (
            f"Sent {sent}× config ping on CH{link_channel}/air{link_air_rate} "
            f"→ HAB applies CH{target_channel}/air{target_air_rate}/{target_tx_dbm} dBm"
            if sent
            else "Downlink failed — no bytes sent"
        ),
    }