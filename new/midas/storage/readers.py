"""Eight idle read-only connections, checked out exclusively and reset at fork."""
import atexit
import os
import threading

_idle = []
_lock = threading.Lock()


def take(key):
    with _lock:
        for index in range(len(_idle)-1, -1, -1):
            if _idle[index][0] == key:
                return _idle.pop(index)[1]
    return None


def release(key, connection):
    if connection.in_transaction:
        connection.rollback()
    with _lock:
        _idle.append((key, connection))
        while len(_idle) > 8:
            _idle.pop(0)[1].close()


def clear():
    with _lock:
        for _, connection in _idle:
            connection.close()
        _idle.clear()


def child():
    global _lock
    for _, connection in _idle:
        connection.close()
    _idle.clear()
    _lock = threading.Lock()


def before():
    _lock.acquire()


def parent():
    _lock.release()


atexit.register(clear)
os.register_at_fork(before=before, after_in_parent=parent, after_in_child=child)
