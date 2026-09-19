"""Opt-in experimental RG650V NR RACH ranges. No reconnects or band locking.

B88A v3.18 offsets are hypotheses, not a vendor-validated schema. Every output
retains raw evidence and is explicitly unsuitable for confirmed mast positions.
"""
import os
import selectors
import shlex
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def _crc_table():
    table = []
    for n in range(256):
        for _ in range(8):
            n = (n >> 1) ^ 0x8408 if n & 1 else n >> 1
        table.append(n)
    return table


CRC_TABLE = _crc_table()


class DiagnosticPacket(bytes):
    def __new__(cls, data, subscription=None):
        obj = super().__new__(cls, data)
        obj.subscription = subscription
        return obj


def packets(data):
    for raw in data.split(b'\x7e')[:-1]:
        out = bytearray()
        escape = False
        for b in raw:
            if escape:
                out.append(b ^ 32)
                escape = False
            elif b == 125:
                escape = True
            else:
                out.append(b)
        crc = 65535
        for b in out:
            crc = (crc >> 8) ^ CRC_TABLE[(crc ^ b) & 255]
        if crc == 0xf0b8 and not escape:
            p = bytes(out[:-2])
            subscription = None
            if p and p[0] == 0x98:
                if len(p)<8 or p[1:4] != b'\x01\x00\x00':
                    continue
                value = struct.unpack_from('<I', p, 4)[0]
                subscription = value if value in (1, 2) else None
                p = p[8:]
            if len(p) >= 16 and p[0] == 0x10:
                yield DiagnosticPacket(p, subscription)


class StreamFrames:
    """Preserve transport and HDLC fragments across arbitrary pipe reads."""
    def __init__(self):
        self.transport = bytearray()
        self.diag = b''

    def feed(self, data):
        self.transport.extend(data)
        batches = []
        while len(self.transport) >= 9:
            header = self.transport[:9]
            if header[8] != 10 or any(c not in b'0123456789abcdef' for c in header[:8]):
                raise ValueError('Malformed diagnostic stream framing')
            length = int(header[:8], 16)
            if length > 65536:
                raise ValueError('Oversized diagnostic stream frame')
            if len(self.transport) < 9+length:
                break
            payload = bytes(self.transport[9:9+length])
            del self.transport[:9+length]
            combined = self.diag + payload
            split = combined.rfind(b'\x7e')
            if split >= 0:
                batches.append((combined[:split+1], length))
                self.diag = combined[split+1:]
            else:
                batches.append((b'', length))
                self.diag = combined
            if len(self.diag) > 1024*1024:
                raise ValueError('Unterminated diagnostic packet')
        return batches


def timestamp(raw):
    # Qualcomm timestamp: upper ticks at 800 Hz, lower fractional chips.
    return 315964800 + (raw >> 16) / 800 + (raw & 65535) / 40960000


def find(value, key):
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                yield v
            yield from find(v, key)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from find(v, key)


def candidate(body):
    if len(body) != 232 or struct.unpack_from('<HH', body) != (18, 3):
        return None
    if body[19] & 10 != 10 or not any(body[164:192]):
        return None
    index = struct.unpack_from('<H', body, 176)[0]
    if index > 3846:
        return None
    return {'ta_index_candidate': index,
            'crnti': struct.unpack_from('<H', body, 228)[0], 'payload_hex': body.hex()}


def candidate_adjustment(body):
    """Narrow B886 v3.8 hypothesis, cross-checked with 12 AT readings.

    Only the observed one-record/one-carrier, TA-only 24-byte layout is accepted.
    Do not generalize offsets to longer MAC CE reports or associate this carrier
    index with the current cell without RRC state. No absolute range is returned.
    """
    if len(body) != 24 or struct.unpack_from('<HH', body) != (8, 3):
        return None
    if body[7] != 1 or body[12] != 1 or body[18:20] != b'\x00\x20':
        return None
    if body[20] > 3 or body[21] > 63 or body[22:24] != b'\x00\x00':
        return None
    return {'status': 'experimental_relative_correction', 'validated': False,
            'carrier_index_candidate': body[16] & 15,
            'tag_id_candidate': body[20], 'ta_command_candidate': body[21],
            'delta_steps_candidate': body[21]-31,
            'absolute_range_m': None, 'requires_absolute_baseline': True,
            'payload_hex': body.hex()}


class Decoder:
    def __init__(self):
        self.configs = []
        self.seen = set()

    def decode(self, data, logs=None):
        logs = sorted(packets(data) if logs is None else logs, key=lambda p: struct.unpack_from('<Q', p, 8)[0])
        result = []
        for p in logs:
            code = struct.unpack_from('<H', p, 6)[0]
            raw = struct.unpack_from('<Q', p, 8)[0]
            ts, body = timestamp(raw), p[16:]
            subscription = getattr(p, 'subscription', None)
            if code in (0xb062, 0xb063):
                from lte_timing import initial, adjustments
                events = initial(body) if code == 0xb062 else adjustments(body)
                if code == 0xb062 and not events:
                    # Preserve a real RACH event even if the provisional decoder
                    # rejects its firmware layout or result. Never map this row.
                    events = [dict(event_kind='lte_ta_initial_raw', payload_hex=body.hex(),
                                   map_eligible=False, experimental=True, validated=False,
                                   reason='Initial timing packet not accepted by candidate decoder')]
                for i, event in enumerate(events):
                    key = (subscription, code, i, raw)
                    if key in self.seen:
                        continue
                    self.seen.add(key)
                    event.update(ts=ts, subscription=subscription, rat='lte', modem_timestamp_raw=raw)
                    result.append(event)
            elif code in (0xb0c0, 0xb0c2):
                from lte_rrc import serving_cell, ota_header
                event = serving_cell(body) if code == 0xb0c2 else ota_header(body)
                key = (subscription, code, raw)
                if event is not None and key not in self.seen:
                    self.seen.add(key)
                    event.update(ts=ts, subscription=subscription, rat='lte', modem_timestamp_raw=raw)
                    result.append(event)
            elif code == 0xb821:
                from nr_rrc import decode as decode_rrc
                for config in decode_rrc(body, ts):
                    config['subscription'] = subscription
                    self.configs.append(config)
                self.configs = [v for v in self.configs if 0 <= ts-v['ts'] <= 30]
            elif code == 0xb88a:
                c = candidate(body)
                if c is None or (subscription, raw) in self.seen:
                    continue
                self.seen.add((subscription, raw))
                matches = [v for v in self.configs if subscription is not None and v.get('subscription') == subscription and v['crnti'] == c['crnti'] and 0 <= ts-v['ts'] <= 30]
                identities = {(v['pci'], v['channel'], v['band'], v['mu']) for v in matches}
                row = dict(c, ts=ts, subscription=subscription, modem_timestamp_raw=raw, experimental=True, validated=False,
                           identity_quality='Local radio signature; PCI may repeat', range_m=None,
                           reason='No unambiguous recent RRC association')
                if len(identities) == 1:
                    cfg = matches[-1]
                    step = 299792458 / (2 * 1920000 * 2**cfg['mu'])
                    row.update({k: cfg[k] for k in ('pci', 'channel', 'band', 'mu', 'rrc_payload_hex')})
                    row.update(range_m=round(c['ta_index_candidate']*step, 1), step_m=step,
                               reason='Unvalidated TA offset and range hypothesis')
                result.append(row)
            elif code == 0xb886:
                c = candidate_adjustment(body)
                if c is not None and (subscription, 'adjustment', raw) not in self.seen:
                    self.seen.add((subscription, 'adjustment', raw))
                    result.append(dict(c, ts=ts, subscription=subscription, modem_timestamp_raw=raw,
                                       event_kind='nr_ta_adjustment'))
            self.configs = [v for v in self.configs if ts-v['ts'] <= 120][-100:]
        if len(self.seen) > 4096:
            # Retain recent modem timestamps for both record types.
            self.seen = set(sorted(self.seen, key=lambda x:x[-1] if isinstance(x, tuple) else x)[-2048:])
        return result


def locate(row, fixes, started, ended):
    """Match event time, never the later download/receipt position."""
    row = dict(row, lat=None, lon=None)
    ts = row['ts']
    if not started-2 <= ts <= ended+2:
        row['reason'] = 'Diagnostic clock outside capture window; no GPS assignment'
        return row
    valid = sorted((p for p in fixes if p['acc_m'] <= 30), key=lambda p:p['ts'])
    before = next((p for p in reversed(valid) if p['ts'] <= ts), None)
    after = next((p for p in valid if p['ts'] >= ts), None)
    if before is None or after is None or after['ts']-before['ts'] > 3:
        row['reason'] += '; no GPS fixes bracketing event'
        return row
    fraction = (ts-before['ts'])/(after['ts']-before['ts']) if after['ts'] != before['ts'] else 0
    row.update(lat=before['lat']+(after['lat']-before['lat'])*fraction,
               lon=before['lon']+(after['lon']-before['lon'])*fraction,
               acc_m=max(before['acc_m'], after['acc_m']), position_method='event-time GPS interpolation')
    return row


def associate_range(row, radio_events, session_id, epoch):
    """Confirm a local cell signature against event-time subscription-specific polls.

    A local signature is only scoped to this session/epoch; it is not a global ID.
    Modem packet subscription is required and never inferred from the selected SIM.
    """
    row = dict(row, map_eligible=False)
    ts = row['ts']
    radio = sorted(radio_events, key=lambda r:r['ts'])
    before = next((r for r in reversed(radio) if r['ts']<=ts), None)
    after = next((r for r in radio if r['ts']>=ts), None)
    if not before or not after or after['ts']-before['ts']>3:
        row['reason'] += '; no radio observations bracket event'
        return row
    prefix = row.get('rat', 'nr')
    for r in (before, after):
        if (row.get('subscription') not in (1, 2) or r.get('subscription') != row['subscription'] or
                r.get('session_id') != session_id or r.get('association_epoch') != epoch or
                any(r.get(prefix+'_'+field)!=row.get(key) or row.get(key) is None
                    for field,key in [('band','band'),('pci','pci'),('arfcn','channel')])):
            row['reason'] += '; subscription or cell continuity unconfirmed'
            return row
    plmn = before.get(prefix+'_plmn')
    full_id = before.get(prefix+'_cell_id')
    if not plmn or plmn != after.get(prefix+'_plmn') or full_id != after.get(prefix+'_cell_id'):
        row['reason'] += '; operator or cell identity changed'
        return row
    row.update(plmn=plmn, cell_id=full_id, association_status='confirmed',
               session_id=session_id, association_epoch=epoch,
               identity_quality='Cell ID' if full_id else 'Session-scoped radio signature',
               map_eligible=row.get('lat') is not None and row.get('range_m') is not None)
    if row.get('step_m') is not None and row.get('acc_m') is not None:
        row['quantization_m'] = row['step_m']
        row['uncertainty_m'] = row['step_m'] + row['acc_m'] + 100
        row['uncertainty_source'] = 'Full timing step + reported GPS accuracy + assumed 100 m model allowance; uncalibrated'
    return row


def candidate_areas(rows, session_id, association_epoch):
    """Never combine ambiguous or different timing identities into a location."""
    from mast_estimator import estimate_candidate_area
    keys = ('session_id', 'association_epoch', 'subscription', 'rat', 'plmn',
            'band', 'pci', 'channel')
    groups = {}
    for row in rows:
        if (row.get('session_id') != session_id or
                row.get('association_epoch') != association_epoch or
                row.get('association_status') != 'confirmed' or
                any(row.get(k) is None for k in keys) or
                not row.get('map_eligible') or any(row.get(k) is None for k in ('lat', 'lon', 'range_m'))):
            continue
        groups.setdefault(tuple(row[k] for k in keys), []).append(row)
    result = []
    for key, observations in groups.items():
        area = estimate_candidate_area(observations)
        area.update(dict(zip(keys, key)), cell_id=observations[-1].get('cell_id'), experimental=True,
                    cell_key='|'.join(map(str, key)))
        result.append(area)
    return result


def worker(survey, stop, ssh):
    while not stop.is_set():
        if not survey.ta_enabled:
            survey.ta_status = 'Experimental capture off'
            stop.wait(.25)
            continue
        process = None
        token = uuid.uuid4().hex
        survey.association_epoch = token
        survey.range_areas = []
        remote = '/tmp/atlas-stream.'+token
        try:
            # Never carry associations or relative state across a transport restart.
            decoder, frames = Decoder(), StreamFrames()
            current_epoch = survey.association_epoch
            pending = []
            total_bytes = total_candidates = total_adjustments = 0
            survey.ta_capture = None
            survey.ta_status = 'Starting continuous diagnostic stream…'
            subprocess.run(ssh + ['mkdir '+shlex.quote(remote)], capture_output=True, timeout=8, check=True)
            for local, dest in [('timing.cfg', 'mask.cfg'), ('nr-clean.cfg', 'clean.cfg'),
                                ('nr_stream_aarch64', 'nr_stream')]:
                subprocess.run(ssh + ['cat > '+shlex.quote(remote+'/'+dest)],
                    input=Path(__file__).with_name(local).read_bytes(), capture_output=True, timeout=8, check=True)
            started = time.time()
            command = """d=REMOTE
logger=''
cleanup() {
 trap - EXIT HUP INT TERM
 if [ -n "$logger" ]; then
  kill -INT "$logger" 2>/dev/null
  (sleep 5; kill -KILL "$logger" 2>/dev/null) & killer=$!
  wait "$logger" 2>/dev/null
  kill "$killer" 2>/dev/null
 fi
 rm -rf "$d"
}
trap cleanup EXIT HUP INT TERM
if pidof diag_mdlog nr_stream >/dev/null; then exit 73; fi
chmod 700 "$d/nr_stream"
"$d/nr_stream" "$d/mask.cfg" "$d/clean.cfg" "$d/stop" &
logger=$!
wait "$logger"
""".replace('REMOTE', shlex.quote(remote))
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen(ssh + [command], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=errors, bufsize=0)
                last_heartbeat = time.monotonic()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while survey.ta_enabled and not stop.is_set():
                        if current_epoch != survey.association_epoch:
                            # Never retain RRC/timing associations across a serving-state change.
                            decoder = Decoder()
                            pending = []
                            current_epoch = survey.association_epoch
                        ready = selector.select(.25)
                        if ready:
                            block = os.read(process.stdout.fileno(), 65536)
                            if not block:
                                raise RuntimeError('Diagnostic stream closed')
                            for data, size in frames.feed(block):
                                now = time.time()
                                last_heartbeat = time.monotonic()
                                total_bytes += size
                                decoded_packets = list(packets(data)) if data else []
                                rows = decoder.decode(b'', decoded_packets) if decoded_packets else []
                                for row in rows:
                                    row['received_ts'] = now
                                    row.update(session_id=survey.session_id, association_epoch=current_epoch, rat=row.get('rat', 'nr'),
                                               association_status='subscription and PLMN not yet decoded')
                                    row['delivery_ms'] = round((now-row['ts'])*1000, 1)
                                    event_kind = row.pop('event_kind', None)
                                    if event_kind in ('nr_ta_adjustment', 'lte_ta_adjustment', 'lte_ta_initial_candidate', 'lte_ta_initial_raw',
                                                      'lte_serving_identity', 'lte_rrc_identity'):
                                        if event_kind.endswith('_adjustment'):
                                            total_adjustments += 1
                                        elif event_kind == 'lte_ta_initial_candidate':
                                            total_candidates += 1
                                        # Preserve evidence without inventing a range association.
                                        survey.record(event_kind, row)
                                    else:
                                        total_candidates += 1
                                        pending.append(row)
                                capture = dict(survey.ta_capture or {})
                                capture.update(ts=now, bytes=total_bytes, candidates=total_candidates,
                                               adjustments=total_adjustments, pipeline='direct-callback', heartbeat_interval_s=.25)
                                if data:
                                    capture['data_ts'] = now
                                    if decoded_packets:
                                        latest_ts = max(timestamp(struct.unpack_from('<Q', p, 8)[0]) for p in decoded_packets)
                                        capture['last_record_ts'] = latest_ts
                                        capture['last_record_delivery_ms'] = round((now-latest_ts)*1000, 1)
                                survey.ta_capture = capture
                                survey.ta_status = 'Live timing stream · direct modem feed · experimental'
                        with survey.lock:
                            fixes = list(survey.fixes)
                            radio_events = [e for e in survey.events if e['kind']=='radio']
                        waiting = []
                        for row in pending:
                            located = locate(row, fixes, started, row['received_ts'])
                            located = associate_range(located, radio_events, survey.session_id, current_epoch)
                            # New events often arrive before the following GPS fix.
                            if not located['map_eligible'] and time.time()-row['received_ts'] < 3:
                                waiting.append(row)
                            else:
                                survey.record('nr_range', located)
                                survey.range_areas = candidate_areas(list(survey.ranges), survey.session_id, current_epoch)
                        pending = waiting
                        if time.monotonic()-last_heartbeat > 8:
                            raise RuntimeError('Diagnostic stream heartbeat lost')
        except Exception as e:
            survey.ta_status = f'Experimental stream unavailable: {type(e).__name__}: {str(e)[:140]}'
            stop.wait(2)
        finally:
            if process is not None:
                # Ask the reader to exit so its shell can clean the native logger mask.
                try:
                    subprocess.run(ssh + ['touch '+shlex.quote(remote+'/stop')],
                                   capture_output=True, timeout=6)
                    process.wait(timeout=8)
                except (subprocess.TimeoutExpired, OSError):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                if process.stdout:
                    process.stdout.close()
            else:
                try:
                    subprocess.run(ssh + ['rmdir '+shlex.quote(remote)+' 2>/dev/null'],
                                   capture_output=True, timeout=6)
                except (OSError, subprocess.SubprocessError):
                    pass
