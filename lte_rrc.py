"""Narrow LTE identity evidence, without assumed SIM/carrier associations.

Wire-layout source: SCAT DiagLteLogParser (GPL-2.0), independently implemented
field decoder; no source code copied.
https://github.com/fgsect/scat/blob/master/src/scat/parsers/qualcomm/diagltelogparser.py
OTA headers describe the message cell, not necessarily a current serving cell.
"""
import struct


def serving_cell(body):
    """B0C2 v2/v3 full serving identity; unknown formats fail closed."""
    if not body or body[0] not in (2, 3):
        return None
    fmt = '<HHHBBIH IHBHB' if body[0] == 2 else '<HIIBBIH IHBHB'
    if len(body) != 1 + struct.calcsize(fmt):
        return None
    pci, dl, ul, dlbw, ulbw, cell, tac, band, mcc, digits, mnc, access = struct.unpack(fmt, body[1:])
    if (pci > 503 or cell >= 1 << 28 or digits not in (2, 3) or
            not 1 <= mcc <= 999 or mnc >= 10 ** digits or dl > 262143 or
            not 1 <= band <= 88):
        return None
    plmn = f'{mcc:03d}-{mnc:0{digits}d}'
    return dict(event_kind='lte_serving_identity', source='diag_b0c2', rat='LTE',
                version=body[0], plmn=plmn, cell_id=f'{cell:X}',
                cell_key=f'{plmn}:LTE:{cell:X}', pci=pci, channel=dl,
                uplink_channel=ul, tac=f'{tac:X}', band=f'B{band}',
                dl_bandwidth_prb=dlbw, ul_bandwidth_prb=ulbw,
                subscription=None, carrier_index=None, identity_scope='serving_cell',
                payload_hex=body.hex(), experimental=True, validated=False)


def ota_header(body):
    """B0C0 header identity only. No PLMN/global cell ID in these headers."""
    if not body:
        return None
    version = body[0]
    if version in (30, 31):
        fmt, modern, segmented = '<BBBBBHIHBIHBBB', True, True
    elif version in (25, 26, 27, 29):
        fmt, modern, segmented = '<BBBBBHIHBIH', True, False
    elif version in (8, 9, 12, 13, 14, 15, 16, 19, 20, 22, 24):
        fmt, modern, segmented = '<BBBHIHBIH', False, False
    elif version in (5, 6, 7):
        fmt, modern, segmented = '<BBBHHHBIH', False, False
    elif version in (2, 3, 4):
        fmt, modern, segmented = '<BBBHHHBH', False, False
    else:
        return None
    header_len = struct.calcsize(fmt) + 1
    if len(body) < header_len:
        return None
    fields = struct.unpack(fmt, body[1:header_len])
    skip = 2 if modern else 0
    pci, channel, frame, pdu = fields[3+skip:7+skip]
    length = fields[-4] if segmented else fields[-1]
    if segmented and fields[-1] != 0:
        return None
    sfn, subframe = frame >> 4, frame & 15
    if length != len(body)-header_len or pci > 503 or channel > 262143:
        return None
    return dict(event_kind='lte_rrc_identity', source='diag_b0c0', rat='LTE',
                version=version, pci=pci, channel=channel, sfn=sfn if sfn <= 1023 else None,
                subframe=subframe if subframe <= 9 else None, frame_raw=frame,
                pdu=pdu, subscription=None, carrier_index=None,
                plmn=None, cell_id=None, identity_scope='message_cell_only',
                header_hex=body[:header_len].hex(), experimental=True, validated=False)
