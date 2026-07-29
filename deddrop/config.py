"""Environment-driven configuration and logging setup.

Every knob DedDrop has is read here, once, at import time.
"""
from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from . import TOOL_NAME, TOOL_VERSION


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _path(name: str, default: str | Path) -> Path:
    return Path(os.environ.get(name, str(default)))


# ── Feed & credentials ────────────────────────────────────────────────────
TAR1090_URL = os.environ.get("TAR1090_URL", "").strip()
API_KEY = os.environ.get("WDGWARS_API_KEY", "").strip()
MESHMAPPER_API_KEY = (os.environ.get("MESHMAPPER_API_KEY", "").strip() or API_KEY).strip()

UPLOAD_URL = os.environ.get("WDGWARS_API_URL", "https://wdgwars.pl/endpoint/upload/").strip()
# Mesh nodes are dispatched in their own request, so they can be pointed at a
# dedicated route without moving the aircraft feed. Defaults to the same URL.
MESH_UPLOAD_URL = (os.environ.get("WDGWARS_MESH_API_URL", "").strip() or UPLOAD_URL).strip()
ME_URL = os.environ.get("WDGWARS_ME_URL", "https://wdgwars.pl/api/me").strip()

# ── Timing ────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
UPLOAD_INTERVAL_HOURS = float(os.environ.get("UPLOAD_INTERVAL_HOURS", "6"))
RETRY_INTERVAL_MINUTES = float(os.environ.get("RETRY_INTERVAL_MINUTES", "15"))

# ── Storage ───────────────────────────────────────────────────────────────
STATE_FILE = _path("STATE_FILE", "/data/state/accumulator.json")
SNAPSHOT_DIR = _path("SNAPSHOT_DIR", "/data/snapshots")
SNAPSHOT_RETENTION = int(os.environ.get("SNAPSHOT_RETENTION", "200"))
SAVE_LATEST_RAW = _flag("SAVE_LATEST_RAW", "true")
LATEST_RAW_PATH = _path("LATEST_RAW_PATH", "/data/latest_raw.json")

# ── Web dashboard & ingest ────────────────────────────────────────────────
WEB_ENABLED = _flag("WEB_ENABLED", "true")
WEB_BIND = os.environ.get("WEB_BIND", "0.0.0.0").strip()
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_DIR = _path("WEB_DIR", Path(__file__).resolve().parent.parent / "web")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "").strip()

# Cross-origin reads are off by default: /api/* exposes accumulated telemetry.
# Set to a specific origin to opt in.
CORS_ALLOW_ORIGIN = os.environ.get("CORS_ALLOW_ORIGIN", "").strip()

# Content-Length is attacker-controlled, so inbound bodies are capped.
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", str(8 * 1024 * 1024)))

# Minted per process and embedded in the dashboard when it is served. Control
# endpoints require it, so a cross-origin page — which cannot read our HTML
# without CORS — cannot drive them.
CONTROL_TOKEN = secrets.token_urlsafe(32)

# ── Upload tuning ─────────────────────────────────────────────────────────
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "500"))
CHUNK_COOLDOWN_S = float(os.environ.get("CHUNK_COOLDOWN_S", "1"))
REQUEST_TIMEOUT_S = float(os.environ.get("REQUEST_TIMEOUT_S", "60"))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
BACKOFF_BASE_S = float(os.environ.get("BACKOFF_BASE_S", "2"))

# ── Behaviour ─────────────────────────────────────────────────────────────
DRY_RUN = _flag("DRY_RUN", "false")
RUN_ONCE = _flag("RUN_ONCE", "false")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

USER_AGENT = f"{TOOL_NAME}/{TOOL_VERSION}"

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(TOOL_NAME)
