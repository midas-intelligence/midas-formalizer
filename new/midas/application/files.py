"""Bounded command inputs and streamed, non-replacing artifact outputs."""
from pathlib import Path
import os
import tempfile

from ..domain.encoding import load_metadata


def read(path, maximum):
    with Path(path).open('rb') as source:
        data = source.read(maximum+1)
    if len(data) > maximum:
        raise ValueError(f'Input exceeds {maximum} bytes: {path}')
    return data


def document(path):
    return load_metadata(read(path, 16384))


def config(path=None):
    from ..policy import Config, load_config
    return load_config(read(path, 16384)) if path is not None else Config()


def script(path):
    """Small manifest points to raw model replies; replies never enter JSON."""
    values = document(path)
    if type(values) is not list or not 1 <= len(values) <= 512:
        raise ValueError('Script must list 1 through 512 role/file pairs')
    result, total = [], 0
    for row in values:
        if (type(row) is not dict or set(row) != {'role', 'file'}
                or row['role'] not in ('prover', 'reviewer', 'research') or type(row['file']) is not str):
            raise ValueError('Expected script entries with role and file')
        payload = read(Path(path).resolve().parent/row['file'], min(4*1024**2, 64*1024**2-total))
        total += len(payload)
        result.append((row['role'], payload))
    return tuple(result)


def write(destination, chunks):
    """Verify the entire stream before publishing; never overwrite a destination."""
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f'Output already exists: {destination}')
    fd, temporary = tempfile.mkstemp(prefix='.midas-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            for chunk in chunks:
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def package(application, destination, path, *, base=None, cancellation=None):
    from ..environment import PackageConfig, Dependency, attach
    from .preparation import index_sources, operate
    data = document(path)
    parent = Path(path).resolve().parent
    if type(data) is not dict:
        raise ValueError('Package configuration must be an object')
    if base is not None:
        if set(data) != {'name', 'path', 'module_paths', 'source_roots', 'prefixes'}:
            raise ValueError('Attachment requires name, path, module_paths, source_roots and prefixes')
        dependency = Dependency(data['name'], parent/data['path'], tuple(data['module_paths']), tuple(data['source_roots']))
        environment = application.environment(base)
        prepared, _ = operate(lambda context: attach(environment, destination, dependency,
            tuple(data['prefixes']), context,
            _before_publish=lambda attached: index_sources(attached, context)), cancellation=cancellation)
        return prepared
    if set(data) - {'version', 'toolchain', 'roots', 'project_inputs', 'search_paths', 'max_bytes', 'preflight_imports'}:
        raise ValueError('Unknown package configuration field')
    if not {'version', 'toolchain', 'roots', 'project_inputs', 'search_paths'} <= data.keys():
        raise ValueError('Package requires version, toolchain, roots, project_inputs and search_paths')
    if type(data['roots']) is not dict:
        raise ValueError('Package roots must map names to directories')
    settings = PackageConfig(data['version'], parent/data['toolchain'],
        tuple((name, parent/value) for name, value in data['roots'].items()),
        tuple(data['project_inputs']), tuple(data['search_paths']),
        data.get('max_bytes', 16*1024**3), tuple(data.get('preflight_imports', ())))
    return application.package(destination, settings, cancellation=cancellation)
