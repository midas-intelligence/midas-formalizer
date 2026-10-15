"""Owned bidirectional subprocesses for trusted provider protocol clients.

This does not grant compiler capacity or launch untrusted model-generated code.
The caller uses a worker's deadline and cancellation, and awaits close before
returning. A process group is reaped after protocol completion or failure.
"""
import asyncio
from hashlib import file_digest
from pathlib import Path
import os
import signal

from ..domain import Termination
from .process import _ProcessTree


class ProtocolProcess:
    def __init__(self, command, *, executable_sha256, cwd, environment, frame_bytes, shutdown_ms):
        self.command = tuple(command)
        self.executable_sha256 = executable_sha256
        self.cwd, self.environment = str(cwd), dict(environment)
        self.frame_bytes, self.shutdown_ms = frame_bytes, shutdown_ms
        self.process = None
        self.tree = self.monitor = None
        self.observed = True
        self.termination = Termination.TERMINATED

    async def start(self, arguments):
        if self.process is not None or not self.command or not Path(self.command[0]).is_absolute():
            raise ValueError('Expected one pinned absolute protocol executable')
        with open(self.command[0], 'rb') as source:
            if file_digest(source, 'sha256').hexdigest() != self.executable_sha256:
                raise ValueError('Protocol executable changed')
        launch = asyncio.create_task(asyncio.create_subprocess_exec(*self.command, *arguments,
            cwd=self.cwd, env=self.environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=self.frame_bytes+1))
        try:
            self.process = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Own a process created concurrently with cancellation before unwinding.
            self.process = await launch
            self.termination = Termination.UNKNOWN
            self.observed = False
            raise
        self.termination = Termination.UNKNOWN
        try:
            self.tree = _ProcessTree(self.process.pid)
        except (OSError, ValueError):
            self.observed = False
            raise
        async def monitor():
            try:
                while True:
                    self.tree.sample()
                    await asyncio.sleep(.02)
            except (OSError, ValueError):
                self.observed = False
                self.tree.kill()
        self.monitor = asyncio.create_task(monitor())
        return self.process

    async def close(self):
        if self.process is None:
            return
        if self.monitor is not None:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.process.wait(), self.shutdown_ms / 1000)
        except asyncio.TimeoutError:
            pass
        # Terminate observed descendants and await pidfd-confirmed exit, including
        # children which started a new process group. Keep ownership while waiting.
        try:
            if self.tree is not None:
                try:
                    self.tree.sample(fresh=True)
                except (OSError, ValueError):
                    self.observed = False
                self.tree.kill()
                while self.tree.pins:
                    self.tree._retire_exited()
                    if self.tree.pins:
                        await asyncio.sleep(.02)
        except (OSError, ValueError):
            self.observed = False
        finally:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
            if self.tree is not None:
                self.tree.close()
        self.termination = Termination.TERMINATED if self.observed else Termination.UNKNOWN
