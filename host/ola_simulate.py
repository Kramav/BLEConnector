#!/usr/bin/env python3
"""Generate a synthetic capture, without any hardware.

Feeds firmware-shaped packets through the real receiver pipeline -- the same
StreamAssembler and CSV writer ola_receive.py uses -- so you can exercise and
trust the host half before the board is flashed, and so ola_analyze.py has
something to chew on.

    python ola_simulate.py sim.csv                       # 30 s at rest
    python ola_simulate.py sim.csv --loss 0.001          # 0.1% packet loss
    python ola_simulate.py sim.csv --tone 200            # filtered: no alias
    python ola_simulate.py sim.csv --tone 200 --no-antialias   # aliases

The `--tone` model is a demonstrator, not a device model: the anti-alias
filter is treated as a brick wall at the DLPF's 136 Hz noise bandwidth. Real
data comes from the board.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ola_protocol import (  # noqa: E402
    DEFAULT_ODR_HZ,
    SAMPLES_PER_PACKET,
    StreamAssembler,
    build_data_packet,
    build_status_packet,
    sensitivity_lsb_per_g,
)
from ola_receive import CsvSink, Receiver  # noqa: E402

DLPF_NOISE_BW_HZ = 136.0  # acc_d111bw4_n136bw
STOPBAND_ATTEN = 1e-3     # ~60 dB, what a working DLPF does to an alias


class Args:
    """Minimal stand-in for the argparse namespace report() expects."""

    def __init__(self, out, name):
        self.out = out
        self.name = name
        self.raw = False
        self.quiet = True


def simulate(args):
    odr = args.odr
    lsb = sensitivity_lsb_per_g(args.full_scale)
    n_samples = int(round(args.seconds * odr))
    n_packets = n_samples // SAMPLES_PER_PACKET

    tone_gain = 1.0
    if args.tone is not None and args.antialias and args.tone > DLPF_NOISE_BW_HZ:
        tone_gain = STOPBAND_ATTEN

    rng = random.Random(args.seed)

    def sample(i):
        t = i / odr
        # Gravity on Z, plus sensor noise, plus the optional excitation.
        x = rng.gauss(0.0, args.noise)
        y = rng.gauss(0.0, args.noise)
        z = 1.0 + rng.gauss(0.0, args.noise)
        if args.tone is not None:
            v = args.amplitude * tone_gain * math.sin(2 * math.pi * args.tone * t)
            x += v
        return (
            int(max(-32768, min(32767, round(x * lsb)))),
            int(max(-32768, min(32767, round(y * lsb)))),
            int(max(-32768, min(32767, round(z * lsb)))),
        )

    asm = StreamAssembler()
    sink_args = Args(args.out, "OLA-SIM")
    dropped_packets = 0

    with CsvSink(args.out, asm, include_raw=args.raw) as sink:
        rx = Receiver(sink, asm, quiet=args.quiet)
        rx.on_status(None, build_status_packet(0, 0, 0, odr, args.full_scale, 0))

        for p in range(n_packets):
            if args.loss and rng.random() < args.loss:
                dropped_packets += 1
                continue  # the packet never reaches the host
            base = p * SAMPLES_PER_PACKET
            payload = build_data_packet(
                p & 0xFFFF, [sample(base + k) for k in range(SAMPLES_PER_PACKET)]
            )
            rx.on_data(None, payload)

            if (p + 1) % int(odr / SAMPLES_PER_PACKET) == 0:  # once a second
                seconds = (p + 1) * SAMPLES_PER_PACKET / odr
                rx.on_status(
                    None,
                    build_status_packet(
                        int(seconds * odr), 0, args.high_water, odr, args.full_scale, 0
                    ),
                )

    # Synthesise the arrival window the real receiver would have measured:
    # the first packet lands after its 3 samples were taken, the last after
    # its own, so the span between them is (n - SAMPLES_PER_PACKET) samples.
    rx.first_rx = 0.0
    rx.last_rx = (n_samples - SAMPLES_PER_PACKET) / odr

    from datetime import datetime, timezone  # noqa: PLC0415 - kept local

    from ola_receive import report  # noqa: PLC0415

    report(sink_args, asm, sink, rx, datetime.now(timezone.utc), args.seconds)
    print(f"\nsimulated {n_packets} packets, withheld {dropped_packets}")
    if args.tone is not None:
        if tone_gain < 1.0:
            print(
                f"tone at {args.tone} Hz attenuated by the modelled DLPF "
                f"({STOPBAND_ATTEN:g}x) before sampling"
            )
        elif args.tone > odr / 2:
            print(
                f"tone at {args.tone} Hz sampled UNFILTERED -- expect an alias at "
                f"{abs(args.tone - odr * round(args.tone / odr)):.2f} Hz"
            )
    print(f"\nnow run:  python ola_analyze.py {args.out}", end="")
    if args.tone is not None:
        print(f" --excite {args.tone}")
    else:
        print()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("out", nargs="?", default="sim.csv", help="output CSV path")
    p.add_argument("-t", "--seconds", type=float, default=30.0)
    p.add_argument("--odr", type=float, default=DEFAULT_ODR_HZ)
    p.add_argument("--full-scale", type=int, default=4, choices=(2, 4, 8, 16))
    p.add_argument("--noise", type=float, default=0.004, help="per-axis noise, g rms")
    p.add_argument("--tone", type=float, help="excitation frequency, Hz")
    p.add_argument("--amplitude", type=float, default=0.2, help="tone amplitude, g")
    p.add_argument(
        "--no-antialias",
        dest="antialias",
        action="store_false",
        help="model a broken/disabled DLPF so an out-of-band tone aliases",
    )
    p.add_argument("--loss", type=float, default=0.0, help="packet loss probability")
    p.add_argument("--high-water", type=int, default=64, help="reported peak buffer")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--raw", action="store_true")
    p.add_argument("--quiet", action="store_true", default=True)
    return p.parse_args(argv)


if __name__ == "__main__":
    simulate(parse_args())
