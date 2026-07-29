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


class TestRejectionAccounting(support.RuntimeIsolated):
    """A refusal is a verdict: it must be reported, and never retried forever."""

    REFUSED = {"ok": True, "meshcore_imported": 0, "meshcore_already_seen": 0,
               "meshcore_rejected": 53, "meshcore_reject_reasons": {"bad_node_id": 53}}

    def test_an_itemised_refusal_counts_as_delivered(self):
        """Re-sending records WDGWars has already refused would never end."""
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse(self.REFUSED)), \
             mock.patch.object(config, "DRY_RUN", False):
            ok, _ = uploader.send_chunk(uploader.MESH_FEED, [{"node_id": "n"}], "k", "http://x")
        self.assertTrue(ok)

    def test_the_refusal_and_its_reasons_are_recorded(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse(self.REFUSED)), \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([], [{"node_id": "n"}], "k",
                                    aircraft_url="http://x", mesh_url="http://x")
        self.assertEqual(runtime.last_upload["mesh_rejected"], 53)
        self.assertEqual(runtime.last_upload["mesh_reject_reasons"], {"bad_node_id": 53})
        self.assertEqual(runtime.last_upload["mesh_imported"], 0)

    def test_reasons_accumulate_across_chunks(self):
        records = [{"node_id": f"n{i}"} for i in range(3)]
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse(
                            {"ok": True, "meshcore_rejected": 1,
                             "meshcore_reject_reasons": {"bad_node_id": 1}})), \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "BATCH_SIZE", 1), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            ok, totals = uploader.upload_feed(uploader.MESH_FEED, records, "k", "http://x")
        self.assertTrue(ok)
        self.assertEqual(totals["rejected"], 3)
        self.assertEqual(totals["reasons"], {"bad_node_id": 3})

    def test_a_response_with_no_verdict_at_all_is_still_a_failure(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse({"ok": True})), \
             mock.patch.object(config, "DRY_RUN", False):
            ok, _ = uploader.send_chunk(uploader.MESH_FEED, [{"node_id": "n"}], "k", "http://x")
        self.assertFalse(ok)


class TestFeedSeparation(support.RuntimeIsolated):
    """Aircraft and mesh nodes must travel as separate requests."""

    def test_each_feed_gets_its_own_request_and_url(self):
        sent = []

        def fake_send(feed, chunk, key, url):
            sent.append((feed.name, len(chunk), url))
            return True, {feed.imported[0]: len(chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            result = uploader.upload_records(
                [{"icao": "A"}], [{"node_id": "n"}], "k",
                aircraft_url="http://ac", mesh_url="http://mesh")

        self.assertTrue(result.ok)
        self.assertEqual(sent, [("aircraft", 1, "http://ac"), ("mesh", 1, "http://mesh")])

    def test_a_chunk_never_mixes_feeds(self):
        """The payload key a chunk lands under decides how WDGWars stores it."""
        payloads = []

        def fake_urlopen(req, timeout=None):
            envelope = json.loads(req.data)
            payloads.append(json.loads(base64.b64decode(envelope["data"])))
            return support.FakeResponse({"ok": True, "imported": 1})

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([{"icao": "A"}], [{"node_id": "n", "lat": 1, "lon": 2}], "k",
                                    aircraft_url="http://ac", mesh_url="http://mesh")

        self.assertEqual(len(payloads), 2)
        self.assertEqual(len(payloads[0]["aircraft"]), 1)
        self.assertEqual(payloads[0]["meshcore_nodes"], [])
        self.assertEqual(payloads[1]["aircraft"], [])
        self.assertEqual(payloads[1]["meshcore_nodes"][0]["node_id"], "n")

    def test_mesh_records_travel_verbatim(self):
        """The accumulator already holds the confirmed meshcore wire shape."""
        record = {"node_id": "aabb", "node_type": "REPEATER", "name": "aabb",
                  "lat": 52.1, "lon": 21.0, "rssi": -95,
                  "first_seen": "2026-07-29 12:00:00", "type": "MESHCORE"}
        sent = {}

        def fake_send(feed, chunk, key, url):
            sent[feed.name] = chunk
            return True, {feed.imported[0]: len(chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([], [record], "k",
                                    aircraft_url="http://ac", mesh_url="http://mesh")

        self.assertEqual(sent["mesh"][0], record)

    def test_a_failed_mesh_dispatch_leaves_aircraft_successful(self):
        def fake_send(feed, chunk, key, url):
            return (feed.name == "aircraft"), {feed.imported[0]: len(chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            result = uploader.upload_records([{"icao": "A"}], [{"node_id": "n"}], "k",
                                             aircraft_url="http://ac", mesh_url="http://mesh")

        self.assertTrue(result.aircraft_ok)
        self.assertFalse(result.mesh_ok)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_feeds(), ["mesh"])

    def test_aircraft_counters_cannot_vouch_for_mesh(self):
        """The bug this split exists to prevent: a mesh-free response reading OK."""
        response = {"ok": True, "aircraft_imported": 24, "aircraft_already_seen": 7}
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse(response)), \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            result = uploader.upload_records([{"icao": "A"}], [{"node_id": "n"}], "k",
                                             aircraft_url="http://ac", mesh_url="http://mesh")
        self.assertTrue(result.aircraft_ok)
        self.assertFalse(result.mesh_ok)

    def test_a_feed_with_no_records_is_not_dispatched(self):
        with mock.patch.object(uploader, "send_chunk",
                               return_value=(True, {"imported": 1})) as send:
            uploader.upload_records([{"icao": "A"}], [], "k",
                                    aircraft_url="http://ac", mesh_url="http://mesh")
        self.assertEqual([c.args[0].name for c in send.call_args_list], ["aircraft"])


class TestUploadChunking(support.RuntimeIsolated):
    def test_every_record_is_sent_exactly_once(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        mesh = [{"node_id": f"n{i}"} for i in range(100)]
        sent = {"aircraft": [], "mesh": []}

        def fake_send(feed, chunk, key, url):
            self.assertLessEqual(len(chunk), config.BATCH_SIZE)
            sent[feed.name].extend(chunk)
            return True, {feed.imported[0]: len(chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            self.assertTrue(uploader.upload_records(
                aircraft, mesh, "k", aircraft_url="http://x", mesh_url="http://x").ok)

        self.assertEqual(len(sent["aircraft"]), 1200)
        self.assertEqual(len(sent["mesh"]), 100)
        self.assertEqual(len({a["icao"] for a in sent["aircraft"]}), 1200)

    def test_one_failed_chunk_fails_that_feed(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        results = iter([(True, {}), (False, {}), (True, {})])
        with mock.patch.object(uploader, "send_chunk", side_effect=lambda *a: next(results)), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            result = uploader.upload_records(aircraft, [], "k",
                                             aircraft_url="http://x", mesh_url="http://x")
        self.assertFalse(result.aircraft_ok)
        self.assertTrue(result.mesh_ok)

    def test_nothing_to_upload_is_a_success(self):
        self.assertTrue(uploader.upload_records(
            [], [], "k", aircraft_url="http://x", mesh_url="http://x").ok)

    def test_shutdown_aborts_remaining_chunks(self):
        """SIGTERM must not wait for every chunk to be sent."""
        aircraft = [{"icao": f"{i:06X}"} for i in range(2000)]
        calls = []

        def fake_send(feed, chunk, key, url):
            calls.append(feed.name)
            runtime.shutdown.set()
            return True, {}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            result = uploader.upload_records(aircraft, [{"node_id": "n"}], "k",
                                             aircraft_url="http://x", mesh_url="http://x")
        self.assertFalse(result.ok)
        # The mesh feed is never started, so its window is retained rather than
        # half-sent during shutdown.
        self.assertEqual(calls, ["aircraft"])
        self.assertFalse(result.mesh_ok)

    def test_last_upload_summary_is_recorded(self):
        with mock.patch.object(uploader, "send_chunk",
                               return_value=(True, {"aircraft_imported": 2})), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([{"icao": "A"}, {"icao": "B"}], [], "k",
                                    aircraft_url="http://x", mesh_url="http://x")
        self.assertEqual(runtime.last_upload["aircraft_count"], 2)
        self.assertTrue(runtime.last_upload["success"])
        self.assertTrue(runtime.last_upload["aircraft_success"])
        self.assertTrue(runtime.last_upload["mesh_success"])

    def test_per_feed_outcome_is_recorded(self):
        def fake_send(feed, chunk, key, url):
            return (feed.name == "aircraft"), {feed.imported[0]: len(chunk)}

        with mock.patch.object(uploader, "send_chunk", side_effect=fake_send), \
             mock.patch.object(config, "CHUNK_COOLDOWN_S", 0):
            uploader.upload_records([{"icao": "A"}], [{"node_id": "n"}], "k",
                                    aircraft_url="http://x", mesh_url="http://x")
        self.assertTrue(runtime.last_upload["aircraft_success"])
        self.assertFalse(runtime.last_upload["mesh_success"])
        self.assertFalse(runtime.last_upload["success"])


class TestSendChunk(support.RuntimeIsolated):
    def test_dry_run_does_not_hit_the_network(self):
        with mock.patch.object(config, "DRY_RUN", True), \
             mock.patch("urllib.request.urlopen") as urlopen:
            ok, data = uploader.send_chunk(uploader.AIRCRAFT_FEED, [{"icao": "A"}],
                                           "k", "http://x")
            urlopen.assert_not_called()
        self.assertTrue(ok)
        self.assertTrue(data["dry_run"])

    def test_a_zero_counter_response_is_a_failure(self):
        """A 200 that stored nothing must retain the window, not clear it."""
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse({"ok": True})), \
             mock.patch.object(config, "DRY_RUN", False):
            ok, _ = uploader.send_chunk(uploader.MESH_FEED, [{"node_id": "n"}], "k", "http://x")
        self.assertFalse(ok)

    def test_meshcore_counters_acknowledge_a_mesh_chunk(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=support.FakeResponse(
                            {"ok": True, "meshcore_imported": 3})), \
             mock.patch.object(config, "DRY_RUN", False):
            ok, data = uploader.send_chunk(uploader.MESH_FEED, [{"node_id": "n"}],
                                           "k", "http://x")
        self.assertTrue(ok)
        self.assertEqual(uploader._counter(data, uploader.MESH_FEED.imported), 3)

    def test_413_is_not_retried(self):
        err = urllib.error.HTTPError("http://x", 413, "too big", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, \
             mock.patch.object(config, "DRY_RUN", False):
            ok, _ = uploader.send_chunk(uploader.AIRCRAFT_FEED, [{"icao": "A"}],
                                        "k", "http://x")
        self.assertFalse(ok)
        self.assertEqual(urlopen.call_count, 1)

    def test_429_is_retried(self):
        err = urllib.error.HTTPError("http://x", 429, "slow down", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err) as urlopen, \
             mock.patch.object(config, "DRY_RUN", False), \
             mock.patch.object(config, "MAX_ATTEMPTS", 3), \
             mock.patch.object(config, "BACKOFF_BASE_S", 0):
            ok, _ = uploader.send_chunk(uploader.AIRCRAFT_FEED, [{"icao": "A"}],
                                        "k", "http://x")
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
            ok, _ = uploader.send_chunk(uploader.AIRCRAFT_FEED, [{"icao": "A"}],
                                        "k", "http://x")
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
