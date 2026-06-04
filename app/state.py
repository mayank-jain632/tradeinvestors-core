"""
In-memory state for the solo trader.

Dedup cache uses a sliding window — entries older than DEDUP_WINDOW_SECONDS are dropped.

Nothing here is persisted to disk intentionally: if the process restarts,
the dedup cache resets, which is the safe failure mode.
"""

import threading
import time

from config import cfg


class State:
    def __init__(self) -> None:
        # { signal_hash: timestamp }
        self._dedup_cache: dict[str, float] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Dedup
    # ------------------------------------------------------------------

    def _signal_key(self, symbol: str, signal: str) -> str:
        return f"{symbol.strip().upper()}:{signal.strip().upper()}"

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [h for h, ts in self._dedup_cache.items()
                   if now - ts > cfg.DEDUP_WINDOW_SECONDS]
        for h in expired:
            del self._dedup_cache[h]

    def is_duplicate(self, symbol: str, signal: str) -> bool:
        with self._lock:
            self._evict_expired()
            key = self._signal_key(symbol, signal)
            if key in self._dedup_cache:
                return True
            self._dedup_cache[key] = time.time()
            return False

state = State()
