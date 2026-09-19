"""Cell identity, timestamp association, and explicitly tentative TA localization."""
from collections import deque
import csv
import json
import math
from pathlib import Path
import re
import statistics
import time

from survey_core import finite, meters


def identity(cell):
    rat, plmn, value = cell.get('radio'), cell.get('plmn'), cell.get('cell')
    if rat not in ('LTE', 'NR') or not re.fullmatch(r'\d{3}-\d{2,3}', str(plmn)):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'(?:0[xX])?[0-9a-fA-F]+', value):
        return None
    number = int(value, 16)
    if number >= 2**(28 if rat == 'LTE' else 36):
        return None
    return f'{rat.lower()}:{plmn}:{number:X}'


def nearest_fix(fixes, ts):
    fix = min(fixes, key=lambda p: abs(p['ts']-ts), default=None)
    if not fix or abs(fix['ts']-ts) > 1.5 or fix['accuracy'] > 25:
        return {}
    return {k: fix[k] for k in ('lat', 'lon', 'accuracy')} | {'gps_ts': fix['ts'], 'gps_delta': abs(fix['ts']-ts)}


class CellRegistry:
    def __init__(self):
        self.cells = {}
        self.unresolved = []
        self.observations = 0
        self.ring_count = 0
        self.estimates = {}
        self.matches = {}
        self.trails = {}

    def observations_for(self, radio, fixes):
        ts = radio.get('sample_ts', radio['ts'])
        location = nearest_fix(fixes, ts) if radio.get('latency_ms', 0) <= 1500 else {}
        result = []
        for role in ('lte', 'nr'):
            cell = radio.get(role)
            if not cell:
                continue
            data = {**cell, 'radio': 'LTE' if role == 'lte' else 'NR', 'role': 'serving', 'ts': ts, **location}
            data['key'] = identity(data)
            result.append(data)
        # Neighbours have their own sample time and usually lack full identity.
        for cell in radio.get('neighbours', []):
            nts = radio.get('neighbours_ts', 0)
            if abs(radio['ts']-nts) > 1.5:
                continue
            data = {**cell, 'role': 'neighbour', 'ts': nts, **nearest_fix(fixes, nts)}
            data['key'] = identity(data)
            result.append(data)
        return result

    def remember_radio(self, event):
        observations = event.get('observations', [])
        self.unresolved = [dict(o, observation_id=f"{event['id']}-{i}") for i, o in enumerate(observations) if not o.get('key')]
        for o in observations:
            self.observations += 1
            key = o.get('key')
            if not key:
                continue  # Never aggregate a reused PCI into a fictional unique cell.
            cell = self.cells.setdefault(key, {'key': key, 'first': o['ts'], 'count': 0,
                                              'mapped': 0, 'bands': set(), 'history': deque(maxlen=500),
                                              'rings': deque(maxlen=60), 'best': None})
            cell['count'] += 1
            cell['latest'] = o
            if o.get('band'):
                cell['bands'].add(o['band'])
            cell['history'].append(o)
            if 'lat' in o:
                cell['mapped'] += 1
                if o.get('rsrp') is not None and (cell['best'] is None or o['rsrp'] > cell['best']['rsrp']):
                    cell['best'] = o

    def validate_timing(self, data, fixes):
        key = str(data.get('key', ''))
        if key not in self.cells:
            raise ValueError('Timing advance requires a previously observed full cell identity')
        if data.get('reference_key') != key:
            raise ValueError('Timing reference must explicitly match the measured cell identity')
        ts = finite(data['ts'])
        if not -2 <= time.time()-ts <= 30:
            raise ValueError('Timing timestamp is stale or in the future')
        recent = min(self.cells[key]['history'], key=lambda o: abs(o['ts']-ts))
        if abs(recent['ts']-ts) > 2:
            raise ValueError('No matching cell observation near the timing measurement')
        location = nearest_fix(fixes, ts)
        if not location:
            raise ValueError('Timing measurement needs a fresh GPS fix with accuracy <=25 m')
        value = finite(data['ta_index'])
        if value != int(value) or not 0 <= value <= (1282 if key.startswith('lte:') else 3846):
            raise ValueError('Invalid absolute timing-advance index')
        rat = key.split(':', 1)[0].upper()
        if data.get('encoding') != ('lte-absolute-16ts' if rat == 'LTE' else 'nr-absolute-rar'):
            raise ValueError('Timing encoding does not match the radio; delta commands are not absolute ranges')
        scs = finite(data.get('scs_khz', 15))
        if rat == 'LTE' and scs != 15 or rat == 'NR' and scs not in (15, 30, 60, 120, 240):
            raise ValueError('Invalid uplink subcarrier spacing')
        if rat == 'NR' and 'scs_khz' not in data:
            raise ValueError('NR timing requires the actual uplink subcarrier spacing')
        source = str(data.get('source', '')).strip()
        if not source or len(source) > 100:
            raise ValueError('Timing source is required')
        step = 299792458 * 16 / (15000 * 2048) / 2 * 15/scs
        # One full quantization step + GPS + explicit model allowance, not a confidence interval.
        allowance = finite(data.get('range_error_m', 50))
        if not 0 <= allowance <= 2000:
            raise ValueError('Invalid range-error allowance')
        uncertainty = step + location['accuracy'] + allowance
        return {**location, 'ts': ts, 'key': key, 'reference_key': key, 'source': source,
                'encoding': data['encoding'], 'ta_index': int(value), 'scs_khz': scs,
                'distance_m': value*step, 'step_m': step, 'uncertainty_m': uncertainty,
                'range_error_m': allowance, 'timing_group': str(data.get('timing_group', ''))[:80]}

    def remember_timing(self, event):
        cell = self.cells.get(event.get('key'))
        if cell:
            cell['rings'].append(event)
            self.ring_count += 1

    def summary(self):
        result = []
        for c in self.cells.values():
            result.append({k: c[k] for k in ('key', 'first', 'count', 'mapped', 'best', 'latest')} | {
                'bands': sorted(c['bands']), 'ring_count': len(c['rings']),
                'estimate': self.estimates.get(c['key']), 'matches': self.matches.get(c['key'], [])})
        # Discovery priority is an explicit sort, not a predicted bandwidth score.
        result.sort(key=lambda c: ('n78' in c['bands'], c['latest'].get('radio') == 'NR',
                                   c['latest'].get('bandwidth_mhz') or 0,
                                   (c['best'] or c['latest']).get('rsrp') or -200), reverse=True)
        return result

    def detail(self, key):
        c = self.cells.get(key)
        if not c:
            raise KeyError('Unknown full cell identity')
        return {'key': key, 'history': list(c['history']), 'rings': list(c['rings']),
                'estimate': self.estimates.get(key), 'matches': self.matches.get(key, []),
                'trail':list(self.trails.get(key, []))}


def fit_rings(rings):
    """A broad, tentative hypothesis. Reject poor geometry and inconsistent ranges."""
    if len(rings) < 4:
        return {'status': 'insufficient', 'reason': 'Need at least four attributed TA observations'}
    # At most 30 evenly spaced observations keep fits cheap and spatially representative.
    rings = rings[::max(1, len(rings)//30)][-30:]
    lat, lon = statistics.mean(r['lat'] for r in rings), statistics.mean(r['lon'] for r in rings)
    scale = 111320*math.cos(math.radians(lat))
    pts = [((r['lon']-lon)*scale, (r['lat']-lat)*111320, r['distance_m'], r['uncertainty_m']) for r in rings]
    xx = sum(x*x for x,y,d,u in pts); yy = sum(y*y for x,y,d,u in pts); xy = sum(x*y for x,y,d,u in pts)
    if max(math.hypot(x,y) for x,y,d,u in pts) < 75 or (xx*yy-xy*xy)/max(1,(xx+yy)**2) < .015:
        return {'status': 'poor_geometry', 'reason': 'Collect measurements from different directions'}
    lo_x = max(x-d-u for x,y,d,u in pts); hi_x = min(x+d+u for x,y,d,u in pts)
    lo_y = max(y-d-u for x,y,d,u in pts); hi_y = min(y+d+u for x,y,d,u in pts)
    if hi_x <= lo_x or hi_y <= lo_y:
        return {'status': 'inconsistent', 'reason': 'Ranges disagree; check identity, TA units or reflected paths'}
    def loss(x,y):
        return statistics.mean(min(abs(math.hypot(x-a,y-b)-d)/max(1,u), 5)**2 for a,b,d,u in pts)
    grid = [(loss(x,y),x,y) for x in [lo_x+(hi_x-lo_x)*i/40 for i in range(41)]
            for y in [lo_y+(hi_y-lo_y)*j/40 for j in range(41)]]
    best,x,y = min(grid)
    if best > 2:
        return {'status': 'inconsistent', 'reason': 'No consistent range fit'}
    plausible = [(a,b) for score,a,b in grid if score <= best+.5]
    radius = max(50, max((math.hypot(a-x,b-y) for a,b in plausible), default=0), (hi_x-lo_x)/40, (hi_y-lo_y)/40)
    return {'status': 'tentative', 'lat': lat+y/111320, 'lon': lon+x/scale, 'radius': radius,
            'residual_m': math.sqrt(statistics.mean((math.hypot(x-a,y-b)-d)**2 for a,b,d,u in pts)),
            'reason': 'Model fit and plausible spread; not a surveyed position or statistical confidence interval'}


def load_sites(path):
    if not path:
        return []
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open() as f:
            rows = list(csv.DictReader(f))
    else:
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and raw.get('type') == 'FeatureCollection':
            rows = [dict(f.get('properties', {}), lon=f['geometry']['coordinates'][0], lat=f['geometry']['coordinates'][1])
                    for f in raw['features'] if (f.get('geometry') or {}).get('type') == 'Point']
        elif isinstance(raw, list):
            rows = raw
        else:
            raise ValueError('Sites must be CSV, a JSON array or Point GeoJSON')
    sites = []
    for row in rows:
        lat, lon = finite(row['lat']), finite(row['lon'])
        uncertainty = finite(row.get('uncertainty_m') or 80)
        if not -90 < lat < 90 or not -180 <= lon <= 180 or not 0 <= uncertainty <= 5000:
            raise ValueError('Invalid site coordinates or uncertainty')
        sites.append({'id': str(row['id']), 'lat': lat, 'lon': lon, 'uncertainty_m': uncertainty,
                      'source': str(row.get('source') or 'Imported sites'),
                      'operator': str(row.get('operator') or '')})
    return sites


def match_sites(rings, sites, key):
    if len(rings) < 4:
        return []
    ranked = []
    operator = key.split(':')[1]
    for site in sites:
        if site['operator'] and operator not in site['operator'].split(';'):
            continue
        # Only candidate sites whose distance agrees with each measured radio range.
        residuals = [abs(meters(site,r)-r['distance_m'])/(r['uncertainty_m']+site['uncertainty_m']+1) for r in rings[-30:]]
        score = math.sqrt(statistics.mean(v*v for v in residuals))
        if score <= 1.5:
            ranked.append({**site, 'status': 'candidate', 'range_mismatch': score})
    return sorted(ranked, key=lambda s: s['range_mismatch'])[:3]


def localization_worker(survey, stop):
    versions = {}
    while not stop.is_set():
        with survey.lock:
            work = {key: list(c['rings']) for key,c in survey.evidence.cells.items()
                    if c['rings'] and versions.get(key) != c['rings'][-1]['id']}
        for key, rings in work.items():
            estimate, matches = fit_rings(rings), match_sites(rings, survey.sites, key)
            with survey.lock:
                survey.evidence.estimates[key] = estimate
                survey.evidence.matches[key] = matches
                if estimate.get('status') == 'tentative':
                    trail = survey.evidence.trails.setdefault(key, deque(maxlen=30))
                    trail.append(dict(estimate, ts=rings[-1]['ts']))
            versions[key] = rings[-1]['id']
        stop.wait(2)
