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
from pathlib import Path
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
import uuid

from survey_core import Journal
from cell_evidence import CellRegistry, identity, load_sites, localization_worker, fit_rings, match_sites
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
        self.journal = Journal(db)
        self.db = self.journal  # Existing callers may still close the recorder through this alias.
        self.evidence = CellRegistry()
        self.sites = []
        self.instance = uuid.uuid4().hex
        self.events = deque(maxlen=12000)
        self.fixes = deque(maxlen=600)
        self.latest = {}
        self.spots = {}
        self.cells = {}
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
        self.generation = 0
        for event in self.journal.history():
            self.seq = event['id']
            self._remember(event)

    def _remember(self, event):
        self.events.append(event)
        kind = event['kind']
        self.latest[kind] = event
        if kind == 'gps':
            self.fixes.append(event)
        if kind == 'radio':
            packet = {'ts': event.get('sample_ts', event['ts']), 'latency_ms': event.get('poll_ms', 0)}
            for prefix, rat in (('lte', 'LTE'), ('nr', 'NR')):
                if event.get(prefix+'_band'):
                    packet[prefix] = {'radio':rat, 'plmn':event.get(prefix+'_plmn'),
                        'cell':event.get(prefix+'_cell_id'), 'band':event[prefix+'_band'],
                        'pci':event.get(prefix+'_pci'), 'arfcn':event.get(prefix+'_arfcn'),
                        'rsrp':event.get(prefix+'_rsrp'), 'rsrq':event.get(prefix+'_rsrq'),
                        'bandwidth_mhz':event.get(prefix+'_bw')}
            if 'cell_observations' not in event:
                event['cell_observations'] = self.evidence.observations_for(packet,
                    [dict(p, accuracy=p['acc_m']) for p in self.fixes])
            self.evidence.remember_radio({'id':event['id'], 'observations':event['cell_observations']})
            self._remember_cells(event)
        if kind == 'timing':
            self.evidence.remember_timing(event)
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
        sample_ts = radio.get('sample_ts', radio['ts'])
        gps = min(self.fixes, key=lambda p: abs(p['ts']-sample_ts), default=None)
        located = bool(gps and abs(sample_ts-gps['ts']) <= 1.5 and gps['acc_m'] <= 25 and radio.get('poll_ms',0) <= 1500)
        if located:
            fields = {k: v for k, v in radio.items() if k.startswith(('lte_', 'nr_')) or k in ('rat', 'ca', 'ca_ts')}
            self.radio_track.append({'id': radio['id'], 'ts': sample_ts,
                'gps_ts': gps['ts'], 'gps_delta_s': sample_ts-gps['ts'],
                'lat': gps['lat'], 'lon': gps['lon'], 'acc_m': gps['acc_m'], 'radio': fields})
        for prefix in ('lte', 'nr'):
            band, pci = radio.get(prefix+'_band'), radio.get(prefix+'_pci')
            if not band or pci is None:
                continue
            plmn = radio.get(prefix+'_plmn')
            channel = radio.get(prefix+'_arfcn')
            full_id = radio.get(prefix+'_cell_id')
            cell_key = identity({'radio':prefix.upper(), 'plmn':plmn, 'cell':full_id})
            if not cell_key:
                continue  # Unknown sightings stay in the raw/track history, not a fictional unique cell.
            full_id = cell_key.rsplit(':',1)[1]
            if cell_key not in self.cells:
                self.cells[cell_key] = {'identity': cell_key, 'band': band, 'pci': pci,
                    'plmn': plmn, 'cell_id': full_id, 'channel': channel,
                    'identity_quality': 'Cell ID' if full_id and plmn else 'Radio signature (may repeat)',
                    'first_seen': radio['ts'], 'observations': 0, 'located': 0,
                    'best_position': None, 'candidate_position': None, 'hunt_priority': 0, 'best_rsrp': None, 'best_sinr': None}
            cell = self.cells[cell_key]
            cell.update(last_seen=radio['ts'], band=band, pci=pci, channel=channel, bandwidth_mhz=radio.get(prefix+'_bw'),
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
                        radio_ts=sample_ts, bandwidth_mhz=width, ca=radio.get('ca'),
                        lte_anchor=radio.get('lte_cell_id'), lte_anchor_band=radio.get('lte_band'))
                best = cell['best_position']
                if best is None or (rsrp is not None and (best.get('rsrp') is None or rsrp > best['rsrp'])):
                    cell['best_position'] = {k: gps[k] for k in ('lat', 'lon', 'acc_m', 'ts')}
                    cell['best_position'].update(rsrp=rsrp, sinr=sinr, radio_ts=sample_ts)

    def record(self, kind, data):
        with self.lock:
            data = dict(data)
            data.setdefault('ts', time.time())
            self.seq += 1
            event = {**data, 'kind': kind, 'id': self.seq}
            self._remember(event)
            self.journal.append(event)
            return event

    def gps(self, data):
        p = position(data)
        with self.lock:
            old = self.latest.get('gps')
            if old and p['ts'] <= old['ts']:
                return False
            if old and p['source'] != old.get('source') and time.time()-old['ts'] < 3 and p['acc_m'] > old['acc_m']:
                return False
            self.record('gps', p)
        return True

    def timing(self, payload):
        with self.lock:
            result = self.evidence.validate_timing(payload, [dict(p, accuracy=p['acc_m']) for p in self.fixes])
            rings = self.evidence.cells[result['key']]['rings']
            if rings and result['ts'] <= rings[-1]['ts']:
                return False
            self.record('timing', result)
            return True

    def require_parked(self):
        gps = self.latest.get('gps', {})
        if self.journal.error:
            raise ValueError(self.journal.error)
        if time.time()-gps.get('ts',0) > 3 or gps.get('speed_kmh') is None or gps['speed_kmh'] > 3 or gps.get('acc_m',999) > 25:
            raise ValueError('Park first: verification requires fresh GPS, accuracy ≤25 m and speed ≤3 km/h')

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
            return {'observations': [o for o in self.radio_track if reset or not since or o['id'] > since],
                    'track_limit': self.radio_track.maxlen, 'latest': dict(self.latest), 'events': [e for e in self.events if reset or e['id'] > since],
                    'cells': sorted((dict(c) for c in self.cells.values()), key=lambda c: (c.get('hunt_priority', 0), bool(c.get('candidate_position')), (c.get('candidate_position') or {}).get('sinr') if (c.get('candidate_position') or {}).get('sinr') is not None else -999, c.get('bandwidth_mhz') or 0), reverse=True),
                    'cursor': self.seq, 'reset': reset, 'best': best[:20], 'running': self.running,
                    'busy': self.busy, 'mode': self.mode, 'remaining': self.remaining, 'active': self.active, 'error': self.journal.error or self.error,
                    'instance': self.instance, 'storage': {'saved':self.journal.saved, 'error':self.journal.error},
                    'unresolved':list(self.evidence.unresolved), 'ring_count':self.evidence.ring_count, 'site_count':len(self.sites), 'bytes': self.bytes, 'started': self.started}


def parse_radio(out):
    # Standalone LTE/SA may embed the RAT after servingcell and state;
    # NSA emits separate LTE and NR5G-NSA lines. Normalize both forms.
    raw = out
    state_match = re.search(r'\"servingcell\",\"([^\"]+)\"',out)
    rrc = state_match.group(1) if state_match else None
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
    parsed['rrc'] = rrc
    parsed['nr_bw_raw'] = parsed.get('nr_bw')
    if rrc != 'CONNECT':
        parsed['nr_bw'] = None
    for prefix in ('lte','nr'):
        parsed[prefix+'_sinr_raw'] = parsed.get(prefix+'_sinr')
        parsed[prefix+'_sinr'] = None  # Firmware encoding has not been calibrated.
    parsed['raw_radio'] = raw
    return parsed


def radio_worker(survey, stop, interval):
    count = 0
    while not stop.is_set():
        start = time.monotonic()
        out = router(['AT+QENG="servingcell"'])
        d = parse_radio(out)
        d.update(poll_ms=round((time.monotonic()-start)*1000),
                 sample_ts=time.time()-(time.monotonic()-start)/2,
                 error='' if d['rat'] else 'No modem response / no serving cell')
        with survey.lock:
            context = survey.latest.get('radio_context', {})
            for k in ('ca', 'neighbours', 'n_carriers', 'ca_ts'):
                d[k] = context.get(k)
        survey.record('radio', d)
        if count % 5 == 0 and not stop.is_set():
            context = parse_radio(router(['AT+QCAINFO', 'AT+QENG="neighbourcell"']))
            survey.record('radio_context', {k:context.get(k) for k in ('ca','neighbours','n_carriers','raw_radio')} | {'ca_ts':time.time()})
        count += 1
        stop.wait(max(.05, interval-(time.monotonic()-start)))


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


def upload_batch(url, iface, size, path, method, target, progress=None):
    """Sequential accepted requests share curl's connection pool, with a warm-up first.

    Payload goodput excludes initial connection setup but includes request/response
    waits. Actual duration is retained: adaptive payloads cannot promise a fixed time.
    """
    base_count = 2 if target == 5 else 8
    total_payload = max(262144, min(256*1048576, int(size)))*base_count
    count = max(base_count, math.ceil(total_payload/(32*1048576)))
    size = math.ceil(total_payload/count)
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
        cmd += ['--noproxy', '*', '--interface', iface, '--connect-timeout', '3',
                '--max-time', '12', '--max-filesize', '65536', '--fail',
                '--output', os.devnull, '--header', 'Expect:',
                '--header', 'Content-Type: application/octet-stream', '--request', method,
                '--upload-file', str(path if i else warm), '--write-out', '%{json}\n', url]
    records = []
    start = time.time()
    measured_start = None
    timed_out = threading.Event()
    def expire(process):
        timed_out.set()
        process.kill()
    try:
        # Stderr to a temporary file avoids a pipe deadlock on repeated errors.
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors, text=True) as process:
                timer = threading.Timer(target*2+15, expire, args=(process,))
                timer.start()
                try:
                    for line in process.stdout:
                        info = json.loads(line)
                        records.append(info)
                        if len(records) == 1:
                            measured_start = time.time()
                        elif progress:
                            measured = records[1:]
                            seconds = sum(float(r.get('time_total', 0)) for r in measured)
                            sent = sum(int(r.get('size_upload', 0)) for r in measured)
                            progress({'start': measured_start, 'target': target,
                                      'completed': len(measured), 'chunks': count,
                                      'mbps': sent*8/1e6/seconds if seconds else None})
                    rc = process.wait()
                finally:
                    timer.cancel()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
        measured = records[1:]
        seconds = sum(float(r.get('time_total', 0)) for r in measured)
        sent = sum(int(r.get('size_upload', 0)) for r in measured)
        valid = (not rc and not timed_out.is_set() and len(measured) == count
                 and all(200 <= int(r.get('http_code', 0)) < 300
                         and int(r.get('size_upload', 0)) == size for r in measured))
        rates = [float(r['size_upload'])*8/1e6/float(r['time_total'])
                 for r in measured if float(r.get('time_total', 0)) > 0]
        return {'mbps': sent*8/1e6/seconds if seconds else None,
                'bytes': sum(int(r.get('size_upload', 0)) for r in records) if valid else size*count+262144,
                'bytes_estimated': not valid,
                'payload_bytes': sent, 'transfer_seconds': seconds,
                'measurement_start': measured_start or start,
                'setup_seconds': float(records[0].get('time_pretransfer', 0)) if records else None,
                'warmup_seconds': float(records[0].get('time_total', 0)) if records else None,
                'reused_connections': sum(int(r.get('num_connects', 1)) == 0 for r in measured),
                'chunks': count, 'chunk_mbps': rates,
                'min_mbps': min(rates) if rates else None,
                'max_mbps': max(rates) if rates else None,
                'short': valid and seconds < target*.8,
                'target_seconds': target, 'method': 'warmed-batch-v1',
                'http_status': int(records[-1].get('http_code', 0)) if records else 0,
                'error': '' if valid else (f'Mudi interface {iface} unavailable; reconnect the router' if rc == 45
                    else 'Upload batch exceeded its time limit' if timed_out.is_set()
                    else f'Upload batch incomplete or rejected (HTTP {int(records[-1].get("http_code", 0)) if records else 0}, curl {rc})')}
    except (OSError, ValueError, KeyError):
        return {'mbps': None, 'bytes': size*count+262144, 'bytes_estimated':True, 'error': 'Upload batch failed'}


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
                try:
                    survey.require_parked()
                except ValueError as exc:
                    survey.running = False
                    survey.remaining = 0
                    survey.error = str(exc)
                    survey.record('notice', {'message':str(exc)})
                    continue
                if not survey.running or survey.mode != 'parked':
                    continue
                remaining_bytes = int(getattr(config,'budget_mb',2048)*1048576)-survey.bytes
                if remaining_bytes < 9*262144:
                    survey.running=False
                    survey.remaining=0
                    survey.error='Upload payload budget reached. Passive recording continues.'
                    survey.record('notice',{'message':survey.error})
                    continue
                size=min(size,(remaining_bytes-262144-64)//8)
                mode, generation = survey.mode, survey.generation
                target = 20 if mode == 'parked' else 5
                survey.busy = True
                survey.active = {'start': start, 'target': target, 'mbps': None, 'completed': 0,
                                 'chunks': 8 if mode == 'parked' else 2}
                radio = dict(survey.latest.get('radio', {}))
            def progress(data):
                with survey.lock:
                    survey.active = data
            result = upload_batch(config.upload_url, config.iface, size, Path(folder)/'payload',
                                  config.upload_method, target, progress)
            end = time.time()
            result['mode'] = mode
            with survey.lock:
                result = qualify_probe(result, list(survey.fixes), result.pop('measurement_start', start), end)
                result.update(rat=radio.get('rat'), ca=radio.get('ca'))
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
    """Synthetic full identities, reused PCI, unresolved NSA, and labelled TA evidence."""
    from survey_core import meters
    towers = {'lte:262-01:ABC':{'lat':52.5125,'lon':13.387},
              'nr:262-01:1234001':{'lat':52.5189,'lon':13.395},
              'nr:262-01:1234002':{'lat':52.514,'lon':13.399}}
    if not survey.sites:
        survey.sites = [dict(p,id='DEMO-'+str(i+1),operator='262-01',uncertainty_m=80,
                            source='Synthetic site; not BNetzA') for i,p in enumerate(towers.values())]
    def step(i, stamp):
        phase=i%360/360*math.tau
        p={'lat':52.5156+.0028*math.sin(phase),'lon':13.392+.0048*math.cos(phase),
           'ts':stamp,'acc_m':5,'speed_kmh':31,'source':'simulation'}
        survey.record('gps',p)
        nci='1234001' if i%360<220 else '1234002'
        missing=100<i%360<130
        rsrp=round(-65-20*math.log10(max(1,meters(p,towers['nr:262-01:'+nci])/70)),1)
        r={'ts':stamp,'sample_ts':stamp,'rrc':'CONNECT','rat':'NR5G-NSA' if missing else 'NR5G-SA',
           'nr_band':'n78','nr_plmn':'262-01','nr_cell_id':None if missing else nci,'nr_pci':641,
           'nr_arfcn':630000 if nci=='1234001' else 640000,'nr_bw':100 if nci=='1234001' else 80,
           'nr_rsrp':rsrp,'nr_rsrq':-10,'nr_sinr':24,'sinr_encoding':'synthetic dB',
           'poll_ms':87,'ca':'B3(20)+n78(100)' if missing else 'n78(100)', 'ca_ts':stamp,
           'neighbours':[{'band':'B3','pci':219,'earfcn':1300,'rsrp':-98,'rsrq':-13}]}
        if missing:
            r.update(lte_band='B3',lte_plmn='262-01',lte_cell_id='ABC',lte_pci=218,lte_arfcn=1300,
                     lte_bw=20,lte_rsrp=-86,lte_rsrq=-10,lte_sinr=18)
        event=survey.record('radio',r)
        if i%12==0:
            for o in event['cell_observations']:
                if not o.get('key'):continue
                scs=30 if o['radio']=='NR' else 15
                step_m=299792458*16/(15000*2048)/2*15/scs
                index=round(meters(p,towers[o['key']])/step_m)
                survey.record('timing',dict(lat=p['lat'],lon=p['lon'],accuracy=5,ts=stamp,
                    key=o['key'],reference_key=o['key'],ta_index=index,scs_khz=scs,
                    encoding='nr-absolute-rar' if scs==30 else 'lte-absolute-16ts',
                    distance_m=index*step_m,step_m=step_m,uncertainty_m=step_m+55,
                    source='Synthetic timing advance',range_error_m=50))
    start=time.time()-180
    for i in range(360):step(i,start+i*.5)
    i=0
    while not stop.is_set():
        step(i,time.time());i+=1;stop.wait(.5)


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
                                  'probe_every': config.probe_every, 'radio_interval': config.radio_interval, 'budget_mb':getattr(config,'budget_mb',2048),
                                  'iface': config.iface, 'drive_seconds': 5, 'parked_seconds': 20}
                self.reply(data)
            elif url.path == '/api/cell':
                try:
                    with survey.lock:
                        detail = survey.evidence.detail(parse_qs(url.query).get('key',[''])[0])
                    at = parse_qs(url.query).get('at',[None])[0]
                    if at is not None:
                        stamp = float(at)
                        if not math.isfinite(stamp):
                            raise ValueError('Invalid replay time')
                        detail['rings'] = [r for r in detail['rings'] if r['ts'] <= stamp]
                        detail['history'] = [r for r in detail['history'] if r['ts'] <= stamp]
                        detail['estimate'] = fit_rings(detail['rings'])
                        detail['matches'] = match_sites(detail['rings'], survey.sites, detail['key'])
                        detail['trail'] = [v for v in detail.get('trail',[]) if v['ts'] <= stamp]
                    self.reply(detail)
                except ValueError:
                    self.reply({'error':'Invalid replay time'},400)
                except KeyError:
                    self.reply({'error':'Unknown full cell identity'},404)
            elif url.path == '/api/export':
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow(['id', 'kind', 'ts', 'data_json'])
                with survey.lock:
                    cutoff, tail = survey.seq, list(survey.events)
                records = [e for e in survey.journal.history() if e['id'] <= cutoff]
                last = records[-1]['id'] if records else 0
                records.extend(e for e in tail if last < e['id'] <= cutoff)
                writer.writerows((e['id'],e['kind'],e['ts'],json.dumps(e)) for e in records)
                self.reply(out.getvalue().encode(), mime='text/csv')
            else:
                files = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css',
                         '/views.js': 'views.js', '/views.css': 'views.css',
                         '/radio.js': 'radio.js', '/track-model.js': 'track-model.js', '/track.js': 'track.js', '/track.css': 'track.css', '/evidence.js':'evidence.js', '/evidence.css':'evidence.css',
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
                if self.path == '/api/timing':
                    if config.demo:
                        raise ValueError('Live timing input is disabled in simulation')
                    return self.reply({'ok':True,'accepted':survey.timing(p)})
                if self.path == '/api/control':
                    if not isinstance(p.get('running'), bool):
                        raise ValueError('running must be a boolean')
                    if p['running'] and not (config.upload_url or config.demo):
                        raise ValueError('Configure an accepting HTTPS upload endpoint first')
                    with survey.lock:
                        mode = p.get('mode', survey.mode)
                        if mode != 'parked':
                            raise ValueError('Driving stays passive; only parked verification is supported')
                        if p['running']:
                            survey.require_parked()
                            if survey.busy or survey.running:
                                raise ValueError('Verification already active')
                        if survey.busy and mode != survey.mode:
                            raise ValueError('Pause and wait for the current test to finish before switching modes')
                        survey.generation += 1
                        survey.mode = mode
                        survey.remaining = 3 if mode == 'parked' and p['running'] else 0
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
    ap.add_argument('--radio-interval', type=float, default=.75)
    ap.add_argument('--budget-mb',type=int,default=2048,help='Session upload payload budget in MiB')
    ap.add_argument('--sites', help='Optional CSV/JSON/GeoJSON mast candidates')
    ap.add_argument('--probe-every', type=float, default=1, help='Pause in seconds after each upload batch')
    ap.add_argument('--upload-url', default=os.environ.get('ATLAS_UPLOAD_URL', 'https://speed.cloudflare.com/__up'))
    ap.add_argument('--upload-method', choices=['POST', 'PUT'], default='POST')
    ap.add_argument('--tls-cert')
    ap.add_argument('--tls-key')
    ap.add_argument('--https-port', type=int, help='Additional HTTPS listener sharing the same recording')
    ap.add_argument('--phone-profile', help='Public iPhone certificate profile to serve')
    ap.add_argument('--ssh-known-hosts', help='Known-hosts file for the Mudi SSH connection')
    config = ap.parse_args()
    if config.radio_interval < .25 or config.probe_every < 1 or config.budget_mb < 1:
        ap.error('Minimum radio interval: 0.25 s; minimum probe period: 1 s; budget: 1 MiB')
    if config.upload_url and urlparse(config.upload_url).scheme != 'https':
        ap.error('Upload URL must use HTTPS')
    if bool(config.tls_cert) != bool(config.tls_key):
        ap.error('Supply both --tls-cert and --tls-key')
    if config.https_port and not config.tls_cert:
        ap.error('--https-port requires a certificate and key')
    if config.ssh_known_hosts:
        mudi_survey.SSH[1:1] = ['-o', 'UserKnownHostsFile='+config.ssh_known_hosts]
    survey = Survey(':memory:' if config.demo else config.db)
    survey.sites = load_sites(config.sites)
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
    jobs.append((localization_worker, (survey, stop)))
    threads = []
    for fn, params in jobs:
        thread = threading.Thread(target=fn, args=params, daemon=True)
        thread.start()
        threads.append(thread)
    print(f'Upload Atlas: {"https" if config.tls_cert and not config.https_port else "http"}://localhost:{config.port} — {"SIMULATION" if config.demo else config.db}', flush=True)
    if https_server:
        print(f'Phone HTTPS: port {config.https_port}', flush=True)
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
