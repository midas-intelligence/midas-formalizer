"""Signals and same-user, session-fenced cooperative stop IPC."""
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import signal
import socket
import struct
import threading
import time


def endpoint(directory, session):
    key = f'{Path(directory).resolve()}:{session.run_id}:{session.session_id}:{session.epoch}'
    return '\0midas-stop-'+sha256(key.encode()).hexdigest()


def message(session):
    return f'STOP {session.run_id} {session.session_id} {session.epoch}\n'.encode('ascii')


def same_user(peer):
    return struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1] == os.getuid()


def line(peer, timeout):
    deadline, data = time.monotonic()+timeout, bytearray()
    while not data.endswith(b'\n'):
        remaining = deadline-time.monotonic()
        if remaining <= 0 or len(data) >= 128:
            raise ValueError('Invalid or incomplete stop message')
        peer.settimeout(remaining)
        chunk = peer.recv(128-len(data))
        if not chunk:
            raise ValueError('Incomplete stop message')
        data.extend(chunk)
    return bytes(data)


class Signals:
    def __init__(self):
        self.cancelled = threading.Event()
        self.callback = None
        self.previous = {}

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                self.previous[sig] = signal.signal(sig, self.handle)
        return self

    def handle(self, *_):
        self.cancelled.set()
        if self.callback is not None:
            self.callback()

    def __exit__(self, *_):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


@contextmanager
def listen(run, signals):
    session, directory = run.writer.session, run.writer.directory
    shutdown = threading.Event()
    expected = message(session)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    thread = None
    try:
        listener.bind(endpoint(directory, session))
        listener.listen(4)
        listener.settimeout(.1)
        def serve():
            while not shutdown.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with peer:
                    try:
                        if not same_user(peer) or line(peer, .5) != expected:
                            peer.sendall(b'REJECTED\n')
                            continue
                        run.request_stop()
                        peer.sendall(b'ACCEPTED\n')
                    except (ValueError, OSError):
                        pass
        signals.callback = run.request_stop
        if signals.cancelled.is_set():
            run.request_stop()
        thread = threading.Thread(target=serve, name='midas-stop', daemon=True)
        thread.start()
        yield
    finally:
        signals.callback = None
        shutdown.set()
        listener.close()
        if thread is not None:
            thread.join(timeout=.7)


def stop(directory, *, timeout=5):
    from ..application.inspection import session as current_session
    session = current_session(directory)
    if session is None:
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(timeout)
        try:
            peer.connect(endpoint(directory, session))
            if not same_user(peer):
                raise ValueError('Stop endpoint belongs to another user')
            peer.sendall(message(session))
            if line(peer, timeout) != b'ACCEPTED\n':
                raise ValueError('Controller rejected the stop request')
        except OSError as error:
            raise ValueError('No reachable controller for the recorded session; resume the run to recover it') from error
    return True
