"""Read-only application facts; no compiler, provider or writer construction."""
from collections import deque
from dataclasses import replace

from ..policy import Completion, Policy, Stage, load_config
from ..status import nodes as node_page, progress, project
from ..storage import open_reader
from . import metadata as r


def current_node_counts(reader):
    """Include pending submission entries before they have selected proof edges."""
    counts, seen, submissions = {}, set(), set()
    pending = deque()
    while True:
        batch = tuple(pending.popleft() for _ in range(min(len(pending), 1000)))
        after = ''
        while rows := reader.display_nodes(extra_roots=batch, after=after, limit=1000):
            after = rows[-1].owner.obligation_id
            for node in rows:
                owner = node.owner.obligation_id
                if owner in seen:
                    continue
                seen.add(owner)
                counts[node.state] = counts.get(node.state, 0)+1
                saved = reader.record('application.owners', owner)
                if saved is None or saved.artifact is None:
                    continue
                state = r.Owner.load(r.read(reader, saved.artifact))
                if not state.submission or state.submission in submissions:
                    continue
                submissions.add(state.submission)
                submission = r.value(reader, 'application.submissions', state.submission)
                if submission and submission['phase'] not in ('done', 'failed'):
                    pending.extend(row['owner'] for row in submission['rows'].values() if row['owner'] not in seen)
        if not pending:
            return tuple(sorted(counts.items()))


class Inspector:
    def __init__(self):
        self._cache = {}

    def inspect(self, directory):
        with open_reader(directory) as reader, reader.snapshot():
            committed = reader.status_snapshot()
            # Include file identity; a replaced database is never a cache hit.
            key = reader.cache_identity, committed.revision
            if key in self._cache:
                return self._cache[key]
            frozen = reader.frozen_run()
            inputs = r.Inputs.load(r.read(reader, frozen.inputs))
            configured = reader.record(r.SCOPE, 'configuration')
            policy = Policy(load_config(r.read(reader,
                configured.artifact if configured else frozen.configuration, 16384)))
            stages, allow_sorry = [], False
            for name in ('integration', 'verification'):
                saved = r.value(reader, 'application.export', name)
                valid = saved and saved[0] == 'accepted' and saved[4] == committed.graph_revision
                allow_sorry |= bool(valid and saved[5])
                stages.append(Stage.VERIFIED if valid else Stage.FAILED
                    if saved and saved[0] == 'failed' and saved[-1] == committed.graph_revision else Stage.PENDING)
            view = progress(reader, committed=committed, integration=stages[0],
                            verification=stages[1], allow_sorry=allow_sorry)
            saved = r.value(reader, r.SCOPE, 'completion')
            completion = None
            if saved and saved[0]+1 == committed.revision:
                completion = Completion(saved[1], Stage(saved[2]), Stage(saved[3]), saved[4], tuple(saved[5]), saved[6])
            result = project(reader, policy, workflow=inputs.workflow, progress=view,
                             committed=committed, completion=completion, node_counts=current_node_counts(reader))
            self._cache[key] = result
            while len(self._cache) > 8:
                self._cache.pop(next(iter(self._cache)))
            return result


def nodes(directory, **options):
    with open_reader(directory) as reader, reader.snapshot():
        page = node_page(reader, **options)
        owner = options.get('owner')
        return replace(page, details=node_details(reader, owner)) if owner and page.items else page


def node_details(reader, owner):
    from ..status.views import NodeDetails
    _, state = r.owner_state(reader, owner)
    references = [(name, reference) for name in ('context', 'identity', 'base', 'memory', 'stop_request')
                  if (reference := getattr(state, name)) is not None]
    references.extend(('evidence', ref) for ref in state.evidence)
    return NodeDetails(owner, state.stopped, state.reason, state.submission, tuple(references))


def session(directory):
    with open_reader(directory) as reader:
        return reader.current_session()


def artifact(directory, ident):
    with open_reader(directory) as reader:
        return reader.artifact(ident)


def artifact_chunks(directory, ident, *, max_bytes):
    with open_reader(directory) as reader:
        yield from reader.stream_artifact(reader.artifact(ident), max_bytes=max_bytes)
