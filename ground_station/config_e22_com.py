#!/usr/bin/env python3
"""
Configure the PC-side E22 LoRa HAT to match the Pi transmitter.

Wiring (USB-TTL to Waveshare HAT):
  USB TX  -> HAT RXD     USB RX  -> HAT TXD     GND -> GND
  M0 jumper -> GND (leave on)
  M1 for config: jumper to 3.3V OR wire USB adapter RTS/DTR -> M1 pin

Usage:
  python config_e22_com.py COM6
  python config_e22_com.py COM6 --countdown 15
  python config_e22_com.py COM6 --power 10
"""
import sys
import time
import argparse
import serial

from e22_regs import DEFAULT_TX_DBM, REG3_RSSI_ENABLE, power_reg

# Must match lora_tx.py on the Pi
E22_CHANNEL  = 24
E22_AIR_RATE = 0b011   # air=3 -> SF12/BW125 on this module
BAUD         = 9600


def _countdown(msg: str, seconds: int):
    print(msg)
    for n in range(seconds, 0, -1):
        print(f"  {n}s…", end="\r", flush=True)
        time.sleep(1)
    print("  go!   ")


def enter_config(ser, use_dtr: bool, ready: bool, countdown: int):
    if use_dtr:
        ser.setDTR(True)
        ser.setRTS(False)
        print("DTR=HIGH (M1) — config mode")
    elif ready:
        print("Assuming M1 already strapped HIGH (config mode)")
    elif countdown > 0:
        _countdown(
            "Strap M1 to 3.3V now (M0 stays on GND) — config mode in:",
            countdown,
        )
    else:
        print("Manual: strap M1 to 3.3V now (M0 stays GND), press Enter…")
        input()
    time.sleep(0.6)


def exit_config(ser, use_dtr: bool, ready: bool, countdown: int):
    if use_dtr:
        ser.setDTR(False)
    elif ready:
        print("Assuming M1 returned to LOW (transparent RX)")
    elif countdown > 0:
        _countdown(
            "Remove M1 strap (M1=LOW for transparent RX) in:",
            countdown,
        )
    else:
        print("Remove M1 strap (M1=LOW for transparent RX), press Enter…")
        input()
    time.sleep(0.2)
    print("Transparent RX mode (M0=LOW M1=LOW)")


def read_config(port: str, use_dtr: bool, ready: bool, countdown: int) -> bool:
    """Read current E22 registers (M1 must be HIGH)."""
    with serial.Serial(port, BAUD, timeout=1) as ser:
        enter_config(ser, use_dtr, ready, countdown)
        ser.reset_input_buffer()
        ser.write(bytes([0xC1, 0x00, 0x09]))
        ser.flush()
        time.sleep(0.4)
        resp = ser.read(ser.in_waiting or 12)
        exit_config(ser, use_dtr, ready, countdown)

        if len(resp) < 12 or resp[0] != 0xC1:
            print(f"No valid read response ({len(resp)} bytes) — is M1 in config mode?")
            if resp:
                print(f"  raw: {resp.hex(' ')}")
            return False

        reg0, reg1, reg2, reg3 = resp[6], resp[7], resp[8], resp[9]
        air = reg0 & 0b111
        ch = reg2
        rssi_on = bool(reg3 & REG3_RSSI_ENABLE)
        from e22_regs import REG_TO_DBM
        pbits = reg1 & 0b11
        dbm = REG_TO_DBM.get(pbits, '?')
        print(f"CH={ch} (~{410.125 + ch:.3f} MHz)  air={air}  TX={dbm} dBm  RSSI prefix={'ON' if rssi_on else 'OFF'}")
        if not rssi_on:
            print("  → Run without --read to enable RSSI prefix for the signal meter.")
        return True


def configure(port: str, use_dtr: bool, ready: bool, countdown: int, tx_dbm: int) -> bool:
    reg0 = (0b011 << 5) | (0b00 << 3) | E22_AIR_RATE
    reg1 = (0b00 << 6) | power_reg(tx_dbm)
    reg2 = E22_CHANNEL
    reg3 = REG3_RSSI_ENABLE  # prepend RSSI byte on RX for signal meter
    params = bytes([0x00, 0x00, 0x00, reg0, reg1, reg2, reg3, 0x00, 0x00])
    frame  = bytes([0xC0, 0x00, 0x09]) + params

    with serial.Serial(port, BAUD, timeout=1) as ser:
        enter_config(ser, use_dtr, ready, countdown)
        ser.reset_input_buffer()
        ser.write(frame)
        ser.flush()
        time.sleep(0.5)
        resp = ser.read(ser.in_waiting or 12)

        ok = resp[:3] == bytes([0xC1, 0x00, 0x09]) and resp[3:12] == params
        if ok:
            print(
                f"Config CONFIRMED: CH={E22_CHANNEL} (~434.105 MHz), air={E22_AIR_RATE}, "
                f"{tx_dbm} dBm, RSSI prefix ON"
            )
        elif resp:
            print(f"Unexpected response: {resp.hex(' ')} — check M1 wiring")
        else:
            print("No response — M1 likely not in config mode (HIGH)")

        exit_config(ser, use_dtr, ready, countdown)

        ser.reset_input_buffer()
        ser.write(bytes([0xC1, 0x00, 0x09]))
        ser.flush()
        time.sleep(0.3)
        if ser.read(3)[:1] == bytes([0xC1]):
            print("WARNING: M1 still HIGH — strap M1 to GND for transparent RX")

        return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('port', nargs='?', default='COM6')
    ap.add_argument('--use-dtr', action='store_true',
                    help='Toggle DTR to drive M1 (if wired)')
    ap.add_argument('--ready', action='store_true',
                    help='Skip prompts — M1 already HIGH, then back to LOW')
    ap.add_argument('--countdown', type=int, default=12,
                    help='Seconds to strap/remove M1 (0 = press Enter)')
    ap.add_argument('--power', type=int, default=DEFAULT_TX_DBM,
                    help='TX power dBm on module (RX config only, default 10)')
    ap.add_argument('--read', action='store_true',
                    help='Read current config (RSSI on/off, CH, air) instead of writing')
    args = ap.parse_args()
    if args.read:
        print(f"Reading E22 on {args.port} @ {BAUD} baud…")
        ok = read_config(args.port, args.use_dtr, args.ready, args.countdown)
    else:
        print(f"Configuring E22 on {args.port} @ {BAUD} baud…")
        ok = configure(args.port, args.use_dtr, args.ready, args.countdown, args.power)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()