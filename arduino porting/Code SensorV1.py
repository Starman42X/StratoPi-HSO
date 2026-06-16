import csv
import glob
import os
import struct
import time
from datetime import datetime
from pathlib import Path

import board
import busio
import serial

import adafruit_ahtx0
import adafruit_ens160


# -----------------------------
# Einstellungen
# -----------------------------
INTERVAL_SECONDS = 10

# Datei wird im Home-Ordner des Raspberry Pi gespeichert
CSV_FILE = Path.home() / "Daten Sensoren.csv"

# Plantower am Raspberry Pi UART:
# Plantower TX -> Raspberry Pi RXD / GPIO15 / physischer Pin 10
PLANTOWER_PORT = "/dev/serial0"
PLANTOWER_BAUD = 9600


# -----------------------------
# DS18B20 auslesen
# -----------------------------
def read_ds18b20_sensors():
    sensor_files = glob.glob("/sys/bus/w1/devices/28-*/w1_slave")

    temps = []

    for sensor_file in sensor_files[:2]:
        try:
            with open(sensor_file, "r") as f:
                lines = f.readlines()

            if len(lines) < 2:
                temps.append(-127.0)
                continue

            if "YES" not in lines[0]:
                temps.append(-127.0)
                continue

            pos = lines[1].find("t=")

            if pos == -1:
                temps.append(-127.0)
                continue

            temp_milli_c = int(lines[1][pos + 2:])
            temps.append(temp_milli_c / 1000.0)

        except Exception:
            temps.append(-127.0)

    while len(temps) < 2:
        temps.append(-127.0)

    return temps[0], temps[1]


# -----------------------------
# Plantower auslesen
# -----------------------------
def read_plantower(ser, timeout=3.0):
    start_time = time.time()

    try:
        ser.reset_input_buffer()
    except Exception:
        pass

    while time.time() - start_time < timeout:
        first = ser.read(1)

        if first != b"\x42":
            continue

        second = ser.read(1)

        if second != b"\x4D":
            continue

        frame = ser.read(30)

        if len(frame) != 30:
            continue

        data = b"\x42\x4D" + frame

        checksum_received = struct.unpack(">H", data[30:32])[0]
        checksum_calculated = sum(data[0:30])

        if checksum_received != checksum_calculated:
            continue

        frame_length = struct.unpack(">H", data[2:4])[0]

        if frame_length != 28:
            continue

        # Environmental / atmospheric values
        pm1_env = struct.unpack(">H", data[10:12])[0]
        pm25_env = struct.unpack(">H", data[12:14])[0]
        pm10_env = struct.unpack(">H", data[14:16])[0]

        return pm1_env, pm25_env, pm10_env

    return 9999, 9999, 9999


# -----------------------------
# CSV-Datei vorbereiten
# -----------------------------
def create_csv_if_needed(filename):
    if not filename.exists():
        with open(filename, "w", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow([
                "t",
                "DatumZeit",
                "T",
                "RH",
                "AQI",
                "TVOC",
                "eCO2",
                "PM1",
                "PM25",
                "PM10",
                "DS1",
                "DS2"
            ])


# -----------------------------
# Hauptprogramm
# -----------------------------
def main():
    print("START Raspberry Pi Sensorlogger")

    create_csv_if_needed(CSV_FILE)

    # I2C explizit über die SCL/SDA-Pins des Raspberry Pi:
    # SDA = GPIO2 = physischer Pin 3
    # SCL = GPIO3 = physischer Pin 5
    i2c = busio.I2C(board.SCL, board.SDA)

    # AHT21
    try:
        aht = adafruit_ahtx0.AHTx0(i2c)
        print("AHT OK")
    except Exception as e:
        print("ERR AHT:", e)
        aht = None

    # ENS160
    try:
        ens = adafruit_ens160.ENS160(i2c, address=0x53)
        print("ENS OK")
    except Exception as e:
        print("ERR ENS:", e)
        ens = None

    # Plantower UART
    try:
        pm_ser = serial.Serial(
            PLANTOWER_PORT,
            baudrate=PLANTOWER_BAUD,
            timeout=1
        )
        print("PM UART OK")
    except Exception as e:
        print("ERR PM UART:", e)
        pm_ser = None

    start_time = time.time()

    print("READY")
    print("t;DatumZeit;T;RH;AQI;TVOC;eCO2;PM1;PM25;PM10;DS1;DS2")

    try:
        while True:
            loop_start = time.time()

            t_seconds = int(loop_start - start_time)
            now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # AHT21
            if aht is not None:
                try:
                    temp = float(aht.temperature)
                    rh = float(aht.relative_humidity)
                except Exception:
                    temp = -999.0
                    rh = -999.0
            else:
                temp = -999.0
                rh = -999.0

            # ENS160
            if ens is not None:
                try:
                    if temp > -100 and rh > -100:
                        ens.temperature_compensation = temp
                        ens.humidity_compensation = rh
                    else:
                        ens.temperature_compensation = 20.0
                        ens.humidity_compensation = 50.0

                    aqi = ens.AQI
                    tvoc = ens.TVOC
                    eco2 = ens.eCO2

                    if aqi is None:
                        aqi = -1
                    if tvoc is None:
                        tvoc = -1
                    if eco2 is None:
                        eco2 = -1

                except Exception:
                    aqi = 255
                    tvoc = -1
                    eco2 = -1
            else:
                aqi = 255
                tvoc = -1
                eco2 = -1

            # Plantower
            if pm_ser is not None:
                pm1, pm25, pm10 = read_plantower(pm_ser)
            else:
                pm1, pm25, pm10 = 9999, 9999, 9999

            # DS18B20
            ds1, ds2 = read_ds18b20_sensors()

            row = [
                t_seconds,
                now_text,
                f"{temp:.2f}",
                f"{rh:.2f}",
                aqi,
                tvoc,
                eco2,
                pm1,
                pm25,
                pm10,
                f"{ds1:.2f}",
                f"{ds2:.2f}"
            ]

            print(";".join(str(x) for x in row))

            with open(CSV_FILE, "a", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())

            elapsed = time.time() - loop_start
            sleep_time = INTERVAL_SECONDS - elapsed

            if sleep_time > 0:
                time.sleep(sleep_time)

    except KeyboardInterrupt:
        print("Messung beendet.")


if __name__ == "__main__":
    main()
