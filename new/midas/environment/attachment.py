"""Attach a supplied library to a newly frozen package; no run/store mutation."""
from dataclasses import dataclass
from pathlib import Path

from ..source.context import module_name
from .inventory import EnvironmentError, scan
from .package import Environment, PackageConfig


@dataclass(frozen=True, slots=True)
class Attachment:
    previous_environment_id: str
    environment_id: str
    library: str
    modules: tuple[str, ...]


def attach(environment, destination, dependency, prefixes, context, *, bootstrap=None,
           max_bytes=16 * 1024**3, _before_publish=None):
    if type(prefixes) is not tuple or not prefixes or len(prefixes) > 32:
        raise EnvironmentError('Expected explicit library namespace prefixes')
    for prefix in prefixes:
        module_name(prefix)
    spec = environment.manifest.settings
    if spec.plugins or spec.options or spec.variables:
        raise EnvironmentError('Library attachment does not admit compiler overrides/plugins')
    if dependency.name in environment.roots:
        raise EnvironmentError('Library root conflicts with an existing package root')
    inventory, observed = scan(dependency.path, links=True, maximum=max_bytes, check=context.check)
    modules = set()
    for path in dependency.module_paths:
        prefix = '' if path == '.' else path + '/'
        for file in inventory.files:
            if not file.path.startswith(prefix) or not file.path.endswith('.olean'):
                continue
            stem = file.path[len(prefix):-6]
            name = stem.replace('/', '.')
            module_name(name)
            if (name in modules or name in environment.manifest.modules
                    or not any(name == p or name.startswith(p + '.') for p in prefixes)):
                raise EnvironmentError('Shadowed module or module outside library prefixes')
            sources = [(Path(root) / (stem + '.lean')).as_posix() for root in dependency.source_roots]
            if sum(p in inventory.by_path for p in sources) != 1:
                raise EnvironmentError('Missing or ambiguous library source')
            modules.add(name)
    if not modules:
        raise EnvironmentError('Attached library has no compiled modules')
    environment.validate(check=context.check)
    roots = tuple((name, path) for name, path in environment.roots.items() if name not in ('runtime', 'toolchain'))
    searches = tuple(p for p in spec.search_paths if p != 'toolchain/lib/lean')
    searches += tuple(dependency.name if p == '.' else dependency.name + '/' + p for p in dependency.module_paths)
    searches += ('toolchain/lib/lean',)
    config = PackageConfig(spec.version, environment.toolchain, (*roots, (dependency.name, dependency.path)),
                           spec.project_inputs, searches, max_bytes, tuple(sorted(modules)))
    # Preparation checks imports before publication. Dependency roots retain their
    # admitted locations; only the project/runtime are local snapshots.
    def admit(prepared):
        for name in environment.roots:
            if name not in ('runtime', 'toolchain') and environment._inventories[name] != prepared._inventories[name]:
                raise EnvironmentError('Attachment changed an original package pin')
        _, after = scan(dependency.path, hash_content=False, links=True, maximum=max_bytes, check=context.check)
        if observed != after:
            raise EnvironmentError('Library changed during attachment')
        if _before_publish is not None:
            _before_publish(prepared)
    prepared = Environment.prepare(destination, config, context, bootstrap=bootstrap, _before_publish=admit)
    return prepared, Attachment(environment.id, prepared.id, dependency.name, tuple(sorted(modules)))
