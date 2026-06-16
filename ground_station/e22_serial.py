"""E22 serial helpers — configure, read, transparent TX (ground COM module)."""

from __future__ import annotations

import time
import logging

import serial

from e22_regs import REG3_RSSI_ENABLE, TX_POWER_DBM, power_reg, REG_TO_DBM

log = logging.getLogger("e22")

BAUD = 9600


def build_config_frame(
    channel: int,
    air_rate: int,
    tx_dbm: int,
    *,
    rssi_prefix: bool,
) -> bytes:
    reg0 = (0b011 << 5) | (0b00 << 3) | (int(air_rate) & 0b111)
    reg1 = (0b00 << 6) | power_reg(int(tx_dbm))
    reg2 = int(channel) & 0xFF
    reg3 = REG3_RSSI_ENABLE if rssi_prefix else 0x00
    params = bytes([0x00, 0x00, 0x00, reg0, reg1, reg2, reg3, 0x00, 0x00])
    return bytes([0xC0, 0x00, 0x09]) + params


def enter_config(ser: serial.Serial, *, use_dtr: bool, use_rts: bool) -> None:
    if use_dtr:
        ser.setDTR(True)
    if use_rts:
        ser.setRTS(True)
    if use_dtr or use_rts:
        time.sleep(0.65)


def exit_config(ser: serial.Serial, *, use_dtr: bool, use_rts: bool) -> None:
    if use_dtr:
        ser.setDTR(False)
    if use_rts:
        ser.setRTS(False)
    if use_dtr or use_rts:
        time.sleep(0.25)


def countdown_config_prompt(seconds: int, entering: bool) -> None:
    if seconds <= 0:
        return
    msg = (
        "Strap M1 to 3.3V (M0 on GND) — config mode in:"
        if entering
        else "Remove M1 strap (M1=GND for RX) in:"
    )
    log.info(msg)
    for n in range(seconds, 0, -1):
        log.info("  %ds…", n)
        time.sleep(1)


def write_config(
    ser: serial.Serial,
    *,
    channel: int,
    air_rate: int,
    tx_dbm: int,
    rssi_prefix: bool,
    use_dtr: bool = False,
    use_rts: bool = False,
    countdown: int = 0,
    entering: bool = True,
) -> bool:
    """Write E22 registers. Requires M1=HIGH unless DTR/RTS wired to M1."""
    if not (use_dtr or use_rts):
        countdown_config_prompt(countdown, entering)
    enter_config(ser, use_dtr=use_dtr, use_rts=use_rts)
    ser.reset_input_buffer()
    frame = build_config_frame(channel, air_rate, tx_dbm, rssi_prefix=rssi_prefix)
    ser.write(frame)
    ser.flush()
    time.sleep(0.55)
    resp = ser.read(ser.in_waiting or 12)
    ok = resp[:3] == bytes([0xC1, 0x00, 0x09]) and resp[3:12] == frame[3:]
    exit_config(ser, use_dtr=use_dtr, use_rts=use_rts)
    if not ok and resp:
        log.warning("E22 config unexpected response: %s", resp.hex(" "))
    elif not ok:
        log.warning("E22 config no response — is M1 HIGH? (strap to 3.3V or use --use-dtr)")
    return ok


def read_config(
    ser: serial.Serial,
    *,
    use_dtr: bool = False,
    use_rts: bool = False,
    countdown: int = 0,
) -> dict | None:
    if not (use_dtr or use_rts):
        countdown_config_prompt(countdown, True)
    enter_config(ser, use_dtr=use_dtr, use_rts=use_rts)
    ser.reset_input_buffer()
    ser.write(bytes([0xC1, 0x00, 0x09]))
    ser.flush()
    time.sleep(0.45)
    resp = ser.read(ser.in_waiting or 12)
    exit_config(ser, use_dtr=use_dtr, use_rts=use_rts)
    if len(resp) < 12 or resp[0] != 0xC1:
        return None
    reg0, reg1, reg2, reg3 = resp[6], resp[7], resp[8], resp[9]
    return {
        "channel": reg2,
        "air_rate": reg0 & 0b111,
        "tx_dbm": REG_TO_DBM.get(reg1 & 0b11),
        "rssi_prefix": bool(reg3 & REG3_RSSI_ENABLE),
        "freq_mhz": round(410.125 + reg2, 3),
    }


def configure_rx_module(
    port: str,
    *,
    channel: int = 24,
    air_rate: int = 3,
    tx_dbm: int = 22,
    use_dtr: bool = False,
    use_rts: bool = False,
    countdown: int = 12,
) -> dict:
    """Configure ground module for RX + RSSI + TX power (closes nothing — caller owns port)."""
    with serial.Serial(port, BAUD, timeout=1) as ser:
        ok = write_config(
            ser,
            channel=channel,
            air_rate=air_rate,
            tx_dbm=tx_dbm,
            rssi_prefix=True,
            use_dtr=use_dtr,
            use_rts=use_rts,
            countdown=countdown,
            entering=True,
        )
        if not (use_dtr or use_rts):
            countdown_config_prompt(countdown, False)
        return {
            "ok": ok,
            "channel": channel,
            "air_rate": air_rate,
            "tx_dbm": tx_dbm,
            "rssi_prefix": True,
            "message": (
                f"{'CONFIRMED' if ok else 'FAILED'}: CH{channel} air={air_rate} "
                f"{tx_dbm} dBm, RSSI ON — M1 must be GND for RX"
            ),
        }


def send_transparent(
    ser: serial.Serial,
    payload: str,
    *,
    repeats: int = 1,
    gap_s: float = 0.45,
) -> int:
    ser.reset_input_buffer()
    sent = 0
    raw = (payload.rstrip("\n") + "\n").encode("ascii")
    for i in range(max(1, repeats)):
        ser.write(raw)
        ser.flush()
        sent += 1
        if i + 1 < repeats:
            time.sleep(gap_s)
    return sent