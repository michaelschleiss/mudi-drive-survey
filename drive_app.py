#!/usr/bin/env python3
"""Upload Atlas: independent acquisition workers and a durable, timestamped survey."""
import argparse
from collections import deque
import csv
import io
import json
import math
import os
import re
import signal
from pathlib import Path
import sqlite3
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from mudi_survey import parse_modem, router, passive_rate
import mudi_survey

ROOT = Path(__file__).parent


def distance(a, b):
    return math.hypot((a['lat']-b['lat'])*111320,
                      (a['lon']-b['lon'])*111320*math.cos(math.radians(a['lat'])))


def position(p, now=None):
    now = time.time() if now is None else now
    result = {k: float(p[k]) for k in ('lat', 'lon', 'ts', 'acc_m')}
    if not all(math.isfinite(v) for v in result.values()):
        raise ValueError('GPS values must be finite')
    if not (-90 <= result['lat'] <= 90 and -180 <= result['lon'] <= 180):
        raise ValueError('Invalid coordinates')
    if not 0 <= result['acc_m'] or not -2 <= now-result['ts'] <= 30:
        raise ValueError('GPS timestamp must be recent; synchronize device clocks')
    speed = p.get('speed_kmh')
    if speed is not None and (not math.isfinite(float(speed)) or not 0 <= float(speed) <= 400):
        raise ValueError('Invalid speed')
    result.update(speed_kmh=speed, source=str(p.get('source', 'phone'))[:32])
    return result


def position_at(fixes, timestamp):
    """Interpolate between timestamped fixes; never extrapolate an old fix."""
    before = next((p for p in reversed(fixes) if p['ts'] <= timestamp), None)
    after = next((p for p in fixes if p['ts'] >= timestamp), None)
    if before is None or after is None or after['ts']-before['ts'] > 3:
        return None
    span = after['ts']-before['ts']
    fraction = (timestamp-before['ts'])/span if span else 0
    return {'ts': timestamp, 'lat': before['lat']+(after['lat']-before['lat'])*fraction,
            'lon': before['lon']+(after['lon']-before['lon'])*fraction,
            'acc_m': max(before['acc_m'], after['acc_m']),
            'method': 'interpolated' if span else 'GPS fix'}


def qualify_probe(result, fixes, start, end):
    """Keep the entire measurement footprint; never assign a moving test to its endpoint."""
    result = dict(result)
    usable = sorted((p for p in fixes if start-3 <= p['ts'] <= end and p['acc_m'] <= 30), key=lambda p: p['ts'])
    transport_error = result.get('error', '')
    reason = ''
    if not usable or usable[0]['ts'] > start+1 or end-usable[-1]['ts'] > 3:
        reason = reason or 'Missing fresh, accurate GPS throughout test'
    elif any(b['ts']-a['ts'] > 3 for a, b in zip(usable, usable[1:])):
        reason = reason or 'GPS gap during test'
    extent = max((distance(usable[0], p) for p in usable), default=0) if usable else None
    if extent is not None and extent > 80:
        reason = reason or 'Moving test spans more than 80 m; repeat here'
    midpoint = position_at(usable, (start+end)/2)
    if midpoint is None:
        reason = reason or 'No GPS fixes bracket the measurement midpoint'
    path = [{k: p[k] for k in ('ts', 'lat', 'lon', 'acc_m')} for p in usable if p['ts'] >= start]
    start_position = position_at(usable, start)
    if start_position and (not path or path[0]['ts'] > start):
        path.insert(0, start_position)
    location_eligible = not bool(reason)
    if result.get('mode') == 'parked' and extent is not None and extent > 20:
        reason = reason or 'Parked verification moved more than 20 m'
    if result.get('short'):
        reason = reason or 'Test below target duration; adapting next payload'
    reason = transport_error or reason
    result.update(location_eligible=location_eligible, start=start, ts=end, duration=round(end-start, 3), eligible=not bool(reason), reason=reason,
                  footprint_m=round(extent, 1) if extent is not None else None,
                  lat=midpoint['lat'] if midpoint else None,
                  lon=midpoint['lon'] if midpoint else None,
                  position_ts=(start+end)/2, position_method=midpoint['method'] if midpoint else None,
                  path=path,
                  acc_m=max((p['acc_m'] for p in usable), default=None))
    return result


def hunt_priority(width, rsrp, sinr):
    """Transparent scouting heuristic, not a bandwidth estimate; all bands eligible."""
    wide = width is not None and width >= 20
    clean = rsrp is not None and rsrp >= -105 and sinr is not None and sinr >= 10
    return 2 if wide and clean else 1 if wide or clean else 0


class Survey:
    def __init__(self, db):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT, ts REAL, data TEXT)')
        self.events = deque(maxlen=12000)
        self.fixes = deque(maxlen=600)
        self.latest = {}
        self.spots = {}
        self.cells = {}
        self.target = None
        self.ta_enabled = False
        self.ta_status = 'Experimental capture off'
        self.ta_capture = None
        self.ranges = deque(maxlen=300)
        self.radio_track = deque(maxlen=12000)
        self.seq = 0
        self.started = time.time()
        self.bytes = 0
        self.running = False
        self.busy = False
        self.error = ''
        self.mode = 'parked'
        self.remaining = 0
        self.active = None
        self.uplink_samples = []
        self.generation = 0
        for seq, kind, ts, data in self.db.execute('SELECT id,kind,ts,data FROM events ORDER BY id'):
            self.seq = seq
            self._remember({'id': seq, 'kind': kind, 'ts': ts, **json.loads(data)})

    def _remember(self, event):
        self.events.append(event)
        kind = event['kind']
        self.latest[kind] = event
        if kind == 'nr_range':
            self.ranges.append(event)
        if kind == 'hunt_target':
            self.target = event.get('identity')
        if kind == 'gps':
            self.fixes.append(event)
        if kind == 'radio' and event.get('rat'):
            self._remember_cells(event)
        if kind == 'probe':
            self.bytes += event.get('bytes', 0)
            if event.get('eligible') or (event.get('error') and event.get('location_eligible')):
                # Anchor-radius clustering avoids latitude-dependent grid discontinuities.
                key = next((k for k, s in self.spots.items() if distance(event, s) <= 40), None)
                if key is None:
                    key = event['id']
                    self.spots[key] = {'lat': event['lat'], 'lon': event['lon'], 'values': [], 'failed': 0, 'verified': 0, 'id': key}
                spot = self.spots[key]
                failed = bool(event.get('error'))
                spot['values'].append(0 if failed else event['mbps'])
                spot['failed'] += int(failed)
                spot['verified'] += int(not failed and event.get('mode') == 'parked')
                spot.update(ts=event['ts'], rat=event.get('rat'), ca=event.get('ca'))

    def _remember_cells(self, radio):
        gps = self.latest.get('gps')
        located = bool(gps and abs(radio['ts']-gps['ts']) <= 3 and gps['acc_m'] <= 30)
        if located:
            fields = {k: v for k, v in radio.items() if k.startswith(('lte_', 'nr_')) or k in ('rat', 'ca', 'ca_ts')}
            self.radio_track.append({'id': radio['id'], 'ts': radio['ts'],
                'gps_ts': gps['ts'], 'gps_delta_s': radio['ts']-gps['ts'],
                'lat': gps['lat'], 'lon': gps['lon'], 'acc_m': gps['acc_m'], 'radio': fields})
        for prefix in ('lte', 'nr'):
            band, pci = radio.get(prefix+'_band'), radio.get(prefix+'_pci')
            if not band or pci is None:
                continue
            plmn = radio.get(prefix+'_plmn')
            channel = radio.get(prefix+'_arfcn')
            full_id = radio.get(prefix+'_cell_id')
            # NSA does not expose NR global identity here: never call PCI a mast ID.
            identity = f'{prefix}:{plmn}:{full_id}' if full_id and plmn else f'{prefix}:{plmn}:{band}:{channel}:{pci}'
            if identity not in self.cells:
                self.cells[identity] = {'identity': identity, 'band': band, 'pci': pci,
                    'plmn': plmn, 'cell_id': full_id, 'channel': channel,
                    'identity_quality': 'Cell ID' if full_id and plmn else 'Radio signature (may repeat)',
                    'first_seen': radio['ts'], 'observations': 0, 'located': 0,
                    'best_position': None, 'candidate_position': None, 'hunt_priority': 0, 'best_rsrp': None, 'best_sinr': None}
            cell = self.cells[identity]
            cell.update(last_seen=radio['ts'], bandwidth_mhz=radio.get(prefix+'_bw'),
                        tac=radio.get(prefix+'_tac'))
            cell['observations'] += 1
            rsrp, sinr = radio.get(prefix+'_rsrp'), radio.get(prefix+'_sinr')
            if sinr is not None:
                cell['best_sinr'] = max(cell['best_sinr'] if cell['best_sinr'] is not None else -999, sinr)
            if rsrp is not None:
                cell['best_rsrp'] = max(cell['best_rsrp'] if cell['best_rsrp'] is not None else -999, rsrp)
            if located:
                cell['located'] += 1
                width = radio.get(prefix+'_bw')
                priority = hunt_priority(width, rsrp, sinr)
                candidate = cell['candidate_position']
                rank = (priority, sinr if sinr is not None else -999, rsrp if rsrp is not None else -999)
                if candidate is None or rank > tuple(candidate['rank']):
                    cell['hunt_priority'] = priority
                    cell['candidate_position'] = {k: gps[k] for k in ('lat', 'lon', 'acc_m', 'ts')}
                    cell['candidate_position'].update(rank=list(rank), rsrp=rsrp, sinr=sinr,
                        radio_ts=radio['ts'], bandwidth_mhz=width, ca=radio.get('ca'),
                        lte_anchor=radio.get('lte_cell_id'), lte_anchor_band=radio.get('lte_band'))
                best = cell['best_position']
                if best is None or (rsrp is not None and (best.get('rsrp') is None or rsrp > best['rsrp'])):
                    cell['best_position'] = {k: gps[k] for k in ('lat', 'lon', 'acc_m', 'ts')}
                    cell['best_position'].update(rsrp=rsrp, sinr=sinr, radio_ts=radio['ts'])

    def record(self, kind, data):
        with self.lock:
            data = dict(data)
            data.setdefault('ts', time.time())
            cur = self.db.execute('INSERT INTO events(kind,ts,data) VALUES (?,?,?)',
                                  (kind, data['ts'], json.dumps(data, allow_nan=False)))
            self.db.commit()
            self.seq = cur.lastrowid
            event = {**data, 'kind': kind, 'id': self.seq}
            self._remember(event)
            return event

    def gps(self, data):
        p = position(data)
        with self.lock:
            old = self.latest.get('gps')
            if old and p['ts'] <= old['ts']:
                return False
            self.record('gps', p)
        return True

    def snapshot(self, since):
        with self.lock:
            best = []
            for s in self.spots.values():
                values = sorted(s['values'])
                best.append({k: v for k, v in s.items() if k != 'values'} | {
                    'n': len(values), 'median': statistics.median(values),
                    'floor': values[math.floor((len(values)-1)*.25)],
                    'peak': values[-1], 'success_rate': (len(values)-s['failed'])/len(values), 'confidence': 'Repeated' if len(values) >= 3 else 'Provisional'})
            best.sort(key=lambda s: (s['floor'], s['n']), reverse=True)
            reset = since > self.seq or bool(self.events and since and since < self.events[0]['id']-1)
            return {'experimental_ta': {'enabled': self.ta_enabled, 'status': self.ta_status, 'capture': self.ta_capture,
                    'ranges': [{k:v for k,v in r.items() if not k.endswith('_hex')} for r in self.ranges]},
                    'target': self.target, 'observations': [o for o in self.radio_track if reset or not since or o['id'] > since],
                    'track_limit': self.radio_track.maxlen, 'latest': dict(self.latest), 'events': [e for e in self.events if reset or e['id'] > since],
                    'cells': sorted((dict(c) for c in self.cells.values()), key=lambda c: (c.get('hunt_priority', 0), bool(c.get('candidate_position')), (c.get('candidate_position') or {}).get('sinr') if (c.get('candidate_position') or {}).get('sinr') is not None else -999, c.get('bandwidth_mhz') or 0), reverse=True),
                    'cursor': self.seq, 'reset': reset, 'best': best[:20], 'running': self.running,
                    'busy': self.busy, 'mode': self.mode, 'remaining': self.remaining, 'active': self.active, 'error': self.error, 'bytes': self.bytes, 'started': self.started}


def parse_radio(out):
    # Standalone LTE/SA may embed the RAT after servingcell and state;
    # NSA emits separate LTE and NR5G-NSA lines. Normalize both forms.
    out = re.sub(r'(\+QENG:\s*)"servingcell","[^"]+",("(?:LTE|NR5G-SA)",)', r'\1\2', out)
    parsed = parse_modem(out)
    for line in out.splitlines():
        if not line.strip().startswith('+QENG:'):
            continue
        fields = next(csv.reader([line.split(':', 1)[1].strip()], skipinitialspace=True))
        if len(fields) >= 15 and fields[0] == 'LTE':
            parsed.update(lte_plmn=fields[2]+'-'+fields[3], lte_cell_id=fields[4].upper(),
                          lte_tac=fields[10].upper(), lte_arfcn=mudi_survey.num(fields[6]),
                          lte_ul_bw=mudi_survey.QENG_BW.get(int(mudi_survey.num(fields[8]) or 0)))
        elif len(fields) >= 10 and fields[0] == 'NR5G-NSA':
            parsed.update(nr_plmn=fields[1]+'-'+fields[2], nr_arfcn=mudi_survey.num(fields[7]),
                          nr_rsrq=mudi_survey.num(fields[6]))
        elif len(fields) >= 13 and fields[0] == 'NR5G-SA':
            parsed.update(nr_plmn=fields[2]+'-'+fields[3], nr_cell_id=fields[4].upper(),
                          nr_tac=fields[6].upper(), nr_arfcn=mudi_survey.num(fields[7]),
                          nr_rsrq=mudi_survey.num(fields[11]))
    parsed['raw_radio'] = out
    return parsed


def parse_uplink_layers(out):
    """Published QNWCFG layer layout; not per-carrier traffic confirmation.

    Join bands using only QCAINFO from this same query, never cached CA data.
    Firmware can retain layer counts, so receipt time is not sample age.
    """
    bands = {}
    layers = []
    for line in out.splitlines():
        if not line.startswith(('+QCAINFO:', '+QNWCFG:')):
            continue
        try:
            f = next(csv.reader([line.split(':', 1)[1].strip()], skipinitialspace=True))
            if line.startswith('+QCAINFO:') and len(f) >= 4:
                match = re.fullmatch(r'(LTE|NR5G) BAND (\d+)', f[3])
                if match:
                    rat = 'nr' if match[1] == 'NR5G' else 'lte'
                    bands.setdefault((rat, int(f[1])), set()).add(('n' if rat == 'nr' else 'B')+match[2])
            elif f[0] in ('lte_mimo_info', 'nr5g_mimo_info') and len(f) == 5:
                pci, channel, dl, ul = map(int, f[1:])
                rat = 'nr' if f[0].startswith('nr') else 'lte'
                if 0 <= pci <= (1007 if rat == 'nr' else 503) and channel >= 0 and 0 <= dl <= 8 and 0 <= ul <= 8:
                    layers.append(dict(rat=rat, pci=pci, channel=channel, dl_layers=dl, ul_layers=ul))
        except (ValueError, IndexError, csv.Error):
            continue
    for row in layers:
        candidates = bands.get((row['rat'], row['channel']), set())
        row['band'] = next(iter(candidates)) if len(candidates) == 1 else None
    return {'carriers': layers, 'source': 'QNWCFG MIMO layers',
            'activity_confirmed': False, 'modem_sample_age_known': False,
            'raw': out}


def summarize_uplink(samples):
    carriers = {}
    for sample in samples:
        for c in sample['carriers']:
            key = (c['rat'], c['channel'], c['pci'], c['band'])
            row = carriers.setdefault(key, {**c, 'samples': 0, 'positive_samples': 0, 'max_ul_layers': 0})
            row['samples'] += 1
            row['positive_samples'] += int(c['ul_layers'] > 0)
            row['max_ul_layers'] = max(row['max_ul_layers'], c['ul_layers'])
    return {'samples': len(samples), 'carriers': list(carriers.values()),
            'activity_confirmed': False, 'modem_sample_age_known': False}


def radio_worker(survey, stop, interval):
    next_neighbours = 0
    while not stop.is_set():
        start = time.monotonic()
        commands = ['AT+QENG="servingcell"', 'AT+QCAINFO']
        with survey.lock:
            testing, generation = survey.busy, survey.generation
        neighbours_due = start >= next_neighbours
        if neighbours_due:
            commands += ['AT+QENG="neighbourcell"']
            next_neighbours = start + 2
        if testing:
            commands += ['AT+QNWCFG="lte_mimo_info"', 'AT+QNWCFG="nr5g_mimo_info"']
        out = router(commands)
        d = parse_radio(out)
        with survey.lock:
            previous = survey.latest.get('radio', {})
        if not neighbours_due:
            for k in ('neighbours', 'neighbours_ts'):
                d[k] = previous.get(k)
        else:
            d['neighbours_ts'] = time.time()
        d['ca_ts'] = time.time()
        if testing:
            sample = parse_uplink_layers(out)
            sample.update(ts=time.time(), poll_ms=round((time.monotonic()-start)*1000))
            with survey.lock:
                if survey.busy and survey.generation == generation:
                    survey.uplink_samples.append(sample)
                    if survey.active is not None:
                        survey.active['uplink'] = sample
        d.update(poll_ms=round((time.monotonic()-start)*1000), error='' if d['rat'] else 'No modem response / no serving cell')
        survey.record('radio', d)
        # Serialized acquisition, no catch-up queue when the modem is slow/offline.
        delay = interval if d['rat'] else max(1, interval)
        stop.wait(max(.05, delay-(time.monotonic()-start)))


def traffic_worker(survey, stop, iface):
    prev = (None, 0)
    while not stop.is_set():
        start = time.monotonic()
        rate, prev = passive_rate(iface, prev)
        survey.record('traffic', {'mbps': max(0, rate) if rate is not None else None})
        stop.wait(max(.05, .5-(time.monotonic()-start)))


def upload(url, iface, size, path, method='POST'):
    with open(path, 'wb') as f:
        # Random payload avoids measuring compression of zero-filled data.
        block = os.urandom(min(size, 1048576))
        left = size
        while left:
            part = block[:min(left, len(block))]
            f.write(part)
            left -= len(part)
    cmd = ['curl', '--silent', '--show-error', '--noproxy', '*', '--interface', iface,
           '--connect-timeout', '3', '--max-time', '6', '--max-filesize', '65536',
           '--output', os.devnull, '--header', 'Expect:', '--header', 'Content-Type: application/octet-stream',
           '--request', method, '--upload-file', path, '--write-out', '%{json}', url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        info = json.loads(p.stdout)
        sent = int(info.get('size_upload', 0))
        duration = float(info.get('time_total', 0))
        code = int(info.get('http_code', 0))
        error = ''
        if p.returncode or not 200 <= code < 300 or sent != size:
            error = f'Upload incomplete or rejected (HTTP {code}, curl {p.returncode})'
        elif duration < .4:
            error = 'Test too short; increasing payload'
        return {'mbps': sent*8/1e6/duration if duration else None, 'bytes': sent,
                'transfer_seconds': duration, 'http_status': code, 'error': error}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'mbps': None, 'bytes': 0, 'error': 'Upload failed or timed out'}


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    index = (len(values)-1)*fraction
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo]+(values[hi]-values[lo])*(index-lo)


def request_measurement(info, expected, headers='', now=None):
    """curl approximation to browser requestStart→responseStart; no server subtraction.

    Cloudflare's upload implementation includes a 0.5% estimated header allowance.
    Preserve payload goodput separately so the two metrics cannot be confused.
    """
    sent = int(info.get('size_upload', 0))
    total = float(info.get('time_total', 0))
    setup = float(info.get('time_pretransfer', 0))
    first_byte = float(info.get('time_starttransfer', 0))
    duration = first_byte-setup
    accepted = (int(info.get('exitcode', 0)) == 0 and 200 <= int(info.get('http_code', 0)) < 300
                and sent == expected)
    timing_valid = accepted and duration >= .01 and math.isfinite(duration)
    server_match = re.search(r'cfReq(?:uest)?Dur(?:ation)?;\s*dur=([0-9.]+)', headers, re.I)
    try:
        server_ms = float(server_match.group(1)) if server_match else None
        if server_ms is not None and not math.isfinite(server_ms):
            server_ms = None
    except ValueError:
        server_ms = None
    return {'ts': time.time() if now is None else now, 'bytes': sent,
            'http_status': int(info.get('http_code', 0)), 'accepted': accepted,
            'request_seconds': duration if duration > 0 else None,
            'total_seconds': total, 'setup_seconds': setup,
            'server_reported_ms': server_ms,
            'payload_mbps': sent*8/1e6/total if accepted and total > 0 else None,
            'estimate_mbps': sent*8*1.005/1e6/duration if timing_valid else None,
            'connection_reused': int(info.get('num_connects', 1)) == 0}


def upload_batch(url, iface, size, path, method, target, progress=None):
    """Sequential accepted requests share curl's connection pool, with a warm-up first.

    Parked mode ends at a 20-second wall-clock deadline, including warm-up.
    Only completed, accepted payloads contribute; an in-flight tail is excluded.
    Other modes retain the legacy warmed batch.
    """
    fixed_duration = target == 20
    base_count = 2 if target == 5 else 8
    total_payload = max(262144, min(256*1048576, int(size)))*base_count
    count = max(base_count, math.ceil(total_payload/(32*1048576)))
    size = math.ceil(total_payload/count)
    if fixed_duration:
        size = max(262144, min(1048576, size))
        count = 4096  # Safety ceiling, never a minimum; the timer ends the test.
    block = os.urandom(min(size, 1048576))
    with open(path, 'wb') as f:
        left = size
        while left:
            part = block[:min(left, len(block))]
            f.write(part)
            left -= len(part)
    warm = Path(str(path)+'.warm')
    warm.write_bytes(block[:262144])
    cmd = ['curl', '--silent', '--show-error', '--fail-early', '--no-buffer']
    for i in range(count+1):
        if i:
            cmd.append('--next')
        cmd += ['--no-buffer', '--noproxy', '*', '--interface', iface, '--connect-timeout', '3',
                '--max-time', str(target if fixed_duration else 12), '--max-filesize', '65536', '--fail',
                '--output', os.devnull, '--header', 'Expect:',
                '--header', 'Content-Type: application/octet-stream', '--request', method,
                '--dump-header', str(path)+f'.headers-{i}',
                '--upload-file', str(path if i else warm), '--write-out', ('%{stderr}' if fixed_duration else '')+'%{json}\n', url]
    if fixed_duration:
        # A config file avoids the OS argument-size limit for repeated requests.
        config_path = Path(str(path)+'.curl-config')
        lines = []
        i = 1
        flags = {'--silent', '--show-error', '--fail-early', '--no-buffer', '--next', '--fail'}
        while i < len(cmd):
            option = cmd[i]
            if option in flags:
                lines.append(option[2:]); i += 1
            elif option.startswith('--'):
                value = cmd[i+1].replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                lines.append(option[2:] + ' = "' + value + '"'); i += 2
            else:
                lines.append('url = "' + option.replace('"', '\\"') + '"'); i += 1
        config_path.write_text('\n'.join(lines)+'\n')
        cmd = ['curl', '--config', str(config_path)]
    records = []
    requests = []
    start = time.time()
    clock_start = time.monotonic()
    measured_start = start if fixed_duration else None
    timed_out = threading.Event()
    def expire(process):
        timed_out.set()
        process.kill()
    try:
        # Stderr to a temporary file avoids a pipe deadlock on repeated errors.
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if fixed_duration else errors, text=True) as process:
                timer = threading.Timer(target if fixed_duration else target*2+15, expire, args=(process,))
                timer.start()
                try:
                    for line in process.stdout:
                        if fixed_duration and not line.startswith('{'):
                            continue
                        info = json.loads(line)
                        records.append(info)
                        header_file = Path(str(path)+f'.headers-{len(records)-1}')
                        headers = header_file.read_text(errors='replace') if header_file.exists() else ''
                        request = request_measurement(info, size if len(records) > 1 else warm.stat().st_size, headers)
                        if len(records) > 1:
                            requests.append(request)
                        if len(records) == 1:
                            if not fixed_duration:
                                measured_start = time.time()
                        elif progress:
                            measured = records[1:]
                            seconds = sum(float(r.get('time_total', 0)) for r in measured)
                            sent = sum(int(r.get('size_upload', 0)) for r in measured)
                            if fixed_duration:
                                seconds = time.monotonic()-clock_start
                            progress({'start': measured_start, 'target': target,
                                      'completed': len(measured), 'chunks': count,
                                      'mbps': sent*8/1e6/seconds if seconds else None,
                                      'request': request, 'requests': list(requests)})
                    rc = process.wait()
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
        measured = records[1:]
        seconds = sum(float(r.get('time_total', 0)) for r in measured)
        sent = sum(int(r.get('size_upload', 0)) for r in measured)
        if fixed_duration:
            seconds = time.monotonic()-clock_start
        valid = (((timed_out.is_set() and bool(measured)) if fixed_duration else (not rc and not timed_out.is_set() and len(measured) == count))
                 and all(200 <= int(r.get('http_code', 0)) < 300
                         and int(r.get('size_upload', 0)) == size for r in measured))
        rates = [float(r['size_upload'])*8/1e6/float(r['time_total'])
                 for r in measured if float(r.get('time_total', 0)) > 0]
        estimates = [r['estimate_mbps'] for r in requests if r['estimate_mbps'] is not None]
        return {'mbps': sent*8/1e6/seconds if seconds else None,
                'request_measurements': requests, 'estimate_median_mbps': percentile(estimates, .5),
                'estimate_p90_mbps': percentile(estimates, .9),
                'estimate_count': len(estimates), 'estimate_method': 'curl-ttfb-plus-0.5pct-v1',
                'interface': iface, 'endpoint': urlparse(url).hostname,
                'bytes': sum(int(r.get('size_upload', 0)) for r in records),
                'payload_bytes': sent, 'transfer_seconds': seconds,
                'measurement_start': measured_start or start,
                'setup_seconds': float(records[0].get('time_pretransfer', 0)) if records else None,
                'warmup_seconds': float(records[0].get('time_total', 0)) if records else None,
                'reused_connections': sum(int(r.get('num_connects', 1)) == 0 for r in measured),
                'chunks': len(measured), 'chunk_mbps': rates,
                'min_mbps': min(rates) if rates else None,
                'max_mbps': max(rates) if rates else None,
                'short': valid and seconds < target*.8,
                'target_seconds': target, 'method': 'fixed-window-accepted-payload-v1' if fixed_duration else 'warmed-batch-v2',
                'http_status': int(records[-1].get('http_code', 0)) if records else 0,
                'error': '' if valid else (f'Mudi interface {iface} unavailable; reconnect the router' if rc == 45
                    else 'Upload batch exceeded its time limit' if timed_out.is_set()
                    else f'Upload batch incomplete or rejected (HTTP {int(records[-1].get("http_code", 0)) if records else 0}, curl {rc})')}
    except (OSError, ValueError, KeyError):
        return {'mbps': None, 'bytes': 0, 'error': 'Upload batch failed'}


def probe_worker(survey, stop, config):
    size = 16*1048576
    previous = survey.latest.get('probe', {})
    if previous.get('mbps') and not previous.get('error'):
        size = int(previous['mbps']*1e6/8*2.5)
    failures = 0
    with tempfile.TemporaryDirectory(prefix='upload-atlas-') as folder:
        while not stop.is_set():
            if not survey.running:
                stop.wait(.2)
                continue
            start = time.time()
            with survey.lock:
                mode, generation = survey.mode, survey.generation
                target = 20 if mode == 'parked' else 5
                survey.busy = True
                survey.uplink_samples = []
                survey.active = {'start': start, 'target': target, 'mbps': None, 'completed': 0,
                                 'chunks': None if mode == 'parked' else 2}
                radio = dict(survey.latest.get('radio', {}))
            def progress(data):
                with survey.lock:
                    if survey.uplink_samples:
                        data['uplink'] = survey.uplink_samples[-1]
                    survey.active = data
            result = upload_batch(config.upload_url, config.iface, size, Path(folder)/'payload',
                                  config.upload_method, target, progress)
            end = time.time()
            result['mode'] = mode
            with survey.lock:
                result = qualify_probe(result, list(survey.fixes), result.pop('measurement_start', start), end)
                result.update(rat=radio.get('rat'), ca=radio.get('ca'))
                result['uplink_samples'] = list(survey.uplink_samples)
                result['uplink'] = summarize_uplink(survey.uplink_samples)
                survey.record('probe', result)
                survey.busy = False
                survey.active = None
                survey.error = result['reason']
                if mode == 'parked' and generation == survey.generation:
                    survey.remaining = max(0, survey.remaining-1)
                    if not survey.remaining:
                        survey.running = False
            if result.get('transfer_seconds', 0) > 0:
                size = int(max(262144, min(256*1048576,
                    result.get('payload_bytes', 0)/result['transfer_seconds']*2.5)))
            failures = min(4, failures+1) if result.get('error') else 0
            pause = min(60, 4*2**failures) if failures else config.probe_every
            # Pause is measured AFTER each completed test, not start-to-start.
            stop.wait(pause)


def demo_worker(survey, stop):
    i = 0
    while not stop.is_set():
        t = time.time()
        p = {'lat': 52.515+i*.000009, 'lon': 13.39+math.sin(i/40)*.002,
             'ts': t, 'acc_m': 5, 'speed_kmh': 24, 'source': 'simulation'}
        survey.gps(p)
        speed = 45+100*(1+math.sin(i/30))/2
        survey.record('radio', {'rat': 'NR5G-NSA', 'ca': 'B3(20)+n78(100)', 'lte_rsrp': -85,
                               'lte_sinr': 19, 'nr_rsrp': -79, 'nr_sinr': 25, 'poll_ms': 94})
        survey.record('traffic', {'mbps': speed if survey.running else 0})
        if survey.running and i % 4 == 0:
            survey.record('probe', {**p, 'start': t-2, 'duration': 2, 'mbps': speed,
                                    'bytes': int(speed*1e6/8*2), 'eligible': True, 'reason': '',
                                    'footprint_m': 14, 'rat': 'NR5G-NSA', 'ca': 'B3(20)+n78(100)'})
        i += 1
        stop.wait(.5)


def handler(survey, config):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, status=200, mime='application/json'):
            body = json.dumps(data, allow_nan=False).encode() if mime == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == '/phone.mobileconfig' and getattr(config, 'phone_profile', None):
                self.reply(Path(config.phone_profile).read_bytes(), mime='application/x-apple-aspen-config')
            elif url.path == '/api/state':
                try:
                    since = max(0, int(parse_qs(url.query).get('since', ['0'])[0]))
                except ValueError:
                    return self.reply({'error': 'Invalid cursor'}, 400)
                data = survey.snapshot(since)
                data['config'] = {'demo': config.demo, 'upload_ready': bool(config.upload_url) or config.demo,
                                  'probe_every': config.probe_every, 'radio_interval': config.radio_interval,
                                  'iface': config.iface, 'drive_seconds': 5, 'parked_seconds': 20}
                self.reply(data)
            elif url.path == '/api/export':
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(['id', 'kind', 'ts', 'data_json'])
                with survey.lock:
                    writer.writerows(survey.db.execute('SELECT id,kind,ts,data FROM events ORDER BY id'))
                self.reply(out.getvalue().encode(), mime='text/csv')
            else:
                files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                         '/localization.js': 'localization.js',
                         '/views.js': 'views.js', '/views.css': 'views.css',
                         '/radio.js': 'radio.js', '/track-model.js': 'track-model.js', '/track.js': 'track.js', '/track.css': 'track.css', '/mission.js': 'mission.js', '/mission.css': 'mission.css',
                         '/leaflet.js': 'leaflet.js', '/leaflet.css': 'leaflet.css'}
                name = files.get(url.path)
                if not name or not (ROOT/'web'/name).exists():
                    return self.reply({'error': 'Not found'}, 404)
                mime = 'text/html' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
                self.reply((ROOT/'web'/name).read_bytes(), mime=mime)

        def do_POST(self):
            # Same-origin browser control; native GPS feeders may omit Origin.
            origin = self.headers.get('Origin')
            if origin and urlparse(origin).netloc != self.headers.get('Host'):
                return self.reply({'error': 'Origin mismatch'}, 403)
            try:
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 <= n <= 4096:
                    raise ValueError('Request too large')
                p = json.loads(self.rfile.read(n) or b'{}')
                if not isinstance(p, dict):
                    raise ValueError('Expected a JSON object')
                if self.path == '/api/pos':
                    accepted = survey.gps(p)
                    return self.reply({'ok': True, 'accepted': accepted})
                if self.path == '/api/target':
                    if 'identity' not in p:
                        raise ValueError('identity is required; use null to end the hunt')
                    identity = p.get('identity')
                    with survey.lock:
                        if identity is not None and (not isinstance(identity, str) or identity not in survey.cells):
                            raise ValueError('Choose a recorded cell identity')
                        survey.record('hunt_target', {'identity': identity})
                    return self.reply({'ok': True, 'target': identity})
                if self.path == '/api/experimental-ta':
                    if not isinstance(p.get('enabled'), bool):
                        raise ValueError('enabled must be a boolean')
                    survey.ta_enabled = p['enabled']
                    return self.reply({'ok': True, 'enabled': survey.ta_enabled})
                if self.path == '/api/control':
                    if not isinstance(p.get('running'), bool):
                        raise ValueError('running must be a boolean')
                    if p['running'] and not (config.upload_url or config.demo):
                        raise ValueError('Configure an accepting HTTPS upload endpoint first')
                    with survey.lock:
                        mode = p.get('mode', survey.mode)
                        if mode not in ('drive', 'parked'):
                            raise ValueError('mode must be drive or parked')
                        if survey.busy and mode != survey.mode:
                            raise ValueError('Pause and wait for the current test to finish before switching modes')
                        survey.generation += 1
                        survey.mode = mode
                        survey.remaining = 1 if mode == 'parked' and p['running'] else 0
                        survey.running = p['running']
                    return self.reply({'ok': True})
                self.reply({'error': 'Not found'}, 404)
            except (ValueError, KeyError, TypeError) as e:
                self.reply({'error': str(e)}, 400)
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--iface', default='en12')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--db', default='survey.sqlite3')
    ap.add_argument('--demo', action='store_true')
    ap.add_argument('--experimental-ta', action='store_true', help='Try unvalidated NR RACH range rings; requires pycrate')
    ap.add_argument('--radio-interval', type=float, default=.25)
    ap.add_argument('--probe-every', type=float, default=1, help='Pause in seconds after each upload batch')
    ap.add_argument('--upload-url', default=os.environ.get('ATLAS_UPLOAD_URL', 'https://speed.cloudflare.com/__up'))
    ap.add_argument('--upload-method', choices=['POST', 'PUT'], default='POST')
    ap.add_argument('--tls-cert')
    ap.add_argument('--tls-key')
    ap.add_argument('--https-port', type=int, help='Additional HTTPS listener sharing the same recording')
    ap.add_argument('--phone-profile', help='Public iPhone certificate profile to serve')
    ap.add_argument('--ssh-known-hosts', help='Known-hosts file for the Mudi SSH connection')
    config = ap.parse_args()
    if config.radio_interval < .25 or config.probe_every < 1:
        ap.error('Minimum radio interval: 0.25 s; minimum probe period: 1 s')
    if config.upload_url and urlparse(config.upload_url).scheme != 'https':
        ap.error('Upload URL must use HTTPS')
    if bool(config.tls_cert) != bool(config.tls_key):
        ap.error('Supply both --tls-cert and --tls-key')
    if config.https_port and not config.tls_cert:
        ap.error('--https-port requires a certificate and key')
    if config.ssh_known_hosts:
        mudi_survey.SSH[1:1] = ['-o', 'UserKnownHostsFile='+config.ssh_known_hosts]
    survey = Survey(':memory:' if config.demo else config.db)
    survey.ta_enabled = config.experimental_ta and not config.demo
    stop = threading.Event()
    server = ThreadingHTTPServer((config.host, config.port), handler(survey, config))
    https_server = None
    if config.tls_cert:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(config.tls_cert, config.tls_key)
        if config.https_port:
            https_server = ThreadingHTTPServer((config.host, config.https_port), handler(survey, config))
            https_server.socket = ctx.wrap_socket(https_server.socket, server_side=True)
            threading.Thread(target=https_server.serve_forever, daemon=True).start()
        else:
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
    jobs = [(demo_worker, (survey, stop))] if config.demo else [
        (radio_worker, (survey, stop, config.radio_interval)),
        (traffic_worker, (survey, stop, config.iface))]
    if config.upload_url and not config.demo:
        jobs.append((probe_worker, (survey, stop, config)))
    if not config.demo:
        from nr_localization import worker as ta_worker
        jobs.append((ta_worker, (survey, stop, mudi_survey.SSH)))
    threads = []
    for fn, params in jobs:
        thread = threading.Thread(target=fn, args=params, daemon=True)
        thread.start()
        threads.append(thread)
    print(f'Upload Atlas: {"https" if config.tls_cert and not config.https_port else "http"}://localhost:{config.port} — {"SIMULATION" if config.demo else config.db}', flush=True)
    if https_server:
        print(f'Phone HTTPS: port {config.https_port}', flush=True)
    def shutdown_signal(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, shutdown_signal)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        if https_server:
            https_server.shutdown()
            https_server.server_close()
        for thread in threads:
            thread.join(timeout=60)
        survey.db.close()


if __name__ == '__main__':
    main()
