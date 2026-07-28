"""Accumulator state persistence and the snapshot archive."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, runtime
from .config import log
from .runtime import default_state

SNAPSHOT_LIST_LIMIT = 20

# Summaries keyed by (name, mtime_ns, size). Snapshots are immutable once
# written, so a hit is always valid and the dashboard's 5s poll costs nothing.
_snapshot_cache: dict[tuple[str, int, int], dict] = {}
_snapshot_cache_lock = threading.Lock()


# ── State ─────────────────────────────────────────────────────────────────
def coerce_state(raw) -> dict:
    """Accept only a well-formed state document; repair anything else."""
    state = default_state()
    if not isinstance(raw, dict):
        log.warning("state file is not a JSON object — starting a fresh window")
        return state

    for key in ("accumulator", "mesh_accumulator"):
        value = raw.get(key)
        if isinstance(value, dict):
            # Drop individual malformed records rather than the whole window.
            state[key] = {k: v for k, v in value.items() if isinstance(v, dict)}
            if len(state[key]) != len(value):
                log.warning("dropped %d malformed record(s) from %s",
                            len(value) - len(state[key]), key)
        elif value is not None:
            log.warning("state field %r was %s, expected object — reset to empty",
                        key, type(value).__name__)

    for key, cast in (("window_start", float), ("poll_count", int),
                      ("ingested_pings_count", int)):
        value = raw.get(key)
        if value is None:
            continue
        try:
            state[key] = cast(value)
        except (TypeError, ValueError):
            log.warning("state field %r was not a number — reset to default", key)

    if state["window_start"] > time.time() + 86400:
        log.warning("state window_start is in the future — resetting to now")
        state["window_start"] = time.time()

    return state


def load_state() -> dict:
    try:
        return coerce_state(json.loads(config.STATE_FILE.read_text()))
    except FileNotFoundError:
        return default_state()
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read state file (%s) — starting a fresh window", e)
        return default_state()


def atomic_write(path: Path, blob: str) -> None:
    """Write via a per-writer temp file, fsync, then rename.

    The temp name carries pid+tid so concurrent writers cannot interleave into
    one another's partial file before the rename lands.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident():x}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save_state(state: dict) -> None:
    # Serialize under the state lock so json.dumps never walks a dict another
    # thread is mutating; do the slow disk write without it held.
    with runtime.lock:
        blob = json.dumps(state, separators=(",", ":"))
    with runtime.save_lock:
        atomic_write(config.STATE_FILE, blob)


# ── Snapshots ─────────────────────────────────────────────────────────────
def save_snapshot(aircraft_records: list[dict], mesh_records: list[dict],
                  window_start: float, window_end: float, poll_count: int,
                  snapshot_dir: Path) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(window_end, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"upload_{ts}.json"
    payload = {
        "window_start": datetime.fromtimestamp(window_start, tz=timezone.utc).isoformat(),
        "window_end": datetime.fromtimestamp(window_end, tz=timezone.utc).isoformat(),
        "polls": poll_count,
        "aircraft_count": len(aircraft_records),
        "mesh_nodes_count": len(mesh_records),
        "aircraft": aircraft_records,
        "meshcore_nodes": mesh_records,
    }
    atomic_write(path, json.dumps(payload, separators=(",", ":")))
    return path


def _summarize(path: Path, stat: os.stat_result) -> dict:
    summary = {
        "name": path.name,
        "size": stat.st_size,
        "aircraft_count": 0,
        "mesh_nodes_count": 0,
        "window_start": None,
        "window_end": None,
    }
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        log.debug("could not summarize snapshot %s: %s", path.name, e)
        return summary
    if isinstance(data, dict):
        summary.update({
            "aircraft_count": data.get("aircraft_count", 0),
            "mesh_nodes_count": data.get("mesh_nodes_count", 0),
            "window_start": data.get("window_start"),
            "window_end": data.get("window_end"),
        })
    return summary


def list_snapshots(snapshot_dir: Path, limit: int = SNAPSHOT_LIST_LIMIT) -> list[dict]:
    if not snapshot_dir.is_dir():
        return []

    out: list[dict] = []
    live_keys = set()
    for path in sorted(snapshot_dir.glob("upload_*.json"), reverse=True)[:limit]:
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (path.name, stat.st_mtime_ns, stat.st_size)
        live_keys.add(key)
        with _snapshot_cache_lock:
            cached = _snapshot_cache.get(key)
        if cached is None:
            cached = _summarize(path, stat)
            with _snapshot_cache_lock:
                _snapshot_cache[key] = cached
        out.append(cached)

    with _snapshot_cache_lock:
        for stale in [k for k in _snapshot_cache if k not in live_keys]:
            del _snapshot_cache[stale]

    return out


def prune_snapshots(snapshot_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(snapshot_dir.glob("upload_*.json"))
    for f in files[:-keep] if len(files) > keep else []:
        try:
            f.unlink()
        except OSError as e:
            log.warning("could not prune old snapshot %s: %s", f.name, e)
