"""Only bounded metadata enters JSON; proof/source bytes use artifact streams."""
from ..domain.encoding import canonical

FIELDS = ('input_tokens', 'cache_tokens', 'output_tokens', 'cost_microusd')


def usage(value):
    return {key: getattr(value, key) for key in FIELDS}


def record(view):
    a, c, p = view.status.accounting, view.completion, view.progress
    return dict(type='status', run_id=view.run_id, workflow=view.workflow,
        environment_id=view.environment_id, revision=view.status.revision,
        graph_revision=view.status.graph_revision, outcome=c.outcome, proof=c.proof_state,
        integration=c.integration_state.value, verification=c.export_verification_state.value,
        reasons=[r[:96] for r in c.reasons], reasons_truncated=any(len(r)>96 for r in c.reasons),
        exit_code=c.exit_code, roots=p.roots, verified_roots=p.verified_roots,
        nodes=dict(view.status.nodes if view.node_counts is None else view.node_counts),
        discarded_nodes=view.discarded_nodes, intents=dict(view.status.intents), executions=dict(view.status.executions),
        turns=a.turns, turn_limit=view.turn_limit, elapsed_ms=p.elapsed_ms, runtime_ms=view.runtime_ms,
        usage=usage(a.actual), known_usage=usage(a.known), unknown_reports=usage(a.unknown),
        reserved_usage=usage(a.reserved), models=a.models, model_slots=view.model_slots,
        compilers=a.compilers, compiler_slots=view.compiler_slots, locals=a.locals, networks=a.networks,
        cpu_active=a.compilers+a.locals,
        compiler_memory_mib=a.compiler_memory_mib)


def node_record(node):
    return dict(type='node', id=node.owner.obligation_id, generation=node.owner.generation,
                revision=node.revision, state=node.state, target=node.target,
                interface=node.interface.to_dict())


def safe(value):
    # Escape terminal controls, including C1 and bidi formatting characters.
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in str(value))


def duration(milliseconds):
    minutes, seconds = divmod(max(0, milliseconds // 1000), 60)
    hours, minutes = divmod(minutes, 60)
    return (f'{hours}h' if hours else '') + (f'{minutes}m' if minutes else '') + (f'{seconds}s' if seconds or not (hours or minutes) else '')


def terminal(view):
    a, c, p = view.status.accounting, view.completion, view.progress
    dollars, micros = divmod(a.known.cost_microusd, 1_000_000)
    cost = f'${dollars}.{micros:06d}'
    if a.unknown.cost_microusd:
        cost += ' + unknown'
    counts = dict(view.status.nodes if view.node_counts is None else view.node_counts)
    active = view.status.active_nodes
    reasons = '; '.join(safe(r) for r in c.reasons if r != 'active_workers')
    return (f'time: {duration(p.elapsed_ms)}/{duration(view.runtime_ms)}'
            f' | turns: {a.turns}/{view.turn_limit} | cost: {cost}'
            f' | model: {a.models}/{view.model_slots} | CPU: {a.compilers+a.locals}/{view.compiler_slots}'
            f' | LeanExplore: {a.networks}\n'
            f'completed: {counts.get("VERIFIED", 0)} | conditional: {counts.get("CONDITIONAL", 0)}'
            f' | active: {active} | waiting: {counts.get("UNRESOLVED", 0) - active}'
            f' | discarded: {view.discarded_nodes}'
            + (f' | status: {safe(c.outcome)}' if c.exit_code is not None else '')
            + (f' | reasons: {reasons}' if reasons else '')+'\n')


def write_json(stream, value):
    stream.write(canonical(value).decode('utf-8')+'\n')


def write_nodes(stream, page, *, json=False):
    for node in page.items:
        if json:
            write_json(stream, node_record(node))
        else:
            stream.write(f'{node.owner.obligation_id}  {node.state}  generation={node.owner.generation}'
                         + (f'  target={node.target}' if node.target is not None else '')+'\n')
    if json:
        write_json(stream, dict(type='page', revision=page.revision, after=page.after))
    elif page.after:
        stream.write(f'Continue with --after {page.after}\n')


def write_details(stream, details, *, json=False):
    if json:
        write_json(stream, dict(type='node-details', id=details.id, stopped=details.stopped,
                               reason=details.reason, submission=details.submission))
    else:
        stream.write(f'stopped: {details.stopped} | reason: {safe(details.reason or "none")}\n')
    for label, reference in details.artifacts:
        if json:
            write_json(stream, dict(type='node-artifact', label=label, **reference.to_dict()))
        else:
            stream.write(f'{label}: {reference.id} ({reference.byte_count} bytes)\n')
