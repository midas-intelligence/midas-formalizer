"""Fresh source builds for independent audits, under the caller's reservation."""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..domain import ArtifactRef
from ..domain import encoding as codec
from ..domain.records import identifier
from ..environment import file_hash, scan
from ..executor import Diagnostic, Outcome, ProcessResult
from ..source.context import module_name
from .requests import read

SUFFIXES = ('.olean', '.olean.private', '.olean.server', '.ilean', '.ir', '.ir.sig')


def modules(value):
    if type(value) is not list or len(value) > 10000:
        raise ValueError('Invalid audit module inventory')
    seen = set()
    for row in value:
        codec.fields(row, ('name', 'source', 'compiled'))
        module_name(row['name'])
        if row['name'].casefold() in seen or row['name'] == 'MidasCheck':
            raise ValueError('Duplicate or private audit module')
        seen.add(row['name'].casefold())
        ArtifactRef.from_dict(row['source'])
        compiled = row['compiled']
        if type(compiled) is not dict or '.olean' not in compiled or set(compiled) - set(SUFFIXES):
            raise ValueError('Invalid audit compiled hashes')
        for digest in compiled.values():
            identifier(digest, '')
    return value


def clean_launch(recipe, workspace):
    """Only trusted runtime, toolchain, verifier, and newly built files are visible."""
    from ..executor import Mount
    mounts = tuple(m for m in recipe.mounts if m.target in
                   ('/toolchain', '/runtime', '/checker', '/inputs', '/channel', '/build'))
    if not any(m.target == '/build' for m in mounts):
        mounts += (Mount(str(workspace), '/build'),)
    variables = tuple((k, '/checker:/build/lib:/toolchain/lib/lean' if k == 'LEAN_PATH' else v)
                      for k, v in recipe.variables)
    return replace(recipe, mounts=mounts, variables=variables)


def rebuild(environment, packet, context, *, maximum=16*1024**3):
    """Rebuild exact dependency sources and the entry, never load cached proof objects.

    The aggregate keeps one bounded phase in the receipt even for 10,000 modules.
    Every subprocess consumes the executor context's cumulative resource budget.
    """
    rows = modules(packet['modules'])
    settings = environment.manifest.settings
    if settings.plugins or settings.options or settings.variables:
        raise ValueError('Independent source audit does not admit compiler overrides/plugins')
    environment.validate(hash_toolchain=True, fresh=True, check=context.check)
    workspace = context.workspace / ('rebuild-' + str(uuid4()))
    workspace.mkdir()
    source, library = workspace / 'src', workspace / 'lib'
    source.mkdir()
    library.mkdir()
    entry = packet['logical']
    from ..environment.inventory import relative
    relative(entry)
    if not entry.endswith('.lean'):
        raise ValueError('Audit entry must be Lean source')
    entry_name = entry[:-5].replace('/', '.')
    module_name(entry_name)
    if entry_name in {row['name'] for row in rows}:
        raise ValueError('Audit entry shadows a dependency')
    stdout, stderr, elapsed, rss, anonymous = [], [], 0, 0, 0
    observed = [0, 0]
    truncated = [False, False]
    empty = Diagnostic(b'', 0, False)
    last = ProcessResult(Outcome.COMPLETED, 0, 0, 0, 0, None, empty, empty)
    total = 0

    def run(path, name):
        nonlocal last, elapsed, rss, anonymous
        context.check()
        stem = name.replace('.', '/')
        (library / stem).parent.mkdir(parents=True, exist_ok=True)
        recipe = environment.launch(('--root=/build/src', '-o', '/build/lib/' + stem + '.olean',
            '-i', '/build/lib/' + stem + '.ilean', '/build/src/' + path), build=workspace,
            max_heartbeat=context.limits.max_heartbeat, max_rec_depth=context.limits.max_rec_depth)
        last = context.run_process(clean_launch(recipe, workspace))
        stdout.append(last.stdout.data)
        stderr.append(last.stderr.data)
        for index, stream in enumerate((last.stdout, last.stderr)):
            observed[index] += stream.observed_bytes
            truncated[index] |= stream.truncated
        elapsed += last.elapsed_ms
        rss = max(rss, last.peak_rss_bytes or 0)
        anonymous = max(anonymous, last.peak_anonymous_bytes or 0)
        if last.outcome != Outcome.COMPLETED or last.returncode != 0:
            return False
        return True

    for row in rows:
        path = row['name'].replace('.', '/') + '.lean'
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        ref = ArtifactRef.from_dict(row['source'])
        total += ref.byte_count
        if total > maximum:
            raise ValueError('Rebuilt sources exceed export byte limit')
        target.write_bytes(read(context.reader, ref))
        if not run(path, row['name']):
            break
        stem = library / row['name'].replace('.', '/')
        present = {suffix for suffix in SUFFIXES if Path(str(stem) + suffix).exists()}
        if present != set(row['compiled']) or any(file_hash(Path(str(stem) + suffix),
                check=context.check) != digest for suffix, digest in row['compiled'].items()):
            raise ValueError('Rebuilt dependency differs from its admitted compiled interface: ' + row['name'])
    else:
        # The audit elaborates and kernel-replays the complete entry itself.
        # Recheck all dependency bytes after the last writable compiler process;
        # a later module must not change an earlier, already checked output.
        for row in rows:
            stem = row['name'].replace('.', '/')
            ref = ArtifactRef.from_dict(row['source'])
            if file_hash(source / (stem + '.lean'), maximum=ref.byte_count, check=context.check) != ref.sha256:
                raise ValueError('Rebuilt source changed during compilation')
            for suffix, digest in row['compiled'].items():
                if file_hash(library / (stem + suffix), check=context.check) != digest:
                    raise ValueError('Rebuilt dependency changed during compilation')
        inventory, _ = scan(workspace, maximum=maximum, check=context.check)
        expected = {'src/' + row['name'].replace('.', '/') + '.lean' for row in rows}
        expected.update('lib/' + row['name'].replace('.', '/') + suffix for row in rows for suffix in row['compiled'])
        if set(inventory.by_path) != expected:
            raise ValueError('Rebuild produced unrecorded files')
    evidence = codec.encode({'format': 'midas-source-build-v1', 'modules': rows,
        'logical': entry, 'source_sha256': sha256(packet['source'].encode()).hexdigest()})
    out, err = b''.join(stdout), b''.join(stderr)
    summary = replace(last, elapsed_ms=elapsed, peak_rss_bytes=rss, peak_anonymous_bytes=anonymous,
        stdout=Diagnostic(out, observed[0], truncated[0]),
        stderr=Diagnostic(err, observed[1], truncated[1]),
        evidence=Diagnostic(evidence, len(evidence), False))
    environment.validate(hash_toolchain=True, fresh=True, check=context.check)
    return workspace, summary


def admit_build(value, packet):
    expected = {'format': 'midas-source-build-v1', 'modules': packet['modules'],
                'logical': packet['logical'], 'source_sha256': sha256(packet['source'].encode()).hexdigest()}
    if codec.encode(value) != codec.encode(expected):
        raise ValueError('Independent build receipt differs from requested files')
