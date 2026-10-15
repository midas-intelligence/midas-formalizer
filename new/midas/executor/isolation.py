"""Explicit namespace recipes. Environment/checker owners admit the input bytes.

No package discovery, compiler source assembly, host fallback or implicit /usr.
The installed launcher is checked against its admitted hash at each launch.
"""
from dataclasses import dataclass, replace
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath
import time

from ..domain import ResourceLimits
from ..domain.records import identifier, instance
from .process import Outcome, _run_process, failure

PROFILE = 'linux-bwrap-v1'


def _absolute(value):
    if (type(value) is not str or not value.startswith('/') or value.startswith('//') or '\x00' in value
            or str(PurePosixPath(value)) != value or '..' in value.split('/')):
        raise ValueError('Expected a canonical absolute path')
    return value


@dataclass(frozen=True, slots=True)
class Mount:
    source: str
    target: str
    writable: bool = False

    def __post_init__(self):
        _absolute(self.source)
        _absolute(self.target)
        if type(self.writable) is not bool:
            raise ValueError('Mount writable flag must be boolean')
        if self.target == '/' or any(self.target == p or self.target.startswith(p + '/')
                                     for p in ('/proc', '/dev', '/tmp')):
            raise ValueError('Mount would replace an isolation-owned path')


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Trusted, frozen recipe; construction validates shape, not input provenance.

    Owners authorize every root, executable and writable output directory. Large
    inputs are files in these roots; command/environment metadata is capped.
    """
    argv: tuple[str, ...]
    mounts: tuple[Mount, ...]
    launcher: str
    launcher_sha256: str
    variables: tuple[tuple[str, str], ...] = ()
    cwd: str = '/project'
    profile: str = PROFILE

    def __post_init__(self):
        _absolute(self.launcher)
        _absolute(self.cwd)
        identifier(self.launcher_sha256, '')
        if self.profile != PROFILE:
            raise ValueError('Unsupported isolation profile')
        if type(self.argv) is not tuple or not self.argv or len(self.argv) > 256:
            raise ValueError('Expected bounded immutable argv')
        _absolute(self.argv[0])
        if (type(self.mounts) is not tuple or len(self.mounts) > 256
                or any(not isinstance(m, Mount) for m in self.mounts)):
            raise ValueError('Expected bounded immutable mounts')
        targets = {m.target for m in self.mounts}
        if len(targets) != len(self.mounts):
            raise ValueError('Duplicate mount target')
        if any(str(parent) in targets for target in targets for parent in PurePosixPath(target).parents):
            raise ValueError('Overlapping mount targets')
        if (type(self.variables) is not tuple or len(self.variables) > 128
                or any(type(v) is not tuple or len(v) != 2 for v in self.variables)):
            raise ValueError('Expected bounded immutable environment variables')
        values = (*self.argv, *(s for m in self.mounts for s in (m.source, m.target)),
                  *(s for pair in self.variables for s in pair), self.launcher, self.cwd)
        total = 0
        for value in values:
            if type(value) is not str or '\x00' in value or len(value) > 16384:
                raise ValueError('Invalid launch metadata')
            total += len(value.encode('utf-8'))
        if total > 16384:
            raise ValueError('Launch metadata exceeds 16 KiB')
        names = set()
        for key, _ in self.variables:
            if not key or '=' in key or key in names:
                raise ValueError('Invalid or duplicate environment variable')
            names.add(key)

    def command(self, *, status_fd, scratch_bytes):
        digest = sha256()
        with open(self.launcher, 'rb') as source:
            while chunk := source.read(65536):
                digest.update(chunk)
        if digest.hexdigest() != self.launcher_sha256:
            raise ValueError('Installed bubblewrap differs from admitted launcher')
        command = [self.launcher, '--unshare-all', '--die-with-parent', '--new-session',
                   '--cap-drop', 'ALL', '--clearenv', '--proc', '/proc', '--dev', '/dev',
                   '--size', str(scratch_bytes), '--tmpfs', '/tmp',
                   '--json-status-fd', str(status_fd)]
        for mount in self.mounts:
            command.extend(('--bind' if mount.writable else '--ro-bind', mount.source, mount.target))
        if not any(self.cwd == m.target or self.cwd.startswith(m.target + '/') for m in self.mounts):
            command.extend(('--dir', self.cwd))
        for key, value in self.variables:
            command.extend(('--setenv', key, value))
        return (*command, '--chdir', self.cwd, '--', *self.argv)


def _launch(spec, limits, *, cancel=None, evidence_path=None):
    instance(spec, LaunchSpec)
    instance(limits, ResourceLimits)
    read_fd = write_fd = evidence_fd = None
    made_fifo = False
    started = time.monotonic()
    try:
        if cancel is not None and cancel.is_set():
            return failure(Outcome.CANCELLED, 'Cancelled before launch')
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        if evidence_path is not None:
            # Must be a new FIFO in the caller's private workspace.
            os.mkfifo(evidence_path, 0o600)
            made_fifo = True
            evidence_fd = os.open(evidence_path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        command = spec.command(status_fd=write_fd, scratch_bytes=limits.evidence_bytes)
        remaining = limits.wall_ms - int((time.monotonic() - started) * 1000)
        if remaining <= 0:
            raise TimeoutError('Wall budget exhausted during namespace preparation')
        limits = replace(limits, wall_ms=remaining)
    except (OSError, ValueError) as error:
        for fd in (read_fd, write_fd, evidence_fd):
            if fd is not None:
                os.close(fd)
        if made_fifo:
            Path(evidence_path).unlink()
        return failure(Outcome.RESOURCE_LIMIT if isinstance(error, TimeoutError) else Outcome.SETUP_FAILURE,
                       str(error), limit='wall' if isinstance(error, TimeoutError) else None,
                       elapsed_ms=int((time.monotonic() - started) * 1000))
    try:
        result = _run_process(command, limits, cancel=cancel,
                              status_pipe=(read_fd, write_fd), evidence_fd=evidence_fd)
        return replace(result, elapsed_ms=int((time.monotonic() - started) * 1000))
    finally:
        if evidence_path is not None:
            Path(evidence_path).unlink()
