"""Bounded compiler options from freshly evaluated, admitted Lake configurations."""
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import re
import tempfile

from ..domain.encoding import canonical
from .inventory import EnvironmentError, read_rows, safe_read
from .manifest import label
from .package import output
from ..source.context import module_name

MAGIC = b'MIDASOPT\x01'


def compiler_options(value):
    """Canonical typed pairs; enforce the bound before any JSON operation."""
    if type(value) is not tuple or len(value) > 64:
        raise EnvironmentError('Expected at most 64 compiler options')
    previous, size = '', 0
    for pair in value:
        if type(pair) is not tuple or len(pair) != 2:
            raise EnvironmentError('Invalid compiler option pair')
        key, setting = pair
        if (type(key) is not str or len(key) > 256 or key <= previous or
                re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', key) is None):
            raise EnvironmentError('Expected distinct sorted compiler option names')
        if type(setting) is str:
            if len(setting) > 1024 or '\x00' in setting:
                raise EnvironmentError('Compiler option string exceeds bound')
            # Worst-case JSON escaping, including non-ASCII code points.
            size += 12 * len(setting) + 2
        elif type(setting) is bool:
            size += 5
        elif type(setting) is int and 0 <= setting < 2**64:
            size += 20
        else:
            raise EnvironmentError('Invalid compiler option value')
        size += len(key) + 4
        previous = key
    if size > 12000:
        raise EnvironmentError('Compiler options exceed setup metadata bound')
    return value


def option_rows(options):
    for key, value in compiler_options(options):
        tag = b's' if type(value) is str else b'b' if type(value) is bool else b'n'
        text = value if type(value) is str else str(int(value))
        yield b'O', key.encode(), tag, text.encode()


def saved_options(value):
    if type(value) is not list or len(value) > 64 or any(type(p) is not list or len(p) != 2 for p in value):
        raise EnvironmentError('Invalid saved compiler options')
    return compiler_options(tuple(tuple(pair) for pair in value))


def module_setup(name, package, options=()):
    """Only module identity and typed options enter the small setup document."""
    module_name(name)
    if package is not None:
        label(package)
    compiler_options(options)
    return canonical(dict(name=name, package=package, isModule=False,
                          options=dict(options), importArts={}, dynlibs=[], plugins=[]))


def read_options(source, expected):
    result, pool, pairs, current = {}, {}, [], None

    def finish():
        if current is not None:
            values = compiler_options(tuple(sorted(pairs, key=lambda pair: pair[0])))
            result[current] = pool.setdefault(tuple(option_rows(values)), values)

    for row in read_rows(source, MAGIC):
        if len(row) == 3 and row[0] == b'M':
            finish()
            if len(row[1]) > 256 or len(row[2]) > 64:
                raise EnvironmentError('Configured module name exceeds bound')
            name, package = (part.decode() for part in row[1:])
            if name in result or expected.get(name) != package:
                raise EnvironmentError('Unexpected configured module')
            current, pairs = name, []
        elif len(row) == 4 and row[0] == b'O' and current is not None:
            _, key, tag, value = row
            if len(key) > 256 or len(value) > 4096 or len(pairs) >= 64:
                raise EnvironmentError('Compiler option record exceeds bound')
            if tag == b's':
                setting = value.decode()
            elif tag == b'b' and value in (b'0', b'1'):
                setting = value == b'1'
            elif tag == b'n' and re.fullmatch(b'0|[1-9][0-9]{0,19}', value):
                setting = int(value)
            else:
                raise EnvironmentError('Invalid typed compiler option')
            pairs.append((key.decode(), setting))
        else:
            raise EnvironmentError('Invalid compiler option record')
    finish()
    if result.keys() != expected.keys():
        raise EnvironmentError('Missing configured modules')
    return result


def configure_modules(environment, closure, modules, context):
    """Evaluate each package once, without resolving or building dependencies."""
    if modules.closure_id != closure.id:
        raise EnvironmentError('Module configuration differs from admitted closure')
    if not modules.modules:
        return modules
    environment.validate(hash_toolchain=True, fresh=True, check=context.check)
    pins = {pin.name: pin for pin in closure.pins}
    by_package = {name: [] for name in pins}
    for item in modules.modules:
        if item.package not in pins or item.lake_package != item.package:
            raise EnvironmentError('Module package differs from admitted closure')
        by_package[item.package].append(item)
    # Metadata is checked against the closure as well as the environment inventory.
    from .inventory import file_hash
    for package, path, digest in closure.metadata:
        if file_hash(environment.roots[package] / path, maximum=8*1024**2, check=context.check) != digest:
            raise EnvironmentError('Lake configuration differs from admitted closure')
    with tempfile.TemporaryDirectory(prefix='options-', dir=context.workspace) as temporary:
        workspace = Path(temporary)
        request = workspace / 'modules.tsv'
        with request.open('w', encoding='utf-8') as target:
            for package, items in by_package.items():
                if not items:
                    continue
                target.write('P\t' + package + '\t/' + package + '\t' + pins[package].config + '\n')
                for item in items:
                    fields = (item.name, '/' + package + '/' + item.source.path)
                    if any(any(c in field for c in '\t\r\n') for field in fields):
                        raise EnvironmentError('Unsupported control character in module path')
                    target.write('M\t' + '\t'.join(fields) + '\n')
            if target.tell() > 16*1024**2:
                raise EnvironmentError('Module configuration request exceeds bound')
        helper = Path(__file__).with_name('lean') / 'Options.lean'
        (workspace / 'Options.lean').write_bytes(safe_read(helper, 64*1024))
        recipe = environment.launch(('--load-dynlib=/toolchain/lib/lean/libLake_shared.so',
                                     '--run', '/build/Options.lean', '/build/modules.tsv',
                                     '/build/options.bin', '/build'), build=workspace)
        # Configurations are re-elaborated; only toolchain modules are importable.
        variables = tuple((key, '/toolchain/lib/lean' if key == 'LEAN_PATH' else value)
                          for key, value in recipe.variables)
        mounts = tuple(m for m in recipe.mounts if m.target in
                       ('/toolchain', '/runtime', '/build', *('/' + p for p in pins)))
        output(context.run_process(replace(recipe, variables=variables, mounts=mounts)))
        options = read_options(BytesIO(safe_read(workspace / 'options.bin', 64*1024**2)),
                               {m.name: m.lake_package for m in modules.modules})
    environment.validate(hash_toolchain=True, fresh=True, check=context.check)
    return replace(modules, modules=tuple(replace(m, lean_options=options[m.name]) for m in modules.modules))
