"""Trusted frozen-source inspection through the reserved executor context."""
from dataclasses import replace
from uuid import uuid4

from ..domain import ByteSpan, Declaration, Signature, freeze_selection
from ..domain import encoding as codec
from ..environment import safe_read
from ..executor import Mount, Outcome
from .runtime import prepare_runtime


def inventory(payload, request, source, inputs):
    value = codec.decode(payload)
    codec.fields(value, ('format', 'declarations', 'imported_constants'))
    if value['format'] != 'midas-source-inventory-v1':
        raise ValueError('Unknown Lean inventory format')
    declarations, signatures = [], {}
    for row in value['declarations']:
        codec.fields(row, ('signature', 'presentation', 'selector', 'constant', 'kind', 'declaration', 'replacement',
                           'unfinished', 'source_authored', 'supported', 'type_references', 'value_references'))
        # Domain's structural names are explicitly capped small metadata.
        def name(value):
            codec.name(value)
            return codec.canonical(value)
        constant = name(row['constant'])
        declarations.append(Declaration(row['selector'], constant, row['kind'], ByteSpan(*row['declaration']),
            ByteSpan(*row['replacement']) if row['replacement'] is not None else None,
            row['unfinished'], supported=row['supported'], source_authored=row['source_authored'],
            type_references=tuple(map(name, row['type_references'])),
            value_references=tuple(map(name, row['value_references']))))
        if constant in signatures:
            raise ValueError('Duplicate inspected declaration')
        if type(row['presentation']) is not str or len(row['presentation']) > 1024**2:
            raise ValueError('Invalid inspected type presentation')
        signatures[constant] = Signature(*(codec.encode(row['signature'][k]) for k in ('target', 'universes', 'context')),
                                        presentation=row['presentation'].encode())
    return freeze_selection(request, source, tuple(declarations),
                            frozenset(codec.canonical(n) for n in value['imported_constants']), signatures, inputs)


def inspect(checker, request, context):
    environment = checker.environment
    authorized = environment.selection_inputs()
    binding = authorized.sources_by_path.get(request.source)
    if binding is None:
        raise ValueError('Inspected source is not a frozen project input')
    source = safe_read(environment.roots['project'] / request.source, 64 * 1024**2)
    from hashlib import sha256
    if len(source) != binding.byte_count or sha256(source).hexdigest() != binding.sha256:
        raise ValueError('Inspected source changed')
    runtime = checker.runtime or prepare_runtime(environment, context, checker.cache)
    checker.runtime = runtime
    runtime.validate()
    channel = context.workspace / ('inspect-' + str(uuid4()))
    channel.mkdir()
    spec = environment.launch(('--run', '/checker/Extract.lean', '/project/' + request.source,
                               request.source, '/channel/evidence', str(context.limits.max_heartbeat),
                               str(context.limits.max_rec_depth)),
                              max_heartbeat=context.limits.max_heartbeat, max_rec_depth=context.limits.max_rec_depth)
    spec = replace(spec, mounts=(*spec.mounts, Mount(str(runtime.directory), '/checker'), Mount(str(channel), '/channel')))
    result = context.run_process(spec, evidence_path=channel / 'evidence')
    if (result.outcome != Outcome.COMPLETED or result.evidence is None or result.evidence.truncated
            or result.stdout.truncated or result.stderr.truncated):
        raise ValueError('Source inspection failed: ' + result.stderr.data.decode(errors='replace')[:4096]
                         + result.stdout.data.decode(errors='replace')[:4096])
    return inventory(result.evidence.data, request, source, authorized)
