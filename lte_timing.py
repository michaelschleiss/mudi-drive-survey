"""Strict LTE timing evidence parser; never integrates relative TA into ranges.

Wire-layout reference: SCAT (GPL-2.0), DiagLteLogParser MAC v49/v50 and RACH:
https://github.com/fgsect/scat/blob/master/src/scat/parsers/qualcomm/diagltelogparser.py
Independent bounds-checked field decoder; no SCAT code copied. LTE TA CE bit
meaning follows 3GPP TS36.321: 2-bit TAG and 6-bit relative TA command.
Diagnostic carrier indices are not globally unique cell identities.
"""
import struct


class Cursor:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, count):
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise ValueError('Truncated LTE timing packet')
        value = self.data[self.pos:end]
        self.pos = end
        return value

    def unpack(self, fmt):
        return struct.unpack(fmt, self.take(struct.calcsize(fmt)))

    def finish(self):
        if self.pos != len(self.data):
            raise ValueError('Unconsumed LTE timing payload')


def adjustments(body):
    """B063 v49/v50. A malformed record rejects the whole packet."""
    try:
        cur = Cursor(body)
        version, reserved, count, lcid_count, reason = cur.unpack('<B3sHBB')
        if version not in (49, 50):
            return []
        if version == 49:
            cur.take(19 * 28)
        result = []
        for record in range(count):
            size, padding, timing, carrier_harq, sdu_count, header_length = cur.unpack('<IIIBBH')
            sfn, subframe = timing & 1023, (timing >> 10) & 15
            if subframe > 9:
                raise ValueError('Invalid LTE subframe')
            for sdu in range(sdu_count):
                descriptor = int.from_bytes(cur.take(3), 'little')
                control, lcid, length = descriptor & 1, (descriptor >> 1) & 63, (descriptor >> 7) & 65535
                content = cur.take(9)
                if control:
                    if lcid == 29:
                        if length != 1:
                            raise ValueError('Invalid timing CE size')
                        command = content[0] & 63
                        result.append(dict(event_kind='lte_ta_adjustment', rat='LTE',
                            version=version, record_index=record, sdu_index=sdu,
                            sfn=sfn, subframe=subframe, carrier_index=carrier_harq & 15,
                            harq_id=carrier_harq >> 4, rnti_type=(timing >> 15) & 15,
                            tag_id=content[0] >> 6, ta_command=command,
                            delta_steps=command - 31, absolute_range_m=None,
                            requires_absolute_baseline=True, subscription=None,
                            experimental=True, validated=False, payload_hex=body.hex()))
                else:
                    groups, dynamic = struct.unpack_from('<BH', content, 5)
                    cur.take(dynamic * 4)
                    for _ in range(groups):
                        while True:
                            if not int.from_bytes(cur.take(4), 'little') & 1:
                                break
        cur.finish()
        return result
    except (ValueError, struct.error):
        return []


def initial(body):
    """B062 v1, known RACH subpacket layouts. Candidate only until live tested.

Subpacket size is treated as payload length, following cited layout. Any capture
with another size convention is rejected, never heuristically reinterpreted.
    """
    try:
        cur = Cursor(body)
        version, count, reserved = cur.unpack('<BBH')
        if version != 1:
            return []
        result = []
        for record in range(count):
            kind, revision, length = cur.unpack('<BBH')
            payload = cur.take(length)
            if kind != 6 or revision not in (2, 3, 49, 50):
                continue
            expected = {2: 32, 3: 34, 49: 48, 50: 51}[revision]
            if len(payload) != expected:
                raise ValueError('Unsupported RACH layout length')
            h = 4 if revision == 2 else 6
            attempts, success, contention, mask = payload[h-4:h]
            if success != 0 or mask & 7 != 7:
                continue
            offset = {2: 8, 3: 10, 49: 10, 50: 13}[revision]
            backoff, msg2_result, crnti, ta = struct.unpack_from('<HBHH', payload, offset)
            # Msg2 result meaning is not established independently; retain it.
            if ta > 1282 or not 0 < crnti < 65535:
                continue
            result.append(dict(event_kind='lte_ta_initial_candidate', rat='LTE',
                version=revision, record_index=record, ta_index_candidate=ta,
                crnti=crnti, rach_result=success, msg2_result_raw=msg2_result,
                subscription_index_raw=payload[0] if revision != 2 else None,
                cell_index_raw=payload[1] if revision != 2 else None,
                subscription=None, absolute_range_m=None, experimental=True,
                validated=False, payload_hex=body.hex(),
                reason='RACH layout requires live validation and cell association'))
        cur.finish()
        return result
    except (ValueError, struct.error):
        return []
