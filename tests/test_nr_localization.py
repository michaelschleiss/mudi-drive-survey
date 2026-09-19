import struct
import unittest

from nr_localization import candidate, candidate_adjustment, locate, timestamp, StreamFrames, packets


class ExperimentalRangeTests(unittest.TestCase):
    def test_stream_survives_split_headers_and_escaped_packets(self):
        packet = bytearray(16)
        packet[0] = 0x10
        struct.pack_into('<H', packet, 6, 0xb886)
        packet.extend(b'\x7e\x7d\x00')
        crc = 65535
        for b in packet:
            crc ^= b
            for _ in range(8):
                crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        raw = bytes(packet) + struct.pack('<H', crc ^ 65535)
        raw = raw.replace(b'\x7d', b'\x7d\x5d').replace(b'\x7e', b'\x7d\x5e') + b'\x7e'
        wire = b''
        for part in (raw[:18], raw[18:20], b'', raw[20:]):
            wire += ('%08x\n' % len(part)).encode() + part
        stream, results = StreamFrames(), []
        for b in wire:
            for data, size in stream.feed(bytes([b])):
                results.extend(packets(data))
        self.assertEqual(results, [bytes(packet)])
        self.assertEqual(stream.diag, b'')
        self.assertEqual(stream.transport, b'')

    def test_stream_rejects_bad_or_unbounded_envelopes(self):
        for header in (b'garbage!\n', b'00010001\n'):
            with self.assertRaises(ValueError):
                StreamFrames().feed(header)

    def test_relative_correction_never_becomes_absolute_range(self):
        raw = bytearray.fromhex('08000300500fee010ff170e201dfa5c200a20020001e0000')
        decoded = candidate_adjustment(raw)
        self.assertEqual(decoded['delta_steps_candidate'], -1)
        self.assertIsNone(decoded['absolute_range_m'])
        self.assertTrue(decoded['requires_absolute_baseline'])
        raw[21] = 32
        self.assertEqual(candidate_adjustment(raw)['delta_steps_candidate'], 1)
        raw[21] = 31
        self.assertEqual(candidate_adjustment(raw)['delta_steps_candidate'], 0)
        raw[21] = 64
        self.assertIsNone(candidate_adjustment(raw))
        raw[21] = 30
        self.assertIsNone(candidate_adjustment(raw + b'\x00'))
        raw[19] = 0
        self.assertIsNone(candidate_adjustment(raw))

    def test_incomplete_and_unknown_layout_never_make_zero_range(self):
        body = bytearray(232)
        struct.pack_into('<HH', body, 0, 18, 3)
        body[19] = 1
        self.assertIsNone(candidate(body))
        body[19] = 15
        struct.pack_into('<H', body, 176, 21)
        struct.pack_into('<H', body, 228, 24546)
        self.assertEqual(candidate(body)['ta_index_candidate'], 21)
        self.assertIsNone(candidate(body[:-1]))
        body[0] = 19
        self.assertIsNone(candidate(body))

    def test_event_position_not_download_position(self):
        row = dict(ts=101, range_m=820, reason='Experimental')
        fixes = [dict(ts=100,lat=48,lon=11,acc_m=5),
                 dict(ts=102,lat=48.0002,lon=11,acc_m=6),
                 dict(ts=120,lat=49,lon=12,acc_m=5)]
        result = locate(row, fixes, 99, 121)
        self.assertAlmostEqual(result['lat'], 48.0001)
        self.assertEqual(result['acc_m'], 6)
        self.assertIsNone(locate(row, fixes[2:], 99, 121)['lat'])
        self.assertIsNone(locate(row, fixes, 115, 121)['lat'])
        self.assertIsNone(locate(row, [fixes[0],fixes[2]], 99, 121)['lat'])

    def test_timestamp_ticks(self):
        self.assertEqual(timestamp(800 << 16), 315964801)


if __name__ == '__main__':
    unittest.main()
