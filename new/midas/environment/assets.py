"""Literal Lean include_str inputs, admitted separately from compiled objects."""
from hashlib import sha1, sha256
import posixpath
import re

from ..source.names import code_only
from .inventory import EnvironmentError, File, relative, safe_read

_INCLUDE = re.compile(r'(?<![\w.])include_str\b')
_PATH = re.compile(r'\s*("[^"\\\n]*"(?:\s*/\s*"[^"\\\n]*")*)')


def included_paths(data):
    """Recognize literal paths and literal / chains; Lean evaluates other forms."""
    if b'include_str' not in data:
        return ()
    source = data.decode('utf-8')
    result = []
    for token in _INCLUDE.finditer(code_only(source)):
        match = _PATH.match(source, token.end())
        if match is None:
            raise EnvironmentError('Source replay requires literal include_str paths')
        parts = re.findall(r'"([^"\\\n]*)"', match[1])
        if any(not part or part.startswith('/') or '\x00' in part for part in parts):
            raise EnvironmentError('Invalid include_str path')
        path = '/'.join(parts)
        if path not in result:
            result.append(path)
    return tuple(result)


def source_assets(root, source, name, listing, cache):
    paths = included_paths(safe_read(root / source.path, source.size))
    result = []
    for path in paths:
        original = relative(posixpath.normpath(posixpath.join(posixpath.dirname(source.path), path)))
        target = relative(posixpath.normpath(posixpath.join(name.replace('.', '/').rsplit('/', 1)[0]
                          if '.' in name else '', path)))
        if original not in cache:
            committed = listing(original)
            if original not in committed:
                raise EnvironmentError('Unrecorded source asset: ' + original)
            data = safe_read(root / original, 64 * 1024**2)
            executable = bool((root / original).stat().st_mode & 0o111)
            digest = sha1(b'blob ' + str(len(data)).encode() + b'\0', usedforsecurity=False)
            digest.update(data)
            if committed[original] != (executable, digest.hexdigest()):
                raise EnvironmentError('Source asset differs from committed object: ' + original)
            cache[original] = File(original, len(data), executable, sha256(data).hexdigest())
        result.append((target, cache[original]))
    return tuple(result)
