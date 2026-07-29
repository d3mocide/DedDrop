"""Pure translation of tar1090 and MeshMapper payloads into WDGWars records.

Nothing here touches shared state or the network, so it is directly testable.
"""
from __future__ import annotations

import gzip
import json
import re
import urllib.request
from datetime import datetime, timezone

from .config import USER_AGENT, log

_HEX_ICAO = frozenset("0123456789ABCDEF")
_HEARD_TOKEN_RE = re.compile(r"^([0-9A-Fa-f]{2,})(?:\(([A-Za-z])\))?\(([-+]?\d+(?:\.\d+)?)\)$")

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


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


def _coerce_int(v) -> int:
    if v is None:
        return 0
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _coerce_int_opt(v) -> int | None:
    """Like _coerce_int but keeps 'absent' distinct from 'zero'.

    An aircraft with no ``track`` is not heading due north and one with no
    ``gs`` is not stationary, so unknown telemetry stays null on the wire.
    """
    if v is None:
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


# ── ADS-B ─────────────────────────────────────────────────────────────────
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
    return hex_id, {
        "icao": hex_id,
        "callsign": (ac.get("flight") or "").strip(),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        # "ground" is a real reading of 0 ft, unlike a missing field.
        "alt_ft": 0 if alt_baro == "ground" else _coerce_int_opt(alt_baro),
        "speed_kt": _coerce_int_opt(ac.get("gs")),
        "heading": _coerce_int_opt(ac.get("track")),
        "first_seen": ts_str,
        "type": "ADSB",
    }


def parse_snapshot(snapshot: dict) -> tuple[dict[str, dict], int]:
    """Return (records keyed by ICAO, count of aircraft that were unusable)."""
    now_ts = snapshot.get("now")
    try:
        ts_str = (datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime(_TS_FMT)
                  if now_ts else _now_str())
    except (TypeError, ValueError, OSError, OverflowError):
        ts_str = _now_str()

    records: dict[str, dict] = {}
    skipped = 0
    for ac in snapshot.get("aircraft", []):
        result = normalize_one(ac, ts_str) if isinstance(ac, dict) else None
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


# ── MeshMapper ────────────────────────────────────────────────────────────
def normalize_mesh_ping(ping: dict) -> list[dict]:
    p_type = (ping.get("type") or "").upper().strip()
    lat, lon = ping.get("lat"), ping.get("lon")
    if lat is None or lon is None:
        return []
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return []

    # (0, 0) means the receiver had no GPS fix, not a node in the Gulf of Guinea.
    if (lat == 0 and lon == 0) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return []

    ts = ping.get("timestamp")
    first_seen = _now_str()
    if ts:
        try:
            first_seen = datetime.fromtimestamp(ts, tz=timezone.utc).strftime(_TS_FMT)
        except (TypeError, ValueError, OSError, OverflowError):
            log.debug("mesh ping had unusable timestamp %r — using arrival time", ts)

    def node(node_id: str, node_type: str = "REPEATER", rssi: int | None = None) -> dict:
        return {
            "node_id": node_id,
            "node_type": node_type,
            "name": node_id,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rssi": rssi,
            "first_seen": first_seen,
            "type": "MESHCORE",
        }

    records = []

    if p_type in ("DISC", "TRACE"):
        rep_id = (ping.get("repeater_id") or "").lower().strip()
        if rep_id and rep_id != "none":
            node_type = (ping.get("node_type") or "REPEATER").upper()
            if node_type == "R":
                node_type = "REPEATER"
            records.append(node(rep_id, node_type, _coerce_int_opt(ping.get("local_rssi"))))

    heard = ping.get("heard_repeats")
    if heard and heard != "None":
        for tok in (t.strip() for t in str(heard).split(",")):
            match = _HEARD_TOKEN_RE.match(tok) if tok else None
            if match:
                records.append(node(match.group(1).lower()))

    return records


# WDGWars gates every mesh node on this shape before storing it, and reports
# the misses only afterwards, as meshcore_reject_reasons: {"bad_node_id": n}.
# Confirmed against wdgwars.pl by the reference feeder, Heimdall
# (Yggdrasil-AI-labs/meshcore-to-wdgwars, 2026-07-03).
_SERVER_NODE_ID_GATE = re.compile(r"^[0-9a-f]{8,16}$")


def predict_rejections(records: list[dict]) -> list[str]:
    """Mirror the server's per-record gates so a refusal is explained up front.

    MeshMapper only ever exposes a 2-6 hex tail of a node's public key, which
    is under the server's 8-hex floor, so nothing here is fixable by reshaping
    the record — saying so beats leaving a rejected count as the only clue.
    """
    warnings = []

    short = [r for r in records if not _SERVER_NODE_ID_GATE.match(r.get("node_id") or "")]
    if short:
        warnings.append(
            f"{len(short)} of {len(records)} mesh node_ids are outside the 8-16 lowercase "
            f"hex range WDGWars requires and will come back as bad_node_id. MeshMapper "
            f"exports only carry a 2-6 hex tail of the node's public key, so this cannot "
            f"be fixed from here — the full key never reaches DedDrop.")

    no_gps = [r for r in records if not r.get("lat") and not r.get("lon")]
    if no_gps:
        warnings.append(f"{len(no_gps)} of {len(records)} mesh nodes have no GPS fix "
                        f"(lat/lon 0,0) and will come back as no_gps.")

    return warnings


def merge_mesh_records(mesh_acc: dict[str, dict], new_records: list[dict]) -> None:
    for rec in new_records:
        node_id = rec["node_id"]
        existing = mesh_acc.get(node_id)
        if existing:
            rec["first_seen"] = existing["first_seen"]
        mesh_acc[node_id] = rec
