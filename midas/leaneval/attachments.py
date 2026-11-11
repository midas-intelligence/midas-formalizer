"""Freeze attached source mappings and expose their already admitted objects."""
from hashlib import sha256
from pathlib import Path, PurePosixPath
import posixpath

from ..application import files
from ..checker.rebuild import SUFFIXES
from ..domain import encoding as codec
from ..environment import Dependency, safe_read
from ..environment.assets import included_paths
from ..environment.inventory import relative
from ..source.context import module_name


def configuration(path, environment):
    value = files.document(path)
    codec.fields(value, ('name', 'path', 'module_paths', 'source_roots', 'prefixes'))
    dependency = Dependency(value['name'], Path(path).absolute().parent/value['path'],
                            tuple(value['module_paths']), tuple(value['source_roots']))
    if (dependency.name not in environment.roots or
            dependency.path.resolve() != environment.roots[dependency.name].resolve()):
        raise ValueError('Attach the library to the package before preparing its LeanEval workspace')
    prefixes = value['prefixes']
    if type(prefixes) is not list or not 1 <= len(prefixes) <= 32 or len(set(prefixes)) != len(prefixes):
        raise ValueError('Expected distinct attachment namespace prefixes')
    for prefix in prefixes:
        module_name(prefix)
    return dependency, tuple(prefixes)


def capture(environment, configurations, existing):
    """Return trusted compiled rows and flat, environment-bound source mappings."""
    inventory = environment.manifest.inventory.by_path
    known = set(existing)
    rows, mappings, roots = [], {}, set()
    for dependency, prefixes in configurations:
        root = dependency.name
        if root in roots:
            raise ValueError('Duplicate attached package')
        roots.add(root)
        count = 0
        for name, logical in environment.manifest.modules.items():
            if not logical.startswith(root+'/'):
                continue
            stem = name.replace('.', '/')
            expected = {root+'/'+(p+'/' if p != '.' else '')+stem+'.olean' for p in dependency.module_paths}
            if (logical not in expected or name in known or
                    not any(name == p or name.startswith(p+'.') for p in prefixes)):
                raise ValueError('Attached module shadows a library or is outside its declared prefixes')
            known.add(name)
            sources = [root+'/'+(p+'/' if p != '.' else '')+stem+'.lean' for p in dependency.source_roots]
            sources = [p for p in sources if p in inventory]
            if len(sources) != 1:
                raise ValueError('Attached module requires one frozen source')
            source = sources[0]
            targets = [(stem+'.lean', source)]
            data = read_source(environment, source)
            for asset in included_paths(data):
                original = relative(posixpath.normpath(posixpath.join(posixpath.dirname(source), asset)))
                target = relative(posixpath.normpath(posixpath.join(posixpath.dirname(stem), asset)))
                if not original.startswith(root+'/') or original not in inventory:
                    raise ValueError('Attached source asset is outside its package')
                targets.append((target, original))
            for target, original in targets:
                if target.endswith('.lean') and target[:-5].replace('/', '.') in existing:
                    raise ValueError('Attached asset shadows an existing module')
                if target in mappings and mappings[target] != original:
                    raise ValueError('Attached source paths conflict')
                mappings[target] = original
            compiled = {suffix: inventory[logical[:-6]+suffix].sha256 for suffix in SUFFIXES
                        if logical[:-6]+suffix in inventory}
            rows.append(dict(name=name, package=None, source=None, assets=[], options=[], compiled=compiled))
            count += 1
        if not count:
            raise ValueError('Attached package has no compiled modules')
    return rows, tuple(sorted(mappings.items()))


def read_source(environment, logical):
    relative(logical)
    root, path = logical.split('/', 1)
    entry = environment.manifest.inventory.by_path.get(logical)
    if entry is None or root not in environment.roots or root in ('project', 'toolchain', 'runtime'):
        raise ValueError('Attachment source is outside the frozen dependency inventory')
    data = safe_read(environment.roots[root]/path, min(entry.size, 64*1024**2))
    if len(data) != entry.size or sha256(data).hexdigest() != entry.sha256:
        raise ValueError('Attached source changed')
    return data


def sources(environment, mappings, *, maximum):
    result, total = {}, 0
    for target, logical in mappings:
        relative(target)
        # All files must be explicitly captured. Never overlay control files.
        if target in result or target.split('/')[0] in ('Submission', 'Challenge', 'Solution', '.lake') or target in (
                'Submission.lean', 'Challenge.lean', 'Solution.lean', 'WorkspaceTest.lean',
                'lakefile.toml', 'lakefile.lean', 'lake-manifest.json', 'lean-toolchain', 'config.json', 'holes.json'):
            raise ValueError('Attachment cannot replace benchmark inputs')
        data = read_source(environment, logical)
        total += len(data)
        if total > maximum:
            raise ValueError('Attached sources exceed export limit')
        result[target] = data
    return result


def lakefile(trusted, attached):
    """Keep the benchmark configuration and append local module targets only."""
    import tomllib
    config = tomllib.loads(trusted.decode())
    existing = {row['name'] for row in config.get('lean_lib', ())}
    names = sorted({PurePosixPath(p).parts[0].removesuffix('.lean') for p in attached if p.endswith('.lean')})
    result = trusted
    for name in names:
        module_name(name)
        if name in existing:
            raise ValueError('Attached namespace conflicts with a benchmark library')
        # module_name bounds the identifier; JSON quoting is only small metadata.
        result += b'\n[[lean_lib]]\nname = '+codec.canonical(name)+b'\nleanOptions = { autoImplicit = true }\n'
    return result
