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
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).parent / "web"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
CHUNK_COOLDOWN_S = float(os.environ.get("CHUNK_COOLDOWN_S", "1"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
BACKOFF_BASE_S = float(os.environ.get("BACKOFF_BASE_S", "2"))

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() in ("1", "true", "yes")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").strip().lower() in ("1", "true", "yes")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

TOOL_NAME = "deddrop"
TOOL_VERSION = "1.2.0"
USER_AGENT = f"{TOOL_NAME}/{TOOL_VERSION}"

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
_state_lock = threading.Lock()
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

    record = {
        "icao": hex_id,
        "callsign": (ac.get("flight") or "").strip(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "alt_ft": _coerce_int(ac.get("alt_baro") if ac.get("alt_baro") != "ground" else 0),
        "speed_kt": _coerce_int(ac.get("gs")),
        "heading": _coerce_int(ac.get("track")),
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
    if ts:
        try:
            first_seen = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    else:
        first_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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
def load_state() -> dict:
    try:
        raw = json.loads(STATE_FILE.read_text())
        raw.setdefault("accumulator", {})
        raw.setdefault("mesh_accumulator", {})
        raw.setdefault("window_start", time.time())
        raw.setdefault("poll_count", 0)
        raw.setdefault("ingested_pings_count", 0)
        return raw
    except (OSError, json.JSONDecodeError):
        return {
            "accumulator": {},
            "mesh_accumulator": {},
            "window_start": time.time(),
            "poll_count": 0,
            "ingested_pings_count": 0,
        }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, separators=(",", ":")))
    tmp.replace(STATE_FILE)


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
            if e.code in (429, 413):
                return False, last_response
            if 500 <= e.code < 600 and attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
                continue
            return False, last_response
        except urllib.error.URLError as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF_BASE_S * (2 ** (attempt - 1)))
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
    def log_message(self, format, *args):
        pass

    def _send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url_path = self.path.split("?")[0]

        if url_path in ("/", "/index.html"):
            index_file = WEB_DIR / "index.html"
            if index_file.exists():
                content = index_file.read_bytes()
            else:
                content = b"<html><body><h1>DedDrop Web Dashboard File Missing</h1></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
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

            if PUBLIC_HOST:
                if PUBLIC_HOST.startswith("http://") or PUBLIC_HOST.startswith("https://"):
                    base_host = PUBLIC_HOST.rstrip("/")
                else:
                    base_host = f"http://{PUBLIC_HOST}"
                meshmapper_url = f"{base_host}/api/wardrive"
            else:
                host_header = self.headers.get("Host") or f"localhost:{WEB_PORT}"
                scheme = "https" if "https" in host_header else "http"
                meshmapper_url = f"{scheme}://{host_header}/api/wardrive"
            effective_key = MESHMAPPER_API_KEY or API_KEY or "YOUR_KEY"
            app_link = f"meshmapper://custom-api?url={meshmapper_url}&key={effective_key}"

            self._send_json({
                "ok": True,
                "tool_name": TOOL_NAME,
                "version": TOOL_VERSION,
                "poll_count": poll_cnt,
                "ingested_pings_count": ingested_pings,
                "accumulator_count": acc_count,
                "mesh_accumulator_count": mesh_count,
                "window_start": win_start,
                "elapsed_hours": elapsed_h,
                "upload_interval_hours": UPLOAD_INTERVAL_HOURS,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "dry_run": DRY_RUN,
                "last_poll_time": last_poll,
                "last_upload": last_up,
                "meshmapper_link": app_link,
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
            snapshots = []
            if SNAPSHOT_DIR.exists():
                for f in sorted(SNAPSHOT_DIR.glob("upload_*.json"), reverse=True)[:20]:
                    try:
                        stat = f.stat()
                        data = json.loads(f.read_text())
                        snapshots.append({
                            "name": f.name,
                            "size": stat.st_size,
                            "aircraft_count": data.get("aircraft_count", 0),
                            "mesh_nodes_count": data.get("mesh_nodes_count", 0),
                            "window_start": data.get("window_start"),
                            "window_end": data.get("window_end"),
                        })
                    except Exception:
                        pass
            self._send_json(snapshots)
            return

        self.send_error(404, "Not Found")

    def do_POST(self):
        global _trigger_poll_now, _trigger_flush_now
        url_path = self.path.split("?")[0]

        if url_path == "/api/wardrive":
            api_key = self.headers.get("X-API-Key", "").strip()
            if MESHMAPPER_API_KEY and api_key != MESHMAPPER_API_KEY and api_key != API_KEY:
                self._send_json({"ok": False, "error": "Unauthorized key"}, status=401)
                return

            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b""
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._send_json({"ok": False, "error": "Invalid JSON"}, status=400)
                return

            pings = payload.get("data", [])
            if not isinstance(pings, list):
                self._send_json({"ok": False, "error": "Invalid payload format"}, status=400)
                return

            new_mesh_records = []
            for ping in pings:
                new_mesh_records.extend(normalize_mesh_ping(ping))

            with _state_lock:
                merge_mesh_records(_global_state["mesh_accumulator"], new_mesh_records)
                _global_state["ingested_pings_count"] = _global_state.get("ingested_pings_count", 0) + len(pings)
                save_state(_global_state)

            log.info("MeshMapper ingest: received %d pings -> %d normalized mesh node(s)",
                     len(pings), len(new_mesh_records))

            self._send_json({"ok": True, "accepted_pings": len(pings), "nodes_merged": len(new_mesh_records)})
            return

        if url_path == "/api/trigger-poll":
            _trigger_poll_now = True
            self._send_json({"ok": True, "message": "Feed poll triggered!"})
            return

        if url_path == "/api/trigger-flush":
            _trigger_flush_now = True
            self._send_json({"ok": True, "message": "Upload flush triggered!"})
            return

        self.send_error(404, "Not Found")


def start_web_server():
    if not WEB_ENABLED:
        return
    try:
        server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), WebRequestHandler)
        log.info("DedDrop Web Dashboard & API running at http://0.0.0.0:%d/", WEB_PORT)
        server.serve_forever()
    except Exception as e:
        log.error("Failed to start web server on port %d: %s", WEB_PORT, e)


# ── Poll + flush ─────────────────────────────────────────────────────────
def do_poll(state: dict) -> None:
    global _last_poll_time
    snapshot = fetch_aircraft_json(TAR1090_URL, REQUEST_TIMEOUT_S)

    if SAVE_LATEST_RAW:
        try:
            LATEST_RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
            LATEST_RAW_PATH.write_text(json.dumps(snapshot, separators=(",", ":")))
        except OSError as e:
            log.warning("could not write %s: %s", LATEST_RAW_PATH, e)

    records, skipped = parse_snapshot(snapshot)

    with _state_lock:
        merge_into(state["accumulator"], records)
        state["poll_count"] += 1
        _last_poll_time = time.time()

    save_state(state)


def do_flush(state: dict, *, force: bool = False) -> bool:
    window_start = state["window_start"]
    elapsed_h = (time.time() - window_start) / 3600
    if not force and elapsed_h < UPLOAD_INTERVAL_HOURS:
        return False

    window_end = time.time()
    with _state_lock:
        aircraft_records = list(state["accumulator"].values())
        mesh_records = list(state["mesh_accumulator"].values())
        poll_count = state["poll_count"]

    log.info("upload window elapsed (%.2fh, %d polls) — flushing %d aircraft, %d mesh nodes",
              elapsed_h, poll_count, len(aircraft_records), len(mesh_records))

    path = save_snapshot(aircraft_records, mesh_records, window_start, window_end, poll_count, SNAPSHOT_DIR)
    log.info("saved snapshot %s", path.name)

    upload_records(aircraft_records, mesh_records, API_KEY, UPLOAD_URL)
    prune_snapshots(SNAPSHOT_DIR, SNAPSHOT_RETENTION)

    with _state_lock:
        state["accumulator"] = {}
        state["mesh_accumulator"] = {}
        state["window_start"] = window_end
        state["poll_count"] = 0

    save_state(state)
    return True


def main() -> int:
    global _global_state, _trigger_poll_now, _trigger_flush_now

    if not TAR1090_URL:
        log.error("TAR1090_URL is not set — point it at your tar1090 aircraft.json")
        return 2
    if not API_KEY:
        log.error("WDGWARS_API_KEY is not set — generate one in your WDGWars profile")
        return 2

    log.info("%s v%s starting — feed=%s upload_url=%s poll=%gs upload_every=%gh dry_run=%s web_port=%d",
              TOOL_NAME, TOOL_VERSION, TAR1090_URL, UPLOAD_URL, POLL_INTERVAL_SECONDS,
              UPLOAD_INTERVAL_HOURS, DRY_RUN, WEB_PORT)

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

        slept = 0.0
        while slept < POLL_INTERVAL_SECONDS and not _shutdown:
            if _trigger_poll_now:
                _trigger_poll_now = False
                log.info("manual poll triggered via web interface")
                break
            if _trigger_flush_now:
                _trigger_flush_now = False
                log.info("manual flush triggered via web interface")
                do_flush(state, force=True)

            step = min(1.0, POLL_INTERVAL_SECONDS - slept)
            time.sleep(step)
            slept += step

    log.info("%s stopped", TOOL_NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
