"""Conservative experimental timing baselines; deliberately no distance conversion.

Sequence numbers must represent the complete relevant modem timing-event sequence,
not a counter assigned after filtering packets. Unknown continuity must be passed as
None. Call invalidate when the owning connection/configuration changes.
"""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class TimingIdentity:
    session: str
    epoch: str
    subscription: str
    rat: str
    cell: str
    carrier: str
    tag: int
    numerology: int

    def __post_init__(self):
        if any(not isinstance(v, str) or not v.strip() for v in
               (self.session, self.epoch, self.subscription, self.cell, self.carrier)):
            raise ValueError('Complete timing identity required')
        if self.rat not in ('LTE', 'NR'):
            raise ValueError('Unsupported RAT')
        if type(self.tag) is not int or not 0 <= self.tag <= 3:
            raise ValueError('Invalid timing advance group')
        if type(self.numerology) is not int or not 0 <= self.numerology <= 4:
            raise ValueError('Invalid numerology')
        if self.rat == 'LTE' and self.numerology != 0:
            raise ValueError('LTE requires numerology zero')


class TimingState:
    """One cell/carrier/TAG in one uninterrupted connection epoch.

Units are the caller-verified TA command steps for this identity. Initial absolute
index and subsequent delta steps must use identical units. No accuracy or physical
distance is inferred. Expiry must come from alignment validity, not guesswork.
    """
    def __init__(self, identity):
        if not isinstance(identity, TimingIdentity):
            raise TypeError('TimingIdentity required')
        self.identity = identity
        self.index = self.sequence = self.event_ts = self.expires_at = None
        self.reason = 'No absolute baseline'

    def invalidate(self, reason='Timing continuity lost'):
        self.index = self.sequence = self.event_ts = self.expires_at = None
        self.reason = reason
        return False

    @staticmethod
    def _time(value):
        return type(value) in (int, float) and math.isfinite(value)

    @staticmethod
    def _sequence(value):
        return type(value) is int and value >= 0

    def initialize(self, identity, index, sequence, event_ts, expires_at):
        if identity != self.identity:
            return self.invalidate('Identity changed')
        if type(index) is not int or index < 0:
            return self.invalidate('Invalid absolute timing index')
        if not self._sequence(sequence):
            return self.invalidate('Unknown timing sequence')
        if not self._time(event_ts) or not self._time(expires_at) or expires_at <= event_ts:
            return self.invalidate('Unknown or expired alignment validity')
        if self.event_ts is not None and event_ts <= self.event_ts:
            return self.invalidate('Out-of-order absolute timing event')
        self.index, self.sequence = index, sequence
        self.event_ts, self.expires_at = event_ts, expires_at
        self.reason = 'Experimental absolute baseline'
        return True

    def adjust(self, identity, delta_steps, sequence, event_ts, expires_at=None):
        if identity != self.identity:
            return self.invalidate('Identity changed')
        if self.index is None:
            return False
        if not self._sequence(sequence) or sequence != self.sequence + 1:
            return self.invalidate('Timing sequence gap or replay')
        if not self._time(event_ts) or event_ts <= self.event_ts:
            return self.invalidate('Out-of-order timing event')
        if event_ts >= self.expires_at:
            return self.invalidate('Alignment expired')
        if type(delta_steps) is not int or self.index + delta_steps < 0:
            return self.invalidate('Invalid timing correction')
        if expires_at is not None:
            if not self._time(expires_at) or expires_at <= event_ts:
                return self.invalidate('Invalid alignment expiry')
            self.expires_at = expires_at
        self.index += delta_steps
        self.sequence, self.event_ts = sequence, event_ts
        self.reason = 'Experimental continuous timing baseline'
        return True

    def snapshot(self, now):
        if not self._time(now):
            self.invalidate('Unknown observation time')
        elif self.index is not None and now >= self.expires_at:
            self.invalidate('Alignment expired')
        elif self.event_ts is not None and now < self.event_ts:
            self.invalidate('Observation clock precedes event')
        return dict(identity=asdict(self.identity), timing_index=self.index,
                    event_ts=self.event_ts, expires_at=self.expires_at,
                    sequence=self.sequence, reason=self.reason,
                    experimental=True, validated=False, range_m=None)
