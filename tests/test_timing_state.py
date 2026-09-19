import unittest
from dataclasses import replace
from timing_state import TimingIdentity, TimingState


class TimingStateTests(unittest.TestCase):
    def setUp(self):
        self.identity = TimingIdentity('capture', 'connection', 'sim2', 'NR',
                                       '262-02:123', 'carrier0', 0, 1)
        self.state = TimingState(self.identity)

    def init(self):
        self.assertTrue(self.state.initialize(self.identity, 21, 100, 10., 20.))

    def test_absolute_and_contiguous_adjustments(self):
        self.init()
        self.assertTrue(self.state.adjust(self.identity, -1, 101, 11.))
        self.assertTrue(self.state.adjust(self.identity, 0, 102, 12., 25.))
        result = self.state.snapshot(13.)
        self.assertEqual(result['timing_index'], 20)
        self.assertEqual(result['event_ts'], 12.)
        self.assertFalse(result['validated'])
        self.assertIsNone(result['range_m'])

    def test_relative_never_creates_baseline(self):
        self.assertFalse(self.state.adjust(self.identity, 1, 1, 10.))
        self.assertIsNone(self.state.snapshot(10.)['timing_index'])

    def test_every_identity_component_invalidates(self):
        changes = dict(session='other', epoch='next', subscription='sim1',
                       rat='LTE', cell='other', carrier='carrier1', tag=1,
                       numerology=2)
        for field, value in changes.items():
            with self.subTest(field=field):
                self.state = TimingState(self.identity)
                self.init()
                update = {field: value}
                if field == 'rat':
                    update['numerology'] = 0
                self.assertFalse(self.state.adjust(replace(self.identity, **update), 1, 101, 11.))
                self.assertIsNone(self.state.index)

    def test_gap_unknown_and_replay_destroy_continuity(self):
        for sequence in (None, 100, 102, True):
            with self.subTest(sequence=sequence):
                self.state = TimingState(self.identity)
                self.init()
                self.assertFalse(self.state.adjust(self.identity, 1, sequence, 11.))
                self.assertFalse(self.state.adjust(self.identity, 1, 101, 12.))

    def test_expiry_and_clock_order(self):
        for ts in (9., 10., 20., float('nan')):
            self.state = TimingState(self.identity)
            self.init()
            self.assertFalse(self.state.adjust(self.identity, 1, 101, ts))
        self.state = TimingState(self.identity)
        self.init()
        self.assertIsNone(self.state.snapshot(20.)['timing_index'])

    def test_disconnect_requires_new_absolute_baseline(self):
        self.init()
        self.state.invalidate('Disconnected')
        self.assertFalse(self.state.adjust(self.identity, 1, 101, 11.))
        self.assertTrue(self.state.initialize(self.identity, 4, 200, 12., 22.))

    def test_invalid_baseline_and_negative_accumulation(self):
        for index, seq, ts, expiry in ((-1, 1, 1, 2), (1, None, 1, 2),
                                       (1, 1, 1, 1), (True, 1, 1, 2)):
            self.assertFalse(self.state.initialize(self.identity, index, seq, ts, expiry))
        self.init()
        self.assertFalse(self.state.adjust(self.identity, -22, 101, 11.))


if __name__ == '__main__':
    unittest.main()
