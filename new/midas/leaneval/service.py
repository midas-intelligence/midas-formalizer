"""Verify ordinary exports, invoke the benchmark harness, and publish judge files."""
from pathlib import Path
import time
import tomllib

from ..domain import encoding as codec
from ..environment import describe, match_relocation, safe_read
from ..export import read_bundle
from ..export.layout import _Reader, publish_files, reference, _inventory
from . import comparator
from .workspace import MAX_BYTES, evidence, load, starter


def bundle_files(directory, maximum):
    from ..export.protocol import file_rows
    payload = safe_read(Path(directory)/'manifest.bin', MAX_BYTES)
    return payload, file_rows(codec.decode(payload)['files'], maximum)


def submission_files(archive, source):
    trusted = starter(archive)
    files = {p: data for p, data in trusted.items() if p.startswith('Submission/') and p.endswith('.lean')}
    files['Submission.lean'] = source
    comparator.overlay(trusted, files)
    return files


def judge_files(archive, submission):
    benchmark = describe(archive)
    if benchmark.fixture_only:
        return {}
    trusted = starter(archive)
    if tomllib.loads(trusted['lakefile.toml'].decode()).get('name') != benchmark.problem:
        raise ValueError('Judge package name differs from the benchmark problem')
    files = dict(submission, **{p: trusted[p] for p in ('lakefile.toml', 'lean-toolchain')})
    return {'judge-submission/'+benchmark.problem+'/'+p: data for p, data in files.items()}


def check_bundle(request, workspace):
    if (request.environment_id != workspace.environment_id or request.selection_id != workspace.selection.id
            or request.allow_sorry or request.decode()['packet']['logical'] != 'Submission.lean'):
        raise ValueError('Bundle must be a complete export of the prepared benchmark selection')
    rows = workspace.library.decode() if workspace.library else []
    if request.decode()['packet']['modules'] != rows:
        raise ValueError('Bundle dependency trust differs from the prepared workspace')


def verify(application, bundle, workspace, *, verification, runtime, config, cancellation=None):
    """Fresh source/kernel audit and comparator run; saved success grants no authority."""
    started = time.monotonic()
    request, reader = read_bundle(bundle, maximum=config.export.max_bytes)
    check_bundle(request, workspace)
    match_relocation(verification, workspace.archive, max_bytes=config.export.max_bytes)
    audited = application.verify(bundle, package=workspace.package, config=config, cancellation=cancellation)
    if not audited.verified:
        raise ValueError('Independent source audit failed: '+str(audited.reason))
    from dataclasses import replace
    remaining = config.export.deadline_ms-int((time.monotonic()-started)*1000)
    if remaining <= 0:
        raise ValueError('LeanEval verification deadline exhausted')
    remaining_config = replace(config, export=replace(config.export, deadline_ms=remaining))
    source = reader.read_artifact(dict(request.files)['src/Submission.lean'], max_bytes=MAX_BYTES)
    submission = submission_files(workspace.archive, source)
    environment = application.environment(workspace.package, expected_id=workspace.environment_id)
    comparison = comparator.run(environment, workspace.archive, submission, verification, runtime,
                              config=remaining_config, cancellation=cancellation)
    after, _ = read_bundle(bundle, maximum=config.export.max_bytes)
    if after.payload != request.payload:
        raise ValueError('Bundle changed during verification')
    report = evidence(dict(format='midas-leaneval-verification-v1', archive=workspace.archive.id,
        environment=environment.id, selection=workspace.selection.id, bundle=request.sha256,
        fixture_only=describe(workspace.archive).fixture_only),
        ((b'A', audited.evidence), (b'C', comparison)), config.export.evidence_bytes)
    return request, reader, submission, report


def finalize(application, bundle, destination, *, workspace, verification, runtime, config,
             package=None, cancellation=None):
    workspace = load(application, workspace, package=package)
    request, reader, submission, report = verify(application, bundle, workspace,
        verification=verification, runtime=runtime, config=config, cancellation=cancellation)
    raw = {'verification.bin': report, **{'submission/'+p: data for p, data in submission.items()},
           **judge_files(workspace.archive, submission)}
    raw['bundle/manifest.bin'], bundled = bundle_files(bundle, config.export.max_bytes)
    files = {'bundle/'+path: ref for path, ref in bundled.items()}
    files.update({p: reference(data) for p, data in raw.items()})
    raw['manifest.bin'] = codec.encode(dict(format='midas-leaneval-files-v1', archive=workspace.archive.id,
        files=[[p, ref.to_dict()] for p, ref in sorted(files.items())]))
    files['manifest.bin'] = reference(raw['manifest.bin'])
    def check():
        if cancellation is not None and cancellation.is_set():
            raise RuntimeError('LeanEval publication cancelled')
    environment = application.environment(workspace.package, expected_id=workspace.environment_id)
    publish_files(files, _Reader(reader, {reference(data): data for data in raw.values()}), destination,
        maximum=config.export.max_bytes, protected=(Path(bundle), workspace.directory, workspace.package,
            environment.toolchain, Path(runtime), *(t.path for t in verification.tools),
            *(d.path for d in verification.dependencies)), check=check)
    return dict(type='leaneval', verified=True, fixture_only=describe(workspace.archive).fixture_only,
                submission_ready=not describe(workspace.archive).fixture_only, archive_id=workspace.archive.id)


def verify_submission(application, directory, *, workspace, verification, runtime, config,
                      package=None, cancellation=None):
    from ..export.protocol import file_rows
    directory = Path(directory).absolute()
    workspace = load(application, workspace, package=package)
    manifest = safe_read(directory/'manifest.bin', MAX_BYTES)
    value = codec.decode(manifest)
    codec.fields(value, ('format', 'archive', 'files'))
    if value['format'] != 'midas-leaneval-files-v1' or value['archive'] != workspace.archive.id:
        raise ValueError('Submission archive identity differs')
    files = file_rows(value['files'], config.export.max_bytes)
    if 'manifest.bin' in files:
        raise ValueError('Submission manifest inventories itself')
    files['manifest.bin'] = reference(manifest)
    _inventory(directory, files, config.export.max_bytes, lambda: None)
    request, reader = read_bundle(directory/'bundle', maximum=config.export.max_bytes)
    check_bundle(request, workspace)
    source = reader.read_artifact(dict(request.files)['src/Submission.lean'], max_bytes=MAX_BYTES)
    submission = submission_files(workspace.archive, source)
    raw = {**{'submission/'+p: data for p, data in submission.items()}, **judge_files(workspace.archive, submission)}
    expected = {p: reference(data) for p, data in raw.items()}
    bundled_manifest, bundled = bundle_files(directory/'bundle', config.export.max_bytes)
    expected.update({'bundle/'+path: ref for path, ref in bundled.items()})
    expected['bundle/manifest.bin'] = reference(bundled_manifest)
    # The historical report is inventoried, but never read as acceptance evidence.
    if ('verification.bin' not in files or
            {p: ref for p, ref in files.items() if p not in ('manifest.bin', 'verification.bin')} != expected):
        raise ValueError('Submission copies or judge layout differ from the audited bundle')
    _, _, _, report = verify(application, directory/'bundle', workspace, verification=verification,
                             runtime=runtime, config=config, cancellation=cancellation)
    _inventory(directory, files, config.export.max_bytes, lambda: None)
    return dict(type='leaneval', verified=True, fixture_only=describe(workspace.archive).fixture_only,
                submission_ready=not describe(workspace.archive).fixture_only, archive_id=workspace.archive.id), report
