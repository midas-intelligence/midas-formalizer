"""Immutable semantic memory; model lessons and admitted observations stay distinct."""
from dataclasses import dataclass

from ..domain import ArtifactRef
from ..domain.encoding import fields, load_record, record_parts
from ..domain.records import instance, items, natural

MAX_BYTES = 64 * 1024


def text(value, maximum=8000, *, empty=False):
    if (type(value) is not str or len(value) > maximum or len(value.encode()) > maximum
            or not empty and not value.strip()):
        raise ValueError('Invalid or oversized strategy text')
    return value


def pack(value, limit=MAX_BYTES):
    return b''.join(record_parts(value, limit=limit))


@dataclass(frozen=True, slots=True)
class Lesson:
    id: str
    text: str

    def __post_init__(self):
        text(self.id, 256)
        text(self.text)


@dataclass(frozen=True, slots=True)
class Update:
    strategy: str
    upsert: tuple[Lesson, ...] = ()
    forget: tuple[str, ...] = ()
    pin_evidence: tuple[str, ...] = ()
    unpin_evidence: tuple[str, ...] = ()

    def __post_init__(self):
        text(self.strategy, empty=True)
        items(self.upsert, Lesson)
        if len(self.upsert) > 32 or len({x.id for x in self.upsert}) != len(self.upsert):
            raise ValueError('Duplicate or excessive lesson updates')
        for values, maximum in ((self.forget, 32), (self.pin_evidence, 24), (self.unpin_evidence, 24)):
            items(values, str)
            if len(values) > maximum or len(set(values)) != len(values):
                raise ValueError('Duplicate or excessive memory keys')
            for value in values:
                text(value, 256)
        if (set(self.forget) & {x.id for x in self.upsert}
                or set(self.pin_evidence) & set(self.unpin_evidence)):
            raise ValueError('Conflicting memory update')

    @classmethod
    def from_value(cls, value):
        if value is None:
            return None
        fields(value, ('strategy', 'upsert', 'forget', 'pin_evidence', 'unpin_evidence'))
        for key in ('upsert', 'forget', 'pin_evidence', 'unpin_evidence'):
            if type(value[key]) is not list:
                raise ValueError('Expected memory update arrays')
        for row in value['upsert']:
            fields(row, ('id', 'text'))
        return cls(value['strategy'], tuple(Lesson(**row) for row in value['upsert']),
                   *(tuple(value[k]) for k in ('forget', 'pin_evidence', 'unpin_evidence')))


@dataclass(frozen=True, slots=True)
class Observation:
    """A bounded complete diagnostic with a reference to the full admitted result."""
    source: str
    status: str
    evidence: ArtifactRef
    messages: tuple[str, ...] = ()
    omitted: int = 0

    def __post_init__(self):
        text(self.source, 256)
        text(self.status, 256)
        instance(self.evidence, ArtifactRef)
        items(self.messages, str)
        natural(self.omitted, 'Omitted observations')
        if len(self.messages) > 32 or sum(len(m.encode()) for m in self.messages) > 8000:
            raise ValueError('Observation exceeds memory limit')
        for message in self.messages:
            text(message)

    @classmethod
    def select(cls, source, status, evidence, messages):
        retained, used, omitted = [], 0, 0
        for message in messages:
            text(message, 8 * 1024**2)
            size = len(message.encode())
            if len(retained) < 32 and used + size <= 8000:
                retained.append(message)
                used += size
            else:
                omitted += 1
        return cls(source, status, evidence, tuple(retained), omitted)


@dataclass(frozen=True, slots=True)
class Memory:
    version: int = 0
    strategy: str = ''
    lessons: tuple[Lesson, ...] = ()
    observations: tuple[Observation, ...] = ()
    pins: tuple[str, ...] = ()
    evicted: int = 0

    def __post_init__(self):
        natural(self.version, 'Memory version')
        natural(self.evicted, 'Evicted memory records')
        text(self.strategy, empty=True)
        items(self.lessons, Lesson)
        items(self.observations, Observation)
        items(self.pins, str)
        if (len(self.lessons) > 64 or len(self.observations) > 64 or len(self.pins) > 24
                or len({x.id for x in self.lessons}) != len(self.lessons)
                or len(set(self.pins)) != len(self.pins)):
            raise ValueError('Invalid memory collections')
        for pin in self.pins:
            text(pin, 256)
        self.payload

    def value(self):
        return ['midas-memory-v1', self.version, self.strategy,
                [[x.id, x.text] for x in self.lessons],
                [[x.source, x.status, x.evidence.to_dict(), list(x.messages), x.omitted]
                 for x in self.observations], list(self.pins), self.evicted]

    @property
    def payload(self):
        return pack(self.value())

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=MAX_BYTES)
        if type(v) is not list or len(v) != 7 or v[0] != 'midas-memory-v1':
            raise ValueError('Invalid memory record')
        if any(type(v[i]) is not list for i in (3, 4, 5)):
            raise ValueError('Invalid memory arrays')
        if (any(type(x) is not list or len(x) != 2 for x in v[3])
                or any(type(x) is not list or len(x) != 5 or type(x[3]) is not list for x in v[4])):
            raise ValueError('Invalid memory row')
        return cls(v[1], v[2], tuple(Lesson(*x) for x in v[3]),
                   tuple(Observation(x[0], x[1], ArtifactRef.from_dict(x[2]), tuple(x[3]), x[4])
                         for x in v[4]), tuple(v[5]), v[6])


def update_memory(memory, change=None, *, observation=None, allowed_pins=(), budget=16000):
    """Linear retention of whole records. Pins/strategy are never silently cut.

    The soft budget retains at least the newest lesson and two observations when
    possible; the fixed 64 KiB emergency ceiling always applies. Evictions are
    counted explicitly. Caller compares memory.version in its write transaction.
    """
    instance(memory, Memory)
    if type(budget) is not int or not 2000 <= budget <= 16000:
        raise ValueError('Memory budget must be 2000 through 16000 bytes')
    if change is None and observation is None:
        return memory
    lessons = {x.id: x for x in memory.lessons}
    strategy, pins = memory.strategy, memory.pins
    if change is not None:
        instance(change, Update)
        if not set(change.pin_evidence) <= set(allowed_pins):
            raise ValueError('Unknown or out-of-scope evidence pin')
        strategy = change.strategy
        for key in change.forget:
            lessons.pop(key, None)
        for lesson in change.upsert:
            lessons.pop(lesson.id, None)
            lessons[lesson.id] = lesson
        pins = tuple(dict.fromkeys((*[p for p in pins if p not in change.unpin_evidence],
                                   *change.pin_evidence)))
        if len(pins) > 24:
            raise ValueError('Memory pin limit exceeded')
    observations = list(memory.observations)
    if observation is not None:
        instance(observation, Observation)
        observations.append(observation)
    rows = list(lessons.values())
    # Each binary list member is independently encoded; no repeated whole-memory
    # serialization while evicting. Reserve fixed metadata/collection overhead.
    fixed = len(pack(['midas-memory-v1', memory.version + 1, strategy, [], [], list(pins), memory.evicted]))
    sizes = [len(pack([x.id, x.text])) for x in rows]
    osizes = [len(pack([x.source, x.status, x.evidence.to_dict(), list(x.messages), x.omitted]))
              for x in observations]
    size, a, b = fixed + sum(sizes) + sum(osizes), 0, 0
    while b < len(observations) - 2 and (size > budget or len(observations) - b > 64):
        size -= osizes[b]
        b += 1
    while a < len(rows) - 1 and (size > budget or len(rows) - a > 64):
        size -= sizes[a]
        a += 1
    while b < len(observations) and size > MAX_BYTES:
        size -= osizes[b]
        b += 1
    while a < len(rows) and size > MAX_BYTES:
        size -= sizes[a]
        a += 1
    return Memory(memory.version + 1, strategy, tuple(rows[a:]), tuple(observations[b:]),
                  pins, memory.evicted + a + b)
