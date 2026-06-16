#!/usr/bin/env python3
"""
E22 config test — corrected mode mapping per Waveshare wiki:
  M0=LOW  M1=LOW  → Transparent TX mode
  M0=LOW  M1=HIGH → Configuration / AT-command mode   ← THIS ONE

Hardware state expected:
  M0 jumper to GND  (M0 permanently LOW — per wiki, leave this in)
  B-B jumpers set   (connects Pi UART to E22)
  M1 jumper removed (GPIO27 controls M1)
"""
import serial, time, sys

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    # M0 is fixed LOW by jumper — only M1 needs GPIO
    GPIO.setup(27, GPIO.OUT)
    GPIO.output(27, GPIO.HIGH)   # M1=HIGH → config mode
    print("GPIO27 = HIGH (M1=HIGH, M0=LOW via jumper) → config mode")
    time.sleep(0.8)
except ImportError:
    print("No RPi.GPIO")

ser = serial.Serial("/dev/serial0", 9600, timeout=2)
time.sleep(0.3)

def at(cmd):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    time.sleep(1.0)
    r = ser.read(ser.in_waiting).decode(errors="replace").strip()
    ok = "+OK" in r or (r and "+ERR" not in r)
    print(f"  {'OK' if ok else '!!'} {cmd:35s} -> {r!r}")
    return r

print("\nSending AT commands (config mode)...")
r = at("AT")
if "+OK" not in r:
    print("\nStill no +OK. Trying alternative baud rates...")
    ser.close()
    for baud in [115200, 57600, 19200, 4800]:
        ser2 = serial.Serial("/dev/serial0", baud, timeout=2)
        ser2.reset_input_buffer()
        ser2.write(b"AT\r\n")
        time.sleep(1.0)
        d = ser2.read(256)
        if d:
            print(f"  Response at {baud}: {repr(d)}")
        ser2.close()
    print("\nIf nothing above worked, the radio is transmitting in transparent mode")
    print("with factory defaults. Skip config for now — run lora_tx.py directly.")
else:
    print("\nModule responded! Configuring for HAB mission...")
    at("AT+VER?")
    at("AT+BAND=434200000")    # 434.200 MHz
    at("AT+NETWORKID=18")
    at("AT+ADDRESS=1")
    at("AT+PARAMETER=12,7,1,12")  # SF12 BW125 CR4/5 preamble12
    at("AT+CRFOP=10")              # 10 dBm (German legal limit)
    print("\nVerifying...")
    at("AT+BAND?")
    at("AT+PARAMETER?")
    at("AT+CRFOP?")

print("\nSwitching back to TX mode (GPIO27=LOW)...")
try:
    GPIO.output(27, GPIO.LOW)
    GPIO.cleanup()
except Exception:
    pass

time.sleep(0.3)

print("Sending test packet in transparent mode...")
ser2 = serial.Serial("/dev/serial0", 9600, timeout=1)
pkt = "$$STRATOPI,00001,12:00:00,48.370500,10.873000,100.0,0.0,0.0,0.0,8,1.2,3*XX\n"
ser2.write(pkt.encode())
ser2.flush()
print(f"Sent: {pkt.strip()}")
print("\nWatch SDR# for chirp at 434.200 MHz")
ser2.close()
