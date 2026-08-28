#!/usr/bin/env python3
"""Offline tests for the packet decoding and stream reassembly.

No hardware, no BLE, no dependencies:

    python -m unittest discover host -v
    python host/test_protocol.py

These cover the two things a naive receiver gets wrong -- 16-bit seq
unwrapping and gap accounting -- so that when a real capture reports loss you
know the arithmetic is not the reason.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ola_protocol import (  # noqa: E402
    DATA_PACKET_BYTES,
    SAMPLES_PER_PACKET,
    STATUS_PACKET_BYTES,
    StreamAssembler,
    build_data_packet,
    build_status_packet,
    parse_status_packet,
    sensitivity_lsb_per_g,
)


def packet(seq, first=0):
    """A packet whose samples encode their own absolute index, so the tests
    can prove the reassembler put them back in the right slots."""
    return build_data_packet(
        seq, [(first + i, -(first + i), 1000 + first + i) for i in range(SAMPLES_PER_PACKET)]
    )


class TestWireFormat(unittest.TestCase):
    def test_data_packet_is_exactly_20_bytes(self):
        self.assertEqual(DATA_PACKET_BYTES, 20)
        self.assertEqual(len(packet(0)), 20)

    def test_status_packet_is_exactly_14_bytes(self):
        self.assertEqual(STATUS_PACKET_BYTES, 14)
        self.assertEqual(len(build_status_packet(1, 2, 3)), 14)

    def test_status_round_trip(self):
        raw = build_status_packet(16875, 0, 512, odr_hz=281.25, full_scale_g=4, flags=1)
        st = parse_status_packet(raw)
        self.assertEqual(st.total_samples, 16875)
        self.assertEqual(st.dropped_samples, 0)
        self.assertEqual(st.high_water, 512)
        self.assertEqual(st.odr_hz, 281.25)
        self.assertEqual(st.full_scale_g, 4)
        self.assertTrue(st.overflowed)

    def test_sensitivities_match_the_datasheet(self):
        self.assertEqual(sensitivity_lsb_per_g(2), 16384)
        self.assertEqual(sensitivity_lsb_per_g(4), 8192)
        self.assertEqual(sensitivity_lsb_per_g(8), 4096)
        self.assertEqual(sensitivity_lsb_per_g(16), 2048)
        with self.assertRaises(ValueError):
            sensitivity_lsb_per_g(3)

    def test_negative_counts_survive_the_round_trip(self):
        asm = StreamAssembler()
        raw = build_data_packet(0, [(-32768, 32767, -1)] * SAMPLES_PER_PACKET)
        s = asm.feed_data(raw)[0]
        self.assertEqual((s.x, s.y, s.z), (-32768, 32767, -1))


class TestReassembly(unittest.TestCase):
    def test_clean_stream_has_no_gaps(self):
        asm = StreamAssembler()
        for seq in range(1000):
            got = asm.feed_data(packet(seq, first=seq * SAMPLES_PER_PACKET))
            self.assertEqual(len(got), SAMPLES_PER_PACKET)
        self.assertEqual(asm.gap_packets, 0)
        self.assertEqual(asm.samples_received, 3000)
        self.assertEqual(asm.loss_percent, 0.0)

    def test_sample_indices_are_absolute_and_contiguous(self):
        asm = StreamAssembler()
        indices = []
        for seq in range(50):
            indices += [s.index for s in asm.feed_data(packet(seq))]
        self.assertEqual(indices, list(range(150)))

    def test_payload_lands_in_the_right_slot(self):
        asm = StreamAssembler()
        for seq in range(10):
            for s in asm.feed_data(packet(seq, first=seq * SAMPLES_PER_PACKET)):
                # x was encoded as the sample's own absolute index
                self.assertEqual(s.x, s.index)
                self.assertEqual(s.y, -s.index)

    def test_gap_is_counted_and_indices_skip(self):
        asm = StreamAssembler()
        asm.feed_data(packet(0))
        asm.feed_data(packet(1))
        got = asm.feed_data(packet(5))  # 2, 3, 4 never arrived
        self.assertEqual(asm.gap_packets, 3)
        self.assertEqual(asm.missing_samples, 9)
        self.assertEqual(asm.last_gap, (3, 5))
        self.assertEqual(got[0].index, 15)  # 5 * 3
        self.assertAlmostEqual(asm.loss_percent, 100.0 * 9 / 18)

    def test_seq_wrap_does_not_fold_the_time_axis(self):
        """The 16-bit counter wraps every 65,536 packets -- 11.6 minutes at
        93.75 packets/s. Indices must keep climbing across the wrap."""
        asm = StreamAssembler()
        for seq in range(65530, 65536):
            asm.feed_data(packet(seq))
        before = asm.packet_index
        self.assertEqual(before, 65535)

        for seq in range(0, 5):  # wrapped
            got = asm.feed_data(packet(seq))
        self.assertEqual(asm.packet_index, 65540)
        self.assertEqual(got[0].index, 65540 * SAMPLES_PER_PACKET)
        self.assertEqual(asm.gap_packets, 0)

    def test_gap_across_a_wrap_is_still_counted_once(self):
        asm = StreamAssembler()
        asm.feed_data(packet(65534))
        asm.feed_data(packet(2))  # 65535, 0, 1 missing
        self.assertEqual(asm.gap_packets, 3)
        self.assertEqual(asm.packet_index, 65534 + 4)

    def test_first_packet_sets_the_origin(self):
        """Joining a session late must not be reported as loss."""
        asm = StreamAssembler()
        got = asm.feed_data(packet(100))
        self.assertEqual(asm.gap_packets, 0)
        self.assertEqual(got[0].index, 300)

    def test_short_packet_is_rejected_not_misparsed(self):
        asm = StreamAssembler()
        self.assertEqual(asm.feed_data(b"\x00\x01\x02"), [])
        self.assertEqual(asm.malformed_packets, 1)
        self.assertEqual(asm.samples_received, 0)

    def test_duplicate_packet_is_ignored(self):
        asm = StreamAssembler()
        asm.feed_data(packet(0))
        asm.feed_data(packet(1))
        self.assertEqual(asm.feed_data(packet(1)), [])
        self.assertEqual(asm.duplicate_packets, 1)
        self.assertEqual(asm.samples_received, 6)

    def test_long_run_stays_consistent(self):
        """Two full wraps with a gap in each -- the accounting must add up."""
        asm = StreamAssembler()
        emitted = 0
        dropped = {50017, 120033}
        for seq in range(140000):
            if seq in dropped:
                continue  # lose one packet on either side of a wrap
            asm.feed_data(packet(seq & 0xFFFF))
            emitted += 1
        self.assertEqual(asm.gap_packets, 2)
        self.assertEqual(asm.samples_received, emitted * SAMPLES_PER_PACKET)
        self.assertEqual(asm.packet_index, 139999)


class TestScaleAndTime(unittest.TestCase):
    def test_status_updates_scale_and_odr(self):
        asm = StreamAssembler()
        self.assertEqual(asm.lsb_per_g, 8192.0)
        asm.feed_status(build_status_packet(0, 0, 0, odr_hz=160.7, full_scale_g=16))
        self.assertEqual(asm.odr_hz, 160.7)
        self.assertEqual(asm.lsb_per_g, 2048.0)
        self.assertAlmostEqual(asm.to_g(2048), 1.0)

    def test_one_g_at_default_scale(self):
        asm = StreamAssembler()
        self.assertAlmostEqual(asm.to_g(8192), 1.0)
        self.assertAlmostEqual(asm.to_g(-8192), -1.0)

    def test_timestamps_come_from_the_sample_index(self):
        asm = StreamAssembler()
        self.assertAlmostEqual(asm.timestamp(0), 0.0)
        self.assertAlmostEqual(asm.timestamp(281), 281 / 281.25)
        self.assertAlmostEqual(asm.timestamp(16875), 60.0)

    def test_malformed_status_does_not_corrupt_scale(self):
        asm = StreamAssembler()
        self.assertIsNone(asm.feed_status(b"\x00" * 5))
        self.assertEqual(asm.lsb_per_g, 8192.0)
        self.assertEqual(asm.malformed_packets, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
