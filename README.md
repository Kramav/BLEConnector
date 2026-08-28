# BLEConnector — OpenLog Artemis accelerometer streaming over BLE

Streams the OpenLog Artemis's onboard ICM-20948 accelerometer at **281.25 Hz**
over Bluetooth LE — X/Y/Z in 20-byte binary notifications, buffered in RAM so a
link stall loses nothing, with sequence numbers so the host can *prove* nothing
was silently dropped. No SD card writes.

This is the implementation of [openlog-artemis-ble-streaming-guide.md](openlog-artemis-ble-streaming-guide.md).
The guide explains *why* every number here is what it is; this README is the
build-and-run path.

> **Status: compiles, not yet flashed.** The firmware builds cleanly against
> the real Apollo3 core 2.2.1 headers — 153,052 B flash (15%), 93,464 B RAM
> (23%), no warnings from the sketch — which confirms the library API details
> the guide flagged as unverified: the `ICM_20948_smplrt_t.a` field name, the
> `agmt.acc.axes.x` accessor, the DLPF/full-scale enums, and `dataReady()`.
> The host half is tested against synthetic streams. The power-rail polarities
> and the Apollo3 pin/SPI init are now taken from stock OLA firmware's
> `lowerPower.ino` and `beginIMU()` rather than assumed. What still needs
> hardware: whether `writeValue()` returns false on a full TX queue (the
> mechanism losslessness rests on), whether `central.connected()` alone services
> the BLE stack, and the actual ODR. Section
> [Verify on hardware](#5--verify-on-hardware) is ordered so those fail earliest.

```
firmware/
  OLA_Accel_BLE/OLA_Accel_BLE.ino   the streamer: IMU -> ring buffer -> BLE
  Dockerfile.accel                  pinned toolchain (Apollo3 2.2.1, ArduinoBLE 1.1.3)
host/
  ola_protocol.py                   wire format + seq unwrapping, no dependencies
  ola_receive.py                    connect, capture, write CSV + .meta.json
  ola_scan.py                       test 1: is it advertising?
  ola_analyze.py                    tests 3, 6, 7: rate, 1 g sanity, aliasing
  ola_simulate.py                   synthetic capture, no hardware needed
  test_protocol.py                  offline tests for the decode logic
scripts/
  build_firmware.ps1 / .sh          clone, stage, docker build, extract the .bin
```

---

## 0 · Verify the host half first

Nothing here needs the board, and it takes ten seconds:

```powershell
python host/test_protocol.py                       # 19 tests, no dependencies
python host/ola_simulate.py sim.csv -t 30          # synthetic capture
python host/ola_analyze.py sim.csv                 # should report PASS/PASS
```

To see the analyser catch a real defect, simulate a broken anti-alias filter —
a 200 Hz tone must **not** appear at |200 − 281.25| = 81.25 Hz:

```powershell
python host/ola_simulate.py alias.csv --tone 200 --no-antialias
python host/ola_analyze.py alias.csv --excite 200          # FAIL, peak at 81.25 Hz
python host/ola_simulate.py good.csv --tone 200
python host/ola_analyze.py good.csv --excite 200           # PASS, no alias
```

## 1 · Prerequisites

- **Docker Desktop**, running.
- **git** (the build clones SparkFun's repo for `Extras/UartPower3.zip`, a 7.2 MB
  core patch not worth vendoring).
- **Python 3.8+** on the host: `pip install -r host/requirements.txt`.
- An **OpenLog Artemis V10** (the red board) and a USB-C cable.

> The pin map in the sketch is V10 only. For the X04 (original black) board,
> read the pins from `OpenLog_Artemis.ino` under `HARDWARE_VERSION_MAJOR 0`.

## 2 · Build the firmware

```powershell
.\scripts\build_firmware.ps1              # macOS/Linux: ./scripts/build_firmware.sh
```

This clones OpenLog_Artemis into `build/`, stages the sketch and Dockerfile into
its `Firmware/` directory (the Docker build context), builds, and drops
**`build/OLA_Accel_BLE.ino.bin`**.

The first build is slow — it installs the Apollo3 core. Later builds reuse
Docker's layer cache and only recompile the sketch. `-NoCache` / `--no-cache`
forces a full rebuild.

> **`ArduinoBLE@1.1.3` is pinned and must stay pinned.** Versions ≥ 1.2.0
> compile without a single warning and then hard-fault at boot on Apollo3 core
> 2.x. It reads as a hardware fault, not a dependency problem. See
> [Arduino_Apollo3 #362](https://github.com/sparkfun/Arduino_Apollo3/issues/362).

## 3 · Flash

The Artemis ships with the SVL bootloader, so USB-C is all you need.

1. Get the [Artemis Firmware Upload GUI](https://github.com/sparkfun/Artemis-Firmware-Upload-GUI)
   (or `pip install artemis-uploader` and run `artemis_uploader`).
2. Connect the board, pick the COM port (**Refresh** re-enumerates).
3. Select `build\OLA_Accel_BLE.ino.bin`.
4. Baud **921600**; drop to **115200** if uploads fail intermittently.
5. **Upload Firmware**.

After a successful flash the status LED blinks **once every two seconds** while
advertising, and stays **solid** while a central is connected. A repeating burst
of 2, 3, or 4 blinks is a fault code — see [Troubleshooting](#6--troubleshooting).

**Going back to stock:** flash any `OpenLog_Artemis-V10-v211.bin` from the repo's
`/Binaries` folder (already in `build/OpenLog_Artemis/` after the build). Note
the `-V10-`; the `-X04-` files are for the black board and are not interchangeable.

## 4 · Capture

```powershell
python host/ola_scan.py                            # is it advertising?
python host/ola_receive.py accel.csv -t 60         # 60 s capture
python host/ola_receive.py accel.csv -t 0          # until Ctrl-C
python host/ola_analyze.py accel.csv               # check it
```

`ola_receive.py` writes `accel.csv`:

| column | meaning |
|--------|---------|
| `sample_index` | absolute index in the session, gaps preserved |
| `t_seconds` | `sample_index / ODR` — exact, jitter-free |
| `ax_g`, `ay_g`, `az_g` | acceleration in g |

…plus `accel.csv.meta.json` with the ODR, full-scale range, measured rate, and
both loss figures. Add `--raw` to keep the raw LSB counts alongside.

Timestamps come from the sample index, not from packet arrival times — at a
fixed ODR the index *is* the clock, and BLE arrival times say nothing about when
a sample was taken. To align with another instrument, capture a shared physical
event (a sharp tap) and align on it.

## 5 · Verify on hardware

Run 1–3 before trusting any data. Ordered so the riskiest assumptions fail first.

| # | Test | Command | Pass |
|---|------|---------|------|
| 1 | Advertising | `python host/ola_scan.py` | `OLA-ACCEL` found |
| 2 | Boot survival | `python host/ola_receive.py t2.csv -t 60` | 60 s connected, no drop |
| 3 | Rate | `python host/ola_analyze.py t2.csv` | measured rate within 1% of 281.25 Hz |
| 4 | Losslessness | `python host/ola_receive.py t4.csv -t 600` (still, near host) | `missing 0 samples`, firmware `dropped=0` |
| 5 | Buffer & recovery | capture, walk ~30 s out of range, return | stream resumes, `peak_buffer` climbs, no `OVERFLOWED` |
| 6 | Signal sanity | board flat, `ola_analyze.py` | one axis ±1.000 g, others ≈0; repeat for all six orientations |
| 7 | Aliasing | excite at 200 Hz, `ola_analyze.py c.csv --excite 200` | no peak at 81.25 Hz |

**Test 2 is the ArduinoBLE version test.** A board that flashes fine and then
dies on connect — or never advertises despite a clean compile — is the ≥ 1.2.0
hard-fault. Nothing else in this build fails that way.

**Test 4 is the most important one.** Losslessness rests on
`dataChar.writeValue()` returning `false` when the TX queue is full, which is
what makes `pumpBLE()` stop draining and let the ring buffer absorb the stall.
If that assumption is wrong, backpressure never engages and the ring silently
overruns — and this test is how you find out.

The firmware's `dropped` counter and the host's gap count measure different
things: firmware-side drops mean the ring overflowed, host-side gaps mean
packets never arrived. In a healthy run both are zero.

## 6 · Troubleshooting

| Symptom | Cause |
|---------|-------|
| Status LED blinks 2 / 3 / 4 forever | IMU not found / IMU config failed / BLE stack failed — for blink 2 see below |
| Compiles cleanly, board dead on boot | ArduinoBLE ≥ 1.2.0 — repin to 1.1.3 |
| All readings exactly zero | IMU powered but not sampling — check `PIN_IMU_CHIP_SELECT` is 44 for your board revision |
| Readings 2×, 4×, or 8× off | `ACCEL_FS_G` and the `fss.a` enum disagree |
| Advertises but won't connect | something else is connected — BLE takes one central. Un-pair it in Windows Bluetooth settings |
| Constant `gap` messages when close | backpressure not working — see test 4 |
| `OVERFLOWED` whenever you walk away | raise `RING_SAMPLES`; you have RAM headroom |
| Link dies a second or two after connecting | set `EXPLICIT_BLE_POLL 1` in the sketch |
| Time axis folds back after ~11.6 min | the 16-bit `seq` wrap — `ola_protocol.py` handles it; a custom reader may not |

### Blink 2 — "IMU not found"

`myICM.begin()` failed every attempt. Work down this list:

1. **Power-cycle the board** — unplug and replug, not just reset. `beginIMU()`
   power-cycles the ICM itself, but a cold start is the cleanest test.
2. **Set `DEBUG_SERIAL` to 1**, reflash, open a serial monitor at 115200. It
   prints `myICM.status` per attempt, which separates "no response at all" from
   "responded with the wrong WHO_AM_I" (a bus/pull-up problem).
3. **Set `MICROSD_POWER_OFF` to 0** and reflash. That powers the card like stock
   firmware does, ruling out an unpowered microSD loading the shared SPI bus.
4. **Confirm the board revision.** `PIN_IMU_CHIP_SELECT` is 44 on the V10; the
   X04 differs, and every pin in the map is then wrong.

Three things the sketch already does, which are easy to lose if you edit setup()
and are exactly what stock firmware does — see `beginIMU()` in
`OpenLog_Artemis.ino` and `imuPowerOn()` in `lowerPower.ino`: every `pinMode`
is paired with `pin_config()` (on Apollo3 `pinMode` alone may not re-configure a
pad, so a rail looks driven in code and reads dead on the board); the CIPO line
gets a 1.5K pull-up *after* `SPI.begin()`; and the ICM is power-cycled before
each `begin()` attempt rather than merely powered on.

The `⚠️` items in the guide's [§12](openlog-artemis-ble-streaming-guide.md) list
exactly which API details are unverified: the `ICM_20948_smplrt_t.a` field name,
the `agmt.acc.axes.x` accessor, the `writeValue()` return contract, whether
`central.connected()` alone services the stack, and the power-rail polarities.

## 7 · Changing the configuration

**Sample rate.** `ACCEL_SMPLRT_DIV` and `ODR_CENTI_HZ` in the sketch move
together — `ODR = 1125 / (1 + div)`. The rate is chosen by the anti-alias
filter, not by the signal: 281.25 Hz is the *slowest* rate at which the
`acc_d111bw4_n136bw` filter's 136 Hz noise bandwidth fits under Nyquist. Drop to
`div = 6` (160.7 Hz) only if you also drop to `acc_d50bw4_n68bw8`, which gives a
clean 50 Hz system. The host picks the new ODR up automatically from the status
packet.

**Full scale.** `ACCEL_FS_G` and the `fss.a` enum are one fact in two places —
`gpm2`/`gpm4`/`gpm8`/`gpm16` against 2/4/8/16. The host derives LSB/g from the
status packet, so it needs no edit. Pick the smallest range your signal will not
clip; gravity spends 1 g of the budget at all times.

**Buffer depth.** `RING_SAMPLES × 6 bytes`. The default 8192 is 48 KB ≈ 29 s of
stall tolerance. The measured build uses 93,464 B of globals and leaves
299,752 B, so there is real headroom — 32768 samples (192 KB, ≈ 116 s) still
leaves ~150 KB. Size it from the `peak_buffer` figure the status packet reports
rather than by guessing, and rebuild to confirm it links: too large fails at
link time, not at run time.

## Further reading

The guide's [§12](openlog-artemis-ble-streaming-guide.md) records what SparkFun's
abandoned `Initial_BLE_Support` branch actually did, why this is a standalone
sketch rather than a patch to the stock logger, the ICM-20948 FIFO/DMP path if
polling ever proves insufficient, and the MTU ceiling you would hit with a larger
payload.
