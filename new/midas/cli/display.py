"""One replaceable progress frame; a blocked sink cannot hold up the run."""
import os
import queue
import threading
import time
import unicodedata

from ..status import terminal


def erase(stream, frame):
    if not frame:
        return ''
    try:
        columns, lines = os.get_terminal_size(stream.fileno())
    except (AttributeError, OSError, ValueError):
        columns, lines = 80, 24
    columns = max(1, columns)
    rows = 0
    for line in frame.splitlines():
        rows += 1
        column = 0
        for character in line:
            width = (0 if unicodedata.combining(character) else
                     2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1)
            if width and column + width > columns:
                rows += 1
                column = 0
            column += width
    # Include automatic wrapping, but never move beyond the visible screen.
    return f'\x1b[{min(rows, max(1, lines - 1))}F\x1b[J'


class Display:
    def __init__(self, stream):
        self.stream = stream
        self.pending = queue.Queue(maxsize=1)
        self.closed = threading.Event()
        self.disabled = threading.Event()
        self.thread = threading.Thread(target=self.render, name='midas-display', daemon=True)

    def publish(self, view):
        if self.closed.is_set() or self.disabled.is_set():
            return
        try:
            self.pending.get_nowait()
        except queue.Empty:
            pass
        try:
            self.pending.put_nowait(view)
        except queue.Full:
            pass

    def render(self):
        previous, written = None, 0
        frame = ''
        try:
            interactive = self.stream.isatty()
        except (AttributeError, OSError, ValueError):
            interactive = False
        while not self.closed.is_set() or not self.pending.empty():
            try:
                view = self.pending.get(timeout=.05)
            except queue.Empty:
                continue
            now = time.monotonic()
            text = terminal(view)
            # The frame starts with time: elapsed/limit; only elapsed is volatile.
            key = text.partition('/')[2]
            if key == previous and now-written < 10:
                continue
            try:
                self.stream.write((erase(self.stream, frame) if interactive and key == previous else '') + text)
                self.stream.flush()
            except Exception:
                self.disabled.set()
                return
            previous, written = key, now
            frame = text
        if interactive and frame:
            try:
                self.stream.write(erase(self.stream, frame))
                self.stream.flush()
            except Exception:
                self.disabled.set()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.closed.set()
        self.thread.join(timeout=.1)
