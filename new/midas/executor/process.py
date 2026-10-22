"""Linux process supervision shared by fresh and retained workers.

Memory is sampled anonymous RSS across pinned descendants, as in v2; this is
not a hard cgroup ceiling. PID namespaces are the boundary for untrusted trees.
"""
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import select
import selectors
import signal
import subprocess
import threading
import time

from ..domain import ResourceLimits, Termination

MIB = 1024 * 1024
POLL_SECONDS = 0.02
STATUS_BYTES = 16384


class Outcome(str, Enum):
    COMPLETED = 'completed'
    COMPILER_ERROR = 'compiler_error'
    RESOURCE_LIMIT = 'resource_limit'
    SETUP_FAILURE = 'setup_failure'
    CRASH = 'crash'
    CANCELLED = 'cancelled'


@dataclass(frozen=True, slots=True)
class Diagnostic:
    data: bytes
    observed_bytes: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProcessResult:
    outcome: Outcome
    returncode: int | None
    elapsed_ms: int
    peak_rss_bytes: int | None
    peak_anonymous_bytes: int | None
    limit: str | None
    stdout: Diagnostic
    stderr: Diagnostic
    detail: str = ''
    evidence: Diagnostic | None = None
    termination: Termination = Termination.TERMINATED

    @property
    def signal(self):
        return -self.returncode if self.returncode is not None and self.returncode < 0 else None


class _Capture:
    def __init__(self, maximum):
        self.remaining = maximum
        self.data = [bytearray(), bytearray()]
        self.observed = [0, 0]
        self.truncated = [False, False]

    def append(self, stream, chunk):
        size = len(chunk)
        self.observed[stream] += size
        kept = min(size, self.remaining)
        self.data[stream].extend(chunk if kept == size else memoryview(chunk)[:kept])
        self.remaining -= kept
        if kept != size:
            self.truncated[stream] = True
            return False
        return True

    def result(self, stream):
        return Diagnostic(bytes(self.data[stream]), self.observed[stream], self.truncated[stream])


def failure(outcome, detail, *, elapsed_ms=0, limit=None):
    empty = Diagnostic(b'', 0, False)
    return ProcessResult(outcome, None, elapsed_ms, None, None, limit, empty, empty, detail[:4096])


def _process_info(pid):
    try:
        # Each scan reads every host process. Use one bounded raw read without
        # allocating a Python file object; retain no descriptors between reads.
        # comm itself need not be valid UTF-8.
        fd = os.open(f'/proc/{pid}/stat', os.O_RDONLY | os.O_CLOEXEC)
        try:
            record = os.read(fd, 16384)
        finally:
            os.close(fd)
        tail = record.rsplit(b')', 1)[1].split(None, 20)
        return (int(tail[1]), int(tail[19]), tail[0].decode('ascii'))  # parent, start ticks, state
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (ValueError, IndexError) as error:
        raise OSError('Malformed /proc process identity') from error


def _exiting_without_live_threads(pid, ticks):
    """PF_EXITING tasks can have no mm long before their zombie state appears.

    Do not exempt a thread-group leader while any sibling can still run user
    code. Failure to inspect this transition keeps normal fail-closed handling.
    """
    def stat(path):
        with open(path, 'r') as source:
            tail = source.read(16384).rsplit(')', 1)[1].split()
        return int(tail[19]), int(tail[6])
    try:
        current, flags = stat(f'/proc/{pid}/stat')
        if current != ticks or not flags & 4:  # PF_EXITING, include/linux/sched.h
            return False
        for task in Path(f'/proc/{pid}/task').iterdir():
            try:
                _, task_flags = stat(str(task / 'stat'))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if not task_flags & 4:
                return False
        current, flags = stat(f'/proc/{pid}/stat')
        return current == ticks and bool(flags & 4)
    except (OSError, ValueError, IndexError):
        return False


class _Discovery:
    """Share only process discovery; each monitor rechecks identity and memory.

    A snapshot is reusable for at most one polling interval from scan START.
    Slow scans therefore cannot add another interval of staleness. Fresh reads
    for initial admission and cleanup bypass reuse. Never shared across forks.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.started = float('-inf')
        self.value = None

    def snapshot(self, *, fresh=False):
        with self.lock:
            now = self.clock()
            if not fresh and self.value is not None and 0 <= now - self.started < POLL_SECONDS:
                return self.value
            # Never serve the previous scan after an incomplete/failed refresh.
            self.value = None
            infos, children = {}, {}
            with os.scandir('/proc') as entries:
                for entry in entries:
                    if entry.name.isdecimal():
                        pid = int(entry.name)
                        info = _process_info(pid)
                        if info:
                            infos[pid] = info
                            children.setdefault(info[0], []).append(pid)
            self.started, self.value = now, (infos, children)
            return self.value


_DISCOVERY = _Discovery()


def _reset_discovery():
    global _DISCOVERY
    _DISCOVERY = _Discovery()


os.register_at_fork(after_in_child=_reset_discovery)


class _ProcessTree:
    def __init__(self, pid):
        self.pins = {}  # pid -> (start ticks, pidfd)
        self.memory_misses = {}  # monotonic start of a bounded 20 ms observation gap
        self.sampled = False
        self.pin(pid)

    def pin(self, pid, expected_ticks=None):
        info = _process_info(pid)
        if info is None or (expected_ticks is not None and info[1] != expected_ticks):
            return
        if (pid in self.pins and self.pins[pid][0] == info[1]):
            return
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            return
        try:
            after = _process_info(pid)
        except BaseException:
            os.close(fd)
            raise
        if after is None or after[1] != info[1]:
            os.close(fd)
            return
        old = self.pins.pop(pid, None)
        if old:
            os.close(old[1])
        self.pins[pid] = (info[1], fd)

    def _retire_exited(self):
        # pidfd readiness confirms the whole thread group has exited. A zombie
        # leader in /proc alone does not establish that its threads are gone.
        poller = select.poll()
        identities = {}
        for pid, (ticks, fd) in self.pins.items():
            poller.register(fd, select.POLLIN)
            identities[fd] = (pid, ticks)
        retired = set()
        for fd, events in poller.poll(0):
            if events & (select.POLLERR | select.POLLNVAL):
                raise OSError('Cannot observe pinned process exit')
            if events & (select.POLLIN | select.POLLHUP):
                pid, ticks = identities[fd]
                os.close(fd)
                del self.pins[pid]
                self.memory_misses.pop((pid, ticks), None)
                retired.add((pid, ticks))
        return retired

    def sample(self, *, fresh=False):
        infos, children = _DISCOVERY.snapshot(fresh=fresh or not getattr(self, 'sampled', False))
        self.sampled = True
        pending = [pid for pid, (ticks, _) in self.pins.items()
                   if pid in infos and infos[pid][1] == ticks]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid not in seen:
                seen.add(pid)
                pending.extend(children.get(pid, ()))
        # Discover descendants before retiring their parents, and free exited
        # handles before opening new ones. Surviving reparented pins remain roots.
        retired = self._retire_exited()
        rss = anonymous = 0
        measured = False
        for pid in seen:
            if (pid, infos[pid][1]) in retired:
                continue  # Do not reopen an exited process from a cached scan.
            self.pin(pid, expected_ticks=infos[pid][1])
            if pid not in self.pins or self.pins[pid][0] != infos[pid][1] or infos[pid][2] == 'Z':
                continue
            try:
                with open(f'/proc/{pid}/status', 'r') as source:
                    values = dict(line.split(':', 1) for line in source.read(16384).splitlines() if ':' in line)
                # Missing observations cannot quietly count as zero memory.
                process_rss = int(values['VmRSS'].split()[0]) * 1024
                process_anon = int(values['RssAnon'].split()[0]) * 1024
            except (FileNotFoundError, ProcessLookupError):
                continue
            except (KeyError, ValueError) as error:
                current = _process_info(pid)
                if current is None or current[1] != infos[pid][1] or current[2] in ('Z', 'X', 'x'):
                    continue
                if _exiting_without_live_threads(pid, current[1]):
                    continue
                identity = (pid, current[1])
                now = time.monotonic()
                first = self.memory_misses.setdefault(identity, now)
                if now - first < POLL_SECONDS:
                    # A busy output pipe can cause many samples within 20 ms.
                    # Bound the unknown interval by time, not iteration count.
                    continue
                raise OSError('Cannot observe compiler descendant memory: ' + values.get('Name', '').strip()[:80] + ' state=' + current[2] + ' missing=' + str(error)[:80]) from error
            if _process_info(pid) != infos[pid]:
                # State changes are normal; only identity changes invalidate a sample.
                current = _process_info(pid)
                if current is None or current[1] != infos[pid][1]:
                    continue
            self.memory_misses.pop((pid, infos[pid][1]), None)
            rss += process_rss
            anonymous += process_anon
            measured = True
        # Available measurements remain a lower bound even if another exiting
        # process had no sample. Do not let child churn hide a measured parent.
        return (rss, anonymous) if measured else (None, None)

    def kill(self):
        for _, fd in reversed(tuple(self.pins.values())):
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        for _, fd in self.pins.values():
            os.close(fd)
        self.pins.clear()
        self.memory_misses.clear()

    def terminate(self):
        """Keep ownership until every observed thread group has actually exited."""
        if hasattr(self, '_terminated'):
            return self._terminated
        observed = True
        try:
            self.sample(fresh=True)
        except (OSError, ValueError):
            observed = False
        self.kill()
        while self.pins:
            self._retire_exited()
            if self.pins:
                poller = select.poll()
                for _, fd in self.pins.values():
                    poller.register(fd, select.POLLIN)
                poller.poll(20)
        self._terminated = observed
        return observed


def _stop(process, tree):
    """Best-effort discovery failure never skips killing/waiting for the leader."""
    observed = True
    if tree is not None:
        try:
            observed = tree.terminate()
        except (OSError, ValueError):
            observed = False
    try:
        if process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()
    return Termination.TERMINATED if observed else Termination.UNKNOWN


def _metadata(data, *, maximum=STATUS_BYTES):
    """Only small private control messages use JSON; payload streams never do."""
    if len(data) > maximum:
        raise ValueError('Oversized control message')
    depth = 0
    quoted = escaped = False
    for byte in data:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            if depth > 8:
                raise ValueError('Deep control message')
        elif byte in (93, 125):
            depth -= 1
    return data.decode('utf-8')


def _status_messages(data):
    decoder = json.JSONDecoder()
    value = _metadata(data)
    result = []
    offset = 0
    while offset < len(value):
        if value[offset].isspace():
            offset += 1
            continue
        item, offset = decoder.raw_decode(value, offset)
        if type(item) is not dict:
            raise ValueError('Invalid bubblewrap status')
        result.append(item)
    return result


def _run_process(command, limits, *, cancel=None, status_pipe=None, env=None,
                 evidence_fd=None, cwd=None):
    """Private trusted-argv boundary. Ownership of all supplied FDs transfers here.

    Public execution always constructs a namespace launch in isolation.py.
    Raw argv also runs the fixed trusted search worker, never a compiler fallback.
    """
    if not isinstance(limits, ResourceLimits):
        raise ValueError('Expected ResourceLimits')
    started = time.monotonic()
    capture, evidence = _Capture(limits.output_bytes), _Capture(limits.evidence_bytes)
    process = tree = None
    read_fd, write_fd = status_pipe if status_pipe is not None else (None, None)
    selector = selectors.DefaultSelector()
    status = bytearray()
    forced = limit = code = None
    detail = ''
    peak_rss = peak_anon = None
    termination = Termination.TERMINATED
    outcome = Outcome.SETUP_FAILURE
    try:
        if cancel is not None and cancel.is_set():
            return failure(Outcome.CANCELLED, 'Cancelled before launch')
        if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
            raise OSError('Linux pidfd support is required')
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            env=env if env is not None else {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'},
            pass_fds=() if write_fd is None else (write_fd,), cwd=cwd)
        if write_fd is not None:
            os.close(write_fd)
            write_fd = None
        tree = _ProcessTree(process.pid)
        for stream, kind in ((process.stdout, 0), (process.stderr, 1),
                             (read_fd, 2), (evidence_fd, 3)):
            if stream is not None:
                os.set_blocking(stream if isinstance(stream, int) else stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kind)
        deadline, next_sample = started + limits.wall_ms / 1000, started
        stopped = False
        drain_deadline = None
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if forced is None:
                if cancel is not None and cancel.is_set():
                    forced, detail = Outcome.CANCELLED, 'Cancellation requested'
                elif now >= deadline:
                    forced, limit = Outcome.RESOURCE_LIMIT, 'wall'
                elif now >= next_sample:
                    rss, anon = tree.sample()
                    if rss is not None:
                        peak_rss, peak_anon = max(peak_rss or 0, rss), max(peak_anon or 0, anon)
                        if anon > limits.memory_mib * MIB:
                            forced, limit = Outcome.RESOURCE_LIMIT, 'memory'
                    next_sample = now + POLL_SECONDS
            if (forced is not None or process.poll() is not None) and not stopped:
                # Kill descendants even when the leader exits with pipes open.
                if _stop(process, tree) != Termination.TERMINATED:
                    termination = Termination.UNKNOWN
                stopped = True
                drain_deadline = time.monotonic() + 2
            if stopped and time.monotonic() >= drain_deadline:
                for key in tuple(selector.get_map().values()):
                    if key.data < 2:
                        capture.truncated[key.data] = True
                    elif key.data == 3:
                        evidence.truncated[0] = True
                    selector.unregister(key.fileobj)
                break
            ready = selector.select(0 if stopped else max(0, min(POLL_SECONDS, deadline - now)))
            for key, _ in ready:
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                elif key.data == 2:
                    status.extend(chunk[:max(0, STATUS_BYTES + 1 - len(status))])
                    if len(status) > STATUS_BYTES:
                        forced, detail = Outcome.SETUP_FAILURE, 'Oversized bubblewrap status'
                elif key.data == 3:
                    if not evidence.append(0, chunk) and forced is None:
                        forced, limit = Outcome.RESOURCE_LIMIT, 'evidence'
                elif not capture.append(key.data, chunk) and forced is None:
                    forced, limit = Outcome.RESOURCE_LIMIT, 'output'
            if stopped and not ready and evidence_fd is not None:
                # The supervisor holds the FIFO open, so no EOF is forthcoming.
                try:
                    selector.unregister(evidence_fd)
                except KeyError:
                    pass
        code = process.wait()
        if forced is not None:
            outcome = forced
        elif status_pipe is not None:
            messages = _status_messages(status)
            launched = any(type(m.get('child-pid')) is int and m['child-pid'] > 0 for m in messages)
            exits = [m['exit-code'] for m in messages if type(m.get('exit-code')) is int]
            if not launched or exits != [code]:
                outcome, detail = Outcome.SETUP_FAILURE, 'Incomplete or inconsistent bubblewrap status'
            else:
                outcome = (Outcome.COMPLETED if code == 0 else Outcome.COMPILER_ERROR if code == 1
                           else Outcome.CRASH if code < 0 or code >= 128 else Outcome.SETUP_FAILURE)
        else:
            outcome = Outcome.COMPLETED if code == 0 else Outcome.CRASH if code < 0 else Outcome.COMPILER_ERROR
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        outcome, detail = Outcome.SETUP_FAILURE, str(error)[:4096]
        for key in selector.get_map().values():
            if key.data < 2:
                capture.truncated[key.data] = True
            elif key.data == 3:
                evidence.truncated[0] = True
    finally:
        try:
            if process is not None:
                if _stop(process, tree) != Termination.TERMINATED:
                    termination = Termination.UNKNOWN
                code = process.returncode
        finally:
            if process is not None:
                process.stdout.close()
                process.stderr.close()
            selector.close()
            if tree is not None:
                tree.close()
            for fd in (read_fd, write_fd, evidence_fd):
                if fd is not None:
                    os.close(fd)
    return ProcessResult(outcome, code, int((time.monotonic() - started) * 1000),
                         peak_rss, peak_anon, limit, capture.result(0), capture.result(1), detail,
                         evidence.result(0) if evidence_fd is not None else None, termination)
