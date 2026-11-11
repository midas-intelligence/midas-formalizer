"""Private transport for retained import parents; no checker or environment imports.

The supplied native recipe must use the bundled isolation runtime. Its protected
control FIFO reports completion only after all request descendants have exited.
"""
import ctypes
from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from uuid import uuid4

from ..domain import Termination
from ..domain.records import identifier, natural
from .isolation import LaunchSpec, Mount
from .process import (MIB, POLL_SECONDS, Outcome, ProcessResult, _Capture,
                      _ProcessTree, _metadata, _status_messages, _stop)

LINK_FLAGS = ('-Wl,--wrap=initialize_libuv',
              '-Wl,--wrap=_ZN4lean15event_loop_lockEPNS_12event_loop_tE')


def import_runtime_source():
    return Path(__file__).with_name('import_runtime.c').read_bytes()


def import_worker_platform_supported():
    """Host capability only; recipe owners admit toolchain/runtime compatibility."""
    return (platform.system() == 'Linux' and platform.machine() == 'x86_64'
            and ctypes.CDLL(None).syscall(444, 0, 0, 1) >= 6)


@dataclass(frozen=True, slots=True)
class ImportInput:
    path: str
    sha256: str
    byte_count: int

    def __post_init__(self):
        if (type(self.path) is not str or not self.path or len(self.path) > 1024
                or str(PurePosixPath(self.path)) != self.path or self.path.startswith('/')
                or any(p in ('.', '..', '.midas-control', '.midas-evidence') for p in self.path.split('/'))
                or '\\' in self.path or '\x00' in self.path):
            raise ValueError('Invalid import input path')
        identifier(self.sha256, '')
        natural(self.byte_count, 'Import input bytes')


@dataclass(frozen=True, slots=True)
class ImportRecipe:
    launch: LaunchSpec
    environment_id: str
    runtime_sha256: str
    source: str
    inputs: tuple[ImportInput, ...]

    def __post_init__(self):
        if not isinstance(self.launch, LaunchSpec):
            raise ValueError('Expected an isolated import launch')
        identifier(self.environment_id, 'env_')
        identifier(self.runtime_sha256, '')
        if (type(self.source) is not str or not self.source or '/' in self.source
                or any(c.isspace() for c in self.source) or len(self.source.encode()) > 1024):
            raise ValueError('Expected one bounded request filename')
        if (type(self.inputs) is not tuple or not 1 <= len(self.inputs) <= 256
                or any(not isinstance(i, ImportInput) for i in self.inputs)):
            raise ValueError('Expected 1 through 256 explicit import inputs')
        paths = {i.path for i in self.inputs}
        if len(paths) != len(self.inputs) or self.source not in paths:
            raise ValueError('Duplicate input or undeclared request source')
        if any(str(p) in paths for name in paths for p in PurePosixPath(name).parents):
            raise ValueError('Import file/directory collision')
        if sum(i.byte_count for i in self.inputs) > 64 * MIB:
            raise ValueError('Import inputs exceed 64 MiB')
        if any(m.target == '/midas-inputs' or m.target.startswith('/midas-inputs/') for m in self.launch.mounts):
            raise ValueError('Import transport owns the private input mount')

    def key(self, limits):
        return (self.launch, self.environment_id, self.runtime_sha256, self.source,
                tuple(i for i in self.inputs if i.path != self.source),
                limits.memory_mib, limits.evidence_bytes)


class _ImportWorker:
    def __init__(self, recipe, directory, limits):
        self.id = str(uuid4())
        self.key = recipe.key(limits)
        self.memory_mib = limits.memory_mib
        self.directory = Path(tempfile.mkdtemp(prefix='imports-', dir=directory))
        self.workspace = self.directory / 'inputs'
        self.workspace.mkdir()
        self.closed = False
        self.termination = Termination.TERMINATED
        self.process = self.tree = self.selector = None
        self.fds = []
        self.status = bytearray()
        self.copied = False
        try:
            for name in ('.midas-evidence', '.midas-control'):
                path = self.workspace / name
                os.mkfifo(path, 0o600)
                self.fds.append(os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC))
            read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
            self.fds.append(read_fd)
            try:
                spec = replace(recipe.launch, mounts=(*recipe.launch.mounts, Mount(str(self.workspace), '/midas-inputs')))
                command = spec.command(status_fd=write_fd, scratch_bytes=limits.evidence_bytes)
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                    pass_fds=(write_fd,), env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
            finally:
                os.close(write_fd)
            self.tree = _ProcessTree(self.process.pid)
            self.selector = selectors.DefaultSelector()
            for fd, kind in ((self.process.stdout, 0), (self.process.stderr, 1),
                             (read_fd, 2), (self.fds[0], 3), (self.fds[1], 4)):
                os.set_blocking(fd if isinstance(fd, int) else fd.fileno(), False)
                self.selector.register(fd, selectors.EVENT_READ, kind)
            os.set_blocking(self.process.stdin.fileno(), False)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.process is not None:
                self.termination = _stop(self.process, self.tree)
        finally:
            if self.tree is not None:
                self.tree.close()
            if self.process is not None:
                for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                    stream.close()
            if self.selector is not None:
                self.selector.close()
            for fd in self.fds:
                os.close(fd)
            shutil.rmtree(self.directory)

    def _copy(self, recipe, workspace, check):
        for item in recipe.inputs:
            if self.copied and item.path != recipe.source:
                continue  # Exact immutable dependency hashes are in the key.
            check()
            path = Path(workspace) / item.path
            # The caller's workspace is private and stable for the copy.
            if path.resolve(strict=True) != path.absolute():
                raise ValueError('Import input contains a symlink')
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as source:
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size != item.byte_count:
                    raise ValueError('Import input is not the declared regular file')
                output = self.workspace / item.path
                output.parent.mkdir(parents=True, exist_ok=True)
                digest, count = sha256(), 0
                with open(output, 'wb') as target:
                    while chunk := source.read(min(1024 * 1024, item.byte_count - count + 1)):
                        check()
                        count += len(chunk)
                        if count > item.byte_count:
                            raise ValueError('Import input grew during copy')
                        digest.update(chunk)
                        target.write(chunk)
                if count != item.byte_count or digest.hexdigest() != item.sha256:
                    raise ValueError('Import input differs from its hash')
        self.copied = True

    def run(self, recipe, workspace, limits, cancel):
        started = time.monotonic()
        out, ev = _Capture(limits.output_bytes), _Capture(limits.evidence_bytes)
        control = bytearray()
        marker = None
        peak_rss = peak_anon = None
        bound = forced = None
        detail = ''
        code = None
        deadline = started + limits.wall_ms / 1000

        def check():
            if cancel.is_set():
                raise InterruptedError('Import request cancelled')
            if time.monotonic() >= deadline:
                raise TimeoutError('Import wall budget exhausted')

        def consume(key, data):
            nonlocal forced, bound
            if key.data == 2:
                self.status.extend(data[:max(0, 16385 - len(self.status))])
                if len(self.status) > 16384:
                    raise ValueError('Oversized import bootstrap status')
            elif key.data == 4:
                control.extend(data[:max(0, 4097 - len(control))])
                if len(control) > 4096:
                    raise ValueError('Oversized import completion')
            elif key.data == 3:
                if not ev.append(0, data):
                    forced, bound = Outcome.RESOURCE_LIMIT, 'evidence'
            elif not out.append(key.data, data):
                forced, bound = Outcome.RESOURCE_LIMIT, 'output'

        try:
            key = recipe.key(limits)
            if self.closed or self.key[:-1] != key[:-1] or limits.evidence_bytes > self.key[-1]:
                raise ValueError('Retained worker differs from request inputs or limits')
            self._copy(recipe, workspace, check)
            check()
            request = ('/midas-inputs/' + recipe.source + '\n').encode()
            if os.write(self.process.stdin.fileno(), request) != len(request):
                raise ValueError('Incomplete import request write')
            next_sample = started
            while marker is None and forced is None:
                check()
                now = time.monotonic()
                if now >= next_sample:
                    rss, anon = self.tree.sample()
                    if rss is not None:
                        peak_rss, peak_anon = max(peak_rss or 0, rss), max(peak_anon or 0, anon)
                        if anon > limits.memory_mib * MIB:
                            forced, bound = Outcome.RESOURCE_LIMIT, 'memory'
                    next_sample = now + POLL_SECONDS
                for key, _ in self.selector.select(min(POLL_SECONDS, max(0, deadline - now))):
                    try:
                        data = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if data:
                        consume(key, data)
                    else:
                        self.selector.unregister(key.fileobj)
                if b'\n' in control:
                    prefix = b'MIDAS_WORKER_END '
                    if not control.startswith(prefix) or control.count(b'\n') != 1 or not control.endswith(b'\n'):
                        raise ValueError('Malformed import completion')
                    marker = json.loads(_metadata(control[len(prefix):], maximum=4096))
                    if (type(marker) is not dict or marker.get('path') != '/midas-inputs/' + recipe.source
                            or type(marker.get('exit')) is not int or not 0 <= marker['exit'] <= 255):
                        raise ValueError('Import completion differs from request')
                    code = marker['exit']
                if self.process.poll() is not None and marker is None:
                    forced = Outcome.CRASH
            if forced is None:
                # All request children are dead at the marker. Bounded draining
                # includes bytes queued before completion without waiting for EOF.
                for key in tuple(self.selector.get_map().values()):
                    if key.data == 4:
                        continue
                    for _ in range(1025):
                        check()
                        try:
                            data = os.read(key.fd, 65536)
                        except BlockingIOError:
                            break
                        if not data:
                            break
                        consume(key, data)
                        if forced is not None:
                            break
                    else:
                        forced, bound = Outcome.RESOURCE_LIMIT, 'output'
                messages = _status_messages(self.status)
                if not any(type(m.get('child-pid')) is int and m['child-pid'] > 0 for m in messages):
                    raise ValueError('Unconfirmed import namespace startup')
        except InterruptedError as error:
            forced, detail = Outcome.CANCELLED, str(error)
        except TimeoutError as error:
            forced, bound, detail = Outcome.RESOURCE_LIMIT, 'wall', str(error)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            forced, detail = Outcome.SETUP_FAILURE, str(error)[:4096]
        except BaseException:
            self.close()
            raise
        if forced is None and code == 128 + signal.SIGXCPU:
            forced, bound = Outcome.RESOURCE_LIMIT, 'cpu'
        outcome = forced or (Outcome.COMPLETED if code == 0 else Outcome.COMPILER_ERROR if code == 1
                             else Outcome.SETUP_FAILURE)
        if forced is not None or code not in (0, 1):
            out.truncated = [True, True]
            ev.truncated[0] = True
            self.close()
        return ProcessResult(outcome, code, int((time.monotonic() - started) * 1000),
                             peak_rss, peak_anon, bound, out.result(0), out.result(1), detail, ev.result(0), self.termination)
