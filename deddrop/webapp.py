"""Dashboard, read-only telemetry API, MeshMapper ingest, and control endpoints.

Auth model: ``/api/wardrive`` takes the ingest API key. Control endpoints take
``X-Control-Token`` — minted per process and injected into the dashboard when it
is served — or the API key. Since a cross-origin page cannot read our HTML
without CORS, the token doubles as CSRF protection.
"""
from __future__ import annotations

import gzip
import hmac
import json
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, runtime, storage
from .config import log
from .normalize import normalize_mesh_ping, merge_mesh_records

_GZIP_MIN_BYTES = 1024


class WebRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{config.TOOL_NAME}/{config.TOOL_VERSION}"
    sys_version = ""

    def log_message(self, format, *args):
        # Useful when an ingest isn't landing, noise otherwise.
        log.debug("web %s - %s", self.address_string(), format % args)

    # ── Responses ─────────────────────────────────────────────────────────
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200,
                    *, no_store: bool = False):
        headers = [("Content-Type", content_type)]
        if len(body) > _GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=6)
            headers.append(("Content-Encoding", "gzip"))

        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        if config.CORS_ALLOW_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", config.CORS_ALLOW_ORIGIN)
            self.send_header("Vary", "Origin")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status: int = 200, *, no_store: bool = False):
        # Compact separators: this is polled every few seconds by the dashboard.
        body = json.dumps(data, separators=(",", ":")).encode("utf-8")
        self._send_bytes(body, "application/json", status, no_store=no_store)

    def _unauthorized(self):
        self._send_json({"ok": False, "error": "Unauthorized"}, status=401, no_store=True)

    # ── Auth ──────────────────────────────────────────────────────────────
    def _authorized_key(self) -> bool:
        presented = self.headers.get("X-API-Key", "").strip()
        if not presented:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            presented = (query.get("key") or [""])[0].strip()
        if not presented:
            return False
        return any(hmac.compare_digest(presented, accepted)
                   for accepted in (config.MESHMAPPER_API_KEY, config.API_KEY) if accepted)

    def _authorized_control(self) -> bool:
        presented = self.headers.get("X-Control-Token", "").strip()
        if presented and hmac.compare_digest(presented, config.CONTROL_TOKEN):
            return True
        return self._authorized_key()

    def _meshmapper_link(self) -> str:
        if config.PUBLIC_HOST:
            base = (config.PUBLIC_HOST.rstrip("/")
                    if config.PUBLIC_HOST.startswith(("http://", "https://"))
                    else f"http://{config.PUBLIC_HOST}")
        else:
            host = self.headers.get("Host") or f"localhost:{config.WEB_PORT}"
            scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
            base = f"{scheme}://{host}"
        key = config.MESHMAPPER_API_KEY or config.API_KEY or "YOUR_KEY"
        # Escape only what would truncate the link; ':' and '/' stay literal so
        # the format matches what MeshMapper already accepts.
        esc = lambda v: urllib.parse.quote(v, safe=":/")  # noqa: E731
        return f"meshmapper://custom-api?url={esc(base)}/api/wardrive&key={esc(key)}"

    # ── GET ───────────────────────────────────────────────────────────────
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        handler = {
            "/": self._get_index,
            "/index.html": self._get_index,
            "/healthz": self._get_healthz,
            "/api/status": self._get_status,
            "/api/aircraft": self._get_aircraft,
            "/api/mesh-nodes": self._get_mesh_nodes,
            "/api/snapshots": self._get_snapshots,
            "/api/user-stats": self._get_user_stats,
            "/api/meshmapper-link": self._get_meshmapper_link,
        }.get(path)
        if handler is None:
            self.send_error(404, "Not Found")
            return
        handler()

    def _get_index(self):
        try:
            content = (config.WEB_DIR / "index.html").read_text(encoding="utf-8")
        except OSError:
            self._send_bytes(
                b"<!doctype html><meta charset=utf-8>"
                b"<h1>DedDrop dashboard file missing</h1>"
                b"<p>Expected <code>web/index.html</code>.</p>",
                "text/html; charset=utf-8", status=500)
            return
        content = content.replace("__CONTROL_TOKEN__", config.CONTROL_TOKEN)
        self._send_bytes(content.encode("utf-8"), "text/html; charset=utf-8", no_store=True)

    def _get_healthz(self):
        self._send_json({"ok": True, "version": config.TOOL_VERSION}, no_store=True)

    def _get_status(self):
        with runtime.lock:
            state = runtime.state
            payload = {
                "poll_count": state.get("poll_count", 0),
                "ingested_pings_count": state.get("ingested_pings_count", 0),
                "accumulator_count": len(state.get("accumulator", {})),
                "mesh_accumulator_count": len(state.get("mesh_accumulator", {})),
                "window_start": state.get("window_start", time.time()),
                "last_poll_skipped": runtime.last_skipped,
                "last_poll_time": runtime.last_poll_time,
                "last_upload": dict(runtime.last_upload),
                "retry_pending_in": max(0.0, runtime.next_flush_attempt - time.time()),
            }

        payload.update({
            "ok": True,
            "tool_name": config.TOOL_NAME,
            "version": config.TOOL_VERSION,
            "elapsed_hours": max(0.0, (time.time() - payload["window_start"]) / 3600),
            "upload_interval_hours": config.UPLOAD_INTERVAL_HOURS,
            "poll_interval_seconds": config.POLL_INTERVAL_SECONDS,
            "dry_run": config.DRY_RUN,
        })
        # meshmapper_link is intentionally absent: it embeds the API key and this
        # endpoint is unauthenticated. See /api/meshmapper-link.
        self._send_json(payload)

    def _get_aircraft(self):
        with runtime.lock:
            records = list(runtime.state.get("accumulator", {}).values())
        self._send_json(records)

    def _get_mesh_nodes(self):
        with runtime.lock:
            records = list(runtime.state.get("mesh_accumulator", {}).values())
        self._send_json(records)

    def _get_snapshots(self):
        self._send_json(storage.list_snapshots(config.SNAPSHOT_DIR))

    def _get_user_stats(self):
        from .uploader import fetch_user_stats
        self._send_json(fetch_user_stats())

    def _get_meshmapper_link(self):
        if not self._authorized_control():
            self._unauthorized()
            return
        self._send_json({"ok": True, "meshmapper_link": self._meshmapper_link()},
                        no_store=True)

    # ── POST ──────────────────────────────────────────────────────────────
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/wardrive":
            self._post_wardrive()
        elif path in ("/api/trigger-poll", "/api/trigger-flush"):
            self._post_trigger(path)
        else:
            self.send_error(404, "Not Found")

    def _read_body(self) -> bytes | None:
        """Read the body, refusing anything over MAX_BODY_BYTES."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._send_json({"ok": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if length < 0:
            self._send_json({"ok": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if length > config.MAX_BODY_BYTES:
            log.warning("rejected %d byte body from %s (limit %d)",
                        length, self.address_string(), config.MAX_BODY_BYTES)
            self._send_json({"ok": False, "error": "Payload too large"}, status=413)
            return None
        if length == 0:
            return b""
        try:
            body = self.rfile.read(length)
        except (OSError, ConnectionError) as e:
            log.debug("could not read request body: %s", e)
            return None
        if len(body) != length:
            self._send_json({"ok": False, "error": "Truncated body"}, status=400)
            return None
        return body

    def _post_wardrive(self):
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

        records = []
        for ping in pings:
            if isinstance(ping, dict):
                records.extend(normalize_mesh_ping(ping))

        with runtime.lock:
            merge_mesh_records(runtime.state["mesh_accumulator"], records)
            runtime.state["ingested_pings_count"] = (
                runtime.state.get("ingested_pings_count", 0) + len(pings))
        storage.save_state(runtime.state)

        log.info("MeshMapper ingest: received %d pings -> %d normalized mesh node(s)",
                 len(pings), len(records))
        self._send_json({"ok": True, "accepted_pings": len(pings),
                         "nodes_merged": len(records)})

    def _post_trigger(self, path: str):
        if not self._authorized_control():
            self._unauthorized()
            return
        if path == "/api/trigger-poll":
            runtime.poll_now.set()
            self._send_json({"ok": True, "message": "Feed poll triggered!"})
        else:
            runtime.flush_now.set()
            self._send_json({"ok": True, "message": "Upload flush triggered!"})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_web_server():
    if not config.WEB_ENABLED:
        return
    try:
        server = _Server((config.WEB_BIND, config.WEB_PORT), WebRequestHandler)
    except OSError as e:
        log.error("could not bind web server to %s:%d (%s) — continuing headless",
                  config.WEB_BIND, config.WEB_PORT, e)
        return

    log.info("DedDrop dashboard & ingest API listening on http://%s:%d/",
             config.WEB_BIND, config.WEB_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception:
        log.exception("web server stopped unexpectedly")
    finally:
        server.server_close()
