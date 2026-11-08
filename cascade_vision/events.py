"""Thread-safe JSONL output and bounded request admission."""

from __future__ import annotations

import json
import threading
from pathlib import Path


class JsonlEventWriter:
    """Write sanitized pipeline events. Caller owns the writer's lifetime."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, event):
        with self._lock:
            self._file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            self._file.flush()

    def close(self):
        with self._lock:
            self._file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class CallBudget:
    """Share a maximum number of provider attempts across benchmark strategies."""

    def __init__(self, limit):
        if limit < 0:
            raise ValueError("Call limit must be non-negative; zero means unlimited")
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if self.limit and self.used >= self.limit:
                return False
            self.used += 1
            return True
