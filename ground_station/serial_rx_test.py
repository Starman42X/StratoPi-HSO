#!/usr/bin/env python3
"""Listen on COM port and print every line from the LoRa HAT (debug RX)."""
import sys
import time
import argparse
import serial

BAUD = 9600


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('port', nargs='?', default='COM6')
    ap.add_argument('--duration', '-d', type=int, default=0,
                    help='Stop after N seconds (0 = forever)')
    args = ap.parse_args()

    print(f"Listening on {args.port} @ {BAUD} baud — Ctrl-C to stop")
    t0 = time.time()
    buf = ""

    with serial.Serial(args.port, BAUD, timeout=1) as ser:
        while args.duration <= 0 or time.time() - t0 < args.duration:
            chunk = ser.read(256)
            if not chunk:
                continue
            text = chunk.decode('ascii', errors='replace')
            for c in text:
                if c == '\n':
                    line = buf.strip()
                    buf = ""
                    if line:
                        ts = time.strftime('%H:%M:%S')
                        print(f"[{ts}] {line}")
                else:
                    buf += c


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")