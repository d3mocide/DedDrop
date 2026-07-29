"""The poll/flush lifecycle and process entry point."""
from __future__ import annotations

import json
import os
import signal
import threading
import time
import urllib.error
from datetime import datetime, timezone

from . import config, runtime, storage
from .config import log
from .normalize import fetch_aircraft_json, merge_into, parse_snapshot, predict_rejections
from .uploader import upload_records, validate_api_key
from .webapp import start_web_server


def _handle_signal(signum, _frame):
    log.info("received signal %s, will stop after this poll", signum)
    runtime.shutdown.set()


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


# ── Poll ──────────────────────────────────────────────────────────────────
def do_poll(state: dict) -> None:
    snapshot = fetch_aircraft_json(config.TAR1090_URL, config.REQUEST_TIMEOUT_S)

    if config.SAVE_LATEST_RAW:
        try:
            storage.atomic_write(config.LATEST_RAW_PATH,
                                 json.dumps(snapshot, separators=(",", ":")))
        except OSError as e:
            log.warning("could not write %s: %s", config.LATEST_RAW_PATH, e)

    records, skipped = parse_snapshot(snapshot)

    total = len(records) + skipped
    if skipped and total and skipped == total:
        log.warning("poll rejected all %d aircraft in the feed — check TAR1090_URL format", total)
    elif skipped:
        log.debug("poll skipped %d/%d aircraft (no position or bad ICAO)", skipped, total)

    with runtime.lock:
        merge_into(state["accumulator"], records)
        state["poll_count"] += 1
        runtime.last_poll_time = time.time()
        runtime.last_skipped = skipped

    storage.save_state(state)


# ── Flush ─────────────────────────────────────────────────────────────────
def _drop_uploaded(accumulator: dict[str, dict], uploaded: list[tuple[str, dict]]) -> int:
    """Remove exactly the records that were uploaded, leaving newer ones alone.

    Uploads take minutes and ingest keeps running, so identity-checking each
    record keeps anything re-heard mid-upload in the next window.
    """
    removed = 0
    for key, record in uploaded:
        if accumulator.get(key) is record:
            del accumulator[key]
            removed += 1
    return removed


def do_flush(state: dict, *, force: bool = False) -> bool:
    now = time.time()
    window_start = state["window_start"]
    elapsed_h = (now - window_start) / 3600

    if not force:
        if elapsed_h < config.UPLOAD_INTERVAL_HOURS:
            return False
        if now < runtime.next_flush_attempt:
            return False

    window_end = time.time()
    with runtime.lock:
        # Keep (key, record) pairs so cleanup can distinguish an uploaded record
        # from one that changed while the upload was in flight.
        aircraft_items = list(state["accumulator"].items())
        mesh_items = list(state["mesh_accumulator"].items())
        poll_count = state["poll_count"]

    aircraft = [rec for _, rec in aircraft_items]
    mesh = [rec for _, rec in mesh_items]

    if not aircraft and not mesh:
        with runtime.lock:
            state["window_start"] = window_end
            state["poll_count"] = 0
        storage.save_state(state)
        log.info("upload window elapsed (%.2fh) with nothing accumulated", elapsed_h)
        return True

    log.info("upload window elapsed (%.2fh, %d polls) — flushing %d aircraft, %d mesh nodes",
             elapsed_h, poll_count, len(aircraft), len(mesh))

    try:
        path = storage.save_snapshot(aircraft, mesh, window_start, window_end,
                                     poll_count, config.SNAPSHOT_DIR)
        log.info("saved snapshot %s", path.name)
    except OSError as e:
        log.error("could not write snapshot: %s", e)

    # Say which nodes the server is going to refuse before it refuses them.
    for warning in predict_rejections(mesh):
        log.warning("%s", warning)

    result = upload_records(aircraft, mesh, config.API_KEY,
                            aircraft_url=config.UPLOAD_URL,
                            mesh_url=config.MESH_UPLOAD_URL)

    if not result.ok:
        # Each feed is dispatched separately, so only the ones that failed are
        # retained; whatever landed is dropped rather than re-sent next attempt.
        # The window stays open either way, so the retry covers what is left.
        runtime.next_flush_attempt = time.time() + config.RETRY_INTERVAL_MINUTES * 60
        with runtime.lock:
            if result.aircraft_ok:
                _drop_uploaded(state["accumulator"], aircraft_items)
            if result.mesh_ok:
                _drop_uploaded(state["mesh_accumulator"], mesh_items)
        log.warning("flush unsuccessful for %s — window retained, next attempt in %.0f min",
                    " and ".join(result.failed_feeds()), config.RETRY_INTERVAL_MINUTES)
        # The window is unchanged, but the failed result is worth persisting so a
        # restart before the next poll still reports it.
        storage.save_state(state)
        return False

    runtime.next_flush_attempt = 0.0
    with runtime.lock:
        dropped_ac = _drop_uploaded(state["accumulator"], aircraft_items)
        dropped_mesh = _drop_uploaded(state["mesh_accumulator"], mesh_items)
        kept_ac = len(state["accumulator"])
        kept_mesh = len(state["mesh_accumulator"])
        state["window_start"] = window_end
        state["poll_count"] = 0

    storage.save_state(state)
    if kept_ac or kept_mesh:
        log.info("window rolled: cleared %d aircraft / %d mesh nodes, "
                 "carried %d aircraft / %d mesh nodes seen during upload",
                 dropped_ac, dropped_mesh, kept_ac, kept_mesh)

    storage.prune_snapshots(config.SNAPSHOT_DIR, config.SNAPSHOT_RETENTION)
    return True


# ── Startup ───────────────────────────────────────────────────────────────
def preflight_storage() -> bool:
    """Confirm /data is writable before accumulating anything worth losing.

    A compose bind mount masks the image's ownership, so a host-created ./data
    is root-owned while the container runs as uid 1000.
    """
    targets = (("STATE_FILE", config.STATE_FILE.parent),
               ("SNAPSHOT_DIR", config.SNAPSHOT_DIR))
    for label, path in targets:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / f".deddrop-write-test-{os.getpid()}"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            log.error("%s directory %s is not writable: %s", label, path, e)
            log.error("if you are using docker compose, fix ownership on the host: "
                      "mkdir -p ./data && sudo chown -R 1000:1000 ./data")
            return False
    return True


def _wait_for_next_poll(state: dict) -> None:
    """Sleep out the poll interval, honouring manual triggers and shutdown."""
    deadline = time.monotonic() + config.POLL_INTERVAL_SECONDS
    while not runtime.shutdown.is_set():
        if runtime.poll_now.is_set():
            runtime.poll_now.clear()
            log.info("manual poll triggered via web interface")
            return
        if runtime.flush_now.is_set():
            runtime.flush_now.clear()
            log.info("manual flush triggered via web interface")
            try:
                do_flush(state, force=True)
            except Exception:
                log.exception("manual flush failed")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        runtime.shutdown.wait(timeout=min(0.25, remaining))


def main() -> int:
    install_signal_handlers()

    if not config.TAR1090_URL:
        log.error("TAR1090_URL is not set — point it at your tar1090 aircraft.json")
        return 2
    if not config.API_KEY:
        log.error("WDGWARS_API_KEY is not set — generate one in your WDGWars profile")
        return 2
    if not preflight_storage():
        return 2

    log.info("%s v%s starting — feed=%s upload_url=%s mesh_upload_url=%s poll=%gs "
             "upload_every=%gh dry_run=%s web=%s:%d",
             config.TOOL_NAME, config.TOOL_VERSION, config.TAR1090_URL, config.UPLOAD_URL,
             config.MESH_UPLOAD_URL, config.POLL_INTERVAL_SECONDS,
             config.UPLOAD_INTERVAL_HOURS, config.DRY_RUN, config.WEB_BIND, config.WEB_PORT)

    validate_api_key()

    state = storage.load_state()
    with runtime.lock:
        runtime.state = state

    if state["poll_count"] or state["accumulator"] or state["mesh_accumulator"]:
        log.info("resumed state: %d polls, %d aircraft, %d mesh nodes accumulated since %s",
                 state["poll_count"], len(state["accumulator"]), len(state["mesh_accumulator"]),
                 datetime.fromtimestamp(state["window_start"], tz=timezone.utc).isoformat())

    do_flush(state)

    if config.WEB_ENABLED:
        threading.Thread(target=start_web_server, daemon=True).start()

    while not runtime.shutdown.is_set():
        try:
            do_poll(state)
            do_flush(state)
        except urllib.error.URLError as e:
            log.error("could not reach tar1090 feed: %s", e)
        except json.JSONDecodeError as e:
            log.error("tar1090 feed did not return valid JSON: %s", e)
        except Exception:
            log.exception("unexpected error during poll cycle")

        if config.RUN_ONCE:
            do_flush(state, force=True)
            break
        if runtime.shutdown.is_set():
            break

        _wait_for_next_poll(state)

    try:
        storage.save_state(state)
    except OSError as e:
        log.error("could not persist state on shutdown: %s", e)

    log.info("%s stopped", config.TOOL_NAME)
    return 0
