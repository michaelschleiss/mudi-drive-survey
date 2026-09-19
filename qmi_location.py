"""Subscription-bound QMI LTE timing snapshots, not fresh event-time ranges.

Schema: linux-mobile-broadband/libqmi data/qmi-service-nas.json, message0x43.
Units: Qualcomm-generated network_access_service_v01.h timing_advance field
(microseconds), also upstream src/qmicli/qmicli-nas.c. Physical calibration and
the age of the modem's cached timing value remain unknown.
"""
from pathlib import Path
import re
import shlex
import struct
import subprocess
import time
import uuid


def tlvs(payload):
    result = {}
    pos = 0
    while pos < len(payload):
        if pos + 3 > len(payload):
            raise ValueError('Truncated QMI TLV header')
        kind, size = struct.unpack_from('<BH', payload, pos)
        pos += 3
        if pos + size > len(payload) or kind in result:
            raise ValueError('Truncated or duplicate QMI TLV')
        result[kind] = payload[pos:pos + size]
        pos += size
    return result


def successful(payload):
    items = tlvs(payload)
    if len(items.get(2, b'')) != 4:
        raise ValueError('Missing QMI result')
    result, error = struct.unpack('<HH', items[2])
    if result or error:
        raise ValueError(f'QMI request failed: result={result}, error={error}')
    return items


def parse_location(payload, subscription, started, ended):
    if type(subscription) is not int or subscription not in (1, 2):
        raise ValueError('Subscription must be 1 or 2')
    items = successful(payload)
    row = dict(subscription=subscription, rat='lte', source='qmi_nas_cell_location',
               capture_started_ts=started, capture_ended_ts=ended,
               received_ts=ended, measurement_ts=None, measurement_age_s=None,
               freshness='unknown', geographic_calibration='unvalidated',
               experimental=True, validated=False, units_validated=True,
               timing_advance_us=None, range_m=None, payload_hex=payload.hex())
    info = items.get(0x13)
    if info is None:
        row['reason'] = 'LTE cell identity unavailable'
        return row
    if len(info) < 19 or info[0] not in (0, 1):
        raise ValueError('Malformed LTE cell identity')
    # Variable cell array: count follows four reselection parameters.
    if len(info) != 19 + info[18] * 10:
        raise ValueError('Malformed LTE cell measurements')
    a, b, c = info[1:4]
    mcc_digits = (a & 15, a >> 4, b & 15)
    mnc_digits = (c & 15, c >> 4) + (() if b >> 4 == 15 else (b >> 4,))
    if any(d > 9 for d in mcc_digits + mnc_digits):
        raise ValueError('Invalid PLMN digits')
    plmn = ''.join(map(str, mcc_digits)) + '-' + ''.join(map(str, mnc_digits))
    tac, cell, channel, pci = struct.unpack_from('<HIHH', info, 4)
    if cell == 0xffffffff or pci > 503 or channel == 0xffff:
        row['reason'] = 'Invalid LTE cell identity'
        return row
    row.update(plmn=plmn, tac=f'{tac:X}', cell_id=f'{cell:X}', pci=pci,
               channel=channel, cell_key=f'{plmn}:LTE:{cell:X}',
               qmi_ue_in_idle=bool(info[0]))
    timing = items.get(0x1e)
    if timing is None:
        row['reason'] = 'LTE timing advance absent'
    elif len(timing) != 4:
        raise ValueError('Malformed LTE timing advance')
    else:
        value = struct.unpack('<i', timing)[0]
        if value < 0:
            row['reason'] = 'LTE timing advance unavailable'
        else:
            row.update(timing_advance_us=value, range_m=value * 299792458 / 2000000,
                       range_kind='nominal_propagation_distance',
                       reason='Timing age unknown; geographic distance uncalibrated')
    return row


def parse_output(output, subscription, started, ended):
    if not re.search(r'^init=0$', output, re.M) or not re.search(r'^release=0$', output, re.M):
        raise ValueError('QMI client lifecycle failed')
    for label in ('bind', 'cell'):
        matches = re.findall(r'^' + label + r' rc=0 data=([0-9a-f]+)$', output, re.M)
        if len(matches) != 1:
            raise ValueError(f'QMI {label} transport failed')
        payload = bytes.fromhex(matches[0])
        successful(payload)
    return parse_location(payload, subscription, started, ended)


def query(ssh, subscription, remote_binary=None):
    """One bounded query. ssh is an argv prefix including host, never shell text.

If remote_binary is supplied, caller owns deployment/cleanup. Otherwise deploy a
unique temporary executable and remove it. This never changes modem configuration.
    """
    if type(subscription) is not int or subscription not in (1, 2):
        raise ValueError('Subscription must be 1 or 2')
    temporary = remote_binary is None
    remote_binary = remote_binary or '/tmp/atlas-qmi-' + uuid.uuid4().hex
    path = shlex.quote(remote_binary)
    try:
        if temporary:
            subprocess.run(list(ssh) + [f'umask 077; cat > {path}; chmod 700 {path}'],
                           input=Path(__file__).with_name('qmi_location_aarch64').read_bytes(),
                           capture_output=True, timeout=8, check=True)
        started = time.time()
        result = subprocess.run(list(ssh) + [f'timeout 16 {path} {subscription}'],
                                capture_output=True, timeout=20, check=True, text=True)
        ended = time.time()
        return parse_output(result.stdout, subscription, started, ended)
    finally:
        if temporary:
            subprocess.run(list(ssh) + [f'rm -f {path}'], capture_output=True, timeout=6)


def associate_snapshot(row, before, after, session_id, epoch):
    """Attach known cell scope, never assign GPS to an unknown-age cached TA."""
    fields = {'subscription': 'subscription', 'plmn': 'lte_plmn',
              'cell_id': 'lte_cell_id', 'pci': 'lte_pci', 'channel': 'lte_arfcn'}
    for source, radio in fields.items():
        if row.get(source) is None or any(r.get(radio) != row[source] for r in (before, after)):
            raise ValueError('QMI cell does not match bracketing subscription-specific radio')
    if not before.get('lte_band') or before.get('lte_band') != after.get('lte_band'):
        raise ValueError('LTE band changed during timing query')
    return dict(row, session_id=session_id, association_epoch=epoch,
                association_status='confirmed', band=before['lte_band'],
                ts=row['received_ts'], lat=None, lon=None,
                position_method=None, map_eligible=False)


def worker(survey, stop, ssh):
    remote = '/tmp/atlas-qmi-' + uuid.uuid4().hex
    deployed = False
    try:
        while not stop.is_set():
            if not survey.ta_enabled:
                stop.wait(.25)
                continue
            with survey.lock:
                before = dict(survey.latest.get('radio') or {})
                epoch = survey.association_epoch
            sub = before.get('subscription')
            if sub not in (1, 2) or not before.get('lte_band') or time.time()-before.get('ts', 0)>3:
                survey.lte_ta_status = 'Waiting for subscription-specific LTE reading'
                stop.wait(1)
                continue
            try:
                if not deployed:
                    path = shlex.quote(remote)
                    subprocess.run(list(ssh)+[f'umask 077; cat > {path}; chmod 700 {path}'],
                        input=Path(__file__).with_name('qmi_location_aarch64').read_bytes(),
                        capture_output=True, timeout=8, check=True)
                    deployed = True
                row = query(ssh, sub, remote_binary=remote)
                # Wait for the radio worker to complete the next independent sample.
                deadline = time.monotonic()+3
                while not stop.is_set() and time.monotonic()<deadline:
                    with survey.lock:
                        after = dict(survey.latest.get('radio') or {})
                    if after.get('ts', 0) >= row['received_ts']:
                        break
                    stop.wait(.1)
                with survey.lock:
                    if (epoch != survey.association_epoch or after.get('ts', 0)<row['received_ts']
                            or row['capture_started_ts']-before['ts']>3):
                        raise ValueError('Radio continuity unavailable during QMI query')
                    scoped = associate_snapshot(row, before, after, survey.session_id, epoch)
                    survey.record('lte_timing_snapshot', scoped)
                    survey.lte_ta_status = scoped['reason']
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                survey.lte_ta_status = 'LTE timing query unavailable: '+type(exc).__name__
            stop.wait(1)
    finally:
        if deployed:
            try:
                subprocess.run(list(ssh)+['rm -f '+shlex.quote(remote)],capture_output=True,timeout=6)
            except (OSError, subprocess.SubprocessError):
                pass
