"""Transport-to-decoder checks: subscription is evidence, never a default."""
import struct
import unittest
from nr_localization import CRC_TABLE, Decoder, DiagnosticPacket, packets


def frame(subscription=2, version=1):
    body = b'\x03' + struct.pack('<HIIBBIHIHBHB', 273, 1801, 19801,
                               100, 100, 0x146470a, 0xbba2, 3, 262, 2, 2, 0)
    packet = struct.pack('<BBHHHQ', 0x10, 0, len(body)+12,
                         len(body)+12, 0xb0c2, 123456 << 16) + body
    if subscription is not None:
        packet = struct.pack('<BBHI', 0x98, version, 0, subscription) + packet
    crc = 65535
    for b in packet:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ b) & 255]
    packet += struct.pack('<H', crc ^ 65535)
    return packet.replace(b'\x7d', b'\x7d\x5d').replace(b'\x7e', b'\x7d\x5e') + b'\x7e'


class DiagnosticScopeTests(unittest.TestCase):
    def test_same_timestamp_different_subscriptions_retained(self):
        decoder = Decoder()
        rows = decoder.decode(frame(1) + frame(2))
        self.assertEqual([r['subscription'] for r in rows], [1, 2])
        for row in rows:
            self.assertEqual(row['event_kind'], 'lte_serving_identity')
            self.assertEqual(row['rat'], 'lte')
            self.assertEqual(row['cell_id'], '146470A')
            self.assertNotIn('range_m', row)
        self.assertEqual(decoder.decode(frame(1) + frame(2)), [])

    def test_missing_or_unknown_subscription_never_defaults(self):
        for subscription in (None, 0, 3):
            with self.subTest(subscription=subscription):
                row = Decoder().decode(frame(subscription))[0]
                self.assertIsNone(row['subscription'])

    def test_unrecognized_initial_packet_is_preserved_without_range(self):
        packet = DiagnosticPacket(struct.pack("<BBHHHQ", 0x10, 0, 16, 16, 0xb062, 123456 << 16) + b"\xff\x01\x02\x03", 2)
        row = Decoder().decode(b"", [packet])[0]
        self.assertEqual(row["event_kind"], "lte_ta_initial_raw")
        self.assertEqual(row["payload_hex"], "ff010203")
        self.assertEqual(row["subscription"], 2)
        self.assertFalse(row["map_eligible"])
        self.assertNotIn("range_m", row)

    def test_unknown_wrapper_and_corruption_rejected(self):
        self.assertEqual(list(packets(frame(version=2))), [])
        damaged = bytearray(frame())
        damaged[12] ^= 1
        self.assertEqual(Decoder().decode(bytes(damaged)), [])


if __name__ == '__main__':
    unittest.main()
