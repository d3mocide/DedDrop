"""Dashboard, read-only telemetry API, MeshMapper ingest, and control endpoints.

Auth model: ``/api/wardrive`` takes the ingest API key. Control endpoints take
``X-Control-Token`` — minted per process and injected into the dashboard when it
is served — or the API key. Since a cross-origin page cannot read our HTML
without CORS, the token doubles as CSRF protection.
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import hmac
import json
import posixpath
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, runtime, storage
from .config import log
from .normalize import describe_mesh_ingest, merge_mesh_records, normalize_mesh_capture

_GZIP_MIN_BYTES = 1024

# Only these extensions are ever served from WEB_DIR.
_STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

# No inline scripts or styles remain in the dashboard, so this can be strict:
# an injected <script> or onerror= handler simply will not execute.
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'")


class WebRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"{config.TOOL_NAME}/{config.TOOL_VERSION}"
    sys_version = ""

    def log_message(self, format, *args):
        # Useful when an ingest isn't landing, noise otherwise.
        log.debug("web %s - %s", self.address_string(), format % args)

    # ── Responses ─────────────────────────────────────────────────────────
    def _send_bytes(self, body: bytes, content_type: str, status: int = 200,
                    *, no_store: bool = False, extra_headers: list | None = None):
        headers = [("Content-Type", content_type)]
        if len(body) > _GZIP_MIN_BYTES and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, compresslevel=6)
            headers.append(("Content-Encoding", "gzip"))
        headers.extend(extra_headers or [])

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
        """Build the deep link MeshMapper imports from the clipboard.

        The URL carries no scheme on purpose: MeshMapper prepends https:// to
        whatever it is given, so sending "http://host" produces the unusable
        "https://http://host/api/wardrive". A scheme in PUBLIC_HOST is stripped
        for the same reason.
        """
        if config.PUBLIC_HOST:
            base = config.PUBLIC_HOST.split("://", 1)[-1].strip("/")
        else:
            base = self.headers.get("Host") or f"localhost:{config.WEB_PORT}"
        key = config.MESHMAPPER_API_KEY or config.API_KEY or "YOUR_KEY"
        # Escape only what would truncate the link; ':' and '/' stay literal so
        # the format matches what MeshMapper already accepts.
        esc = lambda v: urllib.parse.quote(v, safe=":/")  # noqa: E731
        return f"meshmapper://custom-api?url={esc(base)}/api/wardrive&key={esc(key)}"

    # ── OPTIONS ───────────────────────────────────────────────────────────
    def do_OPTIONS(self):
        """CORS preflight.

        A cross-origin POST to /api/wardrive carries X-API-Key and a JSON
        content type, so the browser preflights it. Without an answer here the
        base handler replies 501 and the real request is never sent, which
        looks exactly like CORS_ALLOW_ORIGIN having no effect.
        """
        if not config.CORS_ALLOW_ORIGIN:
            self.send_response(405)
            self.send_header("Allow", "GET, POST")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", config.CORS_ALLOW_ORIGIN)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, X-API-Key, X-Control-Token")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

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
            "/api/mesh-ingest-report": self._get_mesh_ingest_report,
        }.get(path)
        if handler is not None:
            handler()
            return
        # Anything else may be a dashboard asset (/app.css, /js/main.js, ...).
        if not self._get_static(path):
            self.send_error(404, "Not Found")

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
        # The page carries a per-process token, so it must never be cached.
        content = content.replace("__CONTROL_TOKEN__", config.CONTROL_TOKEN)
        self._send_bytes(content.encode("utf-8"), "text/html; charset=utf-8",
                         no_store=True, extra_headers=[("Content-Security-Policy", _CSP)])

    def _resolve_static(self, url_path: str) -> Path | None:
        """Map a URL path to a file inside WEB_DIR, or None if it isn't allowed.

        Rejects anything that escapes WEB_DIR, is a symlink out of it, or has an
        extension that isn't explicitly served.
        """
        # normpath collapses ".." before it can be used, and a leading "/" keeps
        # the result anchored so "../" cannot climb past the root.
        clean = posixpath.normpath("/" + urllib.parse.unquote(url_path)).lstrip("/")
        if not clean or clean == ".":
            return None
        # A NUL or control character can smuggle an allowed-looking extension
        # past the check below, and reaches the filesystem call as a ValueError.
        if any(ch < " " or ch == "\x7f" for ch in clean):
            return None

        candidate = config.WEB_DIR / clean
        if candidate.suffix.lower() not in _STATIC_TYPES:
            return None
        try:
            # strict=True resolves symlinks, so a link pointing outside WEB_DIR
            # fails the containment check below rather than being followed.
            resolved = candidate.resolve(strict=True)
            root = config.WEB_DIR.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if not resolved.is_file() or not resolved.is_relative_to(root):
            log.warning("refused static path outside WEB_DIR: %s", url_path)
            return None
        return resolved

    def _get_static(self, url_path: str) -> bool:
        path = self._resolve_static(url_path)
        if path is None:
            return False
        try:
            body = path.read_bytes()
            stat = path.stat()
        except OSError:
            return False

        # Weak validator over content: assets change only on redeploy, so a
        # conditional request is almost always a 304.
        etag = f'W/"{hashlib.sha256(body).hexdigest()[:16]}-{stat.st_size:x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return True

        self._send_bytes(
            body, _STATIC_TYPES[path.suffix.lower()],
            extra_headers=[
                ("ETag", etag),
                # "no-cache" means revalidate, not "don't store" — the browser
                # keeps the bytes and we answer with a 304.
                ("Cache-Control", "no-cache"),
                ("Content-Security-Policy", _CSP),
            ])
        return True

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

    def _get_mesh_ingest_report(self):
        """What the last MeshMapper push contained, verbatim sample included.

        Behind the control token: a raw ping is more than the aggregate counts
        the other read-only endpoints expose.
        """
        if not self._authorized_control():
            self._unauthorized()
            return
        with runtime.lock:
            report = dict(runtime.mesh_ingest)
        if not report:
            report = {"verdict": "no MeshMapper push has arrived since this process started"}
        self._send_json({"ok": True, **report}, no_store=True)

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

        # The whole push is normalized together: a public key in one ping names
        # the same node in another that carried only its short id.
        records = normalize_mesh_capture(pings)
        self._record_ingest_shape(pings, records)

        with runtime.lock:
            merge_mesh_records(runtime.state["mesh_accumulator"], records)
            runtime.state["ingested_pings_count"] = (
                runtime.state.get("ingested_pings_count", 0) + len(pings))
        storage.save_state(runtime.state)

        log.info("MeshMapper ingest: received %d pings -> %d normalized mesh node(s)",
                 len(pings), len(records))
        self._send_json({"ok": True, "accepted_pings": len(pings),
                         "nodes_merged": len(records)})

    def _record_ingest_shape(self, pings: list, records: list) -> None:
        """Keep, and announce once, what a real MeshMapper push looks like.

        Whether the push carries ``public_key`` decides whether mesh nodes clear
        the server's node_id gate at all, and it cannot be settled from the
        MeshMapper docs. The first push says so in the log at INFO — no debug
        level needed — and every push is kept for /api/mesh-ingest-report.
        """
        report = describe_mesh_ingest(pings, records)
        report["timestamp"] = time.time()

        with runtime.lock:
            previous = runtime.mesh_ingest
            runtime.mesh_ingest = report

        # Repeat only when the shape changes: MeshMapper pushes continuously and
        # this is a description of the feed, not an event.
        if previous.get("fields") == report["fields"]:
            return
        log.info("MeshMapper push shape: %d ping(s) carrying %s",
                 report["pings"], ", ".join(report["fields"]) or "no fields")
        log.info("MeshMapper push: %s", report["verdict"])
        log.info("mesh node_ids clearing the WDGWars gate: %d of %d (sample ping: %s)",
                 report["nodes_passing_node_id_gate"], report["nodes"],
                 json.dumps(report["sample_ping"], separators=(",", ":"))[:600])

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
        if e.errno == errno.EADDRNOTAVAIL:
            # Nearly always WEB_BIND set to a LAN address of the *host* while
            # DedDrop runs in a container, where that address does not exist.
            log.error("WEB_BIND=%s is not an address on this machine. In Docker, "
                      "leave WEB_BIND=0.0.0.0 and publish the port on the LAN "
                      "address instead (WEB_PUBLISH_ADDR in .env).", config.WEB_BIND)
        elif e.errno == errno.EADDRINUSE:
            log.error("port %d is already in use — stop the other listener or set "
                      "a different WEB_PORT.", config.WEB_PORT)
        return

    log.info("DedDrop dashboard & ingest API listening on http://%s:%d/",
             config.WEB_BIND, config.WEB_PORT)
    try:
        server.serve_forever(poll_interval=0.5)
    except Exception:
        log.exception("web server stopped unexpectedly")
    finally:
        server.server_close()
