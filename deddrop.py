#!/usr/bin/env python3
"""deddrop.py — DedDrop: A silent, passive telemetry dead drop for WDGWars (wdgwars.pl).

Ingests airborne ADS-B aircraft feeds and MeshMapper LoRa wardriving telemetry,
accumulates seen signals, and flushes HMAC-signed data batches to WDGWars.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────
TAR1090_URL = os.environ.get("TAR1090_URL", "").strip()
API_KEY = os.environ.get("WDGWARS_API_KEY", "").strip()
MESHMAPPER_API_KEY = (os.environ.get("MESHMAPPER_API_KEY", "").strip() or API_KEY).strip()

UPLOAD_URL = os.environ.get("WDGWARS_API_URL", "https://wdgwars.pl/endpoint/upload/").strip()
ME_URL = os.environ.get("WDGWARS_ME_URL", "https://wdgwars.pl/api/me").strip()

POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
UPLOAD_INTERVAL_HOURS = float(os.environ.get("UPLOAD_INTERVAL_HOURS", "6"))

STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/state/accumulator.json"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))
SNAPSHOT_RETENTION = int(os.environ.get("SNAPSHOT_RETENTION", "200"))
SAVE_LATEST_RAW = os.environ.get("SAVE_LATEST_RAW", "true").strip().lower() in ("1", "true", "yes")
LATEST_RAW_PATH = Path(os.environ.get("LATEST_RAW_PATH", "/data/latest_raw.json"))

WEB_ENABLED = os.environ.get("WEB_ENABLED", "true").strip().lower() in ("1", "true", "yes")
WEB_BIND = os.environ.get("WEB_BIND", "0.0.0.0").strip()
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()

# Cross-origin reads are off by default: /api/* exposes accumulated telemetry and
# the MeshMapper enrolment link, neither of which should be readable by any page
# the operator happens to have open. Set to a specific origin to opt in.
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "").strip()

# Hard cap on an inbound /api/wardrive body so a bogus Content-Length cannot
# exhaust memory.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024)))

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
CHUNK_COOLDOWN_S = float(os.environ.get("CHUNK_COOLDOWN_S", "1"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
BACKOFF_BASE_S = float(os.environ.get("BACKOFF_BASE_S", "2"))
# After a failed flush the window is retained and retried this soon, rather than
# waiting out another full UPLOAD_INTERVAL_HOURS.
RETRY_INTERVAL_MINUTES = float(os.environ.get("RETRY_INTERVAL_MINUTES", "15"))

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").strip().lower() in ("1", "true", "yes")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

TOOL_NAME = "deddrop"
TOOL_VERSION = "1.3.0"
USER_AGENT = f"{TOOL_NAME}/{TOOL_VERSION}"

# Minted per process and handed to the dashboard when index.html is served. The
# control endpoints require it, so a cross-origin page — which cannot read our
# HTML without CORS — cannot drive them.
CONTROL_TOKEN = secrets.token_urlsafe(32)

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(TOOL_NAME)

_shutdown = False

# ── Regex for MeshMapper heard_repeats tokens ─────────────────────────────
_HEARD_TOKEN_RE = re.compile(r"^([0-9A-Fa-f]{2,})(?:\(([A-Za-z])\))?\(([-+]?\d+(?:\.\d+)?)\)$")

# ── Thread-Safe Shared State for Web Dashboard & Controls ─────────────────
# Re-entrant so save_state() can serialize under the lock even when the caller
# already holds it (the /api/wardrive handler does).
_state_lock = threading.RLock()
# Held only across the disk write, so two threads can never interleave into the
# same temp file.
_save_lock = threading.Lock()
_global_state: dict = {
    "accumulator": {},
    "mesh_accumulator": {},
    "window_start": time.time(),
    "poll_count": 0,
    "ingested_pings_count": 0,
}
_last_poll_time: float = 0.0
_last_upload_info: dict = {}
_cached_user_stats: dict = {}
_user_stats_updated: float = 0.0
_last_skipped_count: int = 0
_next_flush_attempt: float = 0.0
_trigger_poll_now: bool = False
_trigger_flush_now: bool = False


def _handle_signal(signum, _frame):
    global _shutdown
    log.info("received signal %s, will stop after this poll", signum)
    _shutdown = True


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def scrub(text: str, key: str) -> str:
    """Redact secrets if they ever show up in a log line or server response."""
    if key and key in text:
        return text.replace(key, f"{key[:4]}…{key[-4:]}")
    return text


# ── Fetch tar1090 ────────────────────────────────────────────────────────
def fetch_aircraft_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()

    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    return json.loads(raw.decode("utf-8"))


# ── Normalize tar1090 aircraft.json -> wdgwars "aircraft" wire records ──────
_HEX_ICAO = frozenset("0123456789ABCDEF")


def _coerce_int(v) -> int:
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _coerce_int_opt(v) -> int | None:
    """Like _coerce_int but keeps 'absent' distinct from 'zero'.

    An aircraft with no ``track`` in the feed is not heading due north, and one
    with no ``gs`` is not stationary — reporting 0 for either invents telemetry
    that was never received. The wire format already carries nulls (mesh rssi).
    """
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def normalize_one(ac: dict, ts_str: str) -> tuple[str, dict] | None:
    hex_id = (ac.get("hex") or "").upper().strip()
    if not hex_id or len(hex_id) != 6 or not set(hex_id) <= _HEX_ICAO:
        return None

    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None

    alt_baro = ac.get("alt_baro")
    record = {
        "icao": hex_id,
        "callsign": (ac.get("flight") or "").strip(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        # "ground" is a real altitude reading (0 ft), unlike a missing field.
        "alt_ft": 0 if alt_baro == "ground" else _coerce_int_opt(alt_baro),
        "speed_kt": _coerce_int_opt(ac.get("gs")),
        "heading": _coerce_int_opt(ac.get("track")),
        "first_seen": ts_str,
        "type": "ADSB",
    }
    return hex_id, record


def parse_snapshot(snapshot: dict) -> tuple[dict[str, dict], int]:
    now_ts = snapshot.get("now")
    ts_str = (
        datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if now_ts
        else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    )

    records: dict[str, dict] = {}
    skipped = 0
    for ac in snapshot.get("aircraft", []):
        result = normalize_one(ac, ts_str)
        if result is None:
            skipped += 1
            continue
        icao, record = result
        records[icao] = record
    return records, skipped


def merge_into(accumulator: dict[str, dict], new_records: dict[str, dict]) -> None:
    for icao, record in new_records.items():
        existing = accumulator.get(icao)
        if existing:
            record["first_seen"] = existing["first_seen"]
        accumulator[icao] = record


# ── MeshMapper Normalizer ─────────────────────────────────────────────────
def normalize_mesh_ping(ping: dict) -> list[dict]:
    p_type = (ping.get("type") or "").upper().strip()
    lat = ping.get("lat")
    lon = ping.get("lon")
    if lat is None or lon is None:
        return []
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return []

    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return []

    ts = ping.get("timestamp")
    first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if ts:
        # A ping carrying milliseconds, a string, or a nonsense epoch shouldn't
        # take down the ingest — fall back to arrival time.
        try:
            first_seen = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            log.debug("mesh ping had unusable timestamp %r — using arrival time", ts)

    records = []

    if p_type in ("DISC", "TRACE"):
        rep_id = (ping.get("repeater_id") or "").lower().strip()
        if rep_id and rep_id != "none":
            node_type = (ping.get("node_type") or "REPEATER").upper()
            if node_type == "R":
                node_type = "REPEATER"
            rssi = ping.get("local_rssi")
            try:
                rssi = int(rssi) if rssi is not None else None
            except (TypeError, ValueError):
                rssi = None
            records.append({
                "node_id": rep_id,
                "node_type": node_type,
                "name": rep_id,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "rssi": rssi,
                "first_seen": first_seen,
                "type": "MESHCORE",
            })

    heard = ping.get("heard_repeats")
    if heard and heard != "None":
        tokens = [t.strip() for t in str(heard).split(",") if t.strip()]
        for tok in tokens:
            m = _HEARD_TOKEN_RE.match(tok)
            if m:
                n_id = m.group(1).lower()
                records.append({
                    "node_id": n_id,
                    "node_type": "REPEATER",
                    "name": n_id,
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "rssi": None,
                    "first_seen": first_seen,
                    "type": "MESHCORE",
                })

    return records


def merge_mesh_records(mesh_acc: dict[str, dict], new_mesh_records: list[dict]) -> None:
    for rec in new_mesh_records:
        n_id = rec["node_id"]
        existing = mesh_acc.get(n_id)
        if existing:
            rec["first_seen"] = existing["first_seen"]
        mesh_acc[n_id] = rec


# ── State persistence ─────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "accumulator": {},
        "mesh_accumulator": {},
        "window_start": time.time(),
        "poll_count": 0,
        "ingested_pings_count": 0,
    }


def _coerce_state(raw) -> dict:
    """Accept only a well-formed state document; repair anything else.

    A truncated or hand-edited state file used to surface as an AttributeError
    deep inside the first merge. Validate the shape up front instead.
    """
    state = _default_state()
    if not isinstance(raw, dict):
        log.warning("state file is not a JSON object — starting a fresh window")
        return state

    for key in ("accumulator", "mesh_accumulator"):
        value = raw.get(key)
        if isinstance(value, dict):
            # Drop individual records that aren't objects rather than the lot.
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
        return _coerce_state(json.loads(STATE_FILE.read_text()))
    except FileNotFoundError:
        return _default_state()
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read state file (%s) — starting a fresh window", e)
        return _default_state()


def _atomic_write(path: Path, blob: str) -> None:
    """Write via a per-writer temp file, fsync, then rename.

    The temp name carries pid+tid because two threads sharing one fixed ``.tmp``
    path can interleave their writes before either rename lands.
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
    # Serialize under the state lock so json.dumps never walks a dict that
    # another thread is mutating; do the slow disk write without it held.
    with _state_lock:
        blob = json.dumps(state, separators=(",", ":"))
    with _save_lock:
        _atomic_write(STATE_FILE, blob)


# ── Snapshot archive ──────────────────────────────────────────────────────
def save_snapshot(aircraft_records: list[dict], mesh_records: list[dict],
                   window_start: float, window_end: float, poll_count: int, snapshot_dir: Path) -> Path:
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
    path.write_text(json.dumps(payload, separators=(",", ":")))
    return path


# Summaries keyed by (name, mtime_ns, size). Snapshots are immutable once
# written, so a hit is always valid; this replaces re-parsing every archived
# file on every dashboard poll (~15 MB of JSON every 5s at 20x5k-aircraft).
_snapshot_cache: dict[tuple[str, int, int], dict] = {}
_snapshot_cache_lock = threading.Lock()
SNAPSHOT_LIST_LIMIT = 20


def _summarize_snapshot(path: Path, stat: os.stat_result) -> dict:
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
            cached = _summarize_snapshot(path, stat)
            with _snapshot_cache_lock:
                _snapshot_cache[key] = cached
        out.append(cached)

    # Evict entries for pruned/rotated files so the cache tracks the directory.
    with _snapshot_cache_lock:
        for stale in [k for k in _snapshot_cache if k not in live_keys]:
            del _snapshot_cache[stale]

    return out


def prune_snapshots(snapshot_dir: Path, keep: int) -> None:
    if keep <= 0:
        return
    files = sorted(snapshot_dir.glob("upload_*.json"))
    excess = files[:-keep] if len(files) > keep else []
    for f in excess:
        try:
            f.unlink()
        except OSError as e:
            log.warning("could not prune old snapshot %s: %s", f.name, e)


# ── HMAC envelope + HTTP ─────────────────────────────────────────────────
def build_envelope(payload: dict, api_key: str) -> dict:
    body_json = json.dumps(payload, separators=(",", ":"))
    data_b64 = base64.b64encode(body_json.encode()).decode()
    nonce = secrets.token_hex(8)
    sig = hmac.new(api_key.encode(), (nonce + data_b64).encode(), hashlib.sha256).hexdigest()
    return {"data": data_b64, "nonce": nonce, "sig": sig}


KNOWN_COUNTERS = (
    "aircraft_imported", "aircraft_already_seen",
    "meshcore_imported", "meshcore_already_seen",
    "imported", "captured", "updated", "duplicates", "merged_samples", "already_seen", "no_gps", "bad_rows",
)


def _sleep_interruptible(seconds: float) -> bool:
    """Sleep in short steps, returning True if shutdown was requested.

    A retry backoff used to sit inside an uninterruptible time.sleep(), so
    `docker stop` had to wait out the full delay before SIGKILL landed.
    """
    deadline = time.monotonic() + seconds
    while not _shutdown:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.5, remaining))
    return True


def _retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    """Honour Retry-After when the server sends one, else exponential backoff."""
    backoff = BACKOFF_BASE_S * (2 ** (attempt - 1))
    retry_after = err.headers.get("Retry-After") if err.headers else None
    if retry_after:
        try:
            return max(backoff, min(float(retry_after), 300.0))
        except (TypeError, ValueError):
            pass
    return backoff


def send_chunk(aircraft_chunk: list[dict], mesh_chunk: list[dict], api_key: str, url: str) -> tuple[bool, dict]:
    payload = {"networks": [], "aircraft": aircraft_chunk, "meshcore_nodes": mesh_chunk}
    envelope = build_envelope(payload, api_key)
    body = json.dumps(envelope).encode()

    if DRY_RUN:
        log.info("[dry-run] would POST %d bytes (%d aircraft, %d mesh nodes) to %s",
                 len(body), len(aircraft_chunk), len(mesh_chunk), url)
        return True, {
            "ok": True,
            "dry_run": True,
            "aircraft_imported": len(aircraft_chunk),
            "meshcore_imported": len(mesh_chunk),
            "aircraft_already_seen": 0,
        }

    last_response: dict = {}
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
                txt = resp.read().decode("utf-8", "replace")
                data = json.loads(txt) if txt else {}
                if resp.status == 200 and data.get("ok") and (aircraft_chunk or mesh_chunk):
                    if not any(data.get(k) for k in KNOWN_COUNTERS):
                        log.warning(
                            "HTTP 200 ok:true but counters zero for payload (%d ac, %d mesh): %s",
                            len(aircraft_chunk), len(mesh_chunk), scrub(txt[:800], api_key),
                        )
                        return False, data
                return True, data
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "replace")[:400]
            try:
                last_response = json.loads(err_body) if err_body else {}
            except json.JSONDecodeError:
                last_response = {}
            # 413 means this chunk will never fit; retrying it verbatim is futile.
            if e.code == 413:
                log.error("payload too large for %d aircraft / %d mesh nodes — lower BATCH_SIZE",
                          len(aircraft_chunk), len(mesh_chunk))
                return False, last_response
            if (e.code == 429 or 500 <= e.code < 600) and attempt < MAX_ATTEMPTS:
                delay = _retry_delay(e, attempt)
                log.warning("upload attempt %d/%d failed with HTTP %d — retrying in %.0fs",
                            attempt, MAX_ATTEMPTS, e.code, delay)
                if _sleep_interruptible(delay):
                    return False, last_response
                continue
            return False, last_response
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_ATTEMPTS:
                delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning("upload attempt %d/%d could not reach %s (%s) — retrying in %.0fs",
                            attempt, MAX_ATTEMPTS, url, e, delay)
                if _sleep_interruptible(delay):
                    return False, {}
                continue
            return False, {}
    return False, last_response


def upload_records(aircraft_records: list[dict], mesh_records: list[dict], api_key: str, url: str) -> bool:
    global _last_upload_info
    if not aircraft_records and not mesh_records:
        log.info("nothing to upload this cycle")
        return True

    ok = True
    total_ac_imported = total_ac_seen = 0
    total_mesh_imported = total_mesh_seen = 0

    max_len = max(len(aircraft_records), len(mesh_records))
    n_chunks = (max_len - 1) // BATCH_SIZE + 1 if max_len > 0 else 1

    for chunk_idx in range(n_chunks):
        if _shutdown:
            # Bail out rather than getting SIGKILLed mid-chunk; the caller keeps
            # the window because ok is False, so nothing is lost.
            log.warning("shutdown requested — aborting upload after %d/%d chunks",
                        chunk_idx, n_chunks)
            ok = False
            break

        ac_chunk = aircraft_records[chunk_idx * BATCH_SIZE : (chunk_idx + 1) * BATCH_SIZE]
        mesh_chunk = mesh_records[chunk_idx * BATCH_SIZE : (chunk_idx + 1) * BATCH_SIZE]
        is_last = chunk_idx == n_chunks - 1

        log.info("chunk %d/%d: uploading %d aircraft, %d mesh nodes",
                 chunk_idx + 1, n_chunks, len(ac_chunk), len(mesh_chunk))

        chunk_ok, data = send_chunk(ac_chunk, mesh_chunk, api_key, url)
        ok = ok and chunk_ok
        if chunk_ok:
            total_ac_imported += int(data.get("aircraft_imported", data.get("imported", 0)) or 0)
            total_ac_seen += int(data.get("aircraft_already_seen", data.get("already_seen", 0)) or 0)
            total_mesh_imported += int(data.get("meshcore_imported", data.get("mesh_imported", 0)) or 0)
            total_mesh_seen += int(data.get("meshcore_already_seen", 0) or 0)

        if not is_last and CHUNK_COOLDOWN_S > 0 and not DRY_RUN:
            time.sleep(CHUNK_COOLDOWN_S)

    with _state_lock:
        _last_upload_info = {
            "timestamp": time.time(),
            "aircraft_count": len(aircraft_records),
            "mesh_count": len(mesh_records),
            "aircraft_imported": total_ac_imported,
            "aircraft_seen": total_ac_seen,
            "mesh_imported": total_mesh_imported,
            "mesh_seen": total_mesh_seen,
            "success": ok,
            "dry_run": DRY_RUN,
        }

    if ok and not DRY_RUN:
        log.info("upload accepted: ac sent=%d new=%d seen=%d | mesh sent=%d new=%d seen=%d",
                 len(aircraft_records), total_ac_imported, total_ac_seen,
                 len(mesh_records), total_mesh_imported, total_mesh_seen)
    elif not ok:
        log.error("upload FAILED — retaining %d aircraft and %d mesh nodes for retry",
                  len(aircraft_records), len(mesh_records))
    return ok


def fetch_user_stats(force: bool = False) -> dict:
    global _cached_user_stats, _user_stats_updated
    now = time.time()
    if not force and _cached_user_stats and (now - _user_stats_updated < 60):
        return _cached_user_stats

    if not API_KEY:
        return {}

    req = urllib.request.Request(
        ME_URL, headers={"X-API-Key": API_KEY, "User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok"):
            with _state_lock:
                _cached_user_stats = data
                _user_stats_updated = now
            return data
    except Exception as e:
        log.debug("could not fetch user stats: %s", scrub(str(e), API_KEY))
    return _cached_user_stats


def validate_api_key() -> None:
    data = fetch_user_stats(force=True)
    if data.get("ok"):
        log.info(
            "API key OK — user=%s wifi=%s ble=%s aircraft=%s mesh=%s total=%s",
            data.get("username"), data.get("wifi", 0), data.get("ble", 0),
            data.get("aircraft", 0), data.get("mesh", 0), data.get("total", 0),
        )


# ── Web Server Request Handler ────────────────────────────────────────────
class WebRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{TOOL_NAME}/{TOOL_VERSION}"
    sys_version = ""

    def log_message(self, format, *args):
        # Access logs are noise at INFO but genuinely useful when debugging an
        # ingest that isn't landing, so route them to DEBUG instead of /dev/null.
        log.debug("web %s - %s", self.address_string(), format % args)

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200,
                    *, no_store: bool = False):
        headers = [("Content-Type", content_type)]
        # Compress only what's worth compressing; JSON telemetry is highly
        # repetitive and this is the bulk of dashboard traffic.
        if len(body) > 1024 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=6)
            headers.append(("Content-Encoding", "gzip"))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        # Wildcard CORS previously let any page the operator had open read the
        # accumulated telemetry and the key-bearing enrolment link.
        if CORS_ALLOW_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ALLOW_ORIGIN)
            self.send_header("Vary", "Origin")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data: dict | list, status: int = 200, *, no_store: bool = False):
        # Compact separators, not indent=2: this is a machine-read API polled
        # every few seconds, and pretty-printing added ~39% to every payload.
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, "application/json", status, no_store=no_store)

    def _authorized_key(self) -> bool:
        """Constant-time check of the ingest key from header or ?key=."""
        presented = self.headers.get("X-API-Key", "").strip()
        if not presented:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            presented = (query.get("key") or [""])[0].strip()
        if not presented:
            return False
        return any(
            hmac.compare_digest(presented, accepted)
            for accepted in (MESHMAPPER_API_KEY, API_KEY) if accepted
        )

    def _authorized_control(self) -> bool:
        """Control endpoints accept the per-process token or the API key.

        The token is embedded in index.html, which a cross-origin page cannot
        read without CORS, so it doubles as CSRF protection. Requiring a custom
        header also forces a preflight that same-origin policy will reject.
        """
        presented = self.headers.get("X-Control-Token", "").strip()
        if presented and hmac.compare_digest(presented, CONTROL_TOKEN):
            return True
        return self._authorized_key()

    def _meshmapper_link(self) -> str:
        if PUBLIC_HOST:
            if PUBLIC_HOST.startswith(("http://", "https://")):
                base_host = PUBLIC_HOST.rstrip("/")
            else:
                base_host = f"http://{PUBLIC_HOST}"
        else:
            host_header = self.headers.get("Host") or f"localhost:{WEB_PORT}"
            scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
            base_host = f"{scheme}://{host_header}"
        key = MESHMAPPER_API_KEY or API_KEY or "YOUR_KEY"
        # Escape only what would truncate the link (& # ? %) and leave ':' and
        # '/' literal, so the deep link stays byte-identical to the format
        # MeshMapper already accepts for ordinary hosts and 64-hex keys.
        quote = lambda v: urllib.parse.quote(v, safe=":/")  # noqa: E731
        return (f"meshmapper://custom-api?url={quote(base_host)}/api/wardrive"
                f"&key={quote(key)}")

    def do_GET(self):
        url_path = urllib.parse.urlparse(self.path).path

        if url_path in ("/", "/index.html"):
            index_file = WEB_DIR / "index.html"
            try:
                content = index_file.read_text(encoding="utf-8")
            except OSError:
                self._send_bytes(
                    b"<!doctype html><meta charset=utf-8>"
                    b"<h1>DedDrop dashboard file missing</h1>"
                    b"<p>Expected <code>web/index.html</code> beside deddrop.py.</p>",
                    "text/html; charset=utf-8", status=500)
                return
            content = content.replace("__CONTROL_TOKEN__", CONTROL_TOKEN)
            self._send_bytes(content.encode("utf-8"), "text/html; charset=utf-8",
                             no_store=True)
            return

        if url_path == "/healthz":
            # Cheap liveness probe for Docker; deliberately leaks nothing.
            self._send_json({"ok": True, "version": TOOL_VERSION}, no_store=True)
            return

        if url_path == "/api/meshmapper-link":
            # Carries the ingest key, so it is never served unauthenticated.
            if not self._authorized_control():
                self._send_json({"ok": False, "error": "Unauthorized"}, status=401,
                                no_store=True)
                return
            self._send_json({"ok": True, "meshmapper_link": self._meshmapper_link()},
                            no_store=True)
            return

        if url_path == "/api/status":
            with _state_lock:
                acc_count = len(_global_state.get("accumulator", {}))
                mesh_count = len(_global_state.get("mesh_accumulator", {}))
                poll_cnt = _global_state.get("poll_count", 0)
                ingested_pings = _global_state.get("ingested_pings_count", 0)
                win_start = _global_state.get("window_start", time.time())
                last_poll = _last_poll_time
                last_up = dict(_last_upload_info)
                skipped = _last_skipped_count
                retry_at = _next_flush_attempt

            # This was referenced but never assigned, so every request to
            # /api/status raised NameError and dropped the connection.
            elapsed_h = max(0.0, (time.time() - win_start) / 3600)

            self._send_json({
                "ok": True,
                "tool_name": TOOL_NAME,
                "version": TOOL_VERSION,
                "poll_count": poll_cnt,
                "ingested_pings_count": ingested_pings,
                "accumulator_count": acc_count,
                "mesh_accumulator_count": mesh_count,
                "last_poll_skipped": skipped,
                "window_start": win_start,
                "elapsed_hours": elapsed_h,
                "upload_interval_hours": UPLOAD_INTERVAL_HOURS,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "dry_run": DRY_RUN,
                "last_poll_time": last_poll,
                "last_upload": last_up,
                # Seconds until the next retry of a failed flush, 0 when healthy.
                "retry_pending_in": max(0.0, retry_at - time.time()),
                # NOTE: meshmapper_link is deliberately absent — it embeds the
                # API key. Fetch /api/meshmapper-link with the control token.
            })
            return

        if url_path == "/api/aircraft":
            with _state_lock:
                aircraft_list = list(_global_state.get("accumulator", {}).values())
            self._send_json(aircraft_list)
            return

        if url_path == "/api/mesh-nodes":
            with _state_lock:
                mesh_list = list(_global_state.get("mesh_accumulator", {}).values())
            self._send_json(mesh_list)
            return

        if url_path == "/api/user-stats":
            stats = fetch_user_stats()
            self._send_json(stats)
            return

        if url_path == "/api/snapshots":
            self._send_json(list_snapshots(SNAPSHOT_DIR))
            return

        self.send_error(404, "Not Found")

    def _read_body(self) -> bytes | None:
        """Read the request body, refusing anything over MAX_BODY_BYTES.

        Content-Length is attacker-controlled, so it is validated before it is
        ever used as an allocation size.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if content_length < 0:
            self._send_json({"ok": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if content_length > MAX_BODY_BYTES:
            log.warning("rejected %d byte body from %s (limit %d)",
                        content_length, self.address_string(), MAX_BODY_BYTES)
            self._send_json({"ok": False, "error": "Payload too large"}, status=413)
            return None
        if content_length == 0:
            return b""
        try:
            body = self.rfile.read(content_length)
        except (OSError, ConnectionError) as e:
            log.debug("could not read request body: %s", e)
            return None
        if len(body) != content_length:
            self._send_json({"ok": False, "error": "Truncated body"}, status=400)
            return None
        return body

    def do_POST(self):
        global _trigger_poll_now, _trigger_flush_now
        url_path = urllib.parse.urlparse(self.path).path

        if url_path == "/api/wardrive":
            if not self._authorized_key():
                self._send_json({"ok": False, "error": "Unauthorized key"}, status=401)
                return

            body = self._read_body()
            if body is None:
                return
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"ok": False, "error": "Invalid JSON"}, status=400)
                return

            pings = payload.get("data", []) if isinstance(payload, dict) else None
            if not isinstance(pings, list):
                self._send_json({"ok": False, "error": "Invalid payload format"}, status=400)
                return

            new_mesh_records = []
            for ping in pings:
                if isinstance(ping, dict):
                    new_mesh_records.extend(normalize_mesh_ping(ping))

            with _state_lock:
                merge_mesh_records(_global_state["mesh_accumulator"], new_mesh_records)
                _global_state["ingested_pings_count"] = _global_state.get("ingested_pings_count", 0) + len(pings)
            # Outside the state lock: writing to disk should not stall pollers.
            save_state(_global_state)

            log.info("MeshMapper ingest: received %d pings -> %d normalized mesh node(s)",
                     len(pings), len(new_mesh_records))

            self._send_json({"ok": True, "accepted_pings": len(pings), "nodes_merged": len(new_mesh_records)})
            return

        if url_path in ("/api/trigger-poll", "/api/trigger-flush"):
            # These force network activity and a full upload; they were
            # completely unauthenticated and reachable via cross-site POST.
            if not self._authorized_control():
                self._send_json({"ok": False, "error": "Unauthorized"}, status=401,
                                no_store=True)
                return
            if url_path == "/api/trigger-poll":
                _trigger_poll_now = True
                self._send_json({"ok": True, "message": "Feed poll triggered!"})
            else:
                _trigger_flush_now = True
                self._send_json({"ok": True, "message": "Upload flush triggered!"})
            return

        self.send_error(404, "Not Found")


class _DedDropHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_web_server():
    if not WEB_ENABLED:
        return
    try:
        server = _DedDropHTTPServer((WEB_BIND, WEB_PORT), WebRequestHandler)
    except OSError as e:
        log.error("could not bind web server to %s:%d (%s) — continuing headless",
                  WEB_BIND, WEB_PORT, e)
        return
    log.info("DedDrop dashboard & ingest API listening on http://%s:%d/", WEB_BIND, WEB_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception:
        log.exception("web server stopped unexpectedly")
    finally:
        server.server_close()


# ── Poll + flush ─────────────────────────────────────────────────────────
def do_poll(state: dict) -> None:
    global _last_poll_time, _last_skipped_count
    snapshot = fetch_aircraft_json(TAR1090_URL, REQUEST_TIMEOUT_S)

    if SAVE_LATEST_RAW:
        try:
            LATEST_RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(LATEST_RAW_PATH, json.dumps(snapshot, separators=(",", ":")))
        except OSError as e:
            log.warning("could not write %s: %s", LATEST_RAW_PATH, e)

    records, skipped = parse_snapshot(snapshot)

    # A feed that suddenly skips everything means the format changed or the
    # receiver lost GPS — previously this was computed and thrown away.
    total = len(records) + skipped
    if skipped and total and skipped == total:
        log.warning("poll rejected all %d aircraft in the feed — check TAR1090_URL format", total)
    elif skipped:
        log.debug("poll skipped %d/%d aircraft (no position or bad ICAO)", skipped, total)

    with _state_lock:
        merge_into(state["accumulator"], records)
        state["poll_count"] += 1
        _last_poll_time = time.time()
        _last_skipped_count = skipped

    save_state(state)


def _drop_uploaded(acc: dict[str, dict], uploaded: list[tuple[str, dict]]) -> int:
    """Remove exactly the records that were uploaded, leaving newer ones alone.

    Uploads take minutes, and ingest keeps running throughout. Identity-checking
    each record means a node re-heard mid-upload survives into the next window
    instead of being dropped with the batch it was never part of.
    """
    removed = 0
    for key, record in uploaded:
        if acc.get(key) is record:
            del acc[key]
            removed += 1
    return removed


def do_flush(state: dict, *, force: bool = False) -> bool:
    global _next_flush_attempt

    window_start = state["window_start"]
    now = time.time()
    elapsed_h = (now - window_start) / 3600
    if not force:
        if elapsed_h < UPLOAD_INTERVAL_HOURS:
            return False
        # A previous attempt failed; wait out the retry backoff.
        if now < _next_flush_attempt:
            return False

    window_end = time.time()
    with _state_lock:
        # Keep (key, record) pairs so the post-upload cleanup can tell an
        # uploaded record apart from one that changed while we were uploading.
        aircraft_items = list(state["accumulator"].items())
        mesh_items = list(state["mesh_accumulator"].items())
        poll_count = state["poll_count"]

    aircraft_records = [rec for _, rec in aircraft_items]
    mesh_records = [rec for _, rec in mesh_items]

    if not aircraft_records and not mesh_records:
        # Nothing to send, but the window should still roll over.
        with _state_lock:
            state["window_start"] = window_end
            state["poll_count"] = 0
        save_state(state)
        log.info("upload window elapsed (%.2fh) with nothing accumulated", elapsed_h)
        return True

    log.info("upload window elapsed (%.2fh, %d polls) — flushing %d aircraft, %d mesh nodes",
              elapsed_h, poll_count, len(aircraft_records), len(mesh_records))

    try:
        path = save_snapshot(aircraft_records, mesh_records, window_start, window_end,
                             poll_count, SNAPSHOT_DIR)
        log.info("saved snapshot %s", path.name)
    except OSError as e:
        log.error("could not write snapshot: %s", e)

    ok = upload_records(aircraft_records, mesh_records, API_KEY, UPLOAD_URL)

    if not ok:
        # Retain everything. The window is deliberately left open so the next
        # attempt re-sends this data instead of silently discarding it.
        _next_flush_attempt = time.time() + RETRY_INTERVAL_MINUTES * 60
        log.warning("flush unsuccessful — window retained, next attempt in %.0f min",
                    RETRY_INTERVAL_MINUTES)
        return False

    _next_flush_attempt = 0.0
    with _state_lock:
        dropped_ac = _drop_uploaded(state["accumulator"], aircraft_items)
        dropped_mesh = _drop_uploaded(state["mesh_accumulator"], mesh_items)
        kept_ac = len(state["accumulator"])
        kept_mesh = len(state["mesh_accumulator"])
        state["window_start"] = window_end
        state["poll_count"] = 0

    save_state(state)
    if kept_ac or kept_mesh:
        log.info("window rolled: cleared %d aircraft / %d mesh nodes, "
                 "carried %d aircraft / %d mesh nodes seen during upload",
                 dropped_ac, dropped_mesh, kept_ac, kept_mesh)

    prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)
    return True


def preflight_storage() -> bool:
    """Confirm /data is writable before we accumulate anything worth losing.

    With compose's bind mount, the image's chown is masked and a host-created
    ./data ends up root-owned while the container runs as uid 1000. That used to
    surface hours later as a PermissionError traceback mid-poll.
    """
    for label, path in (("STATE_FILE", STATE_FILE.parent), ("SNAPSHOT_DIR", SNAPSHOT_DIR)):
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


def main() -> int:
    global _global_state, _trigger_poll_now, _trigger_flush_now

    if not TAR1090_URL:
        log.error("TAR1090_URL is not set — point it at your tar1090 aircraft.json")
        return 2
    if not API_KEY:
        log.error("WDGWARS_API_KEY is not set — generate one in your WDGWars profile")
        return 2
    if not preflight_storage():
        return 2

    log.info("%s v%s starting — feed=%s upload_url=%s poll=%gs upload_every=%gh dry_run=%s web=%s:%d",
              TOOL_NAME, TOOL_VERSION, TAR1090_URL, UPLOAD_URL, POLL_INTERVAL_SECONDS,
              UPLOAD_INTERVAL_HOURS, DRY_RUN, WEB_BIND, WEB_PORT)

    validate_api_key()

    state = load_state()
    with _state_lock:
        _global_state = state

    if state["poll_count"] or state["accumulator"] or state.get("mesh_accumulator"):
        log.info("resumed state: %d polls, %d aircraft, %d mesh nodes accumulated since %s",
                  state["poll_count"], len(state["accumulator"]), len(state.get("mesh_accumulator", {})),
                  datetime.fromtimestamp(state["window_start"], tz=timezone.utc).isoformat())

    do_flush(state)

    if WEB_ENABLED:
        t = threading.Thread(target=start_web_server, daemon=True)
        t.start()

    while not _shutdown:
        try:
            do_poll(state)
            do_flush(state)
        except urllib.error.URLError as e:
            log.error("could not reach tar1090 feed: %s", e)
        except json.JSONDecodeError as e:
            log.error("tar1090 feed did not return valid JSON: %s", e)
        except Exception:
            log.exception("unexpected error during poll cycle")

        if RUN_ONCE:
            do_flush(state, force=True)
            break
        if _shutdown:
            break

        deadline = time.monotonic() + POLL_INTERVAL_SECONDS
        while not _shutdown:
            if _trigger_poll_now:
                _trigger_poll_now = False
                log.info("manual poll triggered via web interface")
                break
            if _trigger_flush_now:
                _trigger_flush_now = False
                log.info("manual flush triggered via web interface")
                try:
                    do_flush(state, force=True)
                except Exception:
                    log.exception("manual flush failed")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))

    # Persist whatever is in flight so a restart resumes instead of re-polling
    # a window it already paid for.
    try:
        save_state(state)
    except OSError as e:
        log.error("could not persist state on shutdown: %s", e)

    log.info("%s stopped", TOOL_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
