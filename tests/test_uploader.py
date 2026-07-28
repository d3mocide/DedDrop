"""HMAC envelope, chunking, and retry policy."""
import base64
import hashlib
import hmac
import json
import unittest
import urllib.error
from unittest import mock

import support
from deddrop import config, runtime, uploader

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs


class TestEnvelope(unittest.TestCase):
    def test_signature_verifies_and_nonce_is_fresh(self):
        key = "k" * 64
        payload = {"aircraft": [{"icao": "4CA7B1"}]}
        env = uploader.build_envelope(payload, key)

        self.assertEqual(json.loads(base64.b64decode(env["data"])), payload)
        expected = hmac.new(key.encode(), (env["nonce"] + env["data"]).encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(env["sig"], expected)
        self.assertNotEqual(env["nonce"], uploader.build_envelope(payload, key)["nonce"])


class TestUploadChunking(support.RuntimeIsolated):
    def test_every_record_is_sent_exactly_once(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        mesh = [{"node_id": f"n{i}"} for i in range(100)]
        sent_ac, sent_mesh = [], []

        def fake_send(ac_chunk, mesh_chunk, key, url):
            self.assertLessEqual(len(ac_chunk), config.BATCH_SIZE)
            self.assertLessEqual(len(mesh_chunk), config.BATCH_SIZE)
            sent_ac.extend(ac_chunk)
            sent_mesh.extend(mesh_chunk)
            return True, {"aircraft_imported": len(ac_chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            self.assertTrue(uploader.upload_records(aircraft, mesh, "k", "http://x"))

        self.assertEqual(len(sent_ac), 1200)
        self.assertEqual(len(sent_mesh), 100)
        self.assertEqual(len({a["icao"] for a in sent_ac}), 1200)

    def test_one_failed_chunk_fails_the_upload(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        results = iter([(True, {}), (False, {}), (True, {})])
        with mock.patch.object(uploader, "send_chunk", side_effect=lambda *a: next(results)), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            self.assertFalse(uploader.upload_records(aircraft, [], "k", "http://x"))

    def test_nothing_to_upload_is_a_success(self):
        self.assertTrue(uploader.upload_records([], [], "k", "http://x"))

    def test_shutdown_aborts_remaining_chunks(self):
        """SIGTERM must not wait for every chunk to be sent."""
        aircraft = [{"icao": f"{i:06X}"} for i in range(2000)]
        calls = []

        def fake_send(ac_chunk, mesh_chunk, key, url):
            calls.append(len(ac_chunk))
            runtime.shutdown.set()
            return True, {}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            self.assertFalse(uploader.upload_records(aircraft, [], "k", "http://x"))
        self.assertEqual(len(calls), 1)

    def test_last_upload_summary_is_recorded(self):
        with mock.patch.object(uploader, "send_chunk",
                               return_value=(True, {"aircraft_imported": 2})), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([{"icao": "A"}, {"icao": "B"}], [], "k", "http://x")
        self.assertEqual(runtime.last_upload["aircraft_count"], 2)
        self.assertTrue(runtime.last_upload["success"])


class TestSendChunk(support.RuntimeIsolated):
    def test_dry_run_does_not_hit_the_network(self):
        with mock.patch.object(config, "DRY_RUN", True), \
             mock.patch("urllib.request.urlopen") as urlopen:
            ok, data = uploader.send_chunk([{"icao": "A"}], [], "k", "http://x")
            urlopen.assert_not_called()
        self.assertTrue(ok)
        self.assertTrue(data["dry_run"])

    def test_413_is_not_retried(self):
        err = urllib.error.HTTPError("http://x", 413, "too big", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, \
             mock.patch.object(config, "DRY_RUN", False):
            ok, _ = uploader.send_chunk([{"icao": "A"}], [], "k", "http://x")
        self.assertFalse(ok)
        self.assertEqual(urlopen.call_count, 1)

    def test_429_is_retried(self):
        err = urllib.error.HTTPError("http://x", 429, "slow down", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "MAX_ATTEMPTS", 3), \
             mock.patch.object(config, "BACKOFF_BASE_S", 0):
            ok, _ = uploader.send_chunk([{"icao": "A"}], [], "k", "http://x")
        self.assertFalse(ok)
        self.assertEqual(urlopen.call_count, 3)

    def test_shutdown_stops_retrying(self):
        err = urllib.error.HTTPError("http://x", 503, "boom", {}, None)

        def fail(*a, **kw):
            runtime.shutdown.set()
            raise err

        with mock.patch("urllib.request.urlopen", side_effect=fail) as urlopen, \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "MAX_ATTEMPTS", 5), \
             mock.patch.object(config, "BACKOFF_BASE_S", 0):
            ok, _ = uploader.send_chunk([{"icao": "A"}], [], "k", "http://x")
        self.assertFalse(ok)
        self.assertEqual(urlopen.call_count, 1)


class TestRetryPolicy(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError("http://x", code, "err", headers or {}, None)

    def test_retry_after_header_is_honoured(self):
        self.assertEqual(
            uploader.retry_delay(self._http_error(429, {"Retry-After": "30"}), 1), 30.0)

    def test_retry_after_is_capped(self):
        self.assertEqual(
            uploader.retry_delay(self._http_error(429, {"Retry-After": "99999"}), 1), 300.0)

    def test_garbage_retry_after_falls_back_to_backoff(self):
        err = self._http_error(429, {"Retry-After": "tomorrow"})
        self.assertEqual(uploader.retry_delay(err, 1), config.BACKOFF_BASE_S)

    def test_backoff_is_exponential(self):
        err = self._http_error(503)
        self.assertEqual([uploader.retry_delay(err, n) for n in (1, 2, 3)],
                         [config.BACKOFF_BASE_S,
                          config.BACKOFF_BASE_S * 2,
                          config.BACKOFF_BASE_S * 4])


class TestScrub(unittest.TestCase):
    def test_key_is_redacted(self):
        key = "abcd" + "0" * 56 + "wxyz"
        self.assertNotIn(key, uploader.scrub(f"failed with key {key}", key))

    def test_no_key_is_a_passthrough(self):
        self.assertEqual(uploader.scrub("plain text", ""), "plain text")


if __name__ == "__main__":
    unittest.main()
