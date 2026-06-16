#!/usr/bin/env python3
"""
StratoPi HSO — GNU Radio LoRa receiver
=======================================
Decodes LoRa packets from the NESDR Mini 2+ and forwards raw bytes to
the ground station app via UDP on port 5005.

Run this in the radioconda environment:
  conda activate base   (or your radioconda env)
  python lora_rx.py

Then start the ground station app (in a separate terminal):
  python gs_app.py --no-serial

Works with gr-lora_sdr (EPFL, available via conda-forge):
  conda install -c conda-forge gnuradio-lora_sdr

Hardware: NESDR Mini 2+ (RTL2832U + R820T2)
Transmitter: Waveshare SX1268 HAT / EBYTE E22 @ 434.200 MHz SF12 BW125 CR4/5
"""

import sys
import socket
import threading

# ── Parameters — must match lora_tx.py exactly ─────────────────────────────

CENTER_FREQ  = 434_105_000   # Hz  — 434.105 MHz (E22 CH24 measured center)
BANDWIDTH    =     125_000   # Hz  — 125 kHz
SF           =          12   # Spreading Factor
CR           =           1   # Coding rate index: 1 = 4/5
PREAMBLE     =          12
SYNC_WORD    =        0x12   # EBYTE E22 private network default (NOT LoRaWAN 0x34)

SAMP_RATE    = 1_000_000     # 1 Msps — enough for 125 kHz BW with margin
RTL_GAIN     =          40   # dB — reduce if signal is overloaded (clipping)

UDP_HOST     = "127.0.0.1"
UDP_PORT     =        5005   # gs_app.py listens here

# ── GNU Radio imports ───────────────────────────────────────────────────────

try:
    from gnuradio import gr, blocks
    import osmosdr
except ImportError:
    print("ERROR: GNU Radio / osmosdr not found.")
    print("Install radioconda: https://github.com/ryanvolz/radioconda/releases")
    sys.exit(1)

# Try gr-lora_sdr (EPFL, conda-forge: gnuradio-lora_sdr) — preferred
_lora_mod = None
try:
    import lora_sdr as lora
    _lora_mod = "lora_sdr"
    print("Using gr-lora_sdr (EPFL)")
except ImportError:
    pass

# Fallback: gr-lora (rpp0, older)
if _lora_mod is None:
    try:
        import lora
        _lora_mod = "lora_rpp0"
        print("Using gr-lora (rpp0)")
    except ImportError:
        pass

if _lora_mod is None:
    print("ERROR: No LoRa decoder found.")
    print("Install gr-lora_sdr:")
    print("  conda install -c conda-forge gnuradio-lora_sdr")
    sys.exit(1)


# ── GNU Radio flowgraph ─────────────────────────────────────────────────────

class LoRaReceiver(gr.top_block):

    def __init__(self):
        super().__init__("StratoPi LoRa Receiver")

        # RTL-SDR source (NESDR Mini 2+)
        self.src = osmosdr.source(args="numchan=1 rtl=0")
        self.src.set_sample_rate(SAMP_RATE)
        self.src.set_center_freq(CENTER_FREQ)
        self.src.set_freq_corr(0)          # PPM correction — adjust if freq is off
        self.src.set_dc_offset_mode(0)
        self.src.set_iq_balance_mode(0)
        self.src.set_gain_mode(False)
        self.src.set_gain(RTL_GAIN)
        self.src.set_if_gain(20)
        self.src.set_bb_gain(20)
        self.src.set_bandwidth(BANDWIDTH * 2)

        # LoRa decoder
        if _lora_mod == "lora_sdr":
            self._build_lora_sdr()
        else:
            self._build_lora_rpp0()

    def _build_lora_sdr(self):
        """gr-lora_sdr (EPFL) — conda install -c conda-forge gnuradio-lora_sdr"""
        import lora_sdr

        # Receiver: samp_rate, center_freq, offset_list, bw, sf,
        #           impl_head, pay_len, has_crc, cr_list, pay_len_explicit
        self.lora_rx = lora_sdr.lora_receiver(
            SAMP_RATE,
            CENTER_FREQ,
            [0],           # channel offsets (Hz from centre)
            BANDWIDTH,
            SF,
            False,         # implicit header: off
            255,           # max payload length
            True,          # has CRC
            [CR],          # coding rate list
            False,         # explicit payload length
        )

        # UDP sink → gs_app.py port 5005
        self.udp = blocks.udp_sink(
            gr.sizeof_char * 1,
            UDP_HOST,
            UDP_PORT,
            1472,
            True,
        )

        self.connect(self.src, self.lora_rx)
        self.connect(self.lora_rx, self.udp)

    def _build_lora_rpp0(self):
        """gr-lora (rpp0) fallback — older API."""
        import lora as lora_rpp0

        self.lora_rx = lora_rpp0.lora_receiver(
            SAMP_RATE,
            CENTER_FREQ,
            [CENTER_FREQ],
            BANDWIDTH,
            SF,
            False,   # implicit header
            8,       # payload length (0 = auto in some builds)
        )

        self.udp = blocks.udp_sink(
            gr.sizeof_char * 1,
            UDP_HOST,
            UDP_PORT,
            1472,
            True,
        )

        self.connect(self.src, self.lora_rx)
        self.connect(self.lora_rx, self.udp)


# ── UDP monitor (prints decoded packets locally) ────────────────────────────

def _udp_monitor():
    """Listen on the same UDP port and print decoded strings locally."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", UDP_PORT + 1))   # monitor port, not the main one
    except OSError:
        return   # can't bind a monitor — gs_app already has it
    while True:
        try:
            data, _ = sock.recvfrom(4096)
            text = data.decode("ascii", errors="replace").strip()
            if text:
                print(f"  DECODED → {text}")
        except Exception:
            pass


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    print("StratoPi HSO — LoRa Receiver")
    print(f"  RTL-SDR gain : {RTL_GAIN} dB  (adjust with RTL_GAIN if clipping)")
    print(f"  Frequency    : {CENTER_FREQ/1e6:.3f} MHz")
    print(f"  LoRa params  : SF{SF} / BW{BANDWIDTH//1000}k / CR4/{CR+3} / preamble {PREAMBLE}")
    print(f"  Sync word    : 0x{SYNC_WORD:02X}  (EBYTE E22 private network)")
    print(f"  UDP output   : {UDP_HOST}:{UDP_PORT}  →  gs_app.py")
    print()
    print("Start gs_app.py in another terminal:")
    print("  python gs_app.py --no-serial")
    print()
    print("Press Ctrl-C to stop.\n")

    tb = LoRaReceiver()

    try:
        tb.start()
        print(f"Listening on {CENTER_FREQ/1e6:.3f} MHz …")
        tb.wait()
    except KeyboardInterrupt:
        print("\nStopping…")
        tb.stop()
        tb.wait()
        print("Done.")


if __name__ == "__main__":
    main()
