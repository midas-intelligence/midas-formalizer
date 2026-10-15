"""Bounded, process-local memoization of pure admissions over immutable inputs.

Successful values stay private to their API. A caller returning a mutable value
must detach it. Concurrent builds of one key are coalesced; failed or retired
builds are never admitted. The environment is retained once outside entry bytes.
"""

import os
import sys
import threading

from collections import OrderedDict
from dataclasses import fields, is_dataclass
from weakref import WeakSet


_MEMOS = WeakSet()


def _after_fork():
    for memo in tuple(_MEMOS):
        memo._reset_after_fork()


os.register_at_fork(after_in_child=_after_fork)


def _size(value, seen):
    pending = [value]
    total = 0
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            pending.extend(item)
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
            if hasattr(item, '__dict__'):
                pending.append(item.__dict__)
    return total


class Memo:
    def __init__(self, max_entries=128, max_bytes=16 * 1024**2):
        if any(type(v) is not int or v < 0 for v in (max_entries, max_bytes)):
            raise ValueError('Cache bounds must be nonnegative integers')
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self._reset_after_fork()
        _MEMOS.add(self)

    def _reset_after_fork(self):
        self.lock = threading.Lock()
        self.environment = None
        self.entries = OrderedDict()
        self.bytes = 0
        self.pending = {}

    def reset(self):
        with self.lock:
            self.environment = None
            self.entries.clear()
            self.bytes = 0
            self.pending = {}

    def get(self, environment, key, build):
        if type(environment) is not bytes or len(environment) > 64 * 1024**2:
            return build()
        try:
            hash(key)
        except TypeError:
            return build()
        while True:
            with self.lock:
                if self.environment != environment:
                    self.environment = environment
                    self.entries.clear()
                    self.bytes = 0
                    self.pending = {}
                found = self.entries.get(key)
                if found is not None:
                    self.entries.move_to_end(key)
                    return found[0]
                pending = self.pending
                ready = pending.get(key)
                if ready is None:
                    ready = pending[key] = threading.Event()
                    retained_environment = self.environment
                    break
            ready.wait()
        try:
            value = build()
            size = _size((key, value), {id(retained_environment)})
            with self.lock:
                if self.pending is pending and self.max_entries > 0 and size <= self.max_bytes:
                    while self.entries and (len(self.entries) >= self.max_entries
                                            or self.bytes + size > self.max_bytes):
                        _, (_, removed) = self.entries.popitem(last=False)
                        self.bytes -= removed
                    self.entries[key] = value, size
                    self.bytes += size
            return value
        finally:
            with self.lock:
                pending.pop(key, None)
                ready.set()
