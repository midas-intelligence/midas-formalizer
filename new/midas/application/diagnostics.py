"""Model-free bundle verification and a separate four-call cache diagnostic."""
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ..controller import Controller, Effect, Operations, artifact, checker_operation, model_operation
from ..domain import JobKind, OwnerVersion, ProofNode, ResourceLimits
from ..executor import Executor
from ..export import Export, read_bundle
from ..policy import Config, Policy, Progress
from ..prompt import Options, Prompt, Section, Snapshot, build
from ..provider import Limits, ModelRequest, Route
from ..storage import Changes, FrozenRun, NodeChange, create
from . import preparation, providers, records as r


@dataclass(frozen=True, slots=True)
class Verification:
    verified: bool
    evidence: bytes | None
    reason: str | None

    @property
    def exit_code(self):
        return 0 if self.verified else 1


def verify(application, directory, *, package, config=Config()):
    environment = application.environment(package)
    checker = preparation.ready(application.checker_factory(environment, application.cache))
    bundle, source = read_bundle(directory, maximum=config.export.max_bytes)
    request = Export(checker).prepare_recheck(bundle, source)
    value = request.decode()
    owner = OwnerVersion(*value['owner'])
    with TemporaryDirectory(prefix='midas-verify-') as temporary:
        with create(Path(temporary)/'run') as writer:
            metadata = writer.put(request.payload, max_bytes=r.DATA)
            configuration = writer.put(config.encode(), max_bytes=16384)
            for raw in value['inputs']:
                from ..domain import ArtifactRef
                ref = ArtifactRef.from_dict(raw)
                writer.copy_artifact(source, ref)
            with writer.transaction(writer.session) as tx:
                tx.freeze(FrozenRun(environment.id, metadata, configuration))
                tx.apply('initialize', Changes(nodes=(NodeChange(None, ProofNode(owner, environment.id, metadata)),),
                                              roots=(owner.obligation_id,), graph_revision=0))
            def reduce(reader, invocation, accepted):
                return r.record(reader, r.SCOPE, 'verification', [accepted.failure.value if accepted.failure else None,
                    r.ref(accepted.value.evidence) if accepted.value else None])
            with Executor(Path(temporary), workers=1, compiler_slots=1,
                          compiler_memory_mib=config.scheduling.compiler_memory_mib) as executor:
                controller = Controller(writer, executor, Policy(config),
                    Operations(export_verify=checker_operation(checker, reduce)))
                limits = config.export.limits(comparator=True)
                controller.publish('verify', r.job(writer, 'verify', JobKind.EXPORT_VERIFY, owner, environment.id,
                    request.payload, tuple(r.unref([x['id'], x['sha256'], x['bytes']]) for x in value['inputs']),
                    (owner,), limits, ['verify']))
                def progress(reader):
                    return Progress(1, ready=int(r.value(reader, r.SCOPE, 'verification') is None))
                try:
                    completion = controller.run(progress)
                    result = r.value(writer, r.SCOPE, 'verification')
                    if result is None:
                        return Verification(False, None, completion.outcome)
                    return Verification(result[0] is None,
                        r.read(writer, r.unref(result[1]), r.DATA) if result[1] else None, result[0])
                finally:
                    if not controller.closed:
                        controller.drain(timeout=5)


def cache_probe(application, directory, *, config):
    """Explicit diagnostic only; never invoked by formalization or preflight."""
    route = Route.from_config(config.prover)
    if route.transport == 'codex':
        raise ValueError('Cache diagnostics require a Responses route')
    config = replace(config, run=replace(config.run, turns=4),
        model=replace(config.model, invocation_ms=min(config.model.invocation_ms, 60000)),
        recovery=replace(config.recovery, transport_retries=0, unknown_retries=0))
    provider = providers.construct(config, transports=application.transports, roles=('prover',))
    prefix = uuid4().hex
    instructions = ('Cache diagnostic '+prefix+'\nReturn only {"ok":true}.\n' +
                    '\n'.join(f'Reference {i}: stable instructions.' for i in range(180))).encode()
    schema = b'{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"],"additionalProperties":false}'
    specs = []
    for goal, feedback in (('A', 1), ('A', 2), ('B', 1), ('B', 2)):
        display = Snapshot((Section('subject', 'goal', '\n'.join(f'{goal}: reference fact {i}' for i in range(180)).encode()),
                            Section('feedback', 'feedback', str(feedback).encode())))
        specs.append(build(display, Options(route.transport, route.model, max_output_tokens=256,
            prompt_cache=route.prompt_cache, cache_scope=route.scope if route.prompt_cache == 'explicit' else None),
            renderer=lambda s: Prompt('prover', 'cache-diagnostic', 'cache-diagnostic-v1', instructions, schema, s.sections)))
    with create(directory) as writer:
        config_ref = writer.put(config.encode(), max_bytes=16384)
        inputs = writer.put(r.pack(['midas-cache-diagnostic-v1', prefix]), max_bytes=r.META)
        owner = OwnerVersion('obl_'+inputs.sha256, 0)
        environment_id = 'env_'+route.scope
        with writer.transaction(writer.session) as tx:
            tx.freeze(FrozenRun(environment_id, inputs, config_ref))
            tx.apply('initialize', Changes(nodes=(NodeChange(None, ProofNode(owner, environment_id, inputs)),),
                                          roots=(owner.obligation_id,), graph_revision=0))
        limits = Limits.from_config(config.model)
        def plan(reader, policy, limit):
            rows = r.value(reader, r.SCOPE, 'cache-results') or []
            index = len(rows)
            if index == 4 or rows and rows[-1][0] is not None or reader.intent_state(f'cache:{index}'):
                return
            prepared = specs[index]
            wire = artifact(prepared.wire)
            request = ModelRequest(route, 'prover', limits, wire.reference)
            yield f'cache:{index}', r.merge(r.job(reader, f'cache:{index}', JobKind.MODEL, owner, environment_id,
                request.payload, request.inputs, (owner,),
                ResourceLimits(limits.invocation_ms, config.local.memory_mib, limits.output_bytes, limits.event_bytes+16384),
                ['cache', index]), Effect(artifacts=(wire,)))
        def reduce(reader, invocation, accepted):
            rows = r.value(reader, r.SCOPE, 'cache-results') or []
            output = accepted.value
            rows.append([accepted.failure.value if accepted.failure else None,
                output.outcome.usage.cache_tokens if output else None,
                output.outcome.cache_write_tokens if output else None, invocation.original_execution_id])
            return r.record(reader, r.SCOPE, 'cache-results', rows)
        def progress(reader):
            rows = r.value(reader, r.SCOPE, 'cache-results') or []
            return Progress(1, ready=int(len(rows) < 4 and not (rows and rows[-1][0])))
        with Executor(writer.directory, workers=1, compiler_slots=1,
                      compiler_memory_mib=config.scheduling.compiler_memory_mib) as executor:
            controller = Controller(writer, executor, Policy(config), Operations(model=model_operation(provider, reduce)), planner=plan)
            try:
                controller.run(progress)
            finally:
                if not controller.closed:
                    controller.drain(timeout=5)
        rows = r.value(writer, r.SCOPE, 'cache-results') or []
        known = len(rows) == 4 and all(row[0] is None and row[1] is not None and row[2] is not None for row in rows)
        verified = False
        if known:
            a, b, c, d = rows
            verified = a[1] == 0 and a[2] > 0 and b[1] > c[1] > 0 and c[2] > 0 and d[1] > c[1] and b[2] == d[2] == 0
        return dict(status='complete' if len(rows) == 4 and not any(row[0] for row in rows) else 'stopped',
                    observation='verified' if verified else 'negative' if known else 'unknown', steps=tuple(rows))
