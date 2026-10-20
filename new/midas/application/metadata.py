"""Bounded application metadata shared by workflows and read-only inspection."""
from dataclasses import dataclass

from ..domain import ArtifactRef
from ..domain.encoding import load_record, record_parts
from ..storage.codec import ref, unref

META = 256 * 1024
DATA = 64 * 1024**2
SCOPE = 'application'


def pack(value, limit=META):
    return b''.join(record_parts(value, limit=limit))


def read(reader, reference, limit=META):
    return reader.read_artifact(reference, max_bytes=limit)


def value(reader, scope, key):
    saved = reader.record(scope, key)
    return load_record(read(reader, saved.artifact), limit=META) if saved and saved.artifact else None


@dataclass(frozen=True, slots=True)
class Owner:
    context: ArtifactRef
    identity: ArtifactRef
    index: int
    base: ArtifactRef | None = None
    submission: str | None = None
    memory: ArtifactRef | None = None
    stopped: bool = False
    reason: str | None = None
    evidence: tuple[ArtifactRef, ...] = ()
    stop_request: ArtifactRef | None = None

    @property
    def payload(self):
        return pack([ref(self.context), ref(self.identity), self.index, ref(self.base), self.submission,
                     ref(self.memory), self.stopped, self.reason, [ref(r) for r in self.evidence], ref(self.stop_request)])

    @classmethod
    def load(cls, data):
        v = load_record(data, limit=META)
        if type(v) is not list or len(v) != 10:
            raise ValueError('Invalid application owner')
        return cls(unref(v[0]), unref(v[1]), v[2], unref(v[3]), v[4], unref(v[5]), v[6], v[7],
                   tuple(unref(r) for r in v[8]), unref(v[9]))


def owner_state(reader, key):
    saved = reader.record('application.owners', key)
    return saved, Owner.load(read(reader, saved.artifact))


@dataclass(frozen=True, slots=True)
class Inputs:
    selection: ArtifactRef
    package: str
    workflow: str
    instructions: tuple[tuple[str, ArtifactRef], ...] = ()

    def __post_init__(self):
        if self.workflow not in ('live', 'scripted', 'supplied'):
            raise ValueError('Unknown application workflow')
        if type(self.package) is not str or not self.package or len(self.package.encode()) > 4096:
            raise ValueError('Invalid package locator')

    @property
    def payload(self):
        return pack(['midas-application-inputs-v1', ref(self.selection), self.package, self.workflow,
                     [[role, ref(reference)] for role, reference in self.instructions]])

    @classmethod
    def load(cls, payload):
        v = load_record(payload, limit=META)
        if type(v) is not list or len(v) != 5 or v[0] != 'midas-application-inputs-v1':
            raise ValueError('Invalid application inputs')
        return cls(unref(v[1]), v[2], v[3], tuple((role, unref(r)) for role, r in v[4]))
