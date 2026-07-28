"""HMAC-signed batch upload to WDGWars, plus the profile-stats fetch."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request

from . import config, runtime
from .config import log

# Counter names WDGWars may return. A 200 that reports none of them for a
# non-empty payload means the batch was accepted but not stored.
KNOWN_COUNTERS = (
    "aircraft_imported", "aircraft_already_seen",
    "meshcore_imported", "meshcore_already_seen",
    "imported", "captured", "updated", "duplicates",
    "merged_samples", "already_seen", "no_gps", "bad_rows",
)


def scrub(text: str, key: str) -> str:
    """Redact a secret if it shows up in a log line or server response."""
    if key and key in text:
        return text.replace(key, f"{key[:4]}…{key[-4:]}")
    return text


def build_envelope(payload: dict, api_key: str) -> dict:
    body_json = json.dumps(payload, separators=(",", ":"))
    data_b64 = base64.b64encode(body_json.encode()).decode()
    nonce = secrets.token_hex(8)
    sig = hmac.new(api_key.encode(), (nonce + data_b64).encode(), hashlib.sha256).hexdigest()
    return {"data": data_b64, "nonce": nonce, "sig": sig}


def retry_delay(err: urllib.error.HTTPError, attempt: int) -> float:
    """Honour Retry-After when the server sends one, else exponential backoff."""
    backoff = config.BACKOFF_BASE_S * (2 ** (attempt - 1))
    retry_after = err.headers.get("Retry-After") if err.headers else None
    if retry_after:
        try:
            return max(backoff, min(float(retry_after), 300.0))
        except (TypeError, ValueError):
            pass
    return backoff


def send_chunk(aircraft_chunk: list[dict], mesh_chunk: list[dict],
               api_key: str, url: str) -> tuple[bool, dict]:
    payload = {"networks": [], "aircraft": aircraft_chunk, "meshcore_nodes": mesh_chunk}
    body = json.dumps(build_envelope(payload, api_key)).encode()

    if config.DRY_RUN:
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
    for attempt in range(1, config.MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": api_key,
                "User-Agent": config.USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
                txt = resp.read().decode("utf-8", "replace")
                data = json.loads(txt) if txt else {}
                if resp.status == 200 and data.get("ok") and (aircraft_chunk or mesh_chunk):
                    if not any(data.get(k) for k in KNOWN_COUNTERS):
                        log.warning(
                            "HTTP 200 ok:true but counters zero for payload (%d ac, %d mesh): %s",
                            len(aircraft_chunk), len(mesh_chunk), scrub(txt[:800], api_key))
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
            if (e.code == 429 or 500 <= e.code < 600) and attempt < config.MAX_ATTEMPTS:
                delay = retry_delay(e, attempt)
                log.warning("upload attempt %d/%d failed with HTTP %d — retrying in %.0fs",
                            attempt, config.MAX_ATTEMPTS, e.code, delay)
                if runtime.sleep_interruptible(delay):
                    return False, last_response
                continue
            return False, last_response

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < config.MAX_ATTEMPTS:
                delay = config.BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning("upload attempt %d/%d could not reach %s (%s) — retrying in %.0fs",
                            attempt, config.MAX_ATTEMPTS, url, e, delay)
                if runtime.sleep_interruptible(delay):
                    return False, {}
                continue
            return False, {}

    return False, last_response


def upload_records(aircraft_records: list[dict], mesh_records: list[dict],
                   api_key: str, url: str) -> bool:
    if not aircraft_records and not mesh_records:
        log.info("nothing to upload this cycle")
        return True

    ok = True
    totals = {"ac_imported": 0, "ac_seen": 0, "mesh_imported": 0, "mesh_seen": 0}

    batch = config.BATCH_SIZE
    max_len = max(len(aircraft_records), len(mesh_records))
    n_chunks = (max_len - 1) // batch + 1

    for idx in range(n_chunks):
        if runtime.shutdown.is_set():
            # Stop cleanly rather than being SIGKILLed mid-chunk; ok is False so
            # the caller retains the window.
            log.warning("shutdown requested — aborting upload after %d/%d chunks", idx, n_chunks)
            ok = False
            break

        ac_chunk = aircraft_records[idx * batch:(idx + 1) * batch]
        mesh_chunk = mesh_records[idx * batch:(idx + 1) * batch]

        log.info("chunk %d/%d: uploading %d aircraft, %d mesh nodes",
                 idx + 1, n_chunks, len(ac_chunk), len(mesh_chunk))

        chunk_ok, data = send_chunk(ac_chunk, mesh_chunk, api_key, url)
        ok = ok and chunk_ok
        if chunk_ok:
            totals["ac_imported"] += int(data.get("aircraft_imported", data.get("imported", 0)) or 0)
            totals["ac_seen"] += int(data.get("aircraft_already_seen", data.get("already_seen", 0)) or 0)
            totals["mesh_imported"] += int(data.get("meshcore_imported", data.get("mesh_imported", 0)) or 0)
            totals["mesh_seen"] += int(data.get("meshcore_already_seen", 0) or 0)

        is_last = idx == n_chunks - 1
        if not is_last and config.CHUNK_COOLDOWN_S > 0 and not config.DRY_RUN:
            if runtime.sleep_interruptible(config.CHUNK_COOLDOWN_S):
                ok = False
                break

    with runtime.lock:
        runtime.last_upload = {
            "timestamp": time.time(),
            "aircraft_count": len(aircraft_records),
            "mesh_count": len(mesh_records),
            "aircraft_imported": totals["ac_imported"],
            "aircraft_seen": totals["ac_seen"],
            "mesh_imported": totals["mesh_imported"],
            "mesh_seen": totals["mesh_seen"],
            "success": ok,
            "dry_run": config.DRY_RUN,
        }

    if ok and not config.DRY_RUN:
        log.info("upload accepted: ac sent=%d new=%d seen=%d | mesh sent=%d new=%d seen=%d",
                 len(aircraft_records), totals["ac_imported"], totals["ac_seen"],
                 len(mesh_records), totals["mesh_imported"], totals["mesh_seen"])
    elif not ok:
        log.error("upload FAILED — retaining %d aircraft and %d mesh nodes for retry",
                  len(aircraft_records), len(mesh_records))
    return ok


def fetch_user_stats(force: bool = False) -> dict:
    now = time.time()
    if not force and runtime.user_stats and (now - runtime.user_stats_updated < 60):
        return runtime.user_stats
    if not config.API_KEY:
        return {}

    req = urllib.request.Request(config.ME_URL, headers={
        "X-API-Key": config.API_KEY,
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ok"):
            with runtime.lock:
                runtime.user_stats = data
                runtime.user_stats_updated = now
            return data
    except (urllib.error.URLError, TimeoutError, OSError,
            json.JSONDecodeError, UnicodeDecodeError) as e:
        log.debug("could not fetch user stats: %s", scrub(str(e), config.API_KEY))
    return runtime.user_stats


def validate_api_key() -> None:
    data = fetch_user_stats(force=True)
    if data.get("ok"):
        log.info("API key OK — user=%s wifi=%s ble=%s aircraft=%s mesh=%s total=%s",
                 data.get("username"), data.get("wifi", 0), data.get("ble", 0),
                 data.get("aircraft", 0), data.get("mesh", 0), data.get("total", 0))
