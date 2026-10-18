"""Fresh source builds for independent audits, under the caller's reservation."""
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from ..domain import ArtifactRef
from ..domain import encoding as codec
from ..domain.cache import Memo
from ..domain.records import identifier
from ..environment import file_hash, scan
from ..environment.inventory import relative
from ..environment.options import module_setup, saved_options
from ..environment.manifest import label
from ..executor import Diagnostic, Outcome, ProcessResult
from ..source.context import module_name
from .requests import read

SUFFIXES = ('.olean', '.olean.private', '.olean.server', '.ilean', '.ir', '.ir.sig')
_INVENTORIES = Memo(max_entries=32, max_bytes=32 * 1024**2)


@dataclass(frozen=True, slots=True)
class Module:
    """Validated inventory row; all nested values are immutable."""
    name: str
    package: str | None
    source: ArtifactRef | None
    assets: tuple[tuple[str, ArtifactRef], ...]
    options: tuple[tuple[str, str | bool | int], ...]
    compiled: tuple[tuple[str, str], ...]


def inventory(payload):
    """Decode and validate once, retaining bounded immutable rows for reuse."""
    if type(payload) is not bytes:
        raise ValueError('Expected binary module inventory')
    return _INVENTORIES.get(b'', payload, lambda: modules(codec.decode(payload)))


def load_inventory(packet, reader):
    reference = ArtifactRef.from_dict(packet['inventory'])
    # Always read through the capability and verify the content, even on a hit.
    return inventory(read(reader, reference))


def modules(value):
    if type(value) is not list or len(value) > 10000:
        raise ValueError('Invalid audit module inventory')
    seen = set()
    result = []
    for row in value:
        codec.fields(row, ('name', 'package', 'source', 'assets', 'options', 'compiled'))
        module_name(row['name'])
        if row['package'] is not None:
            label(row['package'])
        options = saved_options(row['options'])
        if row['name'].casefold() in seen or row['name'] == 'MidasCheck':
            raise ValueError('Duplicate or private audit module')
        seen.add(row['name'].casefold())
        source = None
        if row['source'] is not None:
            source = ArtifactRef.from_dict(row['source'])
        elif row['assets'] or row['options']:
            raise ValueError('Trusted compiled modules cannot carry source assets/options')
        if type(row['assets']) is not list or len(row['assets']) > 256:
            raise ValueError('Invalid source assets')
        paths = set()
        assets = []
        for asset in row['assets']:
            if type(asset) is not list or len(asset) != 2 or relative(asset[0]) in paths:
                raise ValueError('Invalid source asset path')
            paths.add(asset[0])
            assets.append((asset[0], ArtifactRef.from_dict(asset[1])))
        compiled = row['compiled']
        if type(compiled) is not dict or '.olean' not in compiled or set(compiled) - set(SUFFIXES):
            raise ValueError('Invalid audit compiled hashes')
        for digest in compiled.values():
            identifier(digest, '')
        result.append(Module(row['name'], row['package'], source, tuple(assets), options,
                             tuple(sorted(compiled.items()))))
    module_files(result)
    return tuple(result)


def module_files(rows):
    """Flat source/asset map shared by workspace capture and independent exports."""
    result = {row.name.replace('.', '/') + '.lean': row.source
              for row in rows if row.source is not None}
    sources = {row.name.replace('.', '/') + '.lean' for row in rows}
    for row in rows:
        for path, ref in row.assets:
            if path in sources or path in result and result[path] != ref:
                raise ValueError('Conflicting source asset: ' + path)
            result[path] = ref
    return result


def trusted_roots(environment, rows):
    """Resolve explicitly trusted objects against the one frozen environment."""
    roots = set()
    for row in rows:
        if row.source is not None:
            continue
        path = environment.manifest.modules.get(row.name, '')
        root = path.split('/')[0]
        if root not in environment.roots or root in ('project', 'toolchain', 'runtime', 'checker', 'inputs', 'channel', 'build'):
            raise ValueError('Trusted compiled module must belong to a dependency root')
        expected = {suffix: item.sha256 for suffix in SUFFIXES
                    if (item := environment.manifest.inventory.by_path.get(path[:-6] + suffix)) is not None}
        if dict(row.compiled) != expected:
            raise ValueError('Trusted compiled dependency differs from frozen artifacts: ' + row.name)
        roots.add(root)
    return tuple(sorted(roots))


def clean_launch(recipe, workspace, *, trusted=()):
    """Use fresh local objects and only the explicitly trusted dependency roots."""
    from ..executor import Mount
    mounts = tuple(m for m in recipe.mounts if m.target in
                   ('/toolchain', '/runtime', '/checker', '/inputs', '/channel', '/build',
                    *('/' + root for root in trusted)))
    if not any(m.target == '/build' for m in mounts):
        mounts += (Mount(str(workspace), '/build'),)
    paths = [p for p in dict(recipe.variables).get('LEAN_PATH', '').split(':')
             if p.startswith('/') and p.split('/')[1] in trusted]
    variables = tuple((k, ':'.join(('/checker', '/build/lib', *paths, '/toolchain/lib/lean')) if k == 'LEAN_PATH' else v)
                      for k, v in recipe.variables)
    return replace(recipe, mounts=mounts, variables=variables)


def rebuild(environment, packet, context, *, maximum=16*1024**3):
    """Rebuild local/source modules, optionally trusting pinned external objects.

    The aggregate keeps one bounded phase in the receipt even for 10,000 modules.
    Every subprocess consumes the executor context's cumulative resource budget.
    The caller validates the environment around the complete rebuild/audit;
    this intermediate build result alone is not proof admission.
    """
    rows = load_inventory(packet, context.reader)
    trusted = trusted_roots(environment, rows)
    source_rows = [row for row in rows if row.source is not None]
    settings = environment.manifest.settings
    if settings.plugins or settings.options or settings.variables:
        raise ValueError('Independent source audit does not admit compiler overrides/plugins')
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
    if entry_name in {row.name for row in rows}:
        raise ValueError('Audit entry shadows a dependency')
    stdout, stderr, elapsed, rss, anonymous = [], [], 0, 0, 0
    observed = [0, 0]
    truncated = [False, False]
    empty = Diagnostic(b'', 0, False)
    last = ProcessResult(Outcome.COMPLETED, 0, 0, 0, 0, None, empty, empty)
    total = 0
    files = module_files(rows)
    if entry in files:
        raise ValueError('Source asset shadows audit entry')
    module_paths = {row.name.replace('.', '/') + '.lean' for row in rows}
    for path, ref in files.items():
        if path not in module_paths:
            total += ref.byte_count
            if total > maximum:
                raise ValueError('Rebuilt sources exceed export byte limit')
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(read(context.reader, ref))

    def run(path, name, package, options=()):
        nonlocal last, elapsed, rss, anonymous
        context.check()
        stem = name.replace('.', '/')
        (library / stem).parent.mkdir(parents=True, exist_ok=True)
        setup = module_setup(name, package, options) if package is not None or options else None
        arguments = ()
        if setup is not None:
            (workspace / 'module.setup.json').write_bytes(setup)
            arguments = ('--setup', '/build/module.setup.json')
        recipe = environment.launch((*arguments, '--root=/build/src', '-o', '/build/lib/' + stem + '.olean',
            '-i', '/build/lib/' + stem + '.ilean', '/build/src/' + path), build=workspace,
            max_heartbeat=context.limits.max_heartbeat, max_rec_depth=context.limits.max_rec_depth)
        last = context.run_process(clean_launch(recipe, workspace, trusted=trusted))
        if setup is not None:
            (workspace / 'module.setup.json').unlink()
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

    for row in source_rows:
        path = row.name.replace('.', '/') + '.lean'
        target = source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        ref = row.source
        total += ref.byte_count
        if total > maximum:
            raise ValueError('Rebuilt sources exceed export byte limit')
        target.write_bytes(read(context.reader, ref))
        if not run(path, row.name, row.package, row.options):
            break
        stem = library / row.name.replace('.', '/')
        present = {suffix for suffix in SUFFIXES if Path(str(stem) + suffix).exists()}
        if present != {suffix for suffix, _ in row.compiled} or any(file_hash(Path(str(stem) + suffix),
                check=context.check) != digest for suffix, digest in row.compiled):
            raise ValueError('Rebuilt dependency differs from its admitted compiled interface: ' + row.name)
    else:
        # The audit elaborates and kernel-replays the complete entry itself.
        # Recheck all dependency bytes after the last writable compiler process;
        # a later module must not change an earlier, already checked output.
        for row in source_rows:
            stem = row.name.replace('.', '/')
            ref = row.source
            if file_hash(source / (stem + '.lean'), maximum=ref.byte_count, check=context.check) != ref.sha256:
                raise ValueError('Rebuilt source changed during compilation')
            for suffix, digest in row.compiled:
                if file_hash(library / (stem + suffix), check=context.check) != digest:
                    raise ValueError('Rebuilt dependency changed during compilation')
        for path, ref in files.items():
            if file_hash(source / path, maximum=ref.byte_count, check=context.check) != ref.sha256:
                raise ValueError('Rebuilt source asset changed during compilation')
        inventory, _ = scan(workspace, maximum=maximum, check=context.check)
        expected = {'src/' + path for path in files}
        expected.update('lib/' + row.name.replace('.', '/') + suffix for row in source_rows for suffix, _ in row.compiled)
        if set(inventory.by_path) != expected:
            raise ValueError('Rebuild produced unrecorded files')
    evidence = codec.encode({'format': 'midas-source-build-v2', 'inventory': packet['inventory'],
        'logical': entry, 'source_sha256': sha256(packet['source'].encode()).hexdigest()})
    out, err = b''.join(stdout), b''.join(stderr)
    summary = replace(last, elapsed_ms=elapsed, peak_rss_bytes=rss, peak_anonymous_bytes=anonymous,
        stdout=Diagnostic(out, observed[0], truncated[0]),
        stderr=Diagnostic(err, observed[1], truncated[1]),
        evidence=Diagnostic(evidence, len(evidence), False))
    return workspace, summary


def admit_build(value, packet):
    expected = {'format': 'midas-source-build-v2', 'inventory': packet['inventory'],
                'logical': packet['logical'], 'source_sha256': sha256(packet['source'].encode()).hexdigest()}
    if codec.encode(value) != codec.encode(expected):
        raise ValueError('Independent build receipt differs from requested files')
