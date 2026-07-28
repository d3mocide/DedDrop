#!/usr/bin/env python3
"""Unit tests for deddrop.

Stdlib unittest only — the project deliberately has no third-party runtime or
test dependencies, so `python3 -m unittest discover tests` is the whole story.

Importing deddrop runs its module-level config, so the environment is set up
before the import below.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="deddrop-tests-")
os.environ.setdefault("TAR1090_URL", "http://127.0.0.1:9/aircraft.json")
os.environ.setdefault("WDGWARS_API_KEY", "a" * 64)
os.environ["STATE_FILE"] = str(Path(_TMP) / "state" / "accumulator.json")
os.environ["SNAPSHOT_DIR"] = str(Path(_TMP) / "snapshots")
os.environ["WEB_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deddrop  # noqa: E402


def setUpModule():
    # Several tests deliberately exercise warning/error paths; their log output
    # would otherwise drown the test results.
    import logging
    logging.disable(logging.CRITICAL)


def tearDownModule():
    import logging
    logging.disable(logging.NOTSET)


# ── ADS-B normalization ───────────────────────────────────────────────────
class TestNormalizeAircraft(unittest.TestCase):
    TS = "2026-01-01 00:00:00"

    def test_accepts_a_well_formed_aircraft(self):
        result = deddrop.normalize_one(
            {"hex": "4ca7b1", "flight": "RYR123 ", "lat": 52.1, "lon": 21.0,
             "alt_baro": 35000, "gs": 450.6, "track": 271.4}, self.TS)
        self.assertIsNotNone(result)
        icao, rec = result
        self.assertEqual(icao, "4CA7B1")
        self.assertEqual(rec["callsign"], "RYR123")
        self.assertEqual(rec["alt_ft"], 35000)
        self.assertEqual(rec["speed_kt"], 450)
        self.assertEqual(rec["heading"], 271)
        self.assertEqual(rec["type"], "ADSB")

    def test_rejects_bad_icao(self):
        for hex_id in ("", "4CA7B", "4CA7B12", "ZZZZZZ", "4ca7bg"):
            with self.subTest(hex=hex_id):
                self.assertIsNone(deddrop.normalize_one(
                    {"hex": hex_id, "lat": 1.0, "lon": 1.0}, self.TS))

    def test_rejects_missing_or_out_of_range_position(self):
        for ac in ({"hex": "4CA7B1"},
                   {"hex": "4CA7B1", "lat": 52.1},
                   {"hex": "4CA7B1", "lat": 91.0, "lon": 21.0},
                   {"hex": "4CA7B1", "lat": 52.1, "lon": 181.0},
                   {"hex": "4CA7B1", "lat": "north", "lon": 21.0}):
            with self.subTest(ac=ac):
                self.assertIsNone(deddrop.normalize_one(ac, self.TS))

    def test_missing_telemetry_is_null_not_zero(self):
        """Regression: absent gs/track used to be reported as 0 kt / due north."""
        _, rec = deddrop.normalize_one(
            {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0}, self.TS)
        self.assertIsNone(rec["speed_kt"])
        self.assertIsNone(rec["heading"])
        self.assertIsNone(rec["alt_ft"])

    def test_ground_is_a_real_zero_altitude(self):
        _, rec = deddrop.normalize_one(
            {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0, "alt_baro": "ground"}, self.TS)
        self.assertEqual(rec["alt_ft"], 0)

    def test_parse_snapshot_counts_skips(self):
        records, skipped = deddrop.parse_snapshot({
            "now": 1700000000,
            "aircraft": [
                {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0},
                {"hex": "BAD"},
                {"hex": "4CA7B2"},
            ],
        })
        self.assertEqual(len(records), 1)
        self.assertEqual(skipped, 2)

    def test_merge_preserves_first_seen(self):
        acc = {}
        deddrop.merge_into(acc, {"A": {"icao": "A", "first_seen": "t1", "lat": 1}})
        deddrop.merge_into(acc, {"A": {"icao": "A", "first_seen": "t2", "lat": 2}})
        self.assertEqual(acc["A"]["first_seen"], "t1")
        self.assertEqual(acc["A"]["lat"], 2)


# ── MeshMapper normalization ──────────────────────────────────────────────
class TestNormalizeMesh(unittest.TestCase):
    def test_disc_ping_becomes_a_repeater(self):
        recs = deddrop.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "AABB", "node_type": "R", "local_rssi": "-95"})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["node_id"], "aabb")
        self.assertEqual(recs[0]["node_type"], "REPEATER")
        self.assertEqual(recs[0]["rssi"], -95)

    def test_rejects_null_island_and_bad_positions(self):
        for ping in ({"type": "DISC", "lat": 0, "lon": 0, "repeater_id": "aa"},
                     {"type": "DISC", "lat": None, "lon": 21.0, "repeater_id": "aa"},
                     {"type": "DISC", "lat": 91, "lon": 21.0, "repeater_id": "aa"}):
            with self.subTest(ping=ping):
                self.assertEqual(deddrop.normalize_mesh_ping(ping), [])

    def test_heard_repeats_tokens(self):
        recs = deddrop.normalize_mesh_ping({
            "type": "TRACE", "lat": 52.1, "lon": 21.0, "repeater_id": "none",
            "heard_repeats": "aabb(R)(-95.5), ccdd(-80), notatoken!"})
        self.assertEqual(sorted(r["node_id"] for r in recs), ["aabb", "ccdd"])

    def test_unusable_timestamp_falls_back_to_arrival(self):
        """A millisecond epoch used to be swallowed by a bare `except Exception`."""
        recs = deddrop.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "timestamp": 99999999999999})
        self.assertEqual(len(recs), 1)
        self.assertRegex(recs[0]["first_seen"], r"^\d{4}-\d{2}-\d{2} ")

    def test_merge_preserves_first_seen(self):
        acc = {}
        deddrop.merge_mesh_records(acc, [{"node_id": "n", "first_seen": "t1"}])
        deddrop.merge_mesh_records(acc, [{"node_id": "n", "first_seen": "t2"}])
        self.assertEqual(acc["n"]["first_seen"], "t1")


# ── HMAC envelope ─────────────────────────────────────────────────────────
class TestEnvelope(unittest.TestCase):
    def test_signature_verifies_and_nonce_is_fresh(self):
        import base64
        import hashlib
        import hmac

        key = "k" * 64
        payload = {"aircraft": [{"icao": "4CA7B1"}]}
        env = deddrop.build_envelope(payload, key)

        self.assertEqual(json.loads(base64.b64decode(env["data"])), payload)
        expected = hmac.new(key.encode(), (env["nonce"] + env["data"]).encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(env["sig"], expected)
        self.assertNotEqual(env["nonce"], deddrop.build_envelope(payload, key)["nonce"])


# ── State persistence ─────────────────────────────────────────────────────
class TestState(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state" / "accumulator.json"
        self._orig = deddrop.STATE_FILE
        deddrop.STATE_FILE = self.path

    def tearDown(self):
        deddrop.STATE_FILE = self._orig
        self.dir.cleanup()

    def test_round_trip(self):
        state = deddrop._default_state()
        state["accumulator"]["A"] = {"icao": "A"}
        state["poll_count"] = 7
        deddrop.save_state(state)
        loaded = deddrop.load_state()
        self.assertEqual(loaded["poll_count"], 7)
        self.assertEqual(loaded["accumulator"]["A"]["icao"], "A")

    def test_missing_file_yields_defaults(self):
        self.assertEqual(deddrop.load_state()["accumulator"], {})

    def test_corrupt_file_does_not_raise(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json")
        self.assertEqual(deddrop.load_state()["accumulator"], {})

    def test_wrong_types_are_repaired(self):
        """Regression: a list accumulator used to blow up on the next merge."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "accumulator": ["not", "a", "dict"],
            "mesh_accumulator": {"good": {"node_id": "good"}, "bad": "string"},
            "poll_count": "seven",
        }))
        state = deddrop.load_state()
        self.assertEqual(state["accumulator"], {})
        self.assertEqual(list(state["mesh_accumulator"]), ["good"])
        self.assertEqual(state["poll_count"], 0)

    def test_top_level_non_object_is_repaired(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("[1, 2, 3]")
        self.assertEqual(deddrop.load_state()["accumulator"], {})

    def test_concurrent_saves_never_corrupt_the_file(self):
        """Regression: every writer shared one fixed `.tmp` path."""
        state = deddrop._default_state()
        state["accumulator"] = {f"K{i}": {"icao": f"K{i}"} for i in range(500)}
        errors = []

        def writer():
            try:
                for _ in range(20):
                    deddrop.save_state(state)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(deddrop.load_state()["accumulator"]), 500)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


# ── Flush retention ───────────────────────────────────────────────────────
class TestFlushRetention(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self._state_file = deddrop.STATE_FILE
        self._snap_dir = deddrop.SNAPSHOT_DIR
        deddrop.STATE_FILE = Path(self.dir.name) / "state" / "accumulator.json"
        deddrop.SNAPSHOT_DIR = Path(self.dir.name) / "snapshots"
        deddrop._next_flush_attempt = 0.0

    def tearDown(self):
        deddrop.STATE_FILE = self._state_file
        deddrop.SNAPSHOT_DIR = self._snap_dir
        deddrop._next_flush_attempt = 0.0

    def _state(self):
        state = deddrop._default_state()
        state["accumulator"] = {"A": {"icao": "A"}, "B": {"icao": "B"}}
        state["mesh_accumulator"] = {"n1": {"node_id": "n1"}}
        return state

    def test_failed_upload_retains_the_window(self):
        """Regression: a failed upload used to silently discard the window."""
        state = self._state()
        with mock.patch.object(deddrop, "upload_records", return_value=False):
            self.assertFalse(deddrop.do_flush(state, force=True))
        self.assertEqual(len(state["accumulator"]), 2)
        self.assertEqual(len(state["mesh_accumulator"]), 1)
        self.assertGreater(deddrop._next_flush_attempt, 0)

    def test_successful_upload_clears_the_window(self):
        state = self._state()
        with mock.patch.object(deddrop, "upload_records", return_value=True):
            self.assertTrue(deddrop.do_flush(state, force=True))
        self.assertEqual(state["accumulator"], {})
        self.assertEqual(state["mesh_accumulator"], {})
        self.assertEqual(state["poll_count"], 0)

    def test_records_arriving_during_upload_survive(self):
        """Regression: mid-upload ingests were wiped along with the batch."""
        state = self._state()

        def slow_upload(aircraft, mesh, key, url):
            state["mesh_accumulator"]["n2"] = {"node_id": "n2"}   # new node
            state["accumulator"]["A"] = {"icao": "A", "alt_ft": 1}  # re-seen
            return True

        with mock.patch.object(deddrop, "upload_records", side_effect=slow_upload):
            self.assertTrue(deddrop.do_flush(state, force=True))

        self.assertIn("n2", state["mesh_accumulator"])
        self.assertIn("A", state["accumulator"])   # replaced, so not uploaded
        self.assertNotIn("B", state["accumulator"])  # uploaded, so dropped

    def test_retry_backoff_is_respected(self):
        state = self._state()
        with mock.patch.object(deddrop, "upload_records", return_value=False):
            deddrop.do_flush(state, force=True)
        state["window_start"] = 0  # window is long overdue
        with mock.patch.object(deddrop, "upload_records", return_value=True) as up:
            self.assertFalse(deddrop.do_flush(state))  # still inside backoff
            up.assert_not_called()

    def test_empty_window_still_rolls_over(self):
        state = deddrop._default_state()
        state["window_start"] = 0
        state["poll_count"] = 5
        with mock.patch.object(deddrop, "upload_records") as up:
            self.assertTrue(deddrop.do_flush(state, force=True))
            up.assert_not_called()
        self.assertEqual(state["poll_count"], 0)
        self.assertGreater(state["window_start"], 0)


# ── Upload chunking ───────────────────────────────────────────────────────
class TestUploadChunking(unittest.TestCase):
    def test_every_record_is_sent_exactly_once(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        mesh = [{"node_id": f"n{i}"} for i in range(100)]
        sent_ac, sent_mesh = [], []

        def fake_send(ac_chunk, mesh_chunk, key, url):
            self.assertLessEqual(len(ac_chunk), deddrop.BATCH_SIZE)
            self.assertLessEqual(len(mesh_chunk), deddrop.BATCH_SIZE)
            sent_ac.extend(ac_chunk)
            sent_mesh.extend(mesh_chunk)
            return True, {"aircraft_imported": len(ac_chunk)}

        with mock.patch.object(deddrop, "send_chunk", side_effect=fake_send), \
             mock.patch.object(deddrop, "CHUNK_COOLDOWN_S", 0):
            self.assertTrue(deddrop.upload_records(aircraft, mesh, "k", "http://x"))

        self.assertEqual(len(sent_ac), 1200)
        self.assertEqual(len(sent_mesh), 100)
        self.assertEqual(len({a["icao"] for a in sent_ac}), 1200)

    def test_one_failed_chunk_fails_the_upload(self):
        aircraft = [{"icao": f"{i:06X}"} for i in range(1200)]
        results = iter([(True, {}), (False, {}), (True, {})])
        with mock.patch.object(deddrop, "send_chunk", side_effect=lambda *a: next(results)), \
             mock.patch.object(deddrop, "CHUNK_COOLDOWN_S", 0):
            self.assertFalse(deddrop.upload_records(aircraft, [], "k", "http://x"))

    def test_nothing_to_upload_is_a_success(self):
        self.assertTrue(deddrop.upload_records([], [], "k", "http://x"))

    def test_shutdown_aborts_remaining_chunks(self):
        """Regression: SIGTERM was ignored until every chunk had been sent."""
        aircraft = [{"icao": f"{i:06X}"} for i in range(2000)]
        calls = []

        def fake_send(ac_chunk, mesh_chunk, key, url):
            calls.append(len(ac_chunk))
            deddrop._shutdown = True
            return True, {}

        try:
            with mock.patch.object(deddrop, "send_chunk", side_effect=fake_send), \
                 mock.patch.object(deddrop, "CHUNK_COOLDOWN_S", 0):
                self.assertFalse(deddrop.upload_records(aircraft, [], "k", "http://x"))
        finally:
            deddrop._shutdown = False
        self.assertEqual(len(calls), 1)


# ── Retry policy ──────────────────────────────────────────────────────────
class TestRetryPolicy(unittest.TestCase):
    def _http_error(self, code, headers=None):
        return urllib.error.HTTPError(
            "http://x", code, "err", headers or {}, None)

    def test_retry_after_header_is_honoured(self):
        err = self._http_error(429, {"Retry-After": "30"})
        self.assertEqual(deddrop._retry_delay(err, 1), 30.0)

    def test_retry_after_is_capped(self):
        err = self._http_error(429, {"Retry-After": "99999"})
        self.assertEqual(deddrop._retry_delay(err, 1), 300.0)

    def test_garbage_retry_after_falls_back_to_backoff(self):
        err = self._http_error(429, {"Retry-After": "tomorrow"})
        self.assertEqual(deddrop._retry_delay(err, 1), deddrop.BACKOFF_BASE_S)

    def test_backoff_is_exponential(self):
        err = self._http_error(503)
        delays = [deddrop._retry_delay(err, n) for n in (1, 2, 3)]
        self.assertEqual(delays, [deddrop.BACKOFF_BASE_S,
                                  deddrop.BACKOFF_BASE_S * 2,
                                  deddrop.BACKOFF_BASE_S * 4])


# ── Snapshot listing & caching ────────────────────────────────────────────
class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        deddrop._snapshot_cache.clear()

    def tearDown(self):
        deddrop._snapshot_cache.clear()
        self.dir.cleanup()

    def _write(self, name, aircraft=3, mesh=1):
        (self.path / name).write_text(json.dumps({
            "aircraft_count": aircraft, "mesh_nodes_count": mesh,
            "window_start": "s", "window_end": "e",
            "aircraft": [{"icao": "4CA7B1"}] * aircraft, "meshcore_nodes": []}))

    def test_lists_newest_first(self):
        for name in ("upload_20260101T000000Z.json", "upload_20260102T000000Z.json"):
            self._write(name)
        names = [s["name"] for s in deddrop.list_snapshots(self.path)]
        self.assertEqual(names[0], "upload_20260102T000000Z.json")

    def test_second_call_is_served_from_cache(self):
        """Regression: every poll re-parsed every archived snapshot."""
        self._write("upload_20260101T000000Z.json")
        first = deddrop.list_snapshots(self.path)
        real_read = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True) as reader:
            reader.side_effect = real_read
            second = deddrop.list_snapshots(self.path)
            reader.assert_not_called()
        self.assertEqual(first, second)

    def test_cache_evicts_pruned_files(self):
        self._write("upload_20260101T000000Z.json")
        deddrop.list_snapshots(self.path)
        self.assertEqual(len(deddrop._snapshot_cache), 1)
        (self.path / "upload_20260101T000000Z.json").unlink()
        self.assertEqual(deddrop.list_snapshots(self.path), [])
        self.assertEqual(len(deddrop._snapshot_cache), 0)

    def test_corrupt_snapshot_is_listed_not_fatal(self):
        (self.path / "upload_20260101T000000Z.json").write_text("{broken")
        result = deddrop.list_snapshots(self.path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["aircraft_count"], 0)

    def test_missing_directory_is_empty(self):
        self.assertEqual(deddrop.list_snapshots(self.path / "nope"), [])

    def test_prune_keeps_newest(self):
        for i in range(5):
            self._write(f"upload_2026010{i}T000000Z.json")
        deddrop.prune_snapshots(self.path, keep=2)
        remaining = sorted(p.name for p in self.path.glob("upload_*.json"))
        self.assertEqual(remaining, ["upload_20260103T000000Z.json",
                                     "upload_20260104T000000Z.json"])


# ── Secret scrubbing ──────────────────────────────────────────────────────
class TestScrub(unittest.TestCase):
    def test_key_is_redacted(self):
        key = "abcd" + "0" * 56 + "wxyz"
        self.assertNotIn(key, deddrop.scrub(f"failed with key {key}", key))

    def test_no_key_is_a_passthrough(self):
        self.assertEqual(deddrop.scrub("plain text", ""), "plain text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
