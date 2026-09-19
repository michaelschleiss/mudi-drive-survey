import math
import unittest
from mast_estimator import estimate_candidate_area
from localization_validation import evaluate_approaches


def observation(x, y, tx=0, ty=300):
    return dict(lat=48+y/111320, lon=11+x/(111320*math.cos(math.radians(48))), range_m=math.hypot(x-tx,y-ty), acc_m=2, step_m=4)


class CandidateAreaTests(unittest.TestCase):
    def estimate(self, rows):
        return estimate_candidate_area(rows, model_error_m=5, grid_m=10, max_grid_side=161)

    def test_stationary_duplicates_do_not_create_fix(self):
        result = self.estimate([observation(0,0)]*100)
        self.assertEqual(result['independent_positions'], 1)
        self.assertIsNone(result['candidate_marker'])
        self.assertEqual(result['regions'], [])

    def test_straight_road_preserves_mirror_regions(self):
        result = self.estimate([observation(x,0) for x in (-300,-150,0,150,300)])
        self.assertEqual(result['status'], 'needs_different_direction')
        self.assertGreaterEqual(len(result['regions']), 2)
        self.assertIsNone(result['candidate_marker'])
        latitudes = [p['lat'] for r in result['regions'] for p in r['cells']]
        self.assertLess(min(latitudes), 48)
        self.assertGreater(max(latitudes), 48)

    def test_loop_area_contains_truth_near_grid(self):
        rows = [observation(400*math.cos(t),300+400*math.sin(t)) for t in [i*math.pi/4 for i in range(8)]]
        result = self.estimate(rows)
        self.assertEqual(result['status'], 'candidate_area')
        self.assertEqual(len(result['regions']), 1)
        marker = result['candidate_marker']
        self.assertLess(abs(marker['lat']-(48+300/111320))*111320, 30)
        self.assertFalse(result['calibrated'])

    def test_one_outlier_does_not_erase_consensus(self):
        rows = [observation(400*math.cos(t),300+400*math.sin(t)) for t in [i*math.pi/4 for i in range(8)]]
        rows[0]['range_m'] += 700
        result = self.estimate(rows)
        self.assertTrue(result['regions'])
        self.assertEqual(result['rejected_clusters_allowed'], 1)

    def test_contradictory_ranges_have_no_candidate(self):
        rows = [observation(-300,0), observation(300,0), observation(0,400)]
        for row in rows:
            row['range_m'] = 5
        result = self.estimate(rows)
        self.assertEqual(result['status'], 'inconsistent_ranges')
        self.assertEqual(result['regions'], [])
        self.assertIsNone(result['candidate_marker'])

    def test_uncertainty_increases_candidate_area(self):
        rows = [observation(400*math.cos(t),300+400*math.sin(t)) for t in [i*math.pi/4 for i in range(8)]]
        narrow = self.estimate(rows)
        wide = estimate_candidate_area(rows, model_error_m=100, grid_m=10, max_grid_side=161)
        self.assertGreater(sum(r['area_m2'] for r in wide['regions']),
                           sum(r['area_m2'] for r in narrow['regions']))

    def test_order_does_not_change_result(self):
        rows = [observation(x,0) for x in (-300,-150,0,150,300)]
        self.assertEqual(self.estimate(rows), self.estimate(list(reversed(rows))))

    def test_invalid_configuration_rejected(self):
        with self.assertRaises(ValueError):
            estimate_candidate_area([], model_error_m=float('nan'))

    def test_held_out_approach_does_not_enter_fit(self):
        rows = []
        for label, offset in [('first',0), ('second',.1), ('bad',.2)]:
            for i in range(8):
                t = i*math.pi/4+offset
                row = observation(400*math.cos(t),300+400*math.sin(t))
                row['approach_id'] = label
                if label == 'bad':
                    row['range_m'] += 500
                rows.append(row)
        result = evaluate_approaches(rows, dict(lat=48+300/111320,lon=11),
            estimator_options=dict(model_error_m=5,grid_m=10,max_grid_side=161))
        held = next(f for f in result['folds'] if f['held_out_approach'] == 'bad')
        self.assertEqual(held['training_approaches'], ['first','second'])
        self.assertEqual(held['training_observations'], 16)
        self.assertTrue(held['known_site_in_candidate_area'])
        self.assertLess(held['marker_error_m'],30)
        self.assertGreater(held['held_out_marker_range_rmse_m'],450)
        self.assertFalse(result['calibrated'])

    def test_evaluation_requires_independent_labels(self):
        with self.assertRaises(ValueError):
            evaluate_approaches([observation(0,0)], dict(lat=48,lon=11))


if __name__ == '__main__':
    unittest.main()
