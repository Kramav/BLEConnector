#!/usr/bin/env python3
"""Receive the OLA-ACCEL BLE stream and write a CSV.

    python ola_receive.py out.csv                 # 60 s (the default)
    python ola_receive.py out.csv -t 600          # 10 minutes  (test 4)
    python ola_receive.py out.csv -t 0            # until Ctrl-C or disconnect
    python ola_receive.py out.csv --raw           # keep raw counts too

Writes `out.csv` plus `out.csv.meta.json` holding the session summary: ODR,
full-scale range, sample counts, and both loss figures.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone

try:
    from bleak import BleakClient, BleakScanner
except ImportError:  # pragma: no cover - dependency hint
    # Deferred so ola_simulate.py can reuse the CSV pipeline without bleak.
    BleakClient = BleakScanner = None

from ola_protocol import (
    DEVICE_NAME,
    UUID_DATA,
    UUID_STATUS,
    StreamAssembler,
)

# How many samples to hold back while waiting for the first status packet,
# which carries the authoritative ODR and full-scale range. Data starts
# immediately; status is up to a second behind it. ~4 s of headroom.
PRIME_LIMIT_SAMPLES = 1200


class CsvSink:
    """Buffers the first samples until the scale factors are known, then
    streams rows straight to disk."""

    def __init__(self, path, assembler, include_raw=False):
        self.path = path
        self.asm = assembler
        self.include_raw = include_raw
        self._fh = None
        self._writer = None
        self._pending = []
        self.primed = False
        self.rows_written = 0

    def __enter__(self):
        self._fh = open(self.path, "w", newline="")
        self._writer = csv.writer(self._fh)
        header = ["sample_index", "t_seconds", "ax_g", "ay_g", "az_g"]
        if self.include_raw:
            header += ["ax_raw", "ay_raw", "az_raw"]
        self._writer.writerow(header)
        return self

    def __exit__(self, *exc):
        self.flush_pending()
        if self._fh:
            self._fh.close()
        return False

    def mark_primed(self):
        """Called once the first status packet has set ODR and full scale."""
        if not self.primed:
            self.primed = True
            self.flush_pending()

    def add(self, samples):
        if not self.primed and len(self._pending) < PRIME_LIMIT_SAMPLES:
            self._pending.extend(samples)
            return
        # No status packet arrived in time -- go with the firmware defaults
        # rather than dropping data on the floor.
        self.flush_pending()
        self._write(samples)

    def flush_pending(self):
        if self._pending:
            pending, self._pending = self._pending, []
            self._write(pending)

    def _write(self, samples):
        asm = self.asm
        odr = asm.odr_hz
        lsb = asm.lsb_per_g
        rows = []
        for s in samples:
            row = [
                s.index,
                f"{s.index / odr:.6f}",
                f"{s.x / lsb:.6f}",
                f"{s.y / lsb:.6f}",
                f"{s.z / lsb:.6f}",
            ]
            if self.include_raw:
                row += [s.x, s.y, s.z]
            rows.append(row)
        self._writer.writerows(rows)
        self.rows_written += len(rows)


class Receiver:
    def __init__(self, sink, assembler, quiet=False):
        self.sink = sink
        self.asm = assembler
        self.quiet = quiet
        # Arrival times of the first and last data notification. The rate
        # check needs the streaming window alone -- scanning and connecting
        # can take seconds and would drag the measured rate down.
        self.first_rx = None
        self.last_rx = None
        self.first_index = None
        self.last_index = None

    def on_data(self, _handle, data: bytearray):
        samples = self.asm.feed_data(bytes(data))
        if self.asm.last_gap and not self.quiet:
            missing, seq = self.asm.last_gap
            print(f"  ! gap: {missing} packet(s) missing before seq={seq}")
        if not samples:
            return

        now = time.monotonic()
        if self.first_rx is None:
            self.first_rx = now
            self.first_index = samples[0].index
        self.last_rx = now
        self.last_index = samples[-1].index
        self.sink.add(samples)

    def on_status(self, _handle, data: bytearray):
        status = self.asm.feed_status(bytes(data))
        if status is None:
            return
        self.sink.mark_primed()
        if not self.quiet:
            print(f"  status: {status.describe()}")


def require_bleak():
    if BleakScanner is None:
        sys.exit("bleak is not installed.  pip install -r host/requirements.txt")


async def find_device(args):
    if args.address:
        print(f"connecting directly to {args.address} ...")
        return args.address
    print(f"scanning for {args.name} ...")
    dev = await BleakScanner.find_device_by_name(args.name, timeout=args.timeout)
    if dev is None:
        sys.exit(
            f"{args.name} not found. Is it powered, advertising (slow LED blink), "
            "and not already connected to something else?"
        )
    print(f"found {dev.address}")
    return dev


async def stream(args):
    require_bleak()
    asm = StreamAssembler()
    device = await find_device(args)

    started_wall = datetime.now(timezone.utc)
    t0 = time.monotonic()  # reset below, once notifications are actually on

    with CsvSink(args.out, asm, include_raw=args.raw) as sink:
        rx = Receiver(sink, asm, quiet=args.quiet)

        disconnected = asyncio.Event()
        loop = asyncio.get_running_loop()

        def on_disconnect(_client):
            loop.call_soon_threadsafe(disconnected.set)

        async with BleakClient(device, disconnected_callback=on_disconnect) as client:
            await client.start_notify(UUID_STATUS, rx.on_status)
            await client.start_notify(UUID_DATA, rx.on_data)
            started_wall = datetime.now(timezone.utc)
            t0 = time.monotonic()

            how_long = "until Ctrl-C" if args.seconds <= 0 else f"for {args.seconds} s"
            print(f"streaming {how_long} -> {args.out}   (Ctrl-C to stop)")

            try:
                if args.seconds > 0:
                    await asyncio.wait_for(disconnected.wait(), timeout=args.seconds)
                    print("\n! the peripheral disconnected before the time was up")
                else:
                    await disconnected.wait()
                    print("\n! the peripheral disconnected")
            except asyncio.TimeoutError:
                pass  # normal end of a timed capture
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\ninterrupted")

            if client.is_connected:
                try:
                    await client.stop_notify(UUID_DATA)
                    await client.stop_notify(UUID_STATUS)
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass

    elapsed = time.monotonic() - t0
    report(args, asm, sink, rx, started_wall, elapsed)


def stream_duration(rx):
    """Seconds between the first and last data notification, or None."""
    if rx.first_rx is None or rx.last_rx is None:
        return None
    return rx.last_rx - rx.first_rx


def measured_odr(asm, rx):
    """Sample rate from the index span over the streaming window.

    Uses the span rather than the number of samples delivered, so lost
    packets show up as loss (test 4) instead of masquerading as a wrong
    ACCEL_SMPLRT_DIV (test 3).
    """
    window = stream_duration(rx)
    if not window or rx.first_index is None:
        return None
    spanned = rx.last_index - rx.first_index
    if spanned <= 0:
        return None
    return spanned / window


def report(args, asm, sink, rx, started_wall, elapsed):
    print(f"\nwrote {sink.rows_written} samples to {args.out}")
    print(
        f"missing {asm.missing_samples} samples "
        f"({asm.loss_percent:.4f}%) across {asm.gap_packets} lost packet(s)"
    )
    odr_hat = measured_odr(asm, rx)
    window = stream_duration(rx)
    if odr_hat is not None:
        err = 100.0 * (odr_hat - asm.odr_hz) / asm.odr_hz
        print(
            f"measured rate {odr_hat:.2f} Hz over {window:.1f} s of streaming "
            f"({err:+.2f}% vs the firmware's {asm.odr_hz} Hz)"
        )
    elif elapsed > 0:
        print(f"captured for {elapsed:.1f} s (firmware ODR {asm.odr_hz} Hz)")
    if asm.malformed_packets:
        print(f"! {asm.malformed_packets} malformed packet(s)")
    if asm.last_status is None:
        print("! no status packet arrived -- scale and ODR are firmware defaults")
    elif asm.last_status.dropped_samples:
        print(
            f"! firmware dropped {asm.last_status.dropped_samples} sample(s): "
            "the ring buffer overflowed. Raise RING_SAMPLES."
        )

    meta = {
        "started_utc": started_wall.isoformat(),
        "duration_s": round(elapsed, 3),
        "stream_duration_s": round(window, 3) if window else None,
        "measured_odr_hz": round(odr_hat, 4) if odr_hat else None,
        "device_name": args.name,
        "odr_hz": asm.odr_hz,
        "full_scale_g": asm.full_scale_g,
        "lsb_per_g": asm.lsb_per_g,
        "samples_written": sink.rows_written,
        "packets_received": asm.packets_received,
        "gap_packets": asm.gap_packets,
        "missing_samples": asm.missing_samples,
        "loss_percent": round(asm.loss_percent, 6),
        "malformed_packets": asm.malformed_packets,
        "duplicate_packets": asm.duplicate_packets,
        "firmware_status": (
            asm.last_status._asdict() if asm.last_status is not None else None
        ),
    }
    meta_path = args.out + ".meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"session summary -> {meta_path}")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", nargs="?", default="accel.csv", help="output CSV path")
    p.add_argument(
        "seconds_pos",
        nargs="?",
        type=float,
        default=None,
        help=argparse.SUPPRESS,  # positional duration, for the guide's syntax
    )
    p.add_argument(
        "-t",
        "--seconds",
        type=float,
        default=60.0,
        help="capture duration; 0 means run until Ctrl-C (default: 60)",
    )
    p.add_argument("--name", default=DEVICE_NAME, help="BLE local name to scan for")
    p.add_argument("--address", help="skip the scan and connect to this address")
    p.add_argument("--timeout", type=float, default=15.0, help="scan timeout")
    p.add_argument("--raw", action="store_true", help="also write raw LSB counts")
    p.add_argument("--quiet", action="store_true", help="suppress per-second status")
    args = p.parse_args(argv)
    if args.seconds_pos is not None:
        args.seconds = args.seconds_pos
    return args


if __name__ == "__main__":
    try:
        asyncio.run(stream(parse_args()))
    except KeyboardInterrupt:
        pass
