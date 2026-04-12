from __future__ import annotations

import itertools
import sys
import threading
import time
from contextlib import AbstractContextManager


class Spinner(AbstractContextManager["Spinner"]):
    def __init__(self, message: str, *, enabled: bool = True) -> None:
        self.message = message
        self.stream = sys.stderr
        self.enabled = enabled and self.stream.isatty()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])

    def __enter__(self) -> "Spinner":
        if self.enabled:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            self.stream.write(f"\r{next(self._frames)} {self.message}")
            self.stream.flush()
            time.sleep(0.08)

    def _finish(self, marker: str, message: str | None = None) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        final_message = message or self.message
        self.stream.write(f"\r{marker} {final_message}\n")
        self.stream.flush()

    def succeed(self, message: str | None = None) -> None:
        self._finish("✔", message)

    def fail(self, message: str | None = None) -> None:
        self._finish("✘", message)

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.succeed()
        else:
            self.fail()
        return False
