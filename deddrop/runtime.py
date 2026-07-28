"""Mutable state shared between the poll loop and the web server.

Accessed as ``runtime.<name>`` so writes from one thread are visible to the
other; importing the names directly would capture a snapshot instead.
"""
from __future__ import annotations

import threading
import time

# Re-entrant: save_state() serializes under this lock even when the caller
# already holds it.
lock = threading.RLock()
# Held only across the disk write, so two threads never share a temp file.
save_lock = threading.Lock()

shutdown = threading.Event()
poll_now = threading.Event()
flush_now = threading.Event()


def default_state() -> dict:
    return {
        "accumulator": {},
        "mesh_accumulator": {},
        "window_start": time.time(),
        "poll_count": 0,
        "ingested_pings_count": 0,
    }


state: dict = default_state()

last_poll_time: float = 0.0
last_upload: dict = {}
last_skipped: int = 0
next_flush_attempt: float = 0.0

user_stats: dict = {}
user_stats_updated: float = 0.0


def sleep_interruptible(seconds: float) -> bool:
    """Sleep in short steps; return True if shutdown was requested."""
    return shutdown.wait(timeout=seconds)


def reset() -> None:
    """Restore process-start defaults. Used by tests."""
    global state, last_poll_time, last_upload, last_skipped, next_flush_attempt
    global user_stats, user_stats_updated
    shutdown.clear()
    poll_now.clear()
    flush_now.clear()
    state = default_state()
    last_poll_time = 0.0
    last_upload = {}
    last_skipped = 0
    next_flush_attempt = 0.0
    user_stats = {}
    user_stats_updated = 0.0
