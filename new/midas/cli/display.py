"""One replaceable progress frame; a blocked sink cannot hold up the run."""
import queue
import threading
import time

from ..status import terminal


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
        while not self.closed.is_set() or not self.pending.empty():
            try:
                view = self.pending.get(timeout=.05)
            except queue.Empty:
                continue
            now = time.monotonic()
            text = terminal(view)
            if text == previous and now-written < 10:
                continue
            try:
                self.stream.write(text)
                self.stream.flush()
            except Exception:
                self.disabled.set()
                return
            previous, written = text, now

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.closed.set()
        self.thread.join(timeout=.1)
