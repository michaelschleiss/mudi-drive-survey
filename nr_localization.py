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
            if p and p[0] == 0x98:
                p = p[8:]
            if len(p) >= 16 and p[0] == 0x10:
                yield p


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
        from pycrate_asn1dir import RRCNR
        self.defs = RRCNR.NR_RRC_Definitions
        self.defs._RRCReconfiguration_IEs_secondaryCellGroup._const_cont = None
        self.configs = []
        self.seen = set()

    def decode(self, data, logs=None):
        logs = sorted(packets(data) if logs is None else logs, key=lambda p: struct.unpack_from('<Q', p, 8)[0])
        result = []
        for p in logs:
            code = struct.unpack_from('<H', p, 6)[0]
            raw = struct.unpack_from('<Q', p, 8)[0]
            ts, body = timestamp(raw), p[16:]
            if code == 0xb821 and len(body) >= 35 and struct.unpack_from('<I', body)[0] == 26:
                h = struct.unpack('<BBBH Q I3sBIHBBBB', body[4:35])
                if h[-1] or h[7] != 11 or len(body[35:]) != h[9]:
                    continue
                try:
                    self.defs.RRCReconfiguration.from_uper(body[35:])
                    outer = self.defs.RRCReconfiguration.get_val()
                    for octets in find(outer, 'secondaryCellGroup'):
                        self.defs.CellGroupConfig.from_uper(octets)
                        for sync in find(self.defs.CellGroupConfig.get_val(), 'reconfigurationWithSync'):
                            common = sync['spCellConfigCommon']
                            scs = common['uplinkConfigCommon']['initialUplinkBWP']['genericParameters']['subcarrierSpacing']
                            mu = {'kHz15': 0, 'kHz30': 1, 'kHz60': 2, 'kHz120': 3}.get(scs)
                            if mu is None:
                                continue
                            self.configs.append(dict(ts=ts, crnti=sync['newUE-Identity'], pci=common['physCellId'],
                                channel=next(find(common, 'absoluteFrequencySSB')),
                                band='n'+str(next(find(common['downlinkConfigCommon'], 'frequencyBandList'))[0]),
                                mu=mu, rrc_payload_hex=body.hex()))
                except (ValueError, KeyError, StopIteration, TypeError):
                    continue
            elif code == 0xb88a:
                c = candidate(body)
                if c is None or raw in self.seen:
                    continue
                self.seen.add(raw)
                matches = [v for v in self.configs if v['crnti'] == c['crnti'] and 0 <= ts-v['ts'] <= 30]
                identities = {(v['pci'], v['channel'], v['band'], v['mu']) for v in matches}
                row = dict(c, ts=ts, modem_timestamp_raw=raw, experimental=True, validated=False,
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
                if c is not None and ('adjustment', raw) not in self.seen:
                    self.seen.add(('adjustment', raw))
                    result.append(dict(c, ts=ts, modem_timestamp_raw=raw,
                                       event_kind='nr_ta_adjustment'))
            self.configs = [v for v in self.configs if ts-v['ts'] <= 120][-100:]
        if len(self.seen) > 4096:
            # Retain recent modem timestamps for both record types.
            self.seen = set(sorted(self.seen, key=lambda x:x[1] if isinstance(x, tuple) else x)[-2048:])
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


def worker(survey, stop, ssh):
    while not stop.is_set():
        if not survey.ta_enabled:
            survey.ta_status = 'Experimental capture off'
            stop.wait(.25)
            continue
        process = None
        token = uuid.uuid4().hex
        remote = '/tmp/atlas-stream.'+token
        try:
            # Never carry associations or relative state across a transport restart.
            decoder, frames = Decoder(), StreamFrames()
            pending = []
            total_bytes = total_candidates = total_adjustments = 0
            survey.ta_capture = None
            survey.ta_status = 'Starting continuous diagnostic stream…'
            subprocess.run(ssh + ['mkdir '+shlex.quote(remote)], capture_output=True, timeout=8, check=True)
            for local, dest in [('nr-rach.cfg', 'mask.cfg'), ('nr-clean.cfg', 'clean.cfg'),
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
                                    row['delivery_ms'] = round((now-row['ts'])*1000, 1)
                                    if row.pop('event_kind', None) == 'nr_ta_adjustment':
                                        total_adjustments += 1
                                        # Relative-only evidence. No guessed absolute baseline/cell.
                                        survey.record('nr_ta_adjustment', row)
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
                        waiting = []
                        for row in pending:
                            located = locate(row, fixes, started, row['received_ts'])
                            # New events often arrive before the following GPS fix.
                            if located['lat'] is None and time.time()-row['received_ts'] < 3:
                                waiting.append(row)
                            else:
                                survey.record('nr_range', located)
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
                subprocess.run(ssh + ['rmdir '+shlex.quote(remote)+' 2>/dev/null'],
                               capture_output=True, timeout=6)
