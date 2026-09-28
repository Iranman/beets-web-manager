"""Process-wide Library payload cache and playlist library index (ARCH-001).

These were module globals in app.py that several functions rebound with
`global`. Rebinding cannot cross module boundaries (a re-exported name is a
snapshot), so the state lives on one shared object that every service module
reads, stores and invalidates in place.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple


class LibraryCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.payload: Optional[dict] = None
        self.ts: float = 0.0
        self.playlist_index_lock = threading.Lock()
        self.playlist_index: Dict[str, Any] = {"mtime": 0.0, "index": None}

    def snapshot(self) -> Tuple[Optional[dict], float]:
        with self._lock:
            return self.payload, self.ts

    def store(self, payload: dict) -> None:
        with self._lock:
            self.payload = payload
            self.ts = time.time()

    def invalidate(self) -> None:
        with self._lock:
            self.payload = None
            self.ts = 0.0
        with self.playlist_index_lock:
            self.playlist_index = {"mtime": 0.0, "index": None}


library_cache = LibraryCache()
