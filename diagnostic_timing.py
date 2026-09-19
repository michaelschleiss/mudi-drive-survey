"""Conservative LTE TA decoder, adapted from the original ta_locate.py.

Consumes already attributed diagnostic records; never starts diagnostics, sends traffic,
changes bands, or reconnects a modem. Attribution must come from a validated feeder.
"""
import argparse
import json
import math
import struct
import sys
from urllib.request import Request, urlopen
from cell_evidence import identity
from ta_locate import parse_b062_rach


def rach(body):
    # Legacy layouts are accepted only when the entire subpacket is present.
    if len(body) < 8:
        return None
    pos = 4
    for _ in range(body[1]):
        if pos+4 > len(body):
            return None
        sid, version, size = struct.unpack_from('<BBH', body, pos)
        if size <= 0 or pos+4+size > len(body):
            return None
        if sid == 6 and version not in (2, 0x32):
            return None
        pos += 4+size
    return parse_b062_rach(body)


def commands(body):
    """Legacy B063 layouts, retaining the TAG bits that the old reader discarded."""
    if len(body) < 8 or body[0] not in (0x31, 0x32):
        raise ValueError('Unknown or truncated LTE MAC log layout')
    count = struct.unpack_from('<H', body, 4)[0]
    pos = 8+(19*28 if body[0] == 0x31 else 0)
    result = []
    for _ in range(count):
        if pos+16 > len(body):raise ValueError('Truncated transport block')
        _, _, _, _, nsdu, _ = struct.unpack_from('<LLLBBH',body,pos);pos += 16
        for _ in range(nsdu):
            if pos+12 > len(body):raise ValueError('Truncated MAC element')
            value = int.from_bytes(body[pos:pos+3],'little');pos += 3
            is_ce, lcid = value&1, (value>>1)&0x3f
            block = body[pos:pos+9];pos += 9
            if is_ce and lcid == 29:
                result.append((block[0]>>6, block[0]&0x3f))
            elif not is_ce:
                pages, dynamic = struct.unpack('<5xBHx',block);pos += dynamic*4
                if pos > len(body):raise ValueError('Truncated MAC payload')
                for _ in range(pages):
                    more = True
                    while more:
                        if pos+4 > len(body):raise ValueError('Truncated page')
                        more = int.from_bytes(body[pos:pos+4],'little')&1;pos += 4
    return result


class LTEAdvance:
    """One attributed timing reference; invalidate on gaps, epoch changes or errors."""
    def __init__(self):
        self.reference = None
        self.sequence = None
        self.ts = None
        self.value = None

    def feed(self, record):
        try:
            key = record['key']
            pieces = key.split(':')
            if len(pieces) != 3 or pieces[0] != 'lte' or identity({'radio':'LTE','plmn':pieces[1],'cell':pieces[2]}) != key:
                raise ValueError('Diagnostics require a canonical full LTE identity')
            if record['reference_key'] != key or not record['epoch']:
                raise ValueError('Explicit timing reference and attachment epoch required')
            group = record['tag_id']
            sequence = record['sequence']
            stamp = float(record['ts'])
            if type(group) is not int or not 0 <= group <= 3 or type(sequence) is not int or sequence < 0 or not math.isfinite(stamp):
                raise ValueError('Invalid diagnostic metadata')
            reference = (key, record['epoch'], group)
            gap = self.sequence is not None and (sequence != self.sequence+1 or stamp < self.ts or stamp-self.ts > 2)
            if reference != self.reference or gap:
                self.value = None
            self.reference, self.sequence, self.ts = reference, sequence, stamp
            body = bytes.fromhex(record['body_hex'])
            code = record['code']
            if code == 'B062':
                self.value = rach(body)
            elif code == 'B063':
                changes = [cmd for tag,cmd in commands(body) if tag == group]
                if self.value is None or not changes:return None
                value = self.value+sum(cmd-31 for cmd in changes)
                if not 0 <= value <= 1282:
                    raise ValueError('Accumulated TA outside supported LTE range')
                self.value = value
            else:
                raise ValueError('Unsupported LTE diagnostic code')
            if self.value is None:return None
            return {'key':key,'reference_key':key,'ts':stamp,'ta_index':self.value,
                    'encoding':'lte-absolute-16ts','scs_khz':15,
                    'timing_group':str(group),'source':'Attributed LTE diagnostics (experimental)'}
        except (KeyError, ValueError, TypeError, struct.error):
            self.value = None
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', default='-', help='Attributed JSONL records, or stdin')
    parser.add_argument('--url', help='Optional local recorder base URL; omitted means JSONL output only')
    args = parser.parse_args()
    stream = sys.stdin if args.input == '-' else open(args.input)
    decoder = LTEAdvance()
    try:
        for line in stream:
            if not line.strip():continue
            event = decoder.feed(json.loads(line))
            if not event:continue
            if args.url:
                req = Request(args.url.rstrip('/')+'/api/timing',data=json.dumps(event).encode(),headers={'Content-Type':'application/json'})
                with urlopen(req,timeout=3) as response:
                    result = json.load(response)
                print(json.dumps(result),flush=True)
            else:print(json.dumps(event),flush=True)
    finally:
        if stream is not sys.stdin:stream.close()


if __name__ == '__main__':main()
