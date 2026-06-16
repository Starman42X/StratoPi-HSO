#!/usr/bin/env python3
"""
Find the actual SX1268 RESET and BUSY pins by brute-force.
Tries each candidate GPIO as RESET, checks if BUSY (all candidates) goes LOW.
Run on the Pi.
"""
import spidev
import RPi.GPIO as GPIO
import time

# Candidate pins to check
RESET_CANDIDATES = [4, 17, 18, 22, 24, 25, 27]
BUSY_CANDIDATES  = [4, 17, 18, 22, 24, 25, 27]
SPI_CS_CANDIDATES= [7, 8]   # CE1=7, CE0=8

# Standard SPI0 pins (fixed)
SPI_BUS, SPI_SPEED = 0, 1_000_000


def read_all_gpio_states(pins):
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    states = {}
    for p in pins:
        try:
            GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
            states[p] = GPIO.input(p)
        except:
            states[p] = '?'
    return states


def try_reset(reset_pin, busy_pin, cs_pin):
    """Assert RESET on reset_pin, check if busy_pin goes LOW."""
    GPIO.setup(reset_pin, GPIO.OUT)
    GPIO.setup(busy_pin,  GPIO.IN)
    GPIO.setup(cs_pin,    GPIO.OUT)
    GPIO.output(cs_pin, GPIO.HIGH)   # deselect chip during reset

    # Reset sequence
    GPIO.output(reset_pin, GPIO.LOW)
    time.sleep(0.02)
    GPIO.output(reset_pin, GPIO.HIGH)

    # Wait up to 100ms for BUSY to go LOW
    deadline = time.time() + 0.15
    while GPIO.input(busy_pin) and time.time() < deadline:
        time.sleep(0.001)

    busy_after = GPIO.input(busy_pin)
    return not busy_after   # True = BUSY went LOW = reset worked


def try_spi(cs_pin):
    """Try SPI GetStatus with given CS pin."""
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, 0 if cs_pin == 8 else 1)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 0

    # GetStatus = 0xC0
    r = spi.xfer2([0xC0, 0x00])
    spi.close()
    return r[1]


print("=== GPIO State Snapshot (before any changes) ===")
all_pins = list(range(2, 28))
GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)
states_before = {}
for p in all_pins:
    try:
        GPIO.setup(p, GPIO.IN, pull_up_down=GPIO.PUD_OFF)
        states_before[p] = GPIO.input(p)
    except:
        pass

high_pins = [p for p, v in states_before.items() if v == 1]
low_pins  = [p for p, v in states_before.items() if v == 0]
print(f"HIGH: {high_pins}")
print(f"LOW:  {low_pins}")

print("\n=== Scanning for RESET / BUSY pin combination ===")
found_reset = None
found_busy  = None
found_cs    = None

for reset_pin in RESET_CANDIDATES:
    for busy_pin in BUSY_CANDIDATES:
        if busy_pin == reset_pin:
            continue
        for cs_pin in SPI_CS_CANDIDATES:
            try:
                ok = try_reset(reset_pin, busy_pin, cs_pin)
                status = "BUSY WENT LOW ✓" if ok else "busy stayed high"
                print(f"  RESET=GPIO{reset_pin:2d} BUSY=GPIO{busy_pin:2d} CS=CE{0 if cs_pin==8 else 1}: {status}")
                if ok and found_reset is None:
                    found_reset = reset_pin
                    found_busy  = busy_pin
                    found_cs    = cs_pin
                    print(f"  *** FOUND: RESET={reset_pin} BUSY={busy_pin} CS={cs_pin} ***")
            except Exception as e:
                print(f"  RESET={reset_pin} BUSY={busy_pin} CS={cs_pin}: error {e}")
            time.sleep(0.05)

if found_reset is None:
    print("\nNo RESET pin found that drives BUSY LOW.")
    print("Possible causes:")
    print("  1. HAT uses UART not SPI (module MCU controls the SX1268)")
    print("  2. HAT not powered / not seated properly")
    print("  3. Chip needs 3.3V on NRESET rather than active-low pulse")
else:
    print(f"\n=== Testing SPI with found pins (RESET={found_reset} BUSY={found_busy} CS={found_cs}) ===")
    try:
        # Do one more clean reset
        try_reset(found_reset, found_busy, found_cs)
        time.sleep(0.01)
        status_byte = try_spi(found_cs)
        print(f"GetStatus = 0x{status_byte:02X}")
        if status_byte not in (0x00, 0xFF):
            print("SPI communication CONFIRMED - chip is alive!")
        else:
            print("Still 0x00/0xFF - check MISO wiring")
    except Exception as e:
        print(f"SPI test error: {e}")

GPIO.cleanup()
print("\nDone.")
