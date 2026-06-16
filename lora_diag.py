#!/usr/bin/env python3
"""Comprehensive SX1268 HAT diagnostic — run on the Pi."""
import serial, time, os

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False
    print("WARNING: RPi.GPIO not available")

# All common Waveshare/EBYTE pin assignments (m0, m1)
PIN_CANDIDATES = [
    (22, 27),
    (6,  26),
    (4,  17),
    (24, 25),
    (None, None),   # no GPIO — mode set by physical jumpers
]

BAUDS = [9600, 115200, 57600, 19200, 4800]
PORT  = "/dev/serial0"
AT_CMDS = [b"AT\r\n", b"AT+VER?\r\n", b"AT+ADDRESS?\r\n"]


def set_gpio(m0, m1, m0_val, m1_val):
    if HAS_GPIO and m0 is not None:
        GPIO.setup(m0, GPIO.OUT)
        GPIO.setup(m1, GPIO.OUT)
        GPIO.output(m0, GPIO.LOW if m0_val == 0 else GPIO.HIGH)
        GPIO.output(m1, GPIO.LOW if m1_val == 0 else GPIO.HIGH)
        time.sleep(0.3)


def probe_uart(baud, m0, m1):
    # Try config mode first: M0=0 M1=1
    set_gpio(m0, m1, 0, 1)
    try:
        s = serial.Serial(PORT, baud, timeout=1.5)
        time.sleep(0.3)
        s.reset_input_buffer()
        for cmd in AT_CMDS:
            s.write(cmd)
            time.sleep(0.8)
            d = s.read(256)
            if d:
                s.close()
                return d, "config_mode"
        s.close()
    except Exception as e:
        return None, str(e)

    # Try TX mode: M0=0 M1=0
    set_gpio(m0, m1, 0, 0)
    try:
        s = serial.Serial(PORT, baud, timeout=1.5)
        time.sleep(0.3)
        s.reset_input_buffer()
        for cmd in AT_CMDS:
            s.write(cmd)
            time.sleep(0.8)
            d = s.read(256)
            if d:
                s.close()
                return d, "tx_mode"
        s.close()
    except Exception as e:
        return None, str(e)

    return b"", "no_response"


print("=== UART probe (all baud rates x GPIO combos) ===")
found = False
for m0, m1 in PIN_CANDIDATES:
    for baud in BAUDS:
        label = f"GPIO M0={m0} M1={m1} @ {baud}"
        data, mode = probe_uart(baud, m0, m1)
        if data:
            print(f"[HIT] {label} ({mode})")
            print(f"      hex: {data.hex()}")
            print(f"      txt: {repr(data)}")
            found = True
        else:
            print(f"[   ] {label}: {mode}")

if not found:
    print("\nNo UART response at any combination.")

if HAS_GPIO:
    GPIO.cleanup()

# Check SPI
spi_devs = [f for f in os.listdir("/dev") if f.startswith("spi")]
print(f"\nSPI devices: {spi_devs if spi_devs else 'none — enable with dtparam=spi=on'}")

# Spontaneous data check
print("\n=== Listening for spontaneous bytes on serial0 @ 9600 (3s) ===")
try:
    s = serial.Serial(PORT, 9600, timeout=3)
    d = s.read(256)
    s.close()
    print(f"Got {len(d)} bytes: hex={d.hex()} txt={repr(d)}")
except Exception as e:
    print(f"Error: {e}")
