#!/usr/bin/env python3
"""
SX1268 SPI probe — run on the Pi.
Tests SPI communication with the Waveshare SX1268 433M LoRa HAT.

Standard Waveshare SX126x HAT pin mapping (BCM):
  SPI  : MOSI=10  MISO=9  SCK=11  NSS/CS=8  (SPI0)
  RESET: GPIO18
  BUSY : GPIO24
  DIO1 : GPIO16
  TXEN : GPIO21
  RXEN : GPIO20
"""

import spidev
import RPi.GPIO as GPIO
import time

# ── Pin config ────────────────────────────────────────────────────────────────
PIN_RESET = 18
PIN_BUSY  = 24
PIN_DIO1  = 16
PIN_TXEN  = 21
PIN_RXEN  = 20

SPI_BUS   = 0
SPI_DEV   = 0
SPI_SPEED = 2_000_000   # 2 MHz (SX1268 supports up to 16 MHz)

# ── SX1268 opcodes ────────────────────────────────────────────────────────────
CMD_GET_STATUS       = 0xC0
CMD_READ_REGISTER    = 0x1D
CMD_WRITE_REGISTER   = 0x0D
CMD_GET_DEVICE_ERRORS= 0x17
REG_LORA_SYNC_WORD   = 0x0740  # should read 0x14 (private) or 0x34 (public)

def setup():
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    for pin in (PIN_RESET, PIN_TXEN, PIN_RXEN):
        GPIO.setup(pin, GPIO.OUT)
    for pin in (PIN_BUSY, PIN_DIO1):
        GPIO.setup(pin, GPIO.IN)

    # Hardware reset
    GPIO.output(PIN_RESET, GPIO.LOW)
    time.sleep(0.01)
    GPIO.output(PIN_RESET, GPIO.HIGH)
    time.sleep(0.01)

    # Wait for BUSY to go low
    deadline = time.time() + 2.0
    while GPIO.input(PIN_BUSY) and time.time() < deadline:
        time.sleep(0.001)
    if GPIO.input(PIN_BUSY):
        print("WARNING: BUSY still high after reset — check wiring")
    else:
        print("BUSY went LOW after reset — chip responded!")

    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEV)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 0
    return spi


def wait_busy():
    deadline = time.time() + 1.0
    while GPIO.input(PIN_BUSY) and time.time() < deadline:
        time.sleep(0.001)


def get_status(spi):
    wait_busy()
    r = spi.xfer2([CMD_GET_STATUS, 0x00])
    return r[1]


def read_register(spi, addr):
    wait_busy()
    msb = (addr >> 8) & 0xFF
    lsb = addr & 0xFF
    r = spi.xfer2([CMD_READ_REGISTER, msb, lsb, 0x00, 0x00])
    return r[4]


def main():
    print("=== SX1268 SPI Diagnostic ===\n")
    setup_ok = False
    spi = None

    try:
        spi = setup()
        setup_ok = True
    except Exception as e:
        print(f"Setup failed: {e}")

    if not setup_ok:
        return

    # ── Test 1: GetStatus ─────────────────────────────────────────────────────
    try:
        status = get_status(spi)
        chip_mode   = (status >> 4) & 0x07
        cmd_status  = (status >> 1) & 0x07
        mode_names  = {2:"STBY_RC", 3:"STBY_XOSC", 4:"FS", 5:"RX", 6:"TX"}
        print(f"GetStatus: 0x{status:02X} "
              f"chip_mode={chip_mode}({mode_names.get(chip_mode,'?')}) "
              f"cmd_status={cmd_status}")
        if status in (0x00, 0xFF):
            print("  WARNING: status is 0x00 or 0xFF — check SPI wiring (MOSI/MISO/CLK/CS)")
        else:
            print("  -> SPI communication confirmed!")
    except Exception as e:
        print(f"GetStatus failed: {e}")

    # ── Test 2: Read sync word register ───────────────────────────────────────
    try:
        sync = read_register(spi, REG_LORA_SYNC_WORD)
        print(f"LoRa sync word reg 0x0740: 0x{sync:02X} "
              f"({'0x14=private' if sync==0x14 else '0x34=public' if sync==0x34 else 'unexpected'})")
        if sync not in (0x14, 0x34, 0x00, 0xFF):
            print("  -> Valid register read, chip is alive!")
    except Exception as e:
        print(f"ReadRegister failed: {e}")

    # ── Test 3: BUSY pin behaviour ────────────────────────────────────────────
    busy_val = GPIO.input(PIN_BUSY)
    print(f"\nBUSY pin: {'HIGH (chip processing)' if busy_val else 'LOW (chip idle)'}")
    print(f"DIO1 pin: {GPIO.input(PIN_DIO1)}")

    if spi:
        spi.close()
    GPIO.cleanup()
    print("\nDone.")


if __name__ == "__main__":
    main()
