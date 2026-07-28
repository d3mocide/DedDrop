"""HTTP surface: auth, headers, ingest, and the control endpoints.

Runs a real server on an ephemeral port and drives it over HTTP, so the
handler's auth and header behaviour is exercised end to end.
"""
import gzip
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import support
from deddrop import config, runtime, webapp

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs

API_KEY = config.API_KEY


def request(url, *, method="GET", data=None, headers=None, gzip_ok=False):
    """Return (status, body_text, headers) without raising on 4xx/5xx."""
    hdrs = dict(headers or {})
    if gzip_ok:
        hdrs["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return resp.status, raw.decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, raw.decode(), dict(e.headers)


class WebServerCase(support.TempConfig):
    def setUp(self):
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.WebRequestHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # Short poll interval: the default 0.5s is paid back on every shutdown.
        thread = threading.Thread(target=self.server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)


class TestPublicEndpoints(WebServerCase):
    def test_status_returns_json(self):
        status, body, _ = request(f"{self.base}/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertIn("elapsed_hours", data)
        self.assertGreaterEqual(data["elapsed_hours"], 0)

    def test_status_never_exposes_the_api_key(self):
        _, body, _ = request(f"{self.base}/api/status")
        self.assertNotIn(API_KEY, body)
        self.assertNotIn("meshmapper_link", body)

    def test_no_wildcard_cors_by_default(self):
        _, _, headers = request(f"{self.base}/api/status")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_cors_can_be_opted_into(self):
        with mock.patch.object(config, "CORS_ALLOW_ORIGIN", "https://example.com"):
            _, _, headers = request(f"{self.base}/api/status")
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), "https://example.com")

    def test_healthz(self):
        status, body, _ = request(f"{self.base}/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unknown_path_is_404(self):
        status, _, _ = request(f"{self.base}/api/nope")
        self.assertEqual(status, 404)

    def test_json_is_compact(self):
        _, body, _ = request(f"{self.base}/api/status")
        self.assertNotIn('\n  "', body)

    def test_large_payloads_are_gzipped(self):
        with runtime.lock:
            runtime.state["accumulator"] = {
                f"{i:06X}": {"icao": f"{i:06X}", "callsign": "TEST123",
                             "lat": 52.0, "lon": 21.0, "first_seen": "t"}
                for i in range(200)
            }
        status, body, headers = request(f"{self.base}/api/aircraft", gzip_ok=True)
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Encoding"), "gzip")
        self.assertEqual(len(json.loads(body)), 200)


class TestWardriveIngest(WebServerCase):
    PING = json.dumps({"data": [{"type": "DISC", "lat": 52.1, "lon": 21.0,
                                 "repeater_id": "aabb"}]}).encode()

    def test_requires_a_key(self):
        status, _, _ = request(f"{self.base}/api/wardrive", method="POST", data=self.PING)
        self.assertEqual(status, 401)

    def test_rejects_a_wrong_key(self):
        status, _, _ = request(f"{self.base}/api/wardrive", method="POST", data=self.PING,
                               headers={"X-API-Key": "b" * 64})
        self.assertEqual(status, 401)

    def test_accepts_the_header_key(self):
        status, body, _ = request(f"{self.base}/api/wardrive", method="POST", data=self.PING,
                                  headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["nodes_merged"], 1)
        with runtime.lock:
            self.assertIn("aabb", runtime.state["mesh_accumulator"])

    def test_accepts_the_query_key(self):
        """The README documents ?key= as a fallback for the header."""
        status, _, _ = request(f"{self.base}/api/wardrive?key={API_KEY}",
                               method="POST", data=self.PING)
        self.assertEqual(status, 200)

    def test_rejects_invalid_json(self):
        status, _, _ = request(f"{self.base}/api/wardrive", method="POST", data=b"{oops",
                               headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 400)

    def test_rejects_a_non_list_payload(self):
        status, _, _ = request(f"{self.base}/api/wardrive", method="POST",
                               data=json.dumps({"data": "nope"}).encode(),
                               headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 400)

    def test_rejects_an_oversized_body(self):
        with mock.patch.object(config, "MAX_BODY_BYTES", 10):
            status, _, _ = request(f"{self.base}/api/wardrive", method="POST",
                                   data=b'{"data":[]}' * 100,
                                   headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 413)

    def test_malformed_pings_are_skipped_not_fatal(self):
        payload = json.dumps({"data": ["not-a-dict", {"type": "DISC", "lat": 52.1,
                                                      "lon": 21.0, "repeater_id": "ccdd"}]})
        status, body, _ = request(f"{self.base}/api/wardrive", method="POST",
                                  data=payload.encode(), headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["nodes_merged"], 1)


class TestControlEndpoints(WebServerCase):
    def test_trigger_poll_requires_auth(self):
        status, _, _ = request(f"{self.base}/api/trigger-poll", method="POST", data=b"")
        self.assertEqual(status, 401)
        self.assertFalse(runtime.poll_now.is_set())

    def test_trigger_flush_requires_auth(self):
        status, _, _ = request(f"{self.base}/api/trigger-flush", method="POST", data=b"")
        self.assertEqual(status, 401)
        self.assertFalse(runtime.flush_now.is_set())

    def test_control_token_authorizes(self):
        status, _, _ = request(f"{self.base}/api/trigger-poll", method="POST", data=b"",
                               headers={"X-Control-Token": config.CONTROL_TOKEN})
        self.assertEqual(status, 200)
        self.assertTrue(runtime.poll_now.is_set())

    def test_api_key_also_authorizes(self):
        status, _, _ = request(f"{self.base}/api/trigger-flush", method="POST", data=b"",
                               headers={"X-API-Key": API_KEY})
        self.assertEqual(status, 200)
        self.assertTrue(runtime.flush_now.is_set())

    def test_wrong_token_is_rejected(self):
        status, _, _ = request(f"{self.base}/api/trigger-poll", method="POST", data=b"",
                               headers={"X-Control-Token": "not-the-token"})
        self.assertEqual(status, 401)


class TestMeshMapperLink(WebServerCase):
    def test_requires_auth(self):
        status, body, _ = request(f"{self.base}/api/meshmapper-link")
        self.assertEqual(status, 401)
        self.assertNotIn(API_KEY, body)

    def test_returns_the_link_when_authorized(self):
        status, body, headers = request(
            f"{self.base}/api/meshmapper-link",
            headers={"X-Control-Token": config.CONTROL_TOKEN})
        self.assertEqual(status, 200)
        link = json.loads(body)["meshmapper_link"]
        self.assertTrue(link.startswith("meshmapper://custom-api?url=http://"))
        self.assertIn(API_KEY, link)
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_public_host_overrides_the_host_header(self):
        with mock.patch.object(config, "PUBLIC_HOST", "192.168.1.100:8080"):
            _, body, _ = request(f"{self.base}/api/meshmapper-link",
                                 headers={"X-Control-Token": config.CONTROL_TOKEN})
        self.assertIn("http://192.168.1.100:8080/api/wardrive",
                      json.loads(body)["meshmapper_link"])


class TestDashboard(WebServerCase):
    def test_control_token_is_injected(self):
        status, body, _ = request(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn(config.CONTROL_TOKEN, body)
        self.assertNotIn("__CONTROL_TOKEN__", body)

    def test_missing_dashboard_file_is_handled(self):
        with mock.patch.object(config, "WEB_DIR", self.root / "nope"):
            status, body, _ = request(f"{self.base}/")
        self.assertEqual(status, 500)
        self.assertIn("dashboard file missing", body)


if __name__ == "__main__":
    unittest.main()


class TestStaticAssets(WebServerCase):
    def test_serves_the_stylesheet(self):
        status, body, headers = request(f"{self.base}/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertIn(":root", body)

    def test_serves_es_modules_with_a_js_content_type(self):
        status, body, headers = request(f"{self.base}/js/main.js")
        self.assertEqual(status, 200)
        # A wrong Content-Type makes the browser refuse to run the module.
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn("import", body)

    def test_assets_revalidate_with_an_etag(self):
        _, _, headers = request(f"{self.base}/app.css")
        etag = headers["ETag"]
        self.assertTrue(etag)
        status, body, _ = request(f"{self.base}/app.css",
                                  headers={"If-None-Match": etag})
        self.assertEqual(status, 304)
        self.assertEqual(body, "")

    def test_changed_asset_gets_a_new_etag(self):
        _, _, first = request(f"{self.base}/app.css")
        css = config.WEB_DIR / "app.css"
        original = css.read_bytes()
        self.addCleanup(css.write_bytes, original)
        css.write_bytes(original + b"\n.injected{}\n")
        _, _, second = request(f"{self.base}/app.css")
        self.assertNotEqual(first["ETag"], second["ETag"])

    def test_directory_traversal_is_refused(self):
        for attack in ("/../deddrop/config.py",
                       "/js/../../deddrop/config.py",
                       "/%2e%2e/deddrop/config.py",
                       "/js/%2e%2e%2f%2e%2e%2fdeddrop/config.py",
                       "/....//deddrop/config.py",
                       "/js/..%2f..%2fdeddrop/uploader.py"):
            with self.subTest(path=attack):
                status, body, _ = request(f"{self.base}{attack}")
                self.assertEqual(status, 404)
                self.assertNotIn("WDGWARS_API_KEY", body)

    def test_absolute_path_escape_is_refused(self):
        status, _, _ = request(f"{self.base}//etc/passwd")
        self.assertEqual(status, 404)

    def test_unlisted_extensions_are_not_served(self):
        secret = config.WEB_DIR / "notes.txt"
        secret.write_text("should not be served")
        self.addCleanup(secret.unlink)
        status, body, _ = request(f"{self.base}/notes.txt")
        self.assertEqual(status, 404)
        self.assertNotIn("should not be served", body)

    def test_symlink_out_of_web_dir_is_refused(self):
        link = config.WEB_DIR / "escape.js"
        target = Path(__file__).resolve().parent.parent / "deddrop" / "config.py"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.addCleanup(link.unlink)
        status, body, _ = request(f"{self.base}/escape.js")
        self.assertEqual(status, 404)
        self.assertNotIn("WDGWARS_API_KEY", body)

    def test_control_characters_are_refused(self):
        """A NUL can smuggle an allowed extension past the check and crash resolve()."""
        for attack in ("/js/main.js%00.png", "/app%00.css", "/js/%01main.js"):
            with self.subTest(path=attack):
                status, _, _ = request(f"{self.base}{attack}")
                self.assertEqual(status, 404)

    def test_missing_asset_is_404(self):
        status, _, _ = request(f"{self.base}/js/nope.js")
        self.assertEqual(status, 404)


class TestContentSecurityPolicy(WebServerCase):
    def test_dashboard_sends_a_strict_csp(self):
        _, _, headers = request(f"{self.base}/")
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)

    def test_dashboard_has_no_inline_script_or_handlers(self):
        """A strict CSP is only meaningful if nothing relies on inline JS."""
        _, body, _ = request(f"{self.base}/")
        self.assertNotIn("onclick=", body)
        self.assertNotIn("<script>", body)
        self.assertNotIn('style="', body)

    def test_dashboard_is_not_cached(self):
        _, _, headers = request(f"{self.base}/")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
