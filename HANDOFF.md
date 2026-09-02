# Handoff — OpenLog Artemis BLE accelerometer streamer

**As of 2026-08-31.** Read this first when picking the project back up; the
[README](README.md) has the full build/run/verify path, the
[guide](openlog-artemis-ble-streaming-guide.md) has the design reasoning.

---

## Status in one line

Firmware builds, flashes, runs; the IMU works; the board advertises and accepts
a connection. **Blocked at subscribe:** the central's CCCD write never
completes — the host reports `BLE error [TimeoutError]` and the board never
prints `subscribed -- streaming`.

## What is verified working on hardware

- Docker build → `build/OLA_Accel_BLE.ino.bin` (158,272 bytes, 15% flash,
  23% RAM, no warnings from the sketch)
- Flashing over USB-C with the Artemis Firmware Upload GUI
- **ICM-20948 initialises** — was the first blocker, now fixed
- **Advertising** as `OLA-ACCEL`; `ola_scan.py` finds it
- **Connection establishes** — serial prints `connected to <address>`

## What is blocked

`start_notify` (the CCCD write) times out. The board stays connected but never
reports the subscription, so it never starts streaming.

Already tried, in order: subscription gating in `loop()`; `delay(1)` yields;
`EXPLICIT_BLE_POLL 1`; BLE-first `setup()` ordering; `BLERead | BLENotify`
instead of notify-only; dropping `setDeviceName()`. The last four match the
sketch in [issue #66](https://github.com/sparkfun/OpenLog_Artemis/issues/66),
which is known to work on this hardware.

**The current binary has NOT been flashed and tested yet** — it was built at the
moment work stopped. That test is step 1 below.

## Next actions, in order

1. **Flash `build/OLA_Accel_BLE.ino.bin`** (it contains the BLE-first
   reordering, untested).
2. **Forget the device** in Settings → Bluetooth & devices first. Windows caches
   a GATT table per address, and this board has advertised several different
   service tables under one address. A stale cache produces exactly this
   signature and nothing on the firmware side can clear it. *This was your own
   last hypothesis and it is still the cheapest thing to rule out.*
3. `python host/ola_scan.py --connect` — reports each phase separately
   (connect / discovery / subscribe status / subscribe data) with timings.
4. If it still times out: `python host/ola_scan.py --connect --pair`.
5. **Try a phone as the central** — nRF Connect or LightBlue, subscribe to
   `f1b7a2c1-…`. This is the highest-value experiment left: it splits the
   problem cleanly in two. If a phone can subscribe, the firmware is correct and
   this is a Windows/WinRT problem. If a phone also fails, it is the firmware,
   and the next move is reducing to a single characteristic (the working example
   had exactly one) to find what the second one changes.

**The decisive question at every step:** does serial ever print
`subscribed -- streaming`? No → the CCCD write is not reaching or not being
processed by the board. Yes → the board is fine and the problem is host-side.

## Bugs already found and fixed — do not re-derive these

1. **CIPO pull-up is cleared by the first SPI transaction.** On the Apollo3 mbed
   core, configuring the 1.5K pull-up on CIPO (pin 6) and then doing any SPI
   transaction silently switches it off. So enabling it once achieves nothing:
   `myICM.begin()` issues that first transaction, loses the pull-up, reads a
   floating line, gets a garbage `WHO_AM_I`, and reports failure —
   indistinguishable from a dead or unpowered IMU. Fix: `primeSpiPullUp()` does
   a throwaway transaction then re-enables, before *every* `begin()` attempt.
   Source: `Firmware/Test Sketches/OLA_IMU_Basics` and issue #66. **This was the
   blink-2 fix.**
2. **A busy `loop()` starves ArduinoBLE's RTOS thread.** The Apollo3 port runs
   the Cordio stack in its own mbed thread (`bleLoopThread` in
   `HCICordioTransport.cpp`), not in `loop()`. `delay()` on this core is
   `rtos::ThisThread::sleep_for()`, which yields; a bare busy-loop never does.
   Starve that thread and the link layer still connects (interrupts keep
   running) but GATT stops entirely. The `delay(1)` calls in `loop()` are
   load-bearing — they look like removable pacing and are not.
3. **Qwiic power polarity was inverted for the V10.** `HIGH` *enables* the Qwiic
   rail on the V10 and disables it on the X04. Was powering the bus it meant to
   cut. Read from `qwiicPowerOn()` in `lowerPower.ino`.

Also incorporated from stock firmware: `pin_config()` paired with every
`pinMode()`; power-cycling the ICM before each `begin()` attempt; auto-detection
of the IMU power rail (27 = V10, 22 = X04) and both microSD chip selects.

Two of these are saved as memories (`apollo3-ble-needs-rtos-yield`,
`ola-cipo-pullup-quirk`) so a fresh session recalls them automatically.

## Environment

- **Docker Desktop must be running** before a build. It is installed at a
  non-default path: `%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe`.
- Build: `.\scripts\build_firmware.ps1` — ~25 s once cached. Clones the OLA repo
  into `build/OpenLog_Artemis/` (needed for `Extras/UartPower3.zip`).
- Flash: Artemis Firmware Upload GUI at 921600, or 115200 if uploads are flaky.
- Serial: **115200**. `DEBUG_SERIAL` is currently **1**.
- Host: Python 3.14, bleak 3.0.2, numpy 2.4.6 installed.

## Current firmware configuration

| Setting | Value | Meaning |
|---|---|---|
| `ACCEL_SMPLRT_DIV` | 3 | 281.25 Hz |
| `ACCEL_FS_G` | 4 | ±4 g, 8192 LSB/g |
| `RING_SAMPLES` | 8192 | 48 KB ≈ 29 s of stall tolerance |
| `EXPLICIT_BLE_POLL` | 1 | extra `BLE.poll()` per pass |
| `DEBUG_SERIAL` | 1 | **set to 0 and rebuild before test 4** |
| `MICROSD_POWER_OFF` | 1 | set 0 to power the card as a bus diagnostic |

## Host tools

```
python host/ola_scan.py                  # is it advertising?
python host/ola_scan.py --connect        # GATT dump + per-phase timings
python host/ola_receive.py accel.csv -t 60
python host/ola_analyze.py accel.csv     # tests 3, 6, 7
python host/ola_simulate.py sim.csv      # no hardware needed
python host/test_protocol.py             # 19 offline tests
```

## Still unverified — needs a working stream

Everything downstream of subscribe: the actual ODR (test 3), `writeValue()`'s
backpressure contract that losslessness depends on (test 4), buffer recovery
(test 5), 1 g sanity (test 6), and anti-aliasing (test 7). The host half of all
of these is already tested against synthetic data, so they should be
measurements rather than debugging once the stream runs.
