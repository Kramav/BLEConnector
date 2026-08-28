"""Wire format and stream reassembly for the OLA-ACCEL BLE streamer.

Pure standard library on purpose: this module has no BLE dependency, so the
decoding logic can be tested without hardware (see test_protocol.py).

Data notification (20 bytes, little-endian):
    uint16  seq
    int16   ax, ay, az     x SAMPLES_PER_PACKET

Status notification (14 bytes, little-endian):
    uint32  total_samples
    uint32  dropped_samples
    uint16  high_water
    uint16  odr_centi_hz
    uint8   full_scale_g
    uint8   flags          bit0 = ring buffer overflowed this session
"""

from __future__ import annotations

import struct
from typing import List, NamedTuple, Optional

# --- must match the firmware -----------------------------------------
UUID_SERVICE = "f1b7a2c0-9e4d-4a1f-8c3b-5d6e7f801234"
UUID_DATA = "f1b7a2c1-9e4d-4a1f-8c3b-5d6e7f801234"
UUID_STATUS = "f1b7a2c2-9e4d-4a1f-8c3b-5d6e7f801234"

DEVICE_NAME = "OLA-ACCEL"

SAMPLES_PER_PACKET = 3
DATA_PACKET_BYTES = 2 + SAMPLES_PER_PACKET * 6  # 20
STATUS_PACKET_BYTES = 14

# Overwritten by the first status notification; these are the firmware
# defaults so the first second of samples is still scaled and timestamped.
DEFAULT_ODR_HZ = 281.25
DEFAULT_FULL_SCALE_G = 4
FLAG_OVERFLOWED = 0x01

_STATUS_FMT = "<IIHHBB"


def sensitivity_lsb_per_g(full_scale_g: int) -> float:
    """LSB per g for an ICM-20948 accelerometer full-scale range.

    +/-2 g -> 16384, +/-4 g -> 8192, +/-8 g -> 4096, +/-16 g -> 2048.
    """
    if full_scale_g not in (2, 4, 8, 16):
        raise ValueError(f"unsupported full-scale range: +/-{full_scale_g} g")
    return 32768.0 / full_scale_g


DEFAULT_LSB_PER_G = sensitivity_lsb_per_g(DEFAULT_FULL_SCALE_G)


class RawSample(NamedTuple):
    """One accelerometer reading, still in raw ICM-20948 counts."""

    index: int  # absolute sample index within the session
    x: int
    y: int
    z: int


class Status(NamedTuple):
    total_samples: int
    dropped_samples: int
    high_water: int
    odr_hz: float
    full_scale_g: int
    flags: int

    @property
    def overflowed(self) -> bool:
        return bool(self.flags & FLAG_OVERFLOWED)

    def describe(self) -> str:
        note = "  OVERFLOWED" if self.overflowed else ""
        return (
            f"sampled={self.total_samples} dropped={self.dropped_samples} "
            f"peak_buffer={self.high_water} odr={self.odr_hz} Hz "
            f"fs=+/-{self.full_scale_g}g{note}"
        )


def parse_status_packet(payload: bytes) -> Status:
    if len(payload) != STATUS_PACKET_BYTES:
        raise ValueError(
            f"status packet is {len(payload)} bytes, expected {STATUS_PACKET_BYTES}"
        )
    total, dropped, high_water, odr_centi, fs_g, flags = struct.unpack(
        _STATUS_FMT, payload
    )
    return Status(total, dropped, high_water, odr_centi / 100.0, fs_g, flags)


def build_status_packet(
    total_samples: int,
    dropped_samples: int,
    high_water: int,
    odr_hz: float = DEFAULT_ODR_HZ,
    full_scale_g: int = DEFAULT_FULL_SCALE_G,
    flags: int = 0,
) -> bytes:
    """Encode a status packet. Used by the tests to mimic the firmware."""
    return struct.pack(
        _STATUS_FMT,
        total_samples,
        dropped_samples,
        high_water,
        round(odr_hz * 100),
        full_scale_g,
        flags,
    )


def build_data_packet(seq: int, samples) -> bytes:
    """Encode one data notification. Used by the tests to mimic the firmware.

    `samples` is a sequence of SAMPLES_PER_PACKET (x, y, z) triples of raw
    counts.
    """
    samples = list(samples)
    if len(samples) != SAMPLES_PER_PACKET:
        raise ValueError(f"need exactly {SAMPLES_PER_PACKET} samples per packet")
    out = struct.pack("<H", seq & 0xFFFF)
    for x, y, z in samples:
        out += struct.pack("<hhh", x, y, z)
    return out


class StreamAssembler:
    """Turns a sequence of notifications into absolutely-indexed samples.

    Two jobs the naive version gets wrong:

    * Unwraps the 16-bit `seq` into a monotonic packet index. The counter
      wraps every 65,536 packets -- about 11.6 minutes at 93.75 packets/s --
      and without unwrapping any longer capture folds its time axis back on
      itself.
    * Counts what never arrived, from `seq` discontinuities, so the closing
      loss figure is a measurement rather than an assumption.

    BLE notifications are delivered in order by the link layer within a
    connection, so this needs gap detection, not reordering.
    """

    def __init__(
        self,
        samples_per_packet: int = SAMPLES_PER_PACKET,
        odr_hz: float = DEFAULT_ODR_HZ,
        full_scale_g: int = DEFAULT_FULL_SCALE_G,
    ) -> None:
        self.samples_per_packet = samples_per_packet
        self.packet_bytes = 2 + samples_per_packet * 6
        self.odr_hz = odr_hz
        self.full_scale_g = full_scale_g
        self.lsb_per_g = sensitivity_lsb_per_g(full_scale_g)

        self.prev_seq: Optional[int] = None
        self.packet_index = 0  # unwrapped, absolute
        self.packets_received = 0
        self.samples_received = 0
        self.gap_packets = 0
        self.malformed_packets = 0
        self.duplicate_packets = 0
        self.last_status: Optional[Status] = None
        self.status_count = 0
        # Set by feed_data when a gap is seen, so callers can log it.
        self.last_gap: Optional[tuple] = None

    # -- notifications --------------------------------------------------
    def feed_data(self, payload: bytes) -> List[RawSample]:
        """Decode one data notification into absolutely-indexed samples."""
        self.last_gap = None

        if len(payload) != self.packet_bytes:
            self.malformed_packets += 1
            return []

        seq = struct.unpack_from("<H", payload, 0)[0]

        if self.prev_seq is None:
            # First packet of the session: trust it as the origin. The
            # firmware resets seq to 0 on connect, so this is normally 0.
            self.packet_index = seq
        else:
            delta = (seq - self.prev_seq) & 0xFFFF
            if delta == 0:
                self.duplicate_packets += 1
                return []
            if delta != 1:
                self.gap_packets += delta - 1
                self.last_gap = (delta - 1, seq)
            self.packet_index += delta
        self.prev_seq = seq
        self.packets_received += 1

        base = self.packet_index * self.samples_per_packet
        out = []
        for i in range(self.samples_per_packet):
            x, y, z = struct.unpack_from("<hhh", payload, 2 + i * 6)
            out.append(RawSample(base + i, x, y, z))
        self.samples_received += len(out)
        return out

    def feed_status(self, payload: bytes) -> Optional[Status]:
        """Decode a status notification and adopt its ODR and full scale."""
        try:
            status = parse_status_packet(payload)
        except ValueError:
            self.malformed_packets += 1
            return None

        self.last_status = status
        self.status_count += 1
        if status.odr_hz > 0:
            self.odr_hz = status.odr_hz
        if status.full_scale_g in (2, 4, 8, 16):
            self.full_scale_g = status.full_scale_g
            self.lsb_per_g = sensitivity_lsb_per_g(status.full_scale_g)
        return status

    # -- derived figures ------------------------------------------------
    def timestamp(self, sample_index: int) -> float:
        """Seconds since the start of the session, from the sample index.

        Exact relative to the IMU's own clock and jitter-free -- BLE arrival
        times say nothing about when a sample was taken.
        """
        return sample_index / self.odr_hz

    def to_g(self, raw: int) -> float:
        return raw / self.lsb_per_g

    @property
    def missing_samples(self) -> int:
        return self.gap_packets * self.samples_per_packet

    @property
    def expected_samples(self) -> int:
        return self.samples_received + self.missing_samples

    @property
    def loss_percent(self) -> float:
        if not self.expected_samples:
            return 0.0
        return 100.0 * self.missing_samples / self.expected_samples
