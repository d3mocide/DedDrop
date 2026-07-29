"""Poll and flush lifecycle."""
import unittest
from unittest import mock

import support
from deddrop import config, runtime, service, storage

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs


class TestFlushRetention(support.TempConfig):
    def _state(self):
        state = runtime.default_state()
        state["accumulator"] = {"A": {"icao": "A"}, "B": {"icao": "B"}}
        state["mesh_accumulator"] = {"n1": {"node_id": "n1"}}
        return state

    def test_failed_upload_retains_the_window(self):
        """A failed upload must never discard the accumulated window."""
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=False):
            self.assertFalse(service.do_flush(state, force=True))
        self.assertEqual(len(state["accumulator"]), 2)
        self.assertEqual(len(state["mesh_accumulator"]), 1)
        self.assertGreater(runtime.next_flush_attempt, 0)

    def test_successful_upload_clears_the_window(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=True):
            self.assertTrue(service.do_flush(state, force=True))
        self.assertEqual(state["accumulator"], {})
        self.assertEqual(state["mesh_accumulator"], {})
        self.assertEqual(state["poll_count"], 0)

    def test_records_arriving_during_upload_survive(self):
        """Mid-upload ingests must not be wiped with the batch."""
        state = self._state()

        def slow_upload(aircraft, mesh, key, url):
            state["mesh_accumulator"]["n2"] = {"node_id": "n2"}     # new node
            state["accumulator"]["A"] = {"icao": "A", "alt_ft": 1}  # re-seen
            return True

        with mock.patch.object(service, "upload_records", side_effect=slow_upload):
            self.assertTrue(service.do_flush(state, force=True))

        self.assertIn("n2", state["mesh_accumulator"])
        self.assertIn("A", state["accumulator"])     # replaced, so not uploaded
        self.assertNotIn("B", state["accumulator"])  # uploaded, so dropped

    def test_retry_backoff_is_respected(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=False):
            service.do_flush(state, force=True)
        state["window_start"] = 0  # window is long overdue
        with mock.patch.object(service, "upload_records", return_value=True) as up:
            self.assertFalse(service.do_flush(state))  # still inside backoff
            up.assert_not_called()

    def test_backoff_clears_after_a_success(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=False):
            service.do_flush(state, force=True)
        with mock.patch.object(service, "upload_records", return_value=True):
            service.do_flush(state, force=True)
        self.assertEqual(runtime.next_flush_attempt, 0.0)

    def test_window_not_yet_elapsed_is_a_noop(self):
        state = self._state()
        with mock.patch.object(service, "upload_records") as up:
            self.assertFalse(service.do_flush(state))
            up.assert_not_called()

    def test_empty_window_still_rolls_over(self):
        state = runtime.default_state()
        state["window_start"] = 0
        state["poll_count"] = 5
        with mock.patch.object(service, "upload_records") as up:
            self.assertTrue(service.do_flush(state, force=True))
            up.assert_not_called()
        self.assertEqual(state["poll_count"], 0)
        self.assertGreater(state["window_start"], 0)

    def test_dispatch_summary_survives_a_restart(self):
        """The dashboard reads the summary from memory; a restart must refill it."""
        state = self._state()
        with mock.patch.object(config, "DRY_RUN", True):
            self.assertTrue(service.do_flush(state, force=True))
        self.assertEqual(runtime.last_upload["aircraft_count"], 2)

        runtime.reset()  # process restart
        self.assertEqual(runtime.last_upload, {})
        service_state = storage.load_state()

        self.assertEqual(runtime.last_upload["aircraft_count"], 2)
        self.assertEqual(runtime.last_upload["mesh_count"], 1)
        self.assertTrue(runtime.last_upload["success"])
        self.assertEqual(service_state["accumulator"], {})  # window was flushed

    def test_failed_dispatch_summary_is_persisted_immediately(self):
        """A restart before the next poll must still report the failure."""
        state = self._state()
        with mock.patch.object(service, "upload_records", side_effect=self._record_failure):
            self.assertFalse(service.do_flush(state, force=True))

        runtime.reset()
        storage.load_state()
        self.assertFalse(runtime.last_upload["success"])
        self.assertEqual(runtime.last_upload["aircraft_count"], 2)

    @staticmethod
    def _record_failure(aircraft, mesh, api_key, url):
        runtime.last_upload = {
            "timestamp": 1700000000.0, "aircraft_count": len(aircraft),
            "mesh_count": len(mesh), "success": False,
        }
        return False

    def test_snapshot_is_written_before_upload(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=True):
            service.do_flush(state, force=True)
        self.assertEqual(len(list(config.SNAPSHOT_DIR.glob("upload_*.json"))), 1)


class TestPoll(support.TempConfig):
    FEED = {"now": 1700000000, "aircraft": [
        {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0, "gs": 450, "track": 270},
        {"hex": "BAD"},
    ]}

    def test_poll_merges_and_counts_skips(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_aircraft_json", return_value=self.FEED), \
             mock.patch.object(config, "SAVE_LATEST_RAW", False):
            service.do_poll(state)
        self.assertEqual(list(state["accumulator"]), ["4CA7B1"])
        self.assertEqual(state["poll_count"], 1)
        self.assertEqual(runtime.last_skipped, 1)
        self.assertGreater(runtime.last_poll_time, 0)

    def test_poll_persists_state(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_aircraft_json", return_value=self.FEED), \
             mock.patch.object(config, "SAVE_LATEST_RAW", False):
            service.do_poll(state)
        self.assertTrue(self.state_file.exists())

    def test_raw_dump_failure_does_not_abort_the_poll(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_aircraft_json", return_value=self.FEED), \
             mock.patch.object(config, "SAVE_LATEST_RAW", True), \
             mock.patch.object(service.storage, "atomic_write",
                               side_effect=[OSError("read-only fs"), None]):
            service.do_poll(state)
        self.assertEqual(state["poll_count"], 1)


class TestPreflight(support.TempConfig):
    def test_writable_directories_pass(self):
        self.assertTrue(service.preflight_storage())

    def test_unwritable_directory_fails(self):
        """The root-owned ./data bind mount must fail loudly at startup."""
        with mock.patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            self.assertFalse(service.preflight_storage())


if __name__ == "__main__":
    unittest.main()
