"""Small in-process TTL cache with per-key locking, plus a parallel map for warehouse calls.

Concurrent requests for the same key share one computation. Exceptions are not cached.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_lock = threading.Lock()
_entries: dict = {}
_key_locks: dict = {}


def cached(key, ttl: float, fn):
    now = time.monotonic()
    hit = _entries.get(key)
    if hit and hit[0] > now:
        return hit[1]
    with _lock:
        klock = _key_locks.setdefault(key, threading.Lock())
    with klock:
        hit = _entries.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        value = fn()
        _entries[key] = (time.monotonic() + ttl, value)
        if len(_entries) > 500:
            for k in [k for k, (exp, _) in _entries.items() if exp <= time.monotonic()]:
                _entries.pop(k, None)
        return value


def pmap(fn, items, workers: int = 8) -> list:
    """Like map, but the calls run concurrently (a fresh pool each time, so nesting cannot deadlock)."""
    items = list(items)
    if len(items) <= 1:
        return [fn(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))
