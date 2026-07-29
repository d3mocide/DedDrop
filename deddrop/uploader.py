"""HMAC-signed batch upload to WDGWars, plus the profile-stats fetch.

Aircraft and mesh nodes are dispatched as separate requests. Bundling them into
one POST hid mesh-side failures: the response carried non-zero aircraft
counters, so the batch read as accepted even when every mesh node in it was
discarded. Sent separately, each feed is judged on its own counters, retried on
its own, and retained or cleared independently of the other.

The meshcore wire shape and its server-side gates are documented by the
reference feeder, Heimdall (Yggdrasil-AI-labs/meshcore-to-wdgwars), which
confirmed them live against wdgwars.pl:

    node_id, node_type, name, lat, lon, rssi, first_seen, type

WDGWars gates each node on a real GPS fix and on ``node_id`` being 8-16
lowercase hex, silently dropping — or explicitly itemising in
``meshcore_reject_reasons`` — anything that misses.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request
from typing import NamedTuple

from . import config, runtime
from .config import log

# Counters that are not specific to either feed. A response carrying one of
# these is reporting on whatever was in the request, which — now that requests
# hold a single feed — is unambiguous.
_SHARED_COUNTERS = ("captured", "updated", "duplicates", "no_gps", "bad_rows")


class Feed(NamedTuple):
    """One record type and the vocabulary WDGWars uses to acknowledge it."""

    name: str            # runtime.last_upload prefix, log wording
    payload_key: str     # key the records travel under in the payload
    imported: tuple      # response keys meaning "stored as new", best first
    seen: tuple          # response keys meaning "already had it", best first
    rejected: tuple      # response keys meaning "read it and refused it"
    reasons: tuple       # response keys holding the per-reason breakdown
    extra: tuple = ()    # further keys that still prove the batch was read

    @property
    def counters(self) -> tuple:
        """Every key whose presence proves the server gave us a verdict.

        A 200 that reports none of them for a non-empty payload means the batch
        was accepted and then dropped on the floor. Rejections count: a refusal
        is a verdict, and re-sending records the server has already refused
        would retry forever.
        """
        return self.imported + self.seen + self.rejected + self.extra + _SHARED_COUNTERS


AIRCRAFT_FEED = Feed(
    name="aircraft",
    payload_key="aircraft",
    imported=("aircraft_imported", "imported"),
    seen=("aircraft_already_seen", "already_seen"),
    rejected=("aircraft_rejected", "rejected"),
    reasons=("aircraft_reject_reasons", "reject_reasons"),
    extra=("merged_samples",),
)

MESH_FEED = Feed(
    name="mesh",
    payload_key="meshcore_nodes",
    imported=("meshcore_imported", "mesh_imported", "imported"),
    seen=("meshcore_already_seen", "mesh_already_seen", "already_seen"),
    rejected=("meshcore_rejected", "mesh_rejected", "rejected"),
    reasons=("meshcore_reject_reasons", "reject_reasons"),
)


class DispatchResult(NamedTuple):
    aircraft_ok: bool
    mesh_ok: bool

    @property
    def ok(self) -> bool:
        return self.aircraft_ok and self.mesh_ok

    def failed_feeds(self) -> list[str]:
        return ([] if self.aircraft_ok else ["aircraft"]) + ([] if self.mesh_ok else ["mesh"])


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


def _counter(data: dict, keys: tuple) -> int:
    """Read the first counter the server actually sent, in preference order."""
    for key in keys:
        if key in data:
            try:
                return int(data[key] or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _reasons(data: dict, keys: tuple) -> dict:
    """Read the per-reason rejection breakdown, e.g. {"bad_node_id": 53}."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, dict):
            return {str(k): v for k, v in value.items()}
    return {}


def send_chunk(feed: Feed, chunk: list[dict], api_key: str, url: str) -> tuple[bool, dict]:
    """POST one chunk of a single feed. Returns (accepted, response body)."""
    # The other lists stay present but empty: this is the payload shape WDGWars
    # already accepts, and only feed.payload_key carries anything.
    payload = {"networks": [], "aircraft": [], "meshcore_nodes": []}
    payload[feed.payload_key] = chunk
    body = json.dumps(build_envelope(payload, api_key)).encode()

    if config.DRY_RUN:
        log.info("[dry-run] would POST %d bytes (%d %s records) to %s",
                 len(body), len(chunk), feed.name, url)
        return True, {"ok": True, "dry_run": True, feed.imported[0]: len(chunk),
                      feed.seen[0]: 0}

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
                log.debug("%s chunk response: HTTP %d %s",
                          feed.name, resp.status, scrub(txt[:800], api_key))
                if resp.status == 200 and data.get("ok") and chunk:
                    if not any(data.get(k) for k in feed.counters):
                        log.warning(
                            "HTTP 200 ok:true but no %s counters for %d records — "
                            "the server accepted the batch and stored none of it: %s",
                            feed.name, len(chunk), scrub(txt[:800], api_key))
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
                log.error("payload too large for %d %s records — lower BATCH_SIZE",
                          len(chunk), feed.name)
                return False, last_response
            if (e.code == 429 or 500 <= e.code < 600) and attempt < config.MAX_ATTEMPTS:
                delay = retry_delay(e, attempt)
                log.warning("%s upload attempt %d/%d failed with HTTP %d — retrying in %.0fs",
                            feed.name, attempt, config.MAX_ATTEMPTS, e.code, delay)
                if runtime.sleep_interruptible(delay):
                    return False, last_response
                continue
            log.error("%s upload failed with HTTP %d: %s",
                      feed.name, e.code, scrub(err_body, api_key))
            return False, last_response

        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < config.MAX_ATTEMPTS:
                delay = config.BACKOFF_BASE_S * (2 ** (attempt - 1))
                log.warning("%s upload attempt %d/%d could not reach %s (%s) — retrying in %.0fs",
                            feed.name, attempt, config.MAX_ATTEMPTS, url, e, delay)
                if runtime.sleep_interruptible(delay):
                    return False, {}
                continue
            return False, {}

    return False, last_response


def _new_totals() -> dict:
    return {"imported": 0, "seen": 0, "rejected": 0, "reasons": {}}


def upload_feed(feed: Feed, records: list[dict], api_key: str, url: str) -> tuple[bool, dict]:
    """Chunk and dispatch one feed. Returns (ok, totals)."""
    totals = _new_totals()
    if not records:
        return True, totals

    batch = config.BATCH_SIZE
    n_chunks = (len(records) - 1) // batch + 1
    ok = True

    for idx in range(n_chunks):
        if runtime.shutdown.is_set():
            # Stop cleanly rather than being SIGKILLed mid-chunk; ok is False so
            # the caller retains this feed's window.
            log.warning("shutdown requested — aborting %s upload after %d/%d chunks",
                        feed.name, idx, n_chunks)
            return False, totals

        chunk = records[idx * batch:(idx + 1) * batch]
        log.info("%s chunk %d/%d: uploading %d records", feed.name, idx + 1, n_chunks, len(chunk))

        chunk_ok, data = send_chunk(feed, chunk, api_key, url)
        ok = ok and chunk_ok
        if chunk_ok:
            totals["imported"] += _counter(data, feed.imported)
            totals["seen"] += _counter(data, feed.seen)
            totals["rejected"] += _counter(data, feed.rejected)
            for reason, count in _reasons(data, feed.reasons).items():
                try:
                    totals["reasons"][reason] = totals["reasons"].get(reason, 0) + int(count or 0)
                except (TypeError, ValueError):
                    totals["reasons"][reason] = totals["reasons"].get(reason, 0)

        is_last = idx == n_chunks - 1
        if not is_last and config.CHUNK_COOLDOWN_S > 0 and not config.DRY_RUN:
            if runtime.sleep_interruptible(config.CHUNK_COOLDOWN_S):
                return False, totals

    if ok:
        _report_feed(feed, len(records), totals)
    return ok, totals


def _report_feed(feed: Feed, sent: int, totals: dict) -> None:
    """Say what became of a dispatched feed, including what it refused.

    Every record should come back imported, already seen, or rejected. WDGWars
    has been seen returning all-zero counters for a payload it itemised as
    rejected moments earlier, so the shortfall is named rather than left to read
    as a clean upload.
    """
    if totals["rejected"]:
        log.error("WDGWars refused %d of %d %s records: %s", totals["rejected"], sent,
                  feed.name, totals["reasons"] or "no reason given")

    accounted = totals["imported"] + totals["seen"] + totals["rejected"]
    if accounted < sent and not config.DRY_RUN:
        log.warning("WDGWars accounted for %d of the %d %s records sent and gave no "
                    "verdict on the other %d — those were NOT stored",
                    accounted, sent, feed.name, sent - accounted)


def upload_records(aircraft_records: list[dict], mesh_records: list[dict], api_key: str,
                   *, aircraft_url: str, mesh_url: str) -> DispatchResult:
    """Dispatch both feeds independently and record the summary."""
    if not aircraft_records and not mesh_records:
        log.info("nothing to upload this cycle")
        return DispatchResult(True, True)

    aircraft_ok, ac_totals = upload_feed(AIRCRAFT_FEED, aircraft_records, api_key, aircraft_url)

    # A shutdown mid-aircraft must not start a fresh mesh request; leaving
    # mesh_ok False keeps that window for the next run.
    if runtime.shutdown.is_set():
        mesh_ok, mesh_totals = not mesh_records, _new_totals()
    else:
        mesh_ok, mesh_totals = upload_feed(MESH_FEED, mesh_records, api_key, mesh_url)

    result = DispatchResult(aircraft_ok, mesh_ok)

    with runtime.lock:
        runtime.last_upload = {
            "timestamp": time.time(),
            "aircraft_count": len(aircraft_records),
            "mesh_count": len(mesh_records),
            "aircraft_imported": ac_totals["imported"],
            "aircraft_seen": ac_totals["seen"],
            "aircraft_rejected": ac_totals["rejected"],
            "mesh_imported": mesh_totals["imported"],
            "mesh_seen": mesh_totals["seen"],
            "mesh_rejected": mesh_totals["rejected"],
            "mesh_reject_reasons": mesh_totals["reasons"],
            "aircraft_success": aircraft_ok,
            "mesh_success": mesh_ok,
            "success": result.ok,
            "dry_run": config.DRY_RUN,
        }

    if result.ok and not config.DRY_RUN:
        log.info("upload accepted: ac sent=%d new=%d seen=%d | mesh sent=%d new=%d seen=%d",
                 len(aircraft_records), ac_totals["imported"], ac_totals["seen"],
                 len(mesh_records), mesh_totals["imported"], mesh_totals["seen"])
    elif not result.ok:
        retained = []
        if not aircraft_ok:
            retained.append(f"{len(aircraft_records)} aircraft")
        if not mesh_ok:
            retained.append(f"{len(mesh_records)} mesh nodes")
        log.error("upload FAILED for %s — retaining %s for retry",
                  " and ".join(result.failed_feeds()), " and ".join(retained))
    return result


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
