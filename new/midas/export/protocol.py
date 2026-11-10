"""Flat export requests and admitted stage handles; no environment inventories."""
from dataclasses import dataclass, field
from hashlib import sha256

from ..checker import CompilerRequest
from ..checker.requests import read
from ..domain import ArtifactRef, JobKind, OwnerVersion, ProofReference, ProofState, ReceiptBinding
from ..domain import encoding as codec
from ..domain.records import identifier, natural
from ..environment.inventory import relative

MAX_BYTES = 64 * 1024**2
MAX_FILES = 10016  # 10,000 modules plus entry and bundle metadata.
KINDS = {'replay': JobKind.ASSEMBLE, 'integrate': JobKind.INTEGRATE, 'audit': JobKind.EXPORT_VERIFY}


def proof_row(proof):
    receipt = proof.receipt
    return [proof.owner.obligation_id, proof.owner.generation, proof.evidence.to_dict(),
            receipt.original_execution_id, receipt.request_ref.to_dict(), receipt.result_ref.to_dict(),
            receipt.seal_ref.to_dict()]


def proof_ref(row):
    if type(row) is not list or len(row) != 7:
        raise ValueError('Invalid selected proof reference')
    return ProofReference(OwnerVersion(*row[:2]), ReceiptBinding(row[3],
        *(ArtifactRef.from_dict(v) for v in row[4:])), ArtifactRef.from_dict(row[2]))


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    owner: OwnerVersion
    interface: ArtifactRef
    state: ProofState
    proof: ProofReference | None

    def row(self):
        return [self.owner.obligation_id, self.owner.generation, self.interface.to_dict(),
                self.state.value, proof_row(self.proof) if self.proof else None]

    @classmethod
    def read(cls, row):
        if type(row) is not list or len(row) != 5:
            raise ValueError('Invalid export node snapshot')
        owner, state = OwnerVersion(*row[:2]), ProofState(row[3])
        proof = proof_ref(row[4]) if row[4] is not None else None
        if (proof is None) != (state == ProofState.UNRESOLVED) or proof and proof.owner != owner:
            raise ValueError('Export node proof differs from its state/owner')
        return cls(owner, ArtifactRef.from_dict(row[2]), state, proof)


def file_rows(files, maximum):
    if type(files) is not list or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('Invalid export file inventory')
    result, folded, total = {}, set(), 0
    for row in files:
        if type(row) is not list or len(row) != 2:
            raise ValueError('Invalid export file')
        path, ref = row
        relative(path)
        if path.casefold() in folded:
            raise ValueError('Duplicate export path')
        folded.add(path.casefold())
        reference = ArtifactRef.from_dict(ref)
        total += reference.byte_count
        if total > maximum:
            raise ValueError('Export exceeds byte limit')
        result[path] = reference
    for path in folded:
        parts = path.split('/')
        if any('/'.join(parts[:i]) in folded for i in range(1, len(parts))):
            raise ValueError('Conflicting export paths')
    return result


@dataclass(frozen=True, slots=True)
class Request:
    payload: bytes = field(repr=False)
    sha256: str = field(init=False)
    kind: JobKind = field(init=False)
    environment_id: str = field(init=False)
    selection_id: str = field(init=False)
    profile: str = field(init=False)
    allow_sorry: bool = field(init=False)
    nodes: tuple[NodeSnapshot, ...] = field(init=False, repr=False)
    proofs: tuple[ProofReference, ...] = field(init=False, repr=False)
    integration: str | None = field(init=False)
    files: tuple[tuple[str, ArtifactRef], ...] = field(init=False, repr=False)
    inputs: tuple[ArtifactRef, ...] = field(init=False, repr=False)
    expected_versions: tuple[OwnerVersion, ...] = field(init=False, repr=False)

    def __post_init__(self):
        value = CompilerRequest(self.payload).decode()
        if value['operation'] not in KINDS or 'export' not in value:
            raise ValueError('Expected an export compiler request')
        metadata = value['export']
        codec.fields(metadata, ('format', 'profile', 'roots', 'parent', 'integration', 'files', 'maximum', 'toolchain',
                                'allow_sorry', 'nodes', *(('lean_options',) if 'lean_options' in metadata else ())))
        if 'lean_options' in metadata:
            options = metadata['lean_options']
            if type(options) is not list or len(options) != 2:
                raise ValueError('Invalid export Lean options')
            natural(options[0], 'max_heartbeat')
            natural(options[1], 'max_rec_depth', positive=True)
        if metadata['format'] != 'midas-export-v1' or metadata['profile'] not in ('source', 'library'):
            raise ValueError('Unsupported export profile')
        natural(metadata['maximum'], 'Export bytes', positive=True)
        if metadata['maximum'] > 1024**4:
            raise ValueError('Export byte limit exceeds supported profile')
        identifier(metadata['toolchain'], '')
        allow_sorry = metadata['allow_sorry']
        if type(allow_sorry) is not bool or value['packet'].get('allow_sorry', False) != allow_sorry:
            raise ValueError('Export sorry option differs from compiler audit')
        if type(metadata['nodes']) is not list or len(metadata['nodes']) > 10000:
            raise ValueError('Invalid export node snapshots')
        nodes = tuple(NodeSnapshot.read(row) for row in metadata['nodes'])
        if bool(nodes) != allow_sorry or len({n.owner.obligation_id for n in nodes}) != len(nodes):
            raise ValueError('Partial export requires distinct node snapshots')
        if type(metadata['roots']) is not list or not (0 if allow_sorry else 1) <= len(metadata['roots']) <= 10000:
            raise ValueError('Invalid export roots')
        refs = tuple(proof_ref(row) for row in metadata['roots'])
        if allow_sorry and refs != tuple(n.proof for n in nodes if n.proof is not None):
            raise ValueError('Partial export proof inputs differ from its nodes')
        if metadata['parent'] is not None:
            proof_ref(metadata['parent'])
        if metadata['integration'] is not None:
            identifier(metadata['integration'], '')
        if value['operation'] == 'replay':
            if allow_sorry or metadata['parent'] is None or metadata['files'] or metadata['integration'] is not None:
                raise ValueError('Invalid assembly bindings')
            files = {}
        else:
            files = file_rows(metadata['files'], metadata['maximum'])
            if files.get('inventory.bin') != ArtifactRef.from_dict(value['packet']['inventory']):
                raise ValueError('Export inventory differs from its audit input')
            if metadata['parent'] is not None or ('src/' + value['packet']['logical']) not in files:
                raise ValueError('Missing integrated source or unexpected parent')
            if (value['operation'] == 'audit') != (metadata['integration'] is not None):
                raise ValueError('Verification requires an exact integration')
        inputs = tuple(ArtifactRef.from_dict(r) for r in value['inputs'])
        input_set = set(inputs)
        if any(p.evidence not in input_set for p in refs):
            raise ValueError('Selected proof evidence is not an input')
        if any(n.interface not in input_set for n in nodes):
            raise ValueError('Export node interface is not an input')
        expected = tuple(dict.fromkeys((OwnerVersion(*value['owner']), *(p.owner for p in refs), *(n.owner for n in nodes),
            *(OwnerVersion(d[0], d[1]) for d in (value['candidate']['dependencies'] if value['candidate'] else [])))))
        for key, data in dict(sha256=sha256(self.payload).hexdigest(), kind=KINDS[value['operation']],
                environment_id=value['environment'], selection_id=value['selection'], profile=metadata['profile'],
                allow_sorry=allow_sorry, nodes=nodes,
                proofs=refs, integration=metadata['integration'], files=tuple(files.items()),
                inputs=inputs, expected_versions=expected).items():
            object.__setattr__(self, key, data)

    @property
    def compiler(self):
        return CompilerRequest(self.payload)

    def decode(self):
        return codec.decode(self.payload)

    def validate_for(self, frozen, reader):
        value = self.compiler.validate_for(frozen, reader)
        if not set(self.expected_versions) <= set(frozen.job.expected_versions):
            raise ValueError('Export does not fence every selected proof')
        metadata = value['export']
        if value['operation'] != 'replay':
            files = file_rows(metadata['files'], metadata['maximum'])
            if not set(files.values()) <= set(self.inputs):
                raise ValueError('Export files are not exact request inputs')
            if read(reader, files['src/' + value['packet']['logical']]).decode() != value['packet']['source']:
                raise ValueError('Audited source differs from exported bytes')
        return value


@dataclass(frozen=True, slots=True)
class AdmittedStage:
    request: Request
    receipt: ReceiptBinding
    evidence: ArtifactRef

    def __post_init__(self):
        if (not isinstance(self.request, Request) or not isinstance(self.receipt, ReceiptBinding)
                or not isinstance(self.evidence, ArtifactRef) or self.request.kind == JobKind.ASSEMBLE):
            raise ValueError('Invalid admitted export stage')

    @property
    def files(self):
        return dict(self.request.files)


@dataclass(frozen=True, slots=True)
class Work:
    kind: JobKind
    owner: OwnerVersion
    proofs: tuple[ProofReference, ...]
