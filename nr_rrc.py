"""Narrow NR RRC context decoding, using 3GPP TS38.331 ASN.1 via pycrate.

Qualcomm B821 v26 PDU mapping matches SCAT diagnrlogparser.py:3 DL-CCCH,
4 DL-DCCH,11 standalone RRCReconfiguration. No segmented packets or inferred
missing cell context. Timer configuration is not proof of a running timer.
"""
import copy
import struct
import threading

_lock = threading.RLock()  # pycrate definitions are mutable singleton objects
SCS = {'kHz15': 0, 'kHz30': 1, 'kHz60': 2, 'kHz120': 3, 'kHz240': 4}


def walk(value, name):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name:
                yield child
            yield from walk(child, name)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from walk(child, name)


def configuration(group):
    """Extract only a self-contained synchronized SpCell, never another SCell.

mu is explicitly the initial UL BWP numerology, not proof of the currently
active BWP. active_mu is provided only for an explicitly selected BWP present
in this same complete message; callers must not use mu for later adjustments.
    """
    try:
        sp = group['spCellConfig']
        sync = sp['reconfigurationWithSync']
        common = sync['spCellConfigCommon']
        ul = common['uplinkConfigCommon']
        dl_frequency = common['downlinkConfigCommon']['frequencyInfoDL']
        bands = dl_frequency['frequencyBandList']
        if len(bands) != 1:
            return None
        band = bands[0]
        if isinstance(band, dict):
            band = band['freqBandIndicatorNR']
        initial = ul['initialUplinkBWP']['genericParameters']
        mu = SCS[initial['subcarrierSpacing']]
        result = dict(crnti=sync['newUE-Identity'], pci=common['physCellId'],
                      channel=dl_frequency['absoluteFrequencySSB'], band='n'+str(band),
                      mu=mu, mu_scope='initial_uplink_bwp', cell_group_id=group['cellGroupId'],
                      tag_id=None, alignment_timer=None, active_mu=None,
                      active_uplink_bwp=None, uplink_bwp_mu={0: mu})
        if not (0 < result['crnti'] <= 65535 and 0 <= result['pci'] <= 1007
                and 0 < result['channel'] < 3279166):
            return None
        dedicated = sp.get('spCellConfigDedicated', {})
        tag = dedicated.get('tag-Id')
        if type(tag) is int and 0 <= tag <= 3:
            result['tag_id'] = tag
            tags = group.get('mac-CellGroupConfig', {}).get('tag-Config', {}).get('tag-ToAddModList', [])
            matches = [t['timeAlignmentTimer'] for t in tags if t.get('tag-Id') == tag]
            if len(matches) == 1:
                result['alignment_timer'] = matches[0]
        uplink = dedicated.get('uplinkConfig', {})
        for bwp in uplink.get('uplinkBWP-ToAddModList', []):
            spacing = bwp.get('bwp-Common', {}).get('genericParameters', {}).get('subcarrierSpacing')
            if spacing in SCS:
                result['uplink_bwp_mu'][bwp['bwp-Id']] = SCS[spacing]
        active = uplink.get('firstActiveUplinkBWP-Id')
        if active in result['uplink_bwp_mu']:
            result['active_uplink_bwp'] = active
            result['active_mu'] = result['uplink_bwp_mu'][active]
        return result
    except (KeyError, TypeError, ValueError):
        return None


def decode(body, ts):
    """Return self-contained NR configs, with raw RRC evidence; fail closed."""
    if len(body) < 35 or struct.unpack_from('<I', body)[0] != 26:
        return []
    header = struct.unpack('<BBBH Q I3sBIHBBBB', body[4:35])
    pdu = header[7]
    if header[-1] or pdu not in (3, 4, 11) or len(body[35:]) != header[9]:
        return []
    from pycrate_asn1dir import RRCNR
    defs = RRCNR.NR_RRC_Definitions
    codec = {3: defs.DL_CCCH_Message, 4: defs.DL_DCCH_Message, 11: defs.RRCReconfiguration}[pdu]
    with _lock:
        try:
            codec.from_uper(body[35:])
            outer = copy.deepcopy(codec.get_val())
            groups = []
            for key in ('masterCellGroup', 'secondaryCellGroup'):
                for value in walk(outer, key):
                    # pycrate may automatically decode CONTAINING into a tuple.
                    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[1], dict):
                        group = value[1]
                    elif isinstance(value, bytes):
                        defs.CellGroupConfig.from_uper(value)
                        group = copy.deepcopy(defs.CellGroupConfig.get_val())
                    else:
                        continue
                    result = configuration(group)
                    if result is not None:
                        result.update(ts=ts, rrc_payload_hex=body.hex(), rrc_pdu=pdu,
                                      group_scope=key, experimental=True, validated=False)
                        groups.append(result)
            return groups
        except Exception:
            # Unknown ASN.1 variant/malformed wire data cannot authorize a range.
            return []
