import struct
import unittest
from qmi_location import parse_location, parse_output, successful, tlvs

# Actual SIM2 radio-only response, no SIM/account identifiers.
ACTUAL = bytes.fromhex('020400000000001327000062f220a2bb0a474601090711010000000002110185ff98fbd7fc0000650168ff5afb6cfc00001402000000150200000016020000001e040012000000260200050027040009070000280100002a0400030000002c0400010000002d0400040000003206003236323032ff')


class QmiLocationTests(unittest.TestCase):
    def test_actual_subscription_identity_and_units(self):
        row = parse_location(ACTUAL, 2, 100, 101)
        self.assertEqual((row['plmn'], row['cell_id'], row['pci'], row['channel']),
                         ('262-02', '146470A', 273, 1801))
        self.assertEqual(row['timing_advance_us'], 18)
        self.assertAlmostEqual(row['range_m'], 2698.132122)
        self.assertFalse(row['qmi_ue_in_idle'])
        self.assertEqual(row['freshness'], 'unknown')
        self.assertIsNone(row['measurement_ts'])
        self.assertFalse(row['validated'])

    def test_sentinel_not_distance(self):
        row = parse_location(ACTUAL.replace(bytes.fromhex('1e040012000000'),
                                           bytes.fromhex('1e0400ffffffff')), 2, 1, 2)
        self.assertIsNone(row['range_m'])

    def test_result_failure_and_malformed(self):
        for payload in (b'', b'\x02', ACTUAL[:-1], ACTUAL + ACTUAL,
                        bytes.fromhex('02040001000d00')):
            with self.subTest(payload=payload[:8]):
                with self.assertRaises(ValueError):
                    parse_location(payload, 2, 1, 2)

    def test_successful_transport_required(self):
        output = 'init=0\nbind rc=0 data=02040000000000\ncell rc=0 data=' + ACTUAL.hex() + '\nrelease=0\n'
        self.assertEqual(parse_output(output, 2, 1, 2)['subscription'], 2)
        for bad in (output.replace('init=0', 'init=1'), output.replace('release=0', 'release=1'),
                    output.replace('bind rc=0', 'bind rc=1')):
            with self.assertRaises(ValueError):
                parse_output(bad, 2, 1, 2)

    def test_missing_identity_cannot_produce_range(self):
        row = parse_location(bytes.fromhex('020400000000001e040012000000'), 2, 1, 2)
        self.assertIsNone(row['range_m'])


if __name__ == '__main__':
    unittest.main()
