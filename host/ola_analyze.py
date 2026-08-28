#!/usr/bin/env python3
"""Check a capture from ola_receive.py against the guide's tests 3, 6 and 7.

    python ola_analyze.py accel.csv
    python ola_analyze.py accel.csv --excite 200     # test 7, aliasing
    python ola_analyze.py accel.csv --plot           # needs matplotlib

Test 3 (rate)      - sample count against the wall-clock duration in the
                     .meta.json sidecar, and index continuity.
Test 6 (sanity)    - at rest one axis reads +/-1.000 g, the other two 0.000 g,
                     and the vector magnitude is 1.000 g.
Test 7 (aliasing)  - excite the board above Nyquist with --excite F and this
                     reports whether energy appeared at the folded frequency.
                     It must not.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

try:
    import numpy as np
except ImportError:  # pragma: no cover - dependency hint
    sys.exit("numpy is required for analysis.  pip install -r requirements.txt")

DEFAULT_ODR_HZ = 281.25


def load_csv(path):
    idx, t, ax, ay, az = [], [], [], [], []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header or header[0] != "sample_index":
            sys.exit(f"{path} does not look like an ola_receive.py CSV")
        for row in reader:
            if len(row) < 5:
                continue
            idx.append(int(row[0]))
            t.append(float(row[1]))
            ax.append(float(row[2]))
            ay.append(float(row[3]))
            az.append(float(row[4]))
    if not idx:
        sys.exit(f"{path} contains no samples")
    return (
        np.array(idx, dtype=np.int64),
        np.array(t),
        np.column_stack([ax, ay, az]),
    )


def load_meta(csv_path):
    meta_path = csv_path + ".meta.json"
    if not os.path.exists(meta_path):
        return None
    try:
        with open(meta_path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def longest_contiguous(idx):
    """Largest run of consecutive sample indices, as a (start, stop) slice."""
    breaks = np.flatnonzero(np.diff(idx) != 1)
    starts = np.concatenate(([0], breaks + 1))
    stops = np.concatenate((breaks + 1, [len(idx)]))
    lengths = stops - starts
    best = int(np.argmax(lengths))
    return int(starts[best]), int(stops[best])


def rule(title):
    print(f"\n{title}\n{'-' * len(title)}")


def test_rate(idx, meta, odr):
    rule("Test 3 - rate")
    n = len(idx)
    span = int(idx[-1] - idx[0]) + 1
    missing = span - n
    print(f"  samples written      : {n}")
    print(f"  index span           : {idx[0]} .. {idx[-1]}  ({span} slots)")
    print(f"  missing within span  : {missing}")

    if meta and meta.get("duration_s"):
        duration = float(meta["duration_s"])
        measured = n / duration if duration else 0.0
        expected = odr * duration
        err = 100.0 * (measured - odr) / odr if odr else 0.0
        print(f"  wall-clock duration  : {duration:.2f} s (from .meta.json)")
        print(f"  expected at {odr} Hz : {expected:.0f} samples")
        print(f"  measured rate        : {measured:.2f} Hz  ({err:+.2f}%)")
        if abs(err) <= 1.0:
            print("  PASS  within 1% of the configured ODR")
        else:
            print(
                "  FAIL  more than 1% off. Check ACCEL_SMPLRT_DIV, and whether\n"
                "        setSampleRate() took (ICM_20948_smplrt_t field name)."
            )
    else:
        print("  (no .meta.json sidecar -- cannot check the rate against a clock)")

    if missing == 0:
        print("  PASS  no gaps: every sample index between first and last arrived")
    else:
        loss = 100.0 * missing / span
        print(f"  FAIL  {missing} samples ({loss:.4f}%) never arrived -- see test 4")


def test_sanity(data, odr):
    rule("Test 6 - signal sanity")
    names = ("ax", "ay", "az")
    for i, name in enumerate(names):
        col = data[:, i]
        print(
            f"  {name}: mean {col.mean():+8.4f} g   sd {col.std():7.4f} g   "
            f"min {col.min():+8.4f}   max {col.max():+8.4f}"
        )
    mag = np.linalg.norm(data, axis=1)
    print(f"  |a|: mean {mag.mean():8.4f} g   sd {mag.std():7.4f} g")

    if abs(mag.mean() - 1.0) < 0.05 and mag.std() < 0.05:
        print("  PASS  at rest and reading 1 g -- scale and orientation look right")
    elif np.allclose(data, 0.0):
        print(
            "  FAIL  every reading is zero: IMU power pin polarity or chip select.\n"
            "        See the guide's section 3 and troubleshooting."
        )
    elif mag.std() >= 0.05:
        print("  (board was moving -- rerun at rest to check the 1 g magnitude)")
    else:
        ratio = mag.mean()
        print(
            f"  FAIL  magnitude is {ratio:.3f} g, not 1 g. If it is ~2x, ~4x or ~8x\n"
            "        off, LSB_PER_G disagrees with ACCEL_FS_G in the firmware."
        )

    clipped = int(np.sum(np.abs(data) >= 3.99))
    if clipped:
        print(f"  ! {clipped} sample(s) at or beyond full scale -- widen the range")


def spectrum(sig, odr):
    n = len(sig)
    win = np.hanning(n)
    sig = (sig - sig.mean()) * win
    mag = np.abs(np.fft.rfft(sig)) * (2.0 / np.sum(win))
    freq = np.fft.rfftfreq(n, d=1.0 / odr)
    return freq, mag


def alias_of(f_in, odr):
    """Where a tone at f_in lands after sampling at odr."""
    return abs(f_in - odr * round(f_in / odr))


def test_aliasing(idx, data, odr, excite, top_n):
    rule("Test 7 - spectrum and aliasing")
    start, stop = longest_contiguous(idx)
    block = data[start:stop]
    n = len(block)
    if n < 256:
        print(f"  contiguous block is only {n} samples -- too short for an FFT")
        return
    print(f"  FFT over {n} contiguous samples ({n / odr:.1f} s), Nyquist {odr / 2:.2f} Hz")

    mag_total = None
    for i in range(3):
        freq, mag = spectrum(block[:, i], odr)
        mag_total = mag if mag_total is None else mag_total + mag
    mag_total /= 3.0

    floor = float(np.median(mag_total[1:]))
    print(f"  noise floor (median bin): {floor:.6g} g")

    order = np.argsort(mag_total[1:])[::-1] + 1
    print(f"  strongest {top_n} components:")
    shown = 0
    seen = []
    for k in order:
        f = freq[k]
        if any(abs(f - s) < 1.0 for s in seen):
            continue  # same peak, adjacent bin
        seen.append(f)
        print(f"    {f:8.2f} Hz   {mag_total[k]:.6g} g   ({mag_total[k] / floor:6.1f}x floor)")
        shown += 1
        if shown >= top_n:
            break

    if excite is None:
        print("  (pass --excite F to check a known out-of-band tone)")
        return

    if excite <= odr / 2:
        print(f"  --excite {excite} Hz is below Nyquist; it should appear as itself.")
        target = excite
    else:
        target = alias_of(excite, odr)
        print(f"  a {excite} Hz input would alias to {target:.2f} Hz if not filtered")

    band = (freq >= target - 2.0) & (freq <= target + 2.0)
    if not band.any():
        print("  target frequency is outside the analysed band")
        return
    peak = float(mag_total[band].max())
    ratio = peak / floor if floor else float("inf")
    print(f"  energy at {target:.2f} +/- 2 Hz: {peak:.6g} g  ({ratio:.1f}x floor)")
    if excite <= odr / 2:
        print("  (in-band tone: a strong peak here is expected, not a failure)")
    elif ratio < 5.0:
        print("  PASS  no alias -- the DLPF removed the tone before sampling")
    else:
        print(
            "  FAIL  the out-of-band tone folded into the data. The DLPF is off or\n"
            "        set wider than acc_d111bw4_n136bw. See the guide's section 10."
        )


def plot(idx, t, data, odr):  # pragma: no cover - interactive
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("\n(matplotlib not installed; skipping --plot)")
        return
    start, stop = longest_contiguous(idx)
    block = data[start:stop]
    freq, mag = spectrum(np.linalg.norm(block, axis=1), odr)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7))
    for i, name in enumerate(("ax", "ay", "az")):
        ax1.plot(t, data[:, i], linewidth=0.6, label=name)
    ax1.set_xlabel("time (s)")
    ax1.set_ylabel("acceleration (g)")
    ax1.legend(loc="upper right")
    ax1.grid(alpha=0.3)

    ax2.semilogy(freq[1:], mag[1:], linewidth=0.7)
    ax2.axvline(odr / 2, linestyle="--", linewidth=1, label=f"Nyquist {odr / 2:.1f} Hz")
    ax2.axvline(111.4, linestyle=":", linewidth=1, label="DLPF 111.4 Hz")
    ax2.set_xlabel("frequency (Hz)")
    ax2.set_ylabel("|a| (g)")
    ax2.legend(loc="upper right")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    plt.show()


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("csv", help="capture written by ola_receive.py")
    p.add_argument("--odr", type=float, help="override the ODR in Hz")
    p.add_argument("--excite", type=float, help="known excitation frequency, Hz")
    p.add_argument("--top", type=int, default=6, help="spectral peaks to list")
    p.add_argument("--plot", action="store_true", help="plot time series and spectrum")
    args = p.parse_args(argv)

    idx, t, data = load_csv(args.csv)
    meta = load_meta(args.csv)
    odr = args.odr or (meta or {}).get("odr_hz") or DEFAULT_ODR_HZ

    print(f"{args.csv}: {len(idx)} samples, ODR {odr} Hz", end="")
    if meta:
        print(f", full scale +/-{meta.get('full_scale_g', '?')} g")
        if meta.get("gap_packets"):
            print(
                f"  receiver reported {meta['gap_packets']} lost packet(s) "
                f"= {meta['missing_samples']} samples ({meta['loss_percent']}%)"
            )
        fw = meta.get("firmware_status") or {}
        if fw.get("dropped_samples"):
            print(f"  firmware reported {fw['dropped_samples']} dropped sample(s)")
    else:
        print(" (no .meta.json sidecar)")

    test_rate(idx, meta, odr)
    test_sanity(data, odr)
    test_aliasing(idx, data, odr, args.excite, args.top)

    if args.plot:
        plot(idx, t, data, odr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
