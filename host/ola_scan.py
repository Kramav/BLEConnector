#!/usr/bin/env python3
"""Scan for BLE peripherals and report whether OLA-ACCEL is advertising.

This is test 1 from the guide: if the board does not show up here, nothing
downstream can work. Check the fault blink code on the status LED first --
2 = IMU not found, 3 = IMU config failed, 4 = BLE stack failed. A slow
once-every-two-seconds blink means the firmware is healthy and advertising.

    python ola_scan.py            # 10 s scan, highlight OLA-ACCEL
    python ola_scan.py --all      # list every device seen
    python ola_scan.py -t 30      # scan longer
"""

from __future__ import annotations

import argparse
import sys

from ola_ble import BleakScanner, require_bleak, run
from ola_protocol import DEVICE_NAME, UUID_SERVICE


async def scan(args):
    require_bleak()
    print(f"scanning for {args.timeout:.0f} s ...\n")
    found = await BleakScanner.discover(timeout=args.timeout, return_adv=True)

    hits = []
    others = []
    for _address, (device, adv) in found.items():
        name = adv.local_name or device.name or "(no name)"
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        is_ola = name == args.name or UUID_SERVICE.lower() in uuids
        entry = (device.address, name, adv.rssi, uuids)
        (hits if is_ola else others).append(entry)

    if args.all and others:
        print(f"{len(others)} other device(s):")
        for address, name, rssi, _uuids in sorted(others, key=lambda e: -(e[2] or -999)):
            print(f"  {address}  {rssi:>4} dBm  {name}")
        print()

    if not hits:
        print(f"NOT FOUND: no peripheral named {args.name}.")
        print("  - status LED blinking 2/3/4 times? that is a firmware fault code")
        print("  - already connected elsewhere? BLE peripherals accept one central")
        print("  - on Windows, un-pair the device in Bluetooth settings if paired")
        return 1

    for address, name, rssi, uuids in hits:
        print(f"FOUND  {name}")
        print(f"  address : {address}")
        print(f"  rssi    : {rssi} dBm")
        print(f"  services: {', '.join(uuids) if uuids else '(none advertised)'}")
        if UUID_SERVICE.lower() in uuids:
            print("  -> advertises the OLA-ACCEL service UUID")
    print("\nTest 1 passed. Next: python ola_receive.py accel.csv -t 60")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("-t", "--timeout", type=float, default=10.0, help="scan seconds")
    p.add_argument("--name", default=DEVICE_NAME, help="local name to look for")
    p.add_argument("--all", action="store_true", help="also list other devices")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(run(scan(parse_args())))
