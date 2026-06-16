#!/usr/bin/env python3
"""Generate a shared LoRa AES key (hex). Copy the same key to Pi and ground station."""
import argparse
import secrets


def main():
    ap = argparse.ArgumentParser(description="Generate StratoPi LoRa AES key")
    ap.add_argument(
        "--bits", type=int, choices=(128, 256), default=128,
        help="AES-128 (16 bytes, default) or AES-256 (32 bytes)",
    )
    args = ap.parse_args()
    nbytes = 16 if args.bits == 128 else 32
    key_hex = secrets.token_hex(nbytes)
    print(f"# AES-{args.bits}-GCM — paste into lora_crypto.json on Pi AND ground station")
    print(f'{{"enabled": true, "key_hex": "{key_hex}"}}')


if __name__ == "__main__":
    main()