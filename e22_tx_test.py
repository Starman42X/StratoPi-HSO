#!/usr/bin/env python3
"""
Transparent TX mode test.
Does NOT need AT commands — just writes bytes to UART in normal mode.
The E22 transmits whatever it receives on UART as a LoRa packet automatically.

Watch SDR# (or any SDR software) at 430–435 MHz for a LoRa chirp signal.
LoRa looks like diagonal sweeping lines in the waterfall display.
"""
import serial, time, sys

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    GPIO.setup(22, GPIO.OUT)
    GPIO.setup(27, GPIO.OUT)
    GPIO.output(22, GPIO.LOW)   # M0=LOW
    GPIO.output(27, GPIO.LOW)   # M1=LOW  → Normal transparent TX mode
    print("GPIO: M0=LOW M1=LOW (normal/transparent TX mode)")
    time.sleep(0.3)
except ImportError:
    print("No RPi.GPIO — assuming module already in TX mode (both jumpers removed)")

PORT = "/dev/serial0"

print(f"\nOpening {PORT} @ 9600...")
try:
    ser = serial.Serial(PORT, 9600, timeout=1)
except Exception as e:
    print(f"Cannot open serial port: {e}")
    sys.exit(1)

time.sleep(0.3)

print("\nSending 5 test packets, 10 seconds apart.")
print("WATCH SDR# at 430-435 MHz — look for diagonal chirp lines in waterfall.\n")

for i in range(1, 6):
    msg = f"STRATOPI TEST {i:02d} HELLO FROM HAB\n"
    n   = ser.write(msg.encode())
    ser.flush()
    print(f"  [{i}/5] Wrote {n} bytes: {msg.strip()!r}")
    if i < 5:
        time.sleep(10)

ser.close()

try:
    import RPi.GPIO as GPIO
    GPIO.cleanup()
except Exception:
    pass

print("\nDone. Did you see a chirp in the waterfall?")
print("  YES → Radio works! Only the AT config was broken. Run lora_tx.py.")
print("  NO  → UART not reaching E22, or E22 not powered. Check seating.")
