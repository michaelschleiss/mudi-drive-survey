import struct
import unittest
from lte_rrc import serving_cell, ota_header

# Actual B0C0 v30 from passive Vodafone capture; header; paging payload replaced by zero bytes.
ACTUAL = bytes.fromhex('1e1120118000110109070000f4360700000000070000000000000000000000')


class LteRrcTests(unittest.TestCase):
    def test_real_ota_header(self):
        row = ota_header(ACTUAL)
        self.assertEqual((row['pci'], row['channel'], row['pdu']), (273, 1801, 7))
        self.assertEqual((row['sfn'], row['subframe']), (879, 4))
        self.assertIsNone(row['subscription'])
        self.assertIsNone(row['cell_id'])
        self.assertEqual(row['identity_scope'], 'message_cell_only')

    def test_truncated_unknown_and_segmented_rejected(self):
        self.assertIsNone(ota_header(ACTUAL[:-1]))
        self.assertIsNone(ota_header(b'\xff' + ACTUAL[1:]))
        value = bytearray(ACTUAL)
        value[23] = 1
        self.assertIsNone(ota_header(value))

    def test_synthetic_serving_identity_v3(self):
        payload = b'\x03' + struct.pack('<HIIBBIHIHBHB', 273, 1801, 19801,
             100, 100, 0x146470a, 0xbba2, 3, 262, 2, 2, 0)
        row = serving_cell(payload)
        self.assertEqual((row['plmn'], row['cell_id'], row['band']), ('262-02', '146470A', 'B3'))
        self.assertIsNone(row['carrier_index'])
        self.assertIsNone(serving_cell(payload[:-1]))

    def test_synthetic_serving_identity_v2_three_digit_mnc(self):
        payload = b'\x02' + struct.pack('<HHHBBIHIHBHB', 1, 100, 18100,
             50, 50, 12345, 2, 1, 310, 3, 260, 0)
        self.assertEqual(serving_cell(payload)['plmn'], '310-260')


if __name__ == '__main__':
    unittest.main()
