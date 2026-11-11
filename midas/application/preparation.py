"""Bounded command preparation before a run exists; no model service is needed."""
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import time

from ..domain import (CapacityGrant, DeliveryCertainty, FrozenRequest, Invocation, Job, JobKind,
    OwnerVersion, ResourceLimits, ResultStatus, Session, Termination, Usage, WorkerResult)
from ..environment import Environment
from ..executor import Executor
from ..storage import Staging
from .records import artifact


DEFAULT_LIMITS = ResourceLimits(600000, 4096, 8*1024**2, 64*1024**2)
SEARCH_INDEX = 'search.sqlite'


def operate(callback, *, limits=DEFAULT_LIMITS, cancellation=None):
    """Give preparation the same executor isolation, resource and drain boundary."""
    with TemporaryDirectory(prefix='midas-prepare-') as temporary:
        root = Path(temporary)
        staging = Staging(root/'staging', max_bytes=limits.output_bytes+limits.evidence_bytes)
        reference = artifact(b'midas-preparation-v1').reference
        owner = OwnerVersion('obl_'+'0'*64, 0)
        job = Job('prepare', JobKind.CHECK_BINDING, owner, (), (owner,), limits)
        request = FrozenRequest(job, 'env_'+'0'*64, reference, reference)
        invocation = Invocation(str(uuid4()), Session(str(uuid4()), str(uuid4()), 1), request, reference)
        result, errors = [], []
        def execute(frozen, context):
            try:
                result.append(callback(context))
            except BaseException as error:
                errors.append(error)
            return WorkerResult(context.invocation_id, context.request_hash,
                ResultStatus.FAILED if errors else ResultStatus.SUCCEEDED, (), None, None,
                Usage(0, 0, 0, 0), DeliveryCertainty.DELIVERED, Termination.TERMINATED)
        with Executor(root, workers=1, compiler_slots=1, compiler_memory_mib=limits.memory_mib) as executor:
            workspace = root/'work'
            workspace.mkdir()
            executor.start(invocation, execute, grant=CapacityGrant(invocation), workspace=workspace,
                           reader=staging, staging=staging)
            try:
                while not (completed := executor.poll()):
                    if cancellation is not None and cancellation.is_set():
                        executor.cancel(invocation.original_execution_id)
                    time.sleep(.01)
                if errors:
                    raise errors[0]
                if completed[0].result.status != ResultStatus.SUCCEEDED or not result:
                    raise RuntimeError('Preparation failed: '+completed[0].detail)
                return result[0]
            finally:
                executor.cancel_owned_work()
                if not executor.drain(timeout=60):
                    raise RuntimeError('Preparation process termination is unconfirmed')
                staging.close()


def package(destination, config, *, limits=DEFAULT_LIMITS, cancellation=None):
    return operate(lambda context: Environment.prepare(destination, config, context,
                       _before_publish=lambda environment: index_sources(environment, context)),
                   limits=limits, cancellation=cancellation)


def index_sources(environment, context):
    """Keep derived search postings with the package; only refs/hash reach tools."""
    from ..tools.search_process import prepare_index
    context.check()
    sources = FrozenSources(environment)
    path = prepare_index(sources.index, sources, environment.directory/SEARCH_INDEX, context)
    context.check()
    return path


def select(checker, request, *, limits=DEFAULT_LIMITS, cancellation=None):
    return operate(lambda context: checker.inspect(request, context), limits=limits, cancellation=cancellation)


def ready(checker, *, limits=DEFAULT_LIMITS, cancellation=None):
    if checker.runtime is None:
        from ..checker.runtime import prepare_runtime
        checker.runtime = operate(lambda context: prepare_runtime(checker.environment, context, checker.cache),
                                  limits=limits, cancellation=cancellation)
    return checker


class FrozenSources:
    """Hash-checked file capability; no environment inventory reaches a worker."""
    def __init__(self, environment):
        from ..domain import ArtifactRef
        from ..tools import SourceEntry, SourceIndex
        from uuid import UUID, uuid5
        entries, paths = [], {}
        modules = environment.manifest.modules
        for item in environment.manifest.inventory.files:
            if not item.path.endswith('.lean'):
                continue
            root, relative = item.path.split('/', 1)
            if root == 'toolchain':
                if not relative.startswith('src/lean/'):
                    continue
                logical = 'lean/'+relative.removeprefix('src/lean/')
            elif root in ('checker', 'runtime'):
                continue
            else:
                logical = item.path
            reference = ArtifactRef(str(uuid5(UUID('d4227202-fc63-46af-a08e-470c92bd1d71'), item.sha256)),
                                    item.sha256, item.size)
            # Match the longest admitted module in this root. Dropping leading
            # source directories supports srcDir layouts without confusing a
            # namespaced dependency with a project module of the same basename.
            parts = logical.removesuffix('.lean').split('/')[1:]
            module = None
            for start in range(len(parts)):
                name = '.'.join(parts[start:])
                compiled = modules.get(name)
                if compiled is not None and compiled.split('/', 1)[0] == root:
                    module = name
                    break
            entries.append(SourceEntry(logical, reference, module))
            paths[reference] = environment.roots[root]/relative
        self.index = SourceIndex(environment.id, tuple(entries))
        self.paths = paths

    def read_artifact(self, reference, *, max_bytes):
        from ..environment import safe_read
        if reference.byte_count > max_bytes:
            raise ValueError('Source exceeds read limit')
        return reference.verify(safe_read(self.paths[reference], max_bytes), max_bytes=max_bytes)

    def artifact_path(self, reference):
        """A file capability; the search child verifies its bytes against the ref."""
        return self.paths[reference]
