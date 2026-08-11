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

# A MeshCore node's on-air name is the leading bytes of its Ed25519 public key,
# so repeater_id is a 1-3 byte prefix of a 32-byte key — 2-6 hex, under the
# server's 8-hex floor. Where the full key travels with the ping we widen the id
# to the first 8 bytes of that same key: the same identity with more digits.
#
# 8 bytes is the canonical length, confirmed with LOCOSP (2026-08-10): node_id is
# varchar(16), and shorter prefixes collide across live nodes — his importer
# updates position on a node_id match, so a collision is two repeaters
# overwriting each other's coordinates.
_NODE_ID_HEX = 16

# Exactly 64 hex — a full Ed25519 key. Anything else is not something we can
# safely slice an identity out of, and the server refuses it as bad_public_key.
_PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

# The MeshMapper push payload has never been observed here, and the exported
# files the reference feeder reads call this public_key. Accept the obvious
# spellings rather than miss the key over a capitalisation.
_PUBLIC_KEY_FIELDS = ("public_key", "publicKey", "pubkey", "pub_key", "public_key_hex")

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
def public_key_of(ping: dict) -> str | None:
    """Return the ping's full public key, or None if it doesn't carry a usable one."""
    for field in _PUBLIC_KEY_FIELDS:
        raw = ping.get(field)
        if raw in (None, "", "None"):
            continue
        key = str(raw).lower().strip()
        if _PUBLIC_KEY_RE.match(key):
            return key
        log.debug("ping field %s=%r is not a 64-hex public key — ignoring it", field, raw)
    return None


def index_public_keys(pings: list) -> dict[str, str]:
    """Map each short-id prefix seen in one capture to the single key it names.

    ``heard_repeats`` tokens carry a short id and no key, and a DISC ping can
    arrive without one, so a key seen anywhere in the same push is allowed to
    name that node. A prefix two keys share maps to "" and stays unresolved:
    guessing there would merge two repeaters onto one id.
    """
    index: dict[str, str] = {}
    keys = {key for ping in pings if isinstance(ping, dict) and (key := public_key_of(ping))}
    for key in keys:
        for length in range(1, _NODE_ID_HEX):
            prefix = key[:length]
            if index.setdefault(prefix, key) != key:
                index[prefix] = ""
    return index


def derive_node_id(short_id: str, public_key: str | None) -> tuple[str, str | None]:
    """Widen a short on-air id to the canonical 8-byte form. Returns (id, key).

    The key is used only when the short id is actually its leading hex, so a
    ping that pairs an id with someone else's key cannot rename a node into that
    other node's identity — it keeps the short id and is reported as a
    predicted rejection instead.
    """
    if not public_key:
        return short_id, None
    if not public_key.startswith(short_id):
        log.warning("mesh node %s came with public key %s… which does not start with it — "
                    "keeping the short id rather than adopting another node's identity",
                    short_id, public_key[:12])
        return short_id, None
    return public_key[:_NODE_ID_HEX], public_key


def normalize_mesh_ping(ping: dict, key_index: dict[str, str] | None = None) -> list[dict]:
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

    index = key_index or {}

    def node(short_id: str, node_type: str = "REPEATER", rssi: int | None = None,
             public_key: str | None = None) -> dict:
        node_id, key = derive_node_id(short_id, public_key or index.get(short_id) or None)
        record = {
            "node_id": node_id,
            "node_type": node_type,
            "name": node_id,
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "rssi": rssi,
            "first_seen": first_seen,
            "type": "MESHCORE",
        }
        # Optional on the wire: the server checks node_id is this key's prefix
        # and never rejects a record for its absence. LOCOSP collects the keys so
        # the canonical id form can be re-derived later without feeders changing.
        if key:
            record["public_key"] = key
        return record

    records = []

    if p_type in ("DISC", "TRACE"):
        rep_id = (ping.get("repeater_id") or "").lower().strip()
        if rep_id and rep_id != "none":
            node_type = (ping.get("node_type") or "REPEATER").upper()
            if node_type == "R":
                node_type = "REPEATER"
            records.append(node(rep_id, node_type, _coerce_int_opt(ping.get("local_rssi")),
                                public_key_of(ping)))

    heard = ping.get("heard_repeats")
    if heard and heard != "None":
        for tok in (t.strip() for t in str(heard).split(",")):
            match = _HEARD_TOKEN_RE.match(tok) if tok else None
            if match:
                # A heard token names a node the receiver relayed through, not the
                # one that sent this ping, so its key is never the ping's own.
                records.append(node(match.group(1).lower()))

    return records


def normalize_mesh_capture(pings: list) -> list[dict]:
    """Normalize a whole push together, so a key in one ping can name a node in another."""
    index = index_public_keys(pings)
    records = []
    for ping in pings:
        if isinstance(ping, dict):
            records.extend(normalize_mesh_ping(ping, index))
    return records


# WDGWars gates every mesh node on this shape before storing it, and reports
# the misses only afterwards, as meshcore_reject_reasons: {"bad_node_id": n}.
# Confirmed against wdgwars.pl by the reference feeder, Heimdall
# (Yggdrasil-AI-labs/meshcore-to-wdgwars, 2026-07-03).
_SERVER_NODE_ID_GATE = re.compile(r"^[0-9a-f]{8,16}$")


def predict_rejections(records: list[dict]) -> list[str]:
    """Mirror the server's per-record gates so a refusal is explained up front.

    A node_id is only short now when nothing in its capture carried the node's
    public key — either the ping arrived without one or two keys shared the
    prefix, so widening it would have been a guess.
    """
    warnings = []

    short = [r for r in records if not _SERVER_NODE_ID_GATE.match(r.get("node_id") or "")]
    if short:
        warnings.append(
            f"{len(short)} of {len(records)} mesh node_ids are outside the 8-16 lowercase "
            f"hex range WDGWars requires and will come back as bad_node_id. These nodes "
            f"were heard without a public_key, and no other ping in the same capture "
            f"resolved their prefix, so the 2-6 hex on-air id is all DedDrop has for them.")

    mismatched = [r for r in records if (key := r.get("public_key"))
                  and not (_PUBLIC_KEY_RE.match(key) and key.startswith(r.get("node_id") or "x"))]
    if mismatched:
        warnings.append(
            f"{len(mismatched)} of {len(records)} mesh nodes carry a public_key that is not "
            f"a 64-hex key beginning with their node_id and will come back as "
            f"bad_public_key or key_prefix_mismatch.")

    no_gps = [r for r in records if not r.get("lat") and not r.get("lon")]
    if no_gps:
        warnings.append(f"{len(no_gps)} of {len(records)} mesh nodes have no GPS fix "
                        f"(lat/lon 0,0) and will come back as no_gps.")

    return warnings


# ── Ingest diagnostics ────────────────────────────────────────────────────
# The MeshMapper push payload is undocumented and nothing here has seen one, so
# whether it carries public_key is answered by looking at a real push rather
# than by reasoning about it. These fields are never telemetry, so a sample of a
# raw ping cannot leak one by accident.
_SECRET_FIELDS = ("api_key", "apikey", "token", "secret", "password", "auth")
_SAMPLE_VALUE_CHARS = 256


def _sample_of(ping: dict) -> dict:
    """A raw ping, trimmed enough to sit in a JSON response and a log line."""
    sample = {}
    for field, value in ping.items():
        name = str(field)
        if any(marker in name.lower() for marker in _SECRET_FIELDS):
            sample[name] = "<redacted>"
        elif isinstance(value, str) and len(value) > _SAMPLE_VALUE_CHARS:
            sample[name] = value[:_SAMPLE_VALUE_CHARS] + "…"
        else:
            sample[name] = value
    return sample


def describe_mesh_ingest(pings: list, records: list[dict]) -> dict:
    """Report what a push actually contained, so the key question is answerable.

    Names every field the pings carried, whether any of them held a public key,
    and how many node_ids that let us widen past the server's 8-hex floor.
    """
    dicts = [p for p in pings if isinstance(p, dict)]
    fields: dict[str, int] = {}
    for ping in dicts:
        for field in ping:
            fields[str(field)] = fields.get(str(field), 0) + 1

    keyed = [p for p in dicts if public_key_of(p)]
    present = next((f for f in _PUBLIC_KEY_FIELDS if f in fields), None)
    widened = [r for r in records if _SERVER_NODE_ID_GATE.match(r.get("node_id") or "")]

    if keyed:
        verdict = (f"pings carry a usable public key ({len(keyed)}/{len(dicts)}) — "
                   f"node_ids are derived from it")
    elif present:
        verdict = (f"pings carry a {present} field but no value in this push was a 64-hex "
                   f"key, so node_ids stay short and WDGWars will refuse them")
    else:
        verdict = ("no public key field in this push — node_ids stay 2-6 hex and WDGWars "
                   "will refuse them as bad_node_id")

    return {
        "pings": len(dicts),
        "fields": sorted(fields),
        "field_counts": fields,
        "public_key_field": present,
        "pings_with_public_key": len(keyed),
        "nodes": len(records),
        "nodes_with_public_key": len([r for r in records if r.get("public_key")]),
        "nodes_passing_node_id_gate": len(widened),
        "nodes_short_id": len(records) - len(widened),
        "sample_ping": _sample_of(keyed[0] if keyed else dicts[0]) if dicts else {},
        "verdict": verdict,
    }


def merge_mesh_records(mesh_acc: dict[str, dict], new_records: list[dict]) -> None:
    for rec in new_records:
        node_id = rec["node_id"]
        existing = mesh_acc.get(node_id)
        if existing:
            rec["first_seen"] = existing["first_seen"]
        mesh_acc[node_id] = rec
