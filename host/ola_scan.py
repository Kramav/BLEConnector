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
import asyncio
import sys
import time

from ola_ble import BleakClient, BleakScanner, require_bleak, run
from ola_protocol import DEVICE_NAME, UUID_DATA, UUID_SERVICE, UUID_STATUS


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


async def inspect(args):
    """Connect and report the real GATT table, then test-subscribe.

    Separates "the firmware's services are wrong" from "streaming is broken",
    which a failed ola_receive.py run cannot distinguish on its own.
    """
    require_bleak()
    print(f"scanning for {args.name} ...")
    dev = await BleakScanner.find_device_by_name(args.name, timeout=args.timeout)
    if dev is None:
        print(f"NOT FOUND: no peripheral named {args.name}.")
        return 1

    # Each step is timed and reported separately: "timeout" means something
    # very different depending on whether it happened during the connection,
    # during service discovery, or during the CCCD write that start_notify
    # performs.
    async def step(label, coro, timeout):
        t0 = time.monotonic()
        print(f"  {label} ... ", end="", flush=True)
        try:
            result = await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            print(f"TIMEOUT after {time.monotonic() - t0:.1f} s")
            raise
        except Exception as exc:  # noqa: BLE001 - reporting, not handling
            print(f"FAILED after {time.monotonic() - t0:.1f} s: "
                  f"[{type(exc).__name__}] {exc}")
            raise
        print(f"ok ({time.monotonic() - t0:.1f} s)")
        return result

    client = BleakClient(dev, timeout=args.connect_timeout, pair=args.pair)
    print(f"connecting to {dev.address}"
          f"{' with pairing' if args.pair else ''} ...")
    try:
        await step("connect", client.connect(), args.connect_timeout + 5)
    except Exception:
        print(
            "\nThe connection itself failed. On Windows, forget the device under\n"
            "Settings > Bluetooth & devices and retry -- a cached GATT table from\n"
            "earlier firmware causes exactly this."
        )
        return 1

    try:
        print(f"connected: {client.is_connected}\n")

        wanted = {
            UUID_SERVICE.lower(): "service",
            UUID_DATA.lower(): "data characteristic",
            UUID_STATUS.lower(): "status characteristic",
        }
        seen = set()

        for service in client.services:
            mark = " <-- OLA-ACCEL" if service.uuid.lower() in wanted else ""
            seen.add(service.uuid.lower())
            print(f"service {service.uuid}{mark}")
            for ch in service.characteristics:
                mark = " <--" if ch.uuid.lower() in wanted else ""
                seen.add(ch.uuid.lower())
                props = ",".join(ch.properties)
                print(f"    char {ch.uuid}  [{props}]{mark}")
        print()

        missing = [f"{uuid} ({what})" for uuid, what in wanted.items() if uuid not in seen]
        if missing:
            print("MISSING from the GATT table:")
            for m in missing:
                print(f"  {m}")
            print(
                "\nThe board is not running the expected firmware, or Windows is "
                "serving a\ncached GATT table -- forget the device in Bluetooth "
                "settings and retry."
            )
            return 1

        print("all expected UUIDs present. subscribing for 5 s ...")
        counts = {"data": 0, "status": 0, "bytes": 0}

        def on_data(_c, d):
            counts["data"] += 1
            counts["bytes"] += len(d)
            if counts["data"] == 1:
                print(f"  first data packet: {len(d)} bytes  {bytes(d).hex()}")

        def on_status(_c, d):
            counts["status"] += 1
            print(f"  status packet: {len(d)} bytes  {bytes(d).hex()}")

        try:
            await step("subscribe status", client.start_notify(UUID_STATUS, on_status), 20)
            await step("subscribe data", client.start_notify(UUID_DATA, on_data), 20)
        except Exception:
            print(
                "\nConnected and discovered services, but subscribing failed.\n"
                "That is the CCCD write. Check the board's serial log: if it\n"
                "never prints 'subscribed -- streaming', the write never arrived."
            )
            return 1

        await asyncio.sleep(5.0)
        if client.is_connected:
            await client.stop_notify(UUID_DATA)
            await client.stop_notify(UUID_STATUS)
    finally:
        if client.is_connected:
            await client.disconnect()

    print(
        f"\n{counts['data']} data packets ({counts['bytes']} bytes), "
        f"{counts['status']} status packets in 5 s"
    )
    if counts["data"] == 0:
        print(
            "\nSubscribed but nothing arrived. The firmware only streams while a\n"
            "central is connected -- check the status LED is solid, not blinking."
        )
        return 1
    rate = counts["data"] * 3 / 5.0
    print(f"~{rate:.0f} samples/s (expect ~281). Streaming works.")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("-t", "--timeout", type=float, default=10.0, help="scan seconds")
    p.add_argument("--name", default=DEVICE_NAME, help="local name to look for")
    p.add_argument("--all", action="store_true", help="also list other devices")
    p.add_argument(
        "--connect",
        action="store_true",
        help="connect and dump the GATT table, then test-subscribe",
    )
    p.add_argument(
        "--connect-timeout", type=float, default=20.0, help="connect timeout"
    )
    p.add_argument(
        "--pair", action="store_true", help="pair on connect (Windows sometimes needs it)"
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    _args = parse_args()
    sys.exit(run(inspect(_args) if _args.connect else scan(_args)))
