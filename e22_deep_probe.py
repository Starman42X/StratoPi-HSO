#!/usr/bin/env python3
"""
E22 deep probe — tries every baud rate, every line ending,
and listens for spontaneous output on mode-switch.
Run after removing both M0/M1 jumpers.
"""
import serial, time, sys

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(22, GPIO.OUT)
    GPIO.setup(27, GPIO.OUT)
    # Start TX mode (both LOW) so we can catch the transition
    GPIO.output(22, GPIO.LOW)
    GPIO.output(27, GPIO.LOW)
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False
    print("No RPi.GPIO")

BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200]
PORT  = "/dev/serial0"

# ── Step 1: catch spontaneous output on mode transition ───────────────────────
print("=== Step 1: listen for spontaneous bytes on mode transition ===")
for baud in [9600, 115200]:
    try:
        ser = serial.Serial(PORT, baud, timeout=0.1)
        time.sleep(0.2)
        ser.reset_input_buffer()

        if HAS_GPIO:
            print(f"Switching to config mode at {baud} baud...")
            GPIO.output(22, GPIO.HIGH)
            GPIO.output(27, GPIO.HIGH)

        # Listen for 2 seconds for anything the module sends
        buf = b""
        deadline = time.time() + 2.0
        while time.time() < deadline:
            d = ser.read(64)
            if d:
                buf += d
        if buf:
            print(f"  [{baud}] Spontaneous: hex={buf.hex()} txt={repr(buf)}")
        else:
            print(f"  [{baud}] Nothing received on transition")

        if HAS_GPIO:
            GPIO.output(22, GPIO.LOW)
            GPIO.output(27, GPIO.LOW)
            time.sleep(0.3)
        ser.close()
    except Exception as e:
        print(f"  [{baud}] Error: {e}")

# ── Step 2: try AT at every baud with long wait ───────────────────────────────
print("\n=== Step 2: AT commands at all baud rates (2 s wait each) ===")

if HAS_GPIO:
    GPIO.output(22, GPIO.HIGH)
    GPIO.output(27, GPIO.HIGH)
    print("Config mode: M0=HIGH M1=HIGH")
    time.sleep(1.5)   # extra long wait for E22 to stabilise

for baud in BAUDS:
    try:
        ser = serial.Serial(PORT, baud, timeout=2)
        time.sleep(0.3)
        ser.reset_input_buffer()

        # Try all line ending variants
        for term, label in [(b"\r\n", "CRLF"), (b"\r", "CR"), (b"\n", "LF")]:
            ser.write(b"AT" + term)
            time.sleep(1.5)
            d = ser.read(256)
            if d:
                print(f"  [HIT] baud={baud} term={label}: hex={d.hex()} txt={repr(d)}")
            ser.reset_input_buffer()
        ser.close()
    except Exception as e:
        print(f"  [{baud}] Error: {e}")

print("\n=== Step 3: UART loopback test (no HAT needed) ===")
print("  Temporarily short GPIO14 (TXD, pin 8) to GPIO15 (RXD, pin 10)")
print("  then press Enter...")
input()
try:
    ser = serial.Serial(PORT, 9600, timeout=1)
    ser.reset_input_buffer()
    ser.write(b"HELLO")
    time.sleep(0.3)
    d = ser.read(16)
    if d == b"HELLO":
        print("  LOOPBACK OK — UART TX/RX wiring works")
    elif d:
        print(f"  Partial: {repr(d)}")
    else:
        print("  No loopback — TX or RX pin not wired correctly")
    ser.close()
except Exception as e:
    print(f"  Error: {e}")

if HAS_GPIO:
    GPIO.cleanup()
print("\nDone.")
