# Streaming accelerometer data from an OpenLog Artemis over BLE

> **Scope:** Turn a SparkFun OpenLog Artemis into a **wireless accelerometer streamer** — X/Y/Z at **281.25 Hz**, sent over Bluetooth Low Energy straight to a computer, with **no SD card writes**, **lossless RAM buffering**, and **sequence numbers** so the host can prove nothing was silently lost. Complete in-house: the feasibility math, a standalone firmware sketch, a purpose-built Docker toolchain, the flashing procedure, a Python receiver, and seven tests that fail if you built it wrong.
> **Target hardware:** **OpenLog Artemis V10** (the red board, `HARDWARE_VERSION_MAJOR 1`). Artemis module = Ambiq **Apollo3 Blue**, Cortex-M4F, 1 MB flash, **384 KB RAM**, Bluetooth 5.0 radio at ~4 dBm. Onboard IMU is an **ICM-20948 on SPI**. The X04 (original black) board differs only in the pin map.
> **As-of:** 2026-08-28 · OLA firmware repo at **v2.11** · Apollo3 core **2.2.1** · ArduinoBLE **1.1.3** (pinned — see [§4](#4--the-docker-toolchain)).
> **Sourcing:** Live-fetched 2026-08-28 — the [OpenLog_Artemis](https://github.com/sparkfun/OpenLog_Artemis) repo contents API (`Firmware/`, `Firmware/Extras/`, `Binaries/`), `COMPILE_BINARY.md`, `UPGRADE.md`, `CHANGELOG.md`, `Firmware/Dockerfile`, `Firmware/OpenLog_Artemis/Sensors.h`, the V10 pin map out of `OpenLog_Artemis.ino`, the [`Initial_BLE_Support`](https://github.com/sparkfun/OpenLog_Artemis/tree/Initial_BLE_Support) branch and its [patch `c4e6ef1`](https://github.com/sparkfun/OpenLog_Artemis/commit/c4e6ef14162c5cab1f81df141638f1a16c6fbf03), the SparkFun ICM-20948 library headers/examples, and two SparkFun forum threads ([BLE status](https://community.sparkfun.com/viewtopic.php?t=56202), [BLE 5.0 MTU limits](https://community.sparkfun.com/viewtopic.php?t=56499)). Search-confirmed only — ICM-20948 ODR/sensitivity figures, ArduinoBLE/Apollo3 incompatibility issues, BLE throughput ranges.
> **⚠️ Not built or flashed.** Every line of firmware and host code here is derived from the repo's own build configuration, pin map, and library APIs — but none of it has been compiled or run on hardware. Items marked ⚠️ are the specific things to verify first; [§8](#8--test-it) is ordered so the riskiest assumptions fail earliest.
> **Related:** anti-aliasing and sample-rate discipline follow [arduino-mains-hum-filtering-guide.md](arduino-mains-hum-filtering-guide.md); the multi-channel version of this budgeting exercise is [multichannel-analog-daq-guide.md](multichannel-analog-daq-guide.md).

---

## Contents

- [What you're building](#what-youre-building)
- [Difficulty · time · cost](#difficulty--time--cost)
- [§1 — What the repo does and doesn't give you](#1--what-the-repo-does-and-doesnt-give-you)
- [§2 — The data budget](#2--the-data-budget)
  - [Payload](#payload)
  - [Picking the sample rate — the anti-alias constraint decides it](#picking-the-sample-rate--the-anti-alias-constraint-decides-it)
  - [Packet format](#packet-format)
  - [What you dodged](#what-you-dodged)
  - [Losslessness](#losslessness)
- [§3 — Hardware setup](#3--hardware-setup)
- [§4 — The Docker toolchain](#4--the-docker-toolchain)
- [§5 — The firmware](#5--the-firmware)
- [§6 — Flashing](#6--flashing)
- [§7 — The host receiver](#7--the-host-receiver)
- [§8 — Test it](#8--test-it)
- [§9 — Troubleshooting](#9--troubleshooting)
- [§10 — Scaling, calibration, and what the numbers mean](#10--scaling-calibration-and-what-the-numbers-mean)
- [§11 — Safety](#11--safety)
- [§12 — Research findings and paths not taken](#12--research-findings-and-paths-not-taken)
  - [What SparkFun's BLE branch actually changed](#what-sparkfuns-ble-branch-actually-changed)
  - [If you port that branch forward, do not replay its signature change](#if-you-port-that-branch-forward-do-not-replay-its-signature-change)
  - [Where SparkFun's own documentation disagrees](#where-sparkfuns-own-documentation-disagrees)
  - [The FIFO path, if polling proves insufficient](#the-fifo-path-if-polling-proves-insufficient)
  - [Toolchain facts this build does not use](#toolchain-facts-this-build-does-not-use)
  - [Verification status of the claims in this guide](#verification-status-of-the-claims-in-this-guide)
- [What it unlocks](#what-it-unlocks)
- [Further reading](#further-reading)

---

## What you're building

A single-purpose firmware image for the OpenLog Artemis that:

- reads **only the accelerometer** from the onboard ICM-20948, at **281.25 Hz**,
- packs samples into **20-byte binary BLE notifications**,
- buffers in RAM so a momentary link stall **loses nothing**,
- numbers every packet so the host can **prove** what arrived,
- and **never touches the SD card**.

Plus a Python receiver that reassembles the stream into a CSV ready for processing.

**This replaces the stock OpenLog Artemis application rather than extending it.** That is a deliberate choice, argued in [§1](#1--what-the-repo-does-and-doesnt-give-you). If you want your logger back, reflash a stock binary from the repo's `/Binaries` folder — nothing here is irreversible.

## Difficulty · time · cost

- **Difficulty:** **medium.** No hardware to build and nothing to solder. The work is firmware, one Docker image, and a host script. The one genuinely nasty trap is a library version ([§4](#4--the-docker-toolchain)) that compiles cleanly and then crashes on boot.
- **Time:** ~2 h for a first flash and a live stream · a further evening for [§8](#8--test-it) properly.
- **Cost:** **$0** beyond hardware you already own. The OpenLog Artemis is ~$55 and a LiPo is ~$10 if you want it untethered.
- **Skills:** Arduino-style C++, Docker basics, and enough Python to run a script.

---

## §1 — What the repo does and doesn't give you

Start here, because the obvious plan — "find the Bluetooth option in the firmware and switch it on" — does not exist.

**The stock firmware has no BLE.** The current release is **v2.11**. The [CHANGELOG](https://github.com/sparkfun/OpenLog_Artemis/blob/main/CHANGELOG.md) never mentions BLE, and none of the ~60 `.bin` files in [`/Binaries`](https://github.com/sparkfun/OpenLog_Artemis/tree/main/Binaries) is BLE-enabled. There is no setting to toggle.

**The hardware absolutely supports it.** The Artemis module is an Ambiq Apollo3 Blue with a Bluetooth 5.0 radio built into the same die as the CPU. Nothing needs to be added. The gap is purely software.

**SparkFun started BLE support and abandoned it.** There is a branch — [`Initial_BLE_Support`](https://github.com/sparkfun/OpenLog_Artemis/tree/Initial_BLE_Support), commit [`c4e6ef1`](https://github.com/sparkfun/OpenLog_Artemis/commit/c4e6ef14162c5cab1f81df141638f1a16c6fbf03), by PaulZC on 2021-07-16, titled *"Adding initial BLE support - work in progress."* It is **8 commits ahead of `main` and 199 behind**. On the [SparkFun forum](https://community.sparkfun.com/viewtopic.php?t=56202) in September 2022 PaulZC explained it was never finished: the restructuring required was too extensive, and the team moved to newer products where WiFi and Bluetooth SPP were far easier.

Do not check that branch out and flash it. It predates two years of fixes, and its design is the wrong shape for this job anyway.

**What that branch did, and why we're not copying it.** It declared **50 `BLEStringCharacteristic` objects** — one per column of OLA's CSV output — under service UUID `2488bd28-b1df-4fe0-8611-22fda7c645f0`, each `BLERead | BLENotify` with a 50-byte maximum, populated in a carousel by a helper `updateBLECharacteristic()` wired into `getData()` and `gatherDeviceValues()`. It was built for *browsing sensor values in a phone app*, and it is:

- **ASCII.** Every reading crosses the air as decimal text — roughly 5–8× the bytes of the same value in binary.
- **Positional.** Characteristic *n* means whatever column *n* of the helper text happens to be, which changes when you enable a different sensor.
- **RAM-hungry.** 50 characteristic objects plus a `char[50][50]` value cache.

For streaming one sensor fast, all three properties are actively harmful. [§12](#12--research-findings-and-paths-not-taken) records exactly which five files that patch touched, and the one refactor that has happened since which you must **not** replay if you ever port it forward.

**Why this guide writes a standalone sketch instead of patching OLA.** Two of your requirements — accelerometer only, and no SD writes — delete almost everything the OLA application does. What remains is a menu system, a ZMODEM file-transfer implementation, an I²C device autodetector, 33 sensor libraries, an SD/settings layer, and `lowerPower.ino`'s aggressive sleep logic. That last one matters most: **OLA is architected to sleep between readings, and a live BLE connection needs the CPU awake.** You would spend most of the project fighting it.

A standalone sketch drops all of it, frees the RAM your lossless buffer needs, and still uses the repo for everything valuable: the pin map, the board FQBN, the Docker build, the `UartPower3` core patch, and the flashing path.

> One thing worth naming honestly: **if you can accept a cable, USB serial is far easier and roughly 100× faster.** The Artemis enumerates as USB CDC and would carry this stream without any of the machinery below. Everything here exists to make it wireless.

---

## §2 — The data budget

Do this arithmetic before writing code. It decides the packet format, and for this workload it turns a hard project into an easy one.

### Payload

Accelerometer only, as three signed 16-bit integers:

```
  int16 ax, ay, az  =  6 bytes per sample
```

No gyro, no magnetometer, no temperature. Two notes on what you're skipping:

- The **magnetometer physically cannot exceed 100 Hz** — it is an AK09916 die inside the ICM-20948 package — so sampling it alongside a 281 Hz accelerometer would transmit repeated values. For reference, the three sensors have very different ceilings: **accelerometer up to 4.5 kHz**, **gyroscope up to 1125 Hz**, **magnetometer 10–100 Hz**. Any design that samples all three at one rate is running two of them wrong.
- **Send no per-sample timestamp.** At a fixed output data rate the sample index *is* the timestamp. One packet counter reconstructs every sample time on the host, which saves 4 bytes per sample — more than half the payload.

### Picking the sample rate — the anti-alias constraint decides it

You asked for ≥160 Hz to get an 80 Hz Nyquist bandwidth. Taking that literally gets you a working link and unusable data, because **the sample rate has to be chosen together with the anti-alias filter**, and on this part they interact badly.

The ICM-20948's accelerometer output data rate is set by a 12-bit divider:

```
  ODR = 1125 / (1 + ACCEL_SMPLRT_DIV)   Hz
```

Its on-chip digital low-pass filter has a fixed menu of settings, each specified by a 3 dB bandwidth and a *noise* bandwidth. Anti-aliasing works only if the **noise bandwidth sits below Nyquist** — otherwise energy above Nyquist folds down into your data and no amount of post-processing removes it.

**160.7 Hz** · `ACCEL_SMPLRT_DIV = 6` · Nyquist **80.4 Hz**
> Widest filter that fits is `acc_d50bw4_n68bw8` — **68.8 Hz** noise bandwidth, leaving only ~50 Hz of usable band. ❌ **Fails the 80 Hz requirement.** The 111 Hz filter's 136 Hz noise bandwidth is far above this Nyquist, so choosing it aliases badly.

**187.5 Hz** · `ACCEL_SMPLRT_DIV = 5` · Nyquist **93.8 Hz**
> Same trap: `acc_d111bw4_n136bw` still overruns Nyquist by 42 Hz, so you are forced back to the 50 Hz filter. ❌ **Fails the 80 Hz requirement.**

**225 Hz** · `ACCEL_SMPLRT_DIV = 4` · Nyquist **112.5 Hz**
> `acc_d111bw4_n136bw` (**136 Hz** noise BW) overruns Nyquist by 23.5 Hz. Content in 112.5–136 Hz folds back to **89–112.5 Hz** — above your band, so **0–80 Hz stays clean**. ⚠️ Usable, but treat 89–112 Hz as junk.

**281.25 Hz** · `ACCEL_SMPLRT_DIV = 3` · Nyquist **140.6 Hz**
> `acc_d111bw4_n136bw` (**136 Hz** noise BW) fits under Nyquist with 4.6 Hz to spare. ✅ **Fully anti-aliased, full 80 Hz band.** This is the recommendation.

**Use `ACCEL_SMPLRT_DIV = 3` → 281.25 Hz with `acc_d111bw4_n136bw`.** It is the slowest rate on the menu at which the filter's 136 Hz noise bandwidth actually fits under Nyquist. The two rates nearest your stated 160 Hz floor both force a filter that amputates the top 30 Hz of the band you asked for.

> At 225 Hz the compromise is subtler and survivable: content between 112.5 and 136 Hz folds back to **89–112.5 Hz**, which is above your 80 Hz band of interest, so 0–80 Hz stays clean. Use 225 Hz if you want the lower data rate; just know that 89–112 Hz is then junk and must not be trusted.

**Throughput at 281.25 Hz:** `281.25 × 6 B` = **1.69 kB/s**.

That is the number that makes this project easy. Practical BLE throughput is roughly **2.6–8.7 kB/s** even with default 20-byte notifications, so you are inside budget with margin to spare.

### Packet format

```
  uint16  seq          packet counter, little-endian
  int16   ax, ay, az   sample n
  int16   ax, ay, az   sample n+1
  int16   ax, ay, az   sample n+2
  ---------------------------------------------------
  2 + 18 = 20 bytes    exactly the default BLE notify payload
```

At 281.25 Hz that is **93.75 notifications/sec** and about **10.7 ms** of packing latency. BLE sustains several hundred notifications per second, so there is roughly 5× headroom — which is also your catch-up capacity after a stall.

> **The subtle trap this avoids:** at the default 20-byte MTU the binding constraint is **packets per second, not bytes per second.** One sample per notification would have needed 281 notifications/sec, pushing against the packet ceiling for no reason. Packing three samples per notification costs nothing and removes the problem. Any design that sends one reading per notification will hit a wall far below the throughput its byte arithmetic predicted.

### What you dodged

Had the payload been larger — full 9-axis at 1 kHz, say — you would have needed to negotiate a larger MTU, and that path is genuinely unpleasant on this chip:

- The Artemis caps MTU at **256 bytes** via `HCI_DRV_MAX_TX_PACKET`, not the 512 that BLE 5.0 permits. After ATT overhead that leaves **242 usable bytes**.
- **ArduinoBLE does not expose the negotiated MTU.** Getting it requires adding an accessor to the library — `ATT.mtu(ATT.connectionHandle(_addressType, _address))` — as discussed in [this SparkFun thread](https://community.sparkfun.com/viewtopic.php?t=56499).
- Anything longer than the negotiated MTU is **silently truncated with no error**, so application-layer fragmentation is mandatory and its absence is invisible until you diff the data.
- Separately, ArduinoBLE's HCI layer stores packet lengths in `uint8_t`, so values over 255 bytes are truncated regardless ([ArduinoBLE #203](https://github.com/arduino-libraries/ArduinoBLE/issues/203)).

None of that applies at 20 bytes. **No MTU negotiation, no library patching, no fragmentation logic.**

### Losslessness

Two mechanisms, working together:

**A RAM ring buffer** absorbs stalls. At 1.69 kB/s, a **48 KB** buffer (8,192 samples) holds about **29 seconds**. The Apollo3 has 384 KB total and this sketch uses very little of it, so growing the buffer is mostly free — [test 5](#8--test-it) tells you how far you can push it.

**Backpressure** keeps it honest. `writeValue()` returns false when the BLE stack's transmit queue is full; the firmware then stops draining and lets the ring buffer grow instead of discarding. Samples are dropped only if the ring itself fills, and that increments a counter reported to the host.

Recovery is fast: drain capacity is ~5× production, so a 29-second backlog clears in roughly 6 seconds.

> **A correction worth making, because it changes what you need to build.** BLE notifications are **acknowledged and delivered in order by the link layer** within a connection. You will not receive packets out of order, and they are not silently dropped in flight. The real failure modes are narrower: the peripheral's TX queue filling (absorbed by the ring buffer) and gaps across a disconnect/reconnect (revealed by `seq`). The host reassembler therefore needs **gap detection**, not reordering — which is why the receiver in [§7](#7--the-host-receiver) is as short as it is.

---

## §3 — Hardware setup

No wiring. The work is knowing which pins the V10 board uses and switching off what you don't want.

**OpenLog Artemis V10 pin map** — taken from `OpenLog_Artemis.ino` at `HARDWARE_VERSION_MAJOR 1`:

```c
const byte PIN_IMU_CHIP_SELECT     = 44;
const byte PIN_IMU_POWER           = 27;
const byte PIN_MICROSD_CHIP_SELECT = 23;
const byte PIN_MICROSD_POWER       = 15;
const byte PIN_QWIIC_POWER         = 18;
const byte PIN_VREG_ENABLE         = 25;
const byte PIN_VIN_MONITOR         = 34;
const byte PIN_PWR_LED             = 29;
const byte PIN_STAT_LED            = 19;
```

> ⚠️ These are **V10 only**. On the X04 (original black) board they differ. Read them out of `OpenLog_Artemis.ino` under `HARDWARE_VERSION_MAJOR 0` if that is your board.

**The IMU is on SPI, not I²C.** Stock firmware declares `ICM_20948_SPI myICM;` and starts it with:

```c
myICM.begin(PIN_IMU_CHIP_SELECT, SPI, 4000000);   // 4 MHz
```

This catches people out — most ICM-20948 breakouts and most library examples are I²C, so example code will not work unchanged.

**Power rails to turn off.** `PIN_MICROSD_POWER` and `PIN_QWIIC_POWER` both gate real current. Since this build uses neither the card nor the Qwiic bus, hold them off. This matters if you run from a LiPo.

> ⚠️ **Check the polarity of the power-enable pins on your board before trusting it.** These rails are driven through P-channel devices on some SparkFun designs, meaning `LOW` enables. Getting it backwards leaves the IMU unpowered and every reading zero — which is exactly what [test 6](#8--test-it) catches. The sketch below writes `HIGH` to enable IMU power and `HIGH` to disable the others, matching stock firmware's usage; verify against your own board.

**`PIN_STAT_LED`** is used below as a link indicator: lit while a central is connected. **`PIN_VIN_MONITOR`** reads battery voltage through a divider if you want to add that later.

---

## §4 — The Docker toolchain

SparkFun's repo recommends Docker for firmware builds, and it is the right call here: it pins the Apollo3 core, the libraries, and a required set of core patches that are miserable to apply by hand.

Their [`Firmware/Dockerfile`](https://github.com/sparkfun/OpenLog_Artemis/blob/main/Firmware/Dockerfile) installs **33 sensor libraries** to build the full logger. This sketch needs two. Rather than editing theirs, use this stripped derivative — it keeps every part that matters and builds in a fraction of the time.

**Prerequisites:** Docker Desktop, and a clone of the OLA repo (you need `Firmware/Extras/UartPower3.zip`, which is 7.2 MB and not worth reproducing).

```
git clone https://github.com/sparkfun/OpenLog_Artemis.git
cd OpenLog_Artemis/Firmware
mkdir OLA_Accel_BLE
# put the sketch from §5 at OLA_Accel_BLE/OLA_Accel_BLE.ino
# put the Dockerfile below at Dockerfile.accel
```

### `Dockerfile.accel`

Every path and quoted string below is **copied from SparkFun's own working Dockerfile**, not reconstructed. That matters: three of the six core-patch destinations are *not* where you would guess.

```dockerfile
FROM ubuntu:latest

RUN apt-get update && apt-get install -y curl unzip \
    && rm -rf /var/lib/apt/lists/*

# arduino-cli. The installer places it on PATH; no ENV line needed.
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh

# SparkFun Apollo3 core 2.2.1 -- the version OLA v2.11 is built against.
# NOTE: --additional-urls belongs on `config init`, not a separate `config add`.
RUN arduino-cli config init --additional-urls https://raw.githubusercontent.com/sparkfun/Arduino_Apollo3/main/package_sparkfun_apollo3_index.json
RUN arduino-cli core update-index
RUN arduino-cli core install "Sparkfun:apollo3@2.2.1"

# Libraries. ONLY these two are needed.
# NOTE the quoting: the @version goes OUTSIDE the quotes. Inside, arduino-cli
# searches for a library literally named "...Arduino Library@1.3.0" and fails.
RUN arduino-cli lib install "SparkFun 9DoF IMU Breakout - ICM 20948 - Arduino Library"@1.3.0
RUN arduino-cli lib install "ArduinoBLE"@1.1.3

# --- UartPower3 core patch, paths copied from SparkFun's Dockerfile ---
# The zip extracts its six files FLAT into the current directory; it does not
# create a UartPower3/ subdirectory.
COPY Extras/UartPower3.zip /Extras/
RUN cd /Extras \
    && unzip -o -q UartPower3.zip \
    && cp HardwareSerial.h    /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/cores/arduino/mbed-bridge/core-extend/HardwareSerial.h \
    && cp HardwareSerial.cpp  /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/cores/arduino/mbed-bridge/core-implement/HardwareSerial.cpp \
    && cp UnbufferedSerial.h  /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/cores/mbed-os/drivers/UnbufferedSerial.h \
    && cp serial_api.c        /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/cores/mbed-os/targets/TARGET_Ambiq_Micro/TARGET_Apollo3/device/serial_api.c \
    && cp libmbed-os.a        /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/variants/SFE_ARTEMIS_ATP/mbed/libmbed-os.a \
    && cp SPI.cpp             /root/.arduino15/packages/SparkFun/hardware/apollo3/2.2.1/libraries/SPI/src/SPI.cpp

COPY OLA_Accel_BLE /OLA_Accel_BLE
RUN cd /OLA_Accel_BLE \
    && arduino-cli compile -v -e -b SparkFun:apollo3:sfe_artemis_atp OLA_Accel_BLE.ino
RUN cp /OLA_Accel_BLE/build/SparkFun.apollo3.sfe_artemis_atp/OLA_Accel_BLE.ino.bin /
```

> ⚠️ **`ArduinoBLE@1.1.3` is not a preference — it is the one landmine in this project.** ArduinoBLE **1.2.0 and later hard-fault at runtime** on Apollo3 core v2.x. The core pins a SparkFun-maintained fork of Mbed 5, whose BLE API diverged from the Mbed 6 that ArduinoBLE moved to. The symptom is brutal: **it compiles without a single warning and then crashes on boot**, so it reads as a hardware fault rather than a dependency problem. See [Arduino_Apollo3 #362](https://github.com/sparkfun/Arduino_Apollo3/issues/362) and [#291](https://github.com/sparkfun/Arduino_Apollo3/issues/291).

> **Two differences from SparkFun's Dockerfile, both deliberate.** Theirs is a multi-stage build whose final stage does `COPY --from=deployment ...`, which is why their `compile_with_docker.bat` passes `--no-cache-filter deployment`; this single-stage version copies the binary to `/` instead, so a plain `docker build` suffices. Theirs also sets `arduino-cli config set library.enable_unsafe_install true` — needed only for its git-URL library install (Blue Robotics MS5837), which this build drops.

> ⚠️ **If a `cp` in the patch block fails, the build stops** — that is the good outcome, and it tells you the core layout changed. `Firmware/Extras/spi.diff` in the repo is worth a look if the SPI patch specifically misbehaves.

### Build

```
docker build -f Dockerfile.accel -t ola_accel_ble --progress=plain .
docker create --name=ola_accel_container ola_accel_ble:latest
docker cp ola_accel_container:/OLA_Accel_BLE.ino.bin .
docker container rm ola_accel_container
```

This mirrors the four-step pattern in SparkFun's own `compile_with_docker.bat`. You now have `OLA_Accel_BLE.ino.bin` in the current directory, ready for [§6](#6--flashing).

**Iteration loop:** edit the sketch → rerun the four commands → reflash. The core install is cached by Docker's layer cache, so rebuilds after the first are quick. Only editing the Dockerfile above the `COPY` line forces a slow rebuild.

---

## §5 — The firmware

One file: `OLA_Accel_BLE/OLA_Accel_BLE.ino`.

```cpp
// =====================================================================
//  OpenLog Artemis -> BLE accelerometer streamer
//
//  Streams ICM-20948 accelerometer X/Y/Z at 281.25 Hz over BLE.
//  No SD card. No menu. Lossless within the ring buffer's depth.
//
//  Board: OpenLog Artemis V10  (FQBN SparkFun:apollo3:sfe_artemis_atp)
//  Build: see Dockerfile.accel -- ArduinoBLE MUST be 1.1.3
// =====================================================================

#include <ArduinoBLE.h>
#include "ICM_20948.h"

// ---------------- OLA V10 pin map ----------------
const byte PIN_IMU_CHIP_SELECT     = 44;
const byte PIN_IMU_POWER           = 27;
const byte PIN_MICROSD_POWER       = 15;
const byte PIN_QWIIC_POWER         = 18;
const byte PIN_STAT_LED            = 19;
const byte PIN_PWR_LED             = 29;

// ---------------- Configuration ----------------
// ODR = 1125 / (1 + ACCEL_SMPLRT_DIV).  3 -> 281.25 Hz.  See §2.
#define ACCEL_SMPLRT_DIV    3
#define ODR_CENTI_HZ        28125       // 281.25 Hz x100, reported to host
#define ACCEL_FS_G          4           // gpm4 -> 8192 LSB/g

#define SAMPLES_PER_PACKET  3           // 2 + 3*6 = 20 bytes exactly
#define RING_SAMPLES        8192        // 8192 * 6 B = 48 KB ~= 29 s

// ---------------- BLE UUIDs (custom family) ----------------
#define UUID_SERVICE "f1b7a2c0-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_DATA    "f1b7a2c1-9e4d-4a1f-8c3b-5d6e7f801234"
#define UUID_STATUS  "f1b7a2c2-9e4d-4a1f-8c3b-5d6e7f801234"

BLEService        accelService(UUID_SERVICE);
BLECharacteristic dataChar  (UUID_DATA,   BLENotify,            20);
BLECharacteristic statusChar(UUID_STATUS, BLERead | BLENotify,  14);

ICM_20948_SPI myICM;

// ---------------- Ring buffer ----------------
struct Sample { int16_t x, y, z; };
static Sample   ring[RING_SAMPLES];
static uint32_t head = 0, tail = 0;

static uint32_t totalSamples   = 0;
static uint32_t droppedSamples = 0;
static uint32_t highWater      = 0;
static uint16_t seq            = 0;
static uint8_t  flags          = 0;      // bit0 = ring overflowed this session

static inline uint32_t ringCount() {
  return (head >= tail) ? (head - tail) : (RING_SAMPLES - tail + head);
}

// =====================================================================
void setup() {
  pinMode(PIN_PWR_LED,  OUTPUT); digitalWrite(PIN_PWR_LED,  LOW);
  pinMode(PIN_STAT_LED, OUTPUT); digitalWrite(PIN_STAT_LED, LOW);

  // Power the IMU; keep microSD and Qwiic rails off -- we use neither.
  pinMode(PIN_IMU_POWER,     OUTPUT); digitalWrite(PIN_IMU_POWER,     HIGH);
  pinMode(PIN_MICROSD_POWER, OUTPUT); digitalWrite(PIN_MICROSD_POWER, HIGH);
  pinMode(PIN_QWIIC_POWER,   OUTPUT); digitalWrite(PIN_QWIIC_POWER,   HIGH);
  delay(100);                       // let the IMU rail settle before SPI

  SPI.begin();

  bool ok = false;
  for (int attempt = 0; attempt < 5 && !ok; attempt++) {
    myICM.begin(PIN_IMU_CHIP_SELECT, SPI, 4000000);
    ok = (myICM.status == ICM_20948_Stat_Ok);
    if (!ok) delay(200);
  }
  if (!ok) fatalBlink(2);           // 2 blinks = IMU not found

  if (configureIMU() != ICM_20948_Stat_Ok) fatalBlink(3);   // 3 = IMU config

  if (!BLE.begin()) fatalBlink(4);                          // 4 = BLE stack

  BLE.setLocalName("OLA-ACCEL");
  BLE.setDeviceName("OLA-ACCEL");
  BLE.setAdvertisedService(accelService);
  accelService.addCharacteristic(dataChar);
  accelService.addCharacteristic(statusChar);
  BLE.addService(accelService);
  BLE.advertise();
}

// ---------------------------------------------------------------------
ICM_20948_Status_e configureIMU() {
  myICM.swReset();   delay(250);
  myICM.sleep(false);
  myICM.lowPower(false);

  // Continuous (not duty-cycled) sampling of the accelerometer only.
  myICM.setSampleMode(ICM_20948_Internal_Acc, ICM_20948_Sample_Mode_Continuous);

  ICM_20948_fss_t fss;
  fss.a = gpm4;                       // +/-4 g -> 8192 LSB/g
  myICM.setFullScale(ICM_20948_Internal_Acc, fss);

  // Anti-alias filter. 136 Hz noise BW < 140.6 Hz Nyquist at 281.25 Hz. §2.
  ICM_20948_dlpcfg_t dlp;
  dlp.a = acc_d111bw4_n136bw;
  myICM.setDLPFcfg(ICM_20948_Internal_Acc, dlp);
  myICM.enableDLPF(ICM_20948_Internal_Acc, true);

  ICM_20948_smplrt_t rate;
  rate.a = ACCEL_SMPLRT_DIV;          // ODR = 1125/(1+3) = 281.25 Hz
  myICM.setSampleRate(ICM_20948_Internal_Acc, rate);

  return myICM.status;
}

// ---------------------------------------------------------------------
void loop() {
  BLEDevice central = BLE.central();
  if (!central) { digitalWrite(PIN_STAT_LED, LOW); return; }

  // A connection starts a fresh session: counters and buffer reset, so the
  // host always sees seq starting near zero with a known sample origin.
  head = tail = 0;
  totalSamples = droppedSamples = highWater = 0;
  seq = 0; flags = 0;
  myICM.getAGMT();                    // flush any stale reading
  digitalWrite(PIN_STAT_LED, HIGH);

  uint32_t lastStatus = millis();

  while (central.connected()) {
    pumpIMU();
    pumpBLE();
    if (millis() - lastStatus >= 1000) { sendStatus(); lastStatus = millis(); }
  }

  digitalWrite(PIN_STAT_LED, LOW);
}

// ---------------------------------------------------------------------
// Drain every sample the IMU has ready into the ring buffer.
void pumpIMU() {
  while (myICM.dataReady()) {
    myICM.getAGMT();

    uint32_t next = (head + 1) % RING_SAMPLES;
    if (next == tail) {              // ring full -- this is the ONLY loss path
      droppedSamples++;
      flags |= 0x01;
      return;                        // stop reading; let pumpBLE catch up
    }
    ring[head].x = myICM.agmt.acc.axes.x;
    ring[head].y = myICM.agmt.acc.axes.y;
    ring[head].z = myICM.agmt.acc.axes.z;
    head = next;
    totalSamples++;

    uint32_t n = ringCount();
    if (n > highWater) highWater = n;
  }
}

// ---------------------------------------------------------------------
// Emit as many 20-byte packets as the BLE stack will accept right now.
void pumpBLE() {
  uint8_t pkt[20];

  while (ringCount() >= SAMPLES_PER_PACKET) {
    pkt[0] = (uint8_t)(seq & 0xFF);
    pkt[1] = (uint8_t)(seq >> 8);

    uint32_t t = tail;
    for (int i = 0; i < SAMPLES_PER_PACKET; i++) {
      int o = 2 + i * 6;
      pkt[o + 0] = (uint8_t)(ring[t].x & 0xFF);
      pkt[o + 1] = (uint8_t)(ring[t].x >> 8);
      pkt[o + 2] = (uint8_t)(ring[t].y & 0xFF);
      pkt[o + 3] = (uint8_t)(ring[t].y >> 8);
      pkt[o + 4] = (uint8_t)(ring[t].z & 0xFF);
      pkt[o + 5] = (uint8_t)(ring[t].z >> 8);
      t = (t + 1) % RING_SAMPLES;
    }

    // Backpressure: a false return means the TX queue is full. Leave the
    // samples in the ring and try again next pass -- do NOT advance tail.
    if (!dataChar.writeValue(pkt, 20)) return;

    tail = t;
    seq++;
  }
}

// ---------------------------------------------------------------------
void sendStatus() {
  uint8_t s[14];
  memcpy(&s[0],  &totalSamples,   4);
  memcpy(&s[4],  &droppedSamples, 4);
  uint16_t hw  = (uint16_t)(highWater > 0xFFFF ? 0xFFFF : highWater);
  memcpy(&s[8],  &hw, 2);
  uint16_t odr = ODR_CENTI_HZ;
  memcpy(&s[10], &odr, 2);
  s[12] = ACCEL_FS_G;
  s[13] = flags;
  statusChar.writeValue(s, 14);
}

// ---------------------------------------------------------------------
// Blink a fault code forever. Count the blinks between long pauses.
void fatalBlink(int code) {
  while (true) {
    for (int i = 0; i < code; i++) {
      digitalWrite(PIN_STAT_LED, HIGH); delay(150);
      digitalWrite(PIN_STAT_LED, LOW);  delay(150);
    }
    delay(1200);
  }
}
```

**Points worth understanding before you modify it:**

- **`pumpBLE()` returning early on a failed `writeValue()` is the entire lossless mechanism.** The samples stay in the ring; `tail` does not advance; nothing is discarded. Delete that early return and you have a lossy streamer that looks identical until you run [test 4](#8--test-it).
- **`pumpIMU()` returns rather than overwriting on a full ring.** Overwriting oldest-first would corrupt the `seq`-to-sample-index mapping the host depends on. Dropping newest is the honest failure and it is counted.
- **A session resets on connect.** The host can therefore assume sample index `0` corresponds to `seq == 0`, packet offset `0`.
- **Status is sent once per second** on its own characteristic, keeping the 20-byte data packet free of metadata.

> ⚠️ Verify four API details against your installed library version before assuming a silent failure is a hardware problem:
> 1. That `ICM_20948_smplrt_t` uses field **`.a`** for the accelerometer divider.
> 2. That raw counts are reached as **`myICM.agmt.acc.axes.x`**.
> 3. That **`BLECharacteristic::writeValue()` returns `false`** on a full TX queue rather than blocking. This is the one that would quietly break losslessness — [test 4](#8--test-it) exists to catch it.
> 4. That **`central.connected()` services the BLE stack** on this port. The `while (central.connected())` pattern is standard ArduinoBLE and normally polls internally, but if the link stalls or drops after a second or two, add an explicit **`BLE.poll()`** as the first statement inside the loop. This is a two-character fix that is impossible to guess at from the symptom.

> **`getAGMT()` reads all sensors, not just the accelerometer.** It pulls accel, gyro, mag, and temperature over SPI every call. At 281.25 Hz that costs nothing you'll notice, and it keeps the code short. If you push the rate substantially higher, this becomes the first thing to optimise — read the accelerometer registers directly, or move to the FIFO path in [§12](#the-fifo-path-if-polling-proves-insufficient).

---

## §6 — Flashing

The Artemis ships with the **SparkFun Variable Loader (SVL)** bootloader, so no programmer is needed — just USB-C.

**Tool:** the [Artemis Firmware Upload GUI](https://github.com/sparkfun/Artemis-Firmware-Upload-GUI), also distributed as the *Artemis Uploader App* and installable as a Python package if no prebuilt binary suits your platform.

**Steps:**

1. Download and extract the Artemis Firmware Upload GUI. Run the executable for your OS from `/Windows`, `/OSX`, `/Linux`, or `/Raspberry_Pi__Debian`. It can take several seconds to appear — normal.
2. Connect the OpenLog Artemis over USB-C.
3. Choose the COM port (**Refresh** re-enumerates).
4. Select `OLA_Accel_BLE.ino.bin` from [§4](#4--the-docker-toolchain).
5. Set the baud rate. **921600** is the usual fast setting; drop to **115200** if uploads fail intermittently.
6. Press **Upload Firmware**.

**Going back to stock:** flash any `OpenLog_Artemis-V10-v211.bin` from the repo's [`/Binaries`](https://github.com/sparkfun/OpenLog_Artemis/tree/main/Binaries) folder. Note the `-V10-` in the filename — the `-X04-` files are for the original black board and are not interchangeable.

**If a flash fails and the board seems dead:** the SVL bootloader lives in a protected region and a failed application upload does not erase it. Retry at a lower baud rate. If the bootloader itself is damaged, the same GUI can reflash it over the Artemis's serial bootloader — this is genuinely hard to brick permanently.

---

## §7 — The host receiver

Python with [`bleak`](https://github.com/hbldh/bleak), which speaks native BLE on Windows, macOS, and Linux.

```
pip install bleak
```

```python
#!/usr/bin/env python3
"""Receive the OLA-ACCEL BLE stream and write a CSV.

Usage:  python ola_receive.py out.csv [seconds]
"""
import asyncio, csv, struct, sys
from bleak import BleakClient, BleakScanner

UUID_DATA   = "f1b7a2c1-9e4d-4a1f-8c3b-5d6e7f801234"
UUID_STATUS = "f1b7a2c2-9e4d-4a1f-8c3b-5d6e7f801234"

DEVICE_NAME        = "OLA-ACCEL"
SAMPLES_PER_PACKET = 3
LSB_PER_G          = 8192.0        # gpm4; see §10 for the other ranges
DEFAULT_ODR        = 281.25        # overwritten by the first status packet


class Receiver:
    def __init__(self, writer):
        self.w = writer
        self.prev_seq = None
        self.unwrapped = 0          # absolute packet index across seq wraps
        # Data notifications start immediately; the first status packet is a
        # second behind it. Seed the ODR so that first second of samples still
        # gets a timestamp instead of an empty column.
        self.odr = DEFAULT_ODR
        self.received = 0
        self.gap_packets = 0

    def on_data(self, _handle, data: bytearray):
        if len(data) != 20:
            print(f"  ! short packet: {len(data)} bytes")
            return

        seq = struct.unpack_from("<H", data, 0)[0]

        # Unwrap the 16-bit counter into a monotonic packet index, and count
        # any packets that never arrived (disconnect gaps, ring overflow).
        if self.prev_seq is None:
            self.unwrapped = seq
        else:
            delta = (seq - self.prev_seq) & 0xFFFF
            if delta != 1:
                self.gap_packets += delta - 1
                print(f"  ! gap: {delta - 1} packet(s) missing before seq={seq}")
            self.unwrapped += delta
        self.prev_seq = seq

        base = self.unwrapped * SAMPLES_PER_PACKET
        for i in range(SAMPLES_PER_PACKET):
            x, y, z = struct.unpack_from("<hhh", data, 2 + i * 6)
            idx = base + i
            self.w.writerow([idx,
                             f"{idx / self.odr:.6f}",
                             f"{x / LSB_PER_G:.6f}",
                             f"{y / LSB_PER_G:.6f}",
                             f"{z / LSB_PER_G:.6f}"])
            self.received += 1

    def on_status(self, _handle, data: bytearray):
        if len(data) != 14:
            return
        total, dropped, highwater, odr_centi, fs_g, flags = \
            struct.unpack("<IIHHBB", data)
        self.odr = odr_centi / 100.0
        note = "  OVERFLOWED" if flags & 0x01 else ""
        print(f"  status: sampled={total} dropped={dropped} "
              f"peak_buffer={highwater} odr={self.odr} Hz fs=+/-{fs_g}g{note}")


async def main(path, duration):
    print(f"scanning for {DEVICE_NAME} ...")
    dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=15.0)
    if dev is None:
        sys.exit(f"{DEVICE_NAME} not found. Is it powered and not already connected?")

    print(f"connecting to {dev.address}")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["sample_index", "t_seconds", "ax_g", "ay_g", "az_g"])
        rx = Receiver(w)

        async with BleakClient(dev) as client:
            await client.start_notify(UUID_STATUS, rx.on_status)
            await client.start_notify(UUID_DATA,   rx.on_data)
            print(f"streaming for {duration} s -> {path}   (ctrl-C to stop)")
            try:
                await asyncio.sleep(duration)
            except asyncio.CancelledError:
                pass
            await client.stop_notify(UUID_DATA)
            await client.stop_notify(UUID_STATUS)

    expected = rx.received + rx.gap_packets * SAMPLES_PER_PACKET
    loss = 100.0 * rx.gap_packets * SAMPLES_PER_PACKET / expected if expected else 0.0
    print(f"\nwrote {rx.received} samples to {path}")
    print(f"missing {rx.gap_packets * SAMPLES_PER_PACKET} samples ({loss:.4f}%)")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "accel.csv"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    try:
        asyncio.run(main(out, secs))
    except KeyboardInterrupt:
        pass
```

**Two things this does that a naive receiver does not:**

- **Unwraps `seq` into an absolute packet index.** The counter is 16-bit and wraps every 65,536 packets — about **11.6 minutes** at 93.75 packets/sec. Without unwrapping, any capture longer than that silently folds its time axis back on itself.
- **Counts what never arrived.** `gap_packets` is derived from `seq` discontinuities, so the closing loss percentage is a measurement, not an assumption. If it prints `0.0000%` you have genuinely lossless capture — which is the whole point of the ring buffer.

The firmware's own `dropped` counter and the host's `gap_packets` measure different things and should be cross-checked: firmware-side drops mean the ring overflowed, host-side gaps mean packets never made it. In a healthy run both are zero.

---

## §8 — Test it

Seven tests, ordered so the riskiest assumptions fail earliest. Run 1–3 before trusting any data.

**Test 1 — Advertising.** Run `python -c "import asyncio,bleak; print(asyncio.run(bleak.BleakScanner.discover()))"` and look for `OLA-ACCEL`.
> Fails → the sketch never reached `BLE.advertise()`. Check the fault blink code on `PIN_STAT_LED` first: **2 = IMU not found, 3 = IMU config failed, 4 = BLE stack failed.**

**Test 2 — Boot survival.** Power the board and watch for 60 seconds with a central connected.
> **This is the ArduinoBLE version test.** A board that flashes fine, advertises, then dies on connect — or never advertises at all despite a clean compile — is the ≥1.2.0 hard-fault described in [§4](#4--the-docker-toolchain). Confirm with `arduino-cli lib list` inside the image. Nothing else in this build fails this way.

**Test 3 — Rate.** Capture 60 s and check the sample count.
> Expect **281.25 × 60 ≈ 16,875 samples**, within 1%. Fails high or low → wrong `ACCEL_SMPLRT_DIV`, or `setSampleRate` did not take because `ICM_20948_smplrt_t.a` is not the field name assumed. **This test proves the premise of the whole build** — everything downstream is defined in terms of a known ODR.

**Test 4 — Losslessness.** Capture 10 minutes sitting still, near the host.
> `gap_packets` must be **0** and the firmware's `dropped` must be **0**. Any loss under ideal conditions means backpressure is not working — most likely `writeValue()` does not return `false` as assumed, so `pumpBLE()` never applies backpressure and the ring silently overruns. This is the single most important test in the guide.

**Test 5 — Buffer depth and recovery.** Start a capture, walk out of range for ~30 s, come back.
> The stream should resume and `peak_buffer` should climb toward `RING_SAMPLES` without `OVERFLOWED` appearing. If it overflows, either raise `RING_SAMPLES` (you have RAM headroom) or accept a shorter stall tolerance. Use the reported `peak_buffer` to size the buffer honestly rather than guessing.

**Test 6 — Signal sanity.** Rest the board flat on a table.
> One axis must read **≈ ±1.000 g** and the other two **≈ 0.000 g**. Rotate through all six orientations and confirm each axis reaches ±1 g. Fails with all-zero readings → IMU power pin polarity or chip-select ([§3](#3--hardware-setup)). Fails with wrong scaling → `LSB_PER_G` does not match the configured full-scale range ([§10](#10--scaling-calibration-and-what-the-numbers-mean)).

**Test 7 — Aliasing.** Excite the board at a known frequency above Nyquist — a small motor, a speaker cone, or a vibration table at, say, **200 Hz** — and FFT the capture.
> With ODR 281.25 Hz, a 200 Hz input must **not** produce a peak at `|200 − 281.25| = 81.25 Hz`. If it does, the DLPF is not enabled or is set wider than `acc_d111bw4_n136bw`, and your 80 Hz band is being contaminated by out-of-band energy. This validates the entire rate/filter argument in [§2](#picking-the-sample-rate--the-anti-alias-constraint-decides-it).

---

## §9 — Troubleshooting

**Blinks 2, 3, or 4 forever on the status LED.**
IMU not found / IMU config failed / BLE stack failed to start, respectively. Blink 2 is usually the IMU power pin polarity or a wrong chip-select; blink 4 is almost always the ArduinoBLE version.

**Compiles perfectly, then the board is dead on boot.**
ArduinoBLE ≥ 1.2.0. See [§4](#4--the-docker-toolchain). This is the single most likely thing to go wrong and it does not look like a software problem.

**Advertises, but the host cannot connect.**
Something else is already connected — BLE peripherals accept one central at a time. Close phone apps like nRF Connect. On Windows, also un-pair the device from Bluetooth settings; pairing is unnecessary here and can hold the link.

**All readings are exactly zero.**
The IMU is unpowered or SPI is not reaching it. Check `PIN_IMU_POWER` polarity, confirm `PIN_IMU_CHIP_SELECT` is 44 for your board revision, and confirm you are using `ICM_20948_SPI` and not the I²C class.

**Readings are 2×, 4×, or 8× off.**
`LSB_PER_G` in the host script disagrees with `ACCEL_FS_G` in the firmware. See the table in [§10](#10--scaling-calibration-and-what-the-numbers-mean).

**`gap` messages appear constantly, even close to the host.**
Backpressure is not working — see [test 4](#8--test-it). Failing that, another 2.4 GHz source is stepping on the link; try a different room or channel-hop by moving the host.

**`OVERFLOWED` appears whenever you move away from the computer.**
The ring buffer is smaller than your stall duration. Raise `RING_SAMPLES`; at 6 bytes/sample you have a lot of headroom in 384 KB. Confirm the new size actually links — a too-large static array fails at link time, not at run time.

**Sample count drifts from 281.25 Hz over long captures.**
Expected at the margins: the ICM-20948's internal oscillator has real tolerance. For absolute timing, discipline the sample index against the host clock over a long window rather than trusting the nominal ODR.

**Sample count is consistently *below* the configured ODR.**
Different problem from drift — the polled read loop is missing samples. Confirm the divider first ([test 3](#8--test-it)), then switch to the IMU's FIFO instead of polling one sample at a time; see [§12](#the-fifo-path-if-polling-proves-insufficient).

**Data stops after ~11.6 minutes, or the time axis folds back.**
The `seq` unwrap is not working on the host. This is exactly the 65,536-packet wrap described in [§7](#7--the-host-receiver).

**Connection drops repeatedly at modest range.**
The Artemis transmits at ~4 dBm and the antenna is on the board. Metal enclosures, USB 3 ports, and being on the far side of a body all cost significant range. Test line-of-sight first to separate a radio problem from a firmware one.

---

## §10 — Scaling, calibration, and what the numbers mean

**Raw counts to g.** The ICM-20948 returns signed 16-bit counts whose meaning depends entirely on the configured full-scale range:

| Full scale | Enum    | Sensitivity     | Use when                             |
|------------|---------|-----------------|--------------------------------------|
| ±2 g       | `gpm2`  | **16384 LSB/g** | tilt, gentle motion, best resolution |
| ±4 g       | `gpm4`  | **8192 LSB/g**  | **default here** — general vibration |
| ±8 g       | `gpm8`  | **4096 LSB/g**  | impacts, machinery                   |
| ±16 g      | `gpm16` | **2048 LSB/g**  | shock events                         |

```
  acceleration_in_g = raw_count / sensitivity
```

Change `ACCEL_FS_G` and the `fss.a` enum in the firmware together, and `LSB_PER_G` in the host script to match. They are three places holding one fact — [test 6](#8--test-it) exists because that is easy to get wrong.

**Choosing the range.** Pick the smallest range your signal will not clip. Clipping is unrecoverable and creates harmonics at frequencies your analysis will happily believe. Remember that **gravity occupies 1 g of your budget at all times** — at ±2 g, a board that is merely tilted has already spent half its range.

**Where the filter sits.** The chosen `acc_d111bw4_n136bw` gives a 3 dB corner at **111.4 Hz** with a **136 Hz** noise bandwidth, against a **140.6 Hz** Nyquist. So:

- **0–80 Hz** — your band of interest, essentially flat.
- **80–111 Hz** — usable but rolling off; correct for the filter response if you care about amplitudes here.
- **111–140 Hz** — heavily attenuated.
- **Above 140.6 Hz** — attenuated by the DLPF before it can alias. This is the property [test 7](#8--test-it) verifies.

If your real band is narrower than 80 Hz, drop to a tighter DLPF and a lower ODR: `acc_d50bw4_n68bw8` at 160.7 Hz (`ACCEL_SMPLRT_DIV = 6`) is a clean, fully anti-aliased 50 Hz system at 57% of the data rate.

**Timestamps.** The host reconstructs time as `sample_index / ODR`. This is exact relative to the IMU's own clock and has no jitter — a real advantage of a fixed-ODR streamer over timestamping each arrival, since BLE arrival times say nothing about when a sample was taken. For absolute alignment to other instruments, capture a shared physical event (a sharp tap visible in both) and align on it.

**Zero-g offset.** Every accelerometer has a per-axis bias. Calibrate by averaging a few seconds in all six orientations: for each axis, `bias = (reading_up + reading_down) / 2`, and `scale = (reading_up − reading_down) / 2`. Subtract the bias and divide by the scale. Worth doing once and storing on the host — there is no reason to spend firmware on it.

---

## §11 — Safety

This is a low-voltage USB and LiPo project. Nothing here can hurt you if the battery is treated properly.

⚠️ **The LiPo is the only real hazard.** The OpenLog Artemis charges at 450 mA/hr through its USB-C port. Use a cell with integrated protection, never charge a physically damaged or swollen pack, do not charge unattended in an enclosed space, and observe polarity — the JST connector is keyed but aftermarket cells are sometimes wired backwards. A reverse-connected LiPo can damage the board and vent the cell.

⚠️ **Mounting for vibration measurement.** If you are attaching this to running machinery, the mechanical risk exceeds the electrical one by a wide margin. Secure the board and the battery so neither can be flung loose, and keep the LiPo away from anything hot or pinching.

**Not a hazard, despite appearances:** a failed firmware flash. The SVL bootloader lives in a protected region and survives a bad application image. See [§6](#6--flashing) for recovery.

---

## §12 — Research findings and paths not taken

Everything below was established while researching this build but sits outside the path the guide takes. It is recorded so the next person — or a cold session — does not have to rediscover it.

### What SparkFun's BLE branch actually changed

The commit [`c4e6ef1`](https://github.com/sparkfun/OpenLog_Artemis/commit/c4e6ef14162c5cab1f81df141638f1a16c6fbf03) touches **five files**. Useful if you ever want to resurrect it rather than replace it:

**`settings.h`** — one line in `struct_settings`
> `bool useBLE = false;` — the persisted enable flag.

**`OpenLog_Artemis.ino`** — the bulk of it (+271 lines)
> `#include <ArduinoBLE.h>`; `#define kTargetServiceUUID "2488bd28-b1df-4fe0-8611-22fda7c645f0"`; `#define kTargetServiceName "OpenLog Artemis"`; `#define kMaxCharacteristics 50`; `#define kMessageMax 50`; fifty `kCharacteristicUUID00`–`49` defines; `BLEService bleService(...)` plus fifty `BLEStringCharacteristic bleCharacteristicNN(..., BLERead | BLENotify, kMessageMax)`; globals `numBLECharacteristics`, `bleCharacteristicsValues[50][50]`, `usingBLE`. In `setup()`: `BLE.begin()`, `setLocalName`, `setDeviceName`, `setAdvertisedService`, `addService`, `advertise`. In `loop()`: `BLEDevice central = BLE.central();`.

**`Sensors.ino`** — the plumbing (+513 lines)
> New `void updateBLECharacteristic(int *theBLECharacteristic, char *theString)`, called from `getData()` and from `gatherDeviceValues()` (whose signature gained an `int *bleCharacteristic` parameter) once per emitted value. `printHelperText()` increments `numBLECharacteristics` per enabled field.

**`menuMain.ino`** — `b) BLE: Enabled/Disabled`, toggled with `settings.useBLE ^= 1;`.

**`nvm.ino`** — persistence: `settingsFile.println("useBLE=" + (String)settings.useBLE);` in `recordSystemSettingsToFile()`, and a matching `else if (strcmp(settingName, "useBLE") == 0) settings.useBLE = d;` in `parseLine()`.

### If you port that branch forward, do not replay its signature change

This is the trap in resurrecting the branch, and it is not obvious from the diff.

The 2021 branch changed `printHelperText(bool terminalOnly)` into `printHelperText(bool terminalPrint, bool filePrint)` so it could ask for terminal-only output while counting BLE fields. **Current firmware has since refactored the same function into a bitmask.** From `Firmware/OpenLog_Artemis/Sensors.h` on `main`:

```c
#define OL_OUTPUT_SERIAL   	0x1
#define OL_OUTPUT_SDCARD	0x2

void printHelperText(uint8_t);
void getData(char *buffer, size_t lenBuffer);
```

So the clean modern port is **to add a third bit**, not to reintroduce two booleans:

```c
#define OL_OUTPUT_BLE       0x4
```

Note also that `getData()` now writes into a caller-supplied buffer rather than returning one. Replaying the old signatures onto current firmware would fight the refactor and produce a conflict-ridden merge for no benefit.

### Where SparkFun's own documentation disagrees

⚠️ Published maximum IMU rates are inconsistent across SparkFun's own material:

- The repo README says OLA "can be configured to take readings at **500 times a second**" and that the ICM-20948 is "capable of nearly **1 kHz** logging of all 9 axis."
- SparkFun product pages describe the same board as capable of "nearly **250 Hz** logging of all 9 axes."

Neither figure is load-bearing here — this build sets the rate directly through `ACCEL_SMPLRT_DIV` and verifies it by measurement in [test 3](#8--test-it) — but do not plan a design around either number without confirming it on hardware.

### The FIFO path, if polling proves insufficient

The firmware in [§5](#5--the-firmware) polls `dataReady()` and reads one sample at a time. At 281.25 Hz that is comfortable. If you later raise the rate and the sample count starts falling short of the configured ODR, the escape hatch is the IMU's own buffer rather than a faster loop:

- The ICM-20948 has a **512-byte FIFO**, letting the host collect many readings in one transaction instead of polling per sample.
- The SparkFun library exposes `readDMPdataFromFIFO()`, which returns **`ICM_20948_Stat_FIFOMoreDataAvail`** when a valid frame was read *and* more remains — so you drain in a `while` loop until it stops saying so.
- `initializeDMP()` (library ≥ 1.2.5) downloads the DMP firmware and configures the registers; you then select sensors and reset/start the FIFO and DMP.
- Worked examples: `Example10_DMP_FastMultipleSensors` and `Example8_DMP_RawAccel` in the library.

> ⚠️ Note the FIFO is small relative to high rates. At 1 kHz with 12-byte accel+gyro frames it fills in roughly **43 ms**, so the drain loop must run more often than that — which in turn constrains how long the BLE stack may block. This is the constraint that makes very high rates hard, not the radio.

### Toolchain facts this build does not use

**The Arduino IDE route exists but is deprecated.** `COMPILE_BINARY.md` documents it: **Arduino IDE 1.8.19** (v2 untested), Apollo3 core **2.2.1** via the board-manager URL `https://raw.githubusercontent.com/sparkfun/Arduino_Apollo3/main/package_sparkfun_apollo3_index.json`, board **"SparkFun Apollo3 → RedBoard Artemis ATP"**, the same six-file `UartPower3` patch applied by hand, then *Sketch → Export compiled Binary*. SparkFun explicitly recommends Docker over this.

**The stock Dockerfile already defaults to V10.** Its `HARDWARE_VERSION_MAJOR/MINOR` `sed` lines are **commented out**, and the sketch's own defaults are 1.0 — so an unmodified build targets the red board. Uncommenting them rewrites the defines to `0` / `4` for the original black X04. This is why `/Binaries` ships parallel `-V10-` and `-X04-` files for every version.

**Stock builds enable the IMU's DMP by `sed`.** The Dockerfile uncomments a line in the library's `ICM_20948_C.h` to switch DMP support on. The build in [§4](#4--the-docker-toolchain) omits this because the polled path does not use the DMP — you would need it if you took the FIFO route above.

**Stock builds install 33 libraries.** Only `SparkFun 9DoF IMU Breakout - ICM 20948@1.3.0` matters here; `SdFat@2.2.2` and the ~31 other sensor libraries exist to support OLA's autodetected Qwiic devices. Dropping them is where most of the build-time saving comes from.

**The core patch lives in the repo.** `Firmware/Extras/UartPower3.zip` (7.2 MB) plus a small `Firmware/Extras/spi.diff`. The Docker build context is `Firmware/`, which is why the Dockerfile in [§4](#4--the-docker-toolchain) copies from `Extras/`.

### Verification status of the claims in this guide

**Live-fetched from source, 2026-08-28** — repo contents API for `Firmware/`, `Firmware/Extras/`, `Firmware/OpenLog_Artemis/`, `Binaries/`, and the branch list; `COMPILE_BINARY.md`; `UPGRADE.md`; `CHANGELOG.md`; `README.md`; `Firmware/Dockerfile`; `Firmware/compile_with_docker.bat`; `Sensors.h`; the V10 pin map and IMU init from `OpenLog_Artemis.ino`; the `c4e6ef1` patch; `settings.h` on the BLE branch; ICM-20948 library headers and `Example2_Advanced`; both SparkFun forum threads.

**Search-confirmed only, not read at source** — ICM-20948 ODR formula and sensitivity figures, ArduinoBLE/Apollo3 hard-fault reports, BLE throughput ranges, Artemis MTU cap, ESP-style packet-rate estimates.

**Not verified at all — these need hardware** — the `ICM_20948_smplrt_t.a` field name; the `myICM.agmt.acc.axes.x` raw accessor; whether `BLECharacteristic::writeValue()` returns `false` on a full TX queue (the mechanism losslessness depends on); whether `central.connected()` alone services the BLE stack or an explicit `BLE.poll()` is required; and the polarity of `PIN_IMU_POWER` / `PIN_MICROSD_POWER` / `PIN_QWIIC_POWER`. [§8](#8--test-it) is ordered to fail on these first.

> The `UartPower3` `cp` destinations were on this list in an earlier draft and are **no longer** — they are now copied verbatim from SparkFun's Dockerfile. Three of the six are in `mbed-bridge/core-extend/`, `mbed-bridge/core-implement/`, and `.../TARGET_Apollo3/device/`, none of which are guessable from the core layout. If you are adapting this guide to a different Apollo3 core version, re-read those paths from source rather than assuming.

---

## What it unlocks

- **Untethered vibration measurement.** Machinery, HVAC, vehicles, structures — anything where dragging a USB cable is impractical or unsafe. An 80 Hz band covers most rotating-machinery fundamentals and their first few harmonics.
- **Live monitoring instead of post-hoc logging.** The OpenLog Artemis normally hands you a CSV after the fact. Streaming means you see the signal while the experiment runs and can stop, adjust, and retry immediately — which usually saves more time than the data rate ever costs.
- **Motion capture on moving subjects.** Gait, sports biomechanics, animal movement. The stock logger already does this; BLE removes the retrieve-and-download cycle between trials.
- **A template for the other sensors.** Everything here — the ring buffer, the packet format, the backpressure loop, the `seq` accounting, the `bleak` receiver — is sensor-agnostic. Swapping in gyro, magnetometer, or a Qwiic sensor is a change to `pumpIMU()` and the sample struct. Re-run the [§2](#2--the-data-budget) arithmetic first; a wider payload can push you into the MTU negotiation problem this build avoided.
- **A reusable discipline.** "Compute the data budget first, pick the sample rate from the anti-alias filter rather than the signal, pack multiple samples per packet, and number everything so loss is measurable" is the correct skeleton for any wireless sensor stream.

---

## Further reading

- **[SparkFun OpenLog Artemis repository](https://github.com/sparkfun/OpenLog_Artemis)** — `COMPILE_BINARY.md` for the full stock build (including the deprecated Arduino IDE route), `UPGRADE.md` for flashing, and `/Binaries` for stock images to restore. The V10 pin map lives at the top of `Firmware/OpenLog_Artemis/OpenLog_Artemis.ino`.
- **[`Initial_BLE_Support` branch](https://github.com/sparkfun/OpenLog_Artemis/tree/Initial_BLE_Support)** and its [patch](https://github.com/sparkfun/OpenLog_Artemis/commit/c4e6ef14162c5cab1f81df141638f1a16c6fbf03) — SparkFun's abandoned attempt. Worth reading to see the characteristic-per-column design and why it does not suit streaming.
- **[SparkFun forum: OpenLog Artemis with BLE](https://community.sparkfun.com/viewtopic.php?t=56202)** — PaulZC's own account of why BLE was dropped. The clearest statement of what you are taking on.
- **[SparkFun forum: Getting the most out of BLE 5.0 on Artemis](https://community.sparkfun.com/viewtopic.php?t=56499)** — the `HCI_DRV_MAX_TX_PACKET` 256-byte cap, the 242 usable bytes, silent truncation, and the `getMtuLength()` accessor. Essential if you ever outgrow 20-byte packets.
- **[Arduino_Apollo3 issue #362](https://github.com/sparkfun/Arduino_Apollo3/issues/362)** and **[#291](https://github.com/sparkfun/Arduino_Apollo3/issues/291)** — the ArduinoBLE version incompatibility, in the reporters' own words. Read these before debugging a boot hang.
- **[TDK InvenSense ICM-20948 datasheet (DS-000189)](https://cdn.sparkfun.com/assets/7/f/e/c/d/DS-000189-ICM-20948-v1.3.pdf)** — the authority for the ODR divider, DLPF bandwidth table, full-scale sensitivities, and noise density. The electrical-characteristics section and the register map are what you want. Link is SparkFun's mirror (v1.3); TDK hosts v1.5 at `product.tdk.com` but blocks automated fetches.
- **[SparkFun ICM-20948 Arduino Library](https://github.com/sparkfun/SparkFun_ICM-20948_ArduinoLibrary)** — `Example2_Advanced` is the reference for `setFullScale`, `setDLPFcfg`, and `setSampleRate`; `DMP.md` covers the FIFO and DMP path if you later need rates the polled approach cannot sustain.
- **[ArduinoBLE documentation](https://www.arduino.cc/reference/en/libraries/arduinoble/)** — `BLECharacteristic`, `writeValue`, and the peripheral lifecycle. Note the reference documents current versions; you are pinned to 1.1.3.
- **[bleak](https://github.com/hbldh/bleak)** — cross-platform BLE for Python. The `BleakScanner` and `start_notify` docs cover everything in [§7](#7--the-host-receiver).
- **[Artemis Firmware Upload GUI](https://github.com/sparkfun/Artemis-Firmware-Upload-GUI)** and **[Apollo3 SVL bootloader](https://github.com/sparkfun/Apollo3_Uploader_SVL)** — the flashing tool and the bootloader it talks to.
