"""Shared BLE plumbing: the bleak import guard and human-readable failures.

bleak raises accurate exceptions, but an unhandled one arrives as a 20-line
traceback whose actual message is the last line. Everything here exists to turn
those into a sentence you can act on.

Verified against bleak 3.0.2 on Windows; the API surface used (discover with
return_adv, find_device_by_name, disconnected_callback, start_notify) has been
stable since 0.21.
"""

from __future__ import annotations

import asyncio
import sys

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:  # pragma: no cover - dependency hint
    BleakClient = BleakScanner = None

    class BleakError(Exception):
        """Placeholder so `except BleakError` still parses without bleak."""


BLEAK_MISSING = (
    "bleak is not installed.\n"
    "  pip install -r host/requirements.txt"
)


def require_bleak():
    if BleakScanner is None:
        sys.exit(BLEAK_MISSING)


def _turn_bluetooth_on_hint() -> str:
    if sys.platform == "win32":
        return (
            "  Settings > Bluetooth & devices, and turn Bluetooth on\n"
            "  (shortcut: press Win+R and run  ms-settings:bluetooth )"
        )
    if sys.platform == "darwin":
        return "  Control Centre > Bluetooth, or System Settings > Bluetooth"
    return (
        "  rfkill unblock bluetooth\n"
        "  bluetoothctl power on"
    )


def explain(exc: Exception) -> str:
    """Turn a bleak exception into an actionable message."""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()

    if name == "BleakBluetoothNotAvailableError" or "not powered on" in low:
        if "no adapter" in low or "not present" in low or "NOT_PRESENT" in text:
            return (
                "No Bluetooth adapter found on this computer.\n"
                "  Check Device Manager for a Bluetooth radio, or plug in a "
                "USB BLE dongle.\n"
                "  Bluetooth 4.0 or later is required."
            )
        return "Bluetooth is turned off on this computer.\n" + _turn_bluetooth_on_hint()

    if name == "BleakDeviceNotFoundError":
        return (
            f"{text}\n"
            "  The board stopped advertising, or something else connected to it "
            "first.\n"
            "  BLE peripherals accept one central at a time."
        )

    if name == "BleakCharacteristicNotFoundError":
        return (
            f"{text}\n"
            "  Connected, but the OLA-ACCEL service is missing. Is this the "
            "right device,\n"
            "  and is it running the firmware from firmware/OLA_Accel_BLE?"
        )

    if "access is denied" in low or "permission" in low:
        if sys.platform == "darwin":
            return (
                f"{text}\n"
                "  macOS requires Bluetooth permission for your terminal:\n"
                "  System Settings > Privacy & Security > Bluetooth."
            )
        return (
            f"{text}\n"
            "  On Windows, un-pair the device in Settings > Bluetooth & devices. "
            "Pairing is\n"
            "  unnecessary here and can hold the link open."
        )

    return text


def run(coro) -> int:
    """asyncio.run for a BLE coroutine, with failures reported as one line.

    Returns a process exit code.
    """
    try:
        result = asyncio.run(coro)
    except BleakError as exc:
        print(f"\nBLE error: {explain(exc)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        # WinRT surfaces some radio failures as bare OSErrors.
        print(f"\nBLE error: {explain(exc)}", file=sys.stderr)
        return 1
    return 0 if result is None else int(result)
