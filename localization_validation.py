"""Offline leave-one-approach-out evaluation, not automatic field calibration.

Input JSON: {"known_site": {"lat": ..., "lon": ...}, "observations":
[{"approach_id": "drive-a/road-west", "lat": ..., "lon": ..., "range_m": ...,
  "acc_m": ..., "step_m": ...}], "estimator_options": {...}}.

Only combine evidence independently confirmed to belong to that physical site.
An approach_id must identify a whole drive segment/visit, never an individual
packet; all rows with its label are excluded from fitting that fold. Site truth
must come from independent survey evidence, not this estimator or guessed PCI.

Run: python localization_validation.py evidence.json > evaluation.json
"""
import argparse
import json
import math
from mast_estimator import estimate_candidate_area


def distance_m(a, b):
    lat1, lat2 = map(math.radians, (a['lat'], b['lat']))
    dlat = lat2-lat1
    dlon = math.radians(b['lon']-a['lon'])
    v = math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371008.8*2*math.asin(math.sqrt(min(1, max(0, v))))


def _contains(result, site):
    for region in result['regions']:
        for ring in region['polygons']:
            # Estimator cells are axis-aligned lat/lon rectangles.
            if (min(p[0] for p in ring) <= site['lat'] <= max(p[0] for p in ring)
                    and min(p[1] for p in ring) <= site['lon'] <= max(p[1] for p in ring)):
                return True
    return False


def evaluate_approaches(observations, known_site, *, estimator_options=None):
    """Return auditable fold errors/coverage; never label the model calibrated.

    Coverage is geometric inclusion of the supplied point in candidate grid
    squares, not a probability or a claim about true-site coordinate accuracy.
    Rows rejected by estimator validation remain counted as input, not evidence.
    """
    if not all(k in known_site and math.isfinite(float(known_site[k])) for k in ('lat','lon')):
        raise ValueError('An independent known_site lat/lon is required')
    site = {k: float(known_site[k]) for k in ('lat','lon')}
    if abs(site['lat']) >= 89 or abs(site['lon']) > 180:
        raise ValueError('Known site is outside supported coordinates')
    rows = list(observations)
    if any(not isinstance(r.get('approach_id'), str) or not r['approach_id'].strip() for r in rows):
        raise ValueError('Every observation requires a whole-approach label')
    labels = sorted({r['approach_id'] for r in rows})
    if len(labels) < 2:
        raise ValueError('At least two independently labelled approaches are required')
    options = dict(estimator_options or {})
    folds = []
    for label in labels:
        training = [r for r in rows if r['approach_id'] != label]
        held = [r for r in rows if r['approach_id'] == label]
        result = estimate_candidate_area(training, **options)
        marker = result['candidate_marker']
        residuals = []
        if marker is not None:
            for row in held:
                try:
                    radius = float(row['range_m'])
                    point = {k: float(row[k]) for k in ('lat','lon')}
                    if radius < 0 or not all(math.isfinite(v) for v in (*point.values(), radius)):
                        continue
                    if abs(point['lat']) >= 89 or abs(point['lon']) > 180:
                        continue
                    residuals.append(distance_m(marker, point)-radius)
                except (KeyError, TypeError, ValueError):
                    continue
        folds.append(dict(held_out_approach=label, training_observations=len(training),
            training_approaches=[v for v in labels if v != label],
            training_accepted_observations=result['accepted_observations'],
            independent_positions=result['independent_positions'], held_out_observations=len(held),
            status=result['status'], region_count=len(result['regions']),
            candidate_area_m2=sum(r['area_m2'] for r in result['regions']),
            known_site_in_candidate_area=_contains(result, site),
            marker_error_m=distance_m(marker, site) if marker else None,
            held_out_marker_range_residuals_m=residuals,
            held_out_marker_range_rmse_m=(math.sqrt(sum(v*v for v in residuals)/len(residuals)) if residuals else None)))
    errors = [f['marker_error_m'] for f in folds if f['marker_error_m'] is not None]
    return dict(calibrated=False, interpretation='Experimental held-out evaluation; not confidence calibration',
        known_site=site, estimator_options=options, approach_count=len(labels), folds=folds,
        folds_with_candidate_area=sum(bool(f['region_count']) for f in folds),
        folds_covering_known_site=sum(f['known_site_in_candidate_area'] for f in folds),
        folds_with_marker=len(errors), max_marker_error_m=max(errors) if errors else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('evidence_json')
    args = parser.parse_args()
    with open(args.evidence_json) as handle:
        data = json.load(handle)
    result = evaluate_approaches(data['observations'], data['known_site'],
                                estimator_options=data.get('estimator_options'))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
