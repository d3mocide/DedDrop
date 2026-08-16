"""Poll and flush lifecycle."""
import json
import time
import unittest
import urllib.error
from unittest import mock

import support
from deddrop import config, runtime, service, storage
from deddrop.uploader import DispatchResult

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs

DISPATCHED = DispatchResult(True, True)
NOTHING_LANDED = DispatchResult(False, False)
MESH_REJECTED = DispatchResult(True, False)


class TestFlushRetention(support.TempConfig):
    def _state(self):
        state = runtime.default_state()
        state["accumulator"] = {"A": {"icao": "A"}, "B": {"icao": "B"}}
        state["mesh_accumulator"] = {"n1": {"node_id": "n1", "lat": 1, "lon": 2}}
        return state

    def test_failed_upload_retains_the_window(self):
        """A failed upload must never discard the accumulated window."""
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=NOTHING_LANDED):
            self.assertFalse(service.do_flush(state, force=True))
        self.assertEqual(len(state["accumulator"]), 2)
        self.assertEqual(len(state["mesh_accumulator"]), 1)
        self.assertGreater(runtime.next_flush_attempt, 0)

    def test_successful_upload_clears_the_window(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=DISPATCHED):
            self.assertTrue(service.do_flush(state, force=True))
        self.assertEqual(state["accumulator"], {})
        self.assertEqual(state["mesh_accumulator"], {})
        self.assertEqual(state["poll_count"], 0)

    def test_a_rejected_feed_is_retained_without_re_sending_the_other(self):
        """Separate dispatches mean one feed's failure cannot drag back the other."""
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=MESH_REJECTED):
            self.assertFalse(service.do_flush(state, force=True))
        self.assertEqual(state["accumulator"], {})           # landed, so cleared
        self.assertEqual(len(state["mesh_accumulator"]), 1)  # rejected, so retried
        self.assertGreater(runtime.next_flush_attempt, 0)

    def test_each_feed_gets_its_own_url(self):
        state = self._state()
        with mock.patch.object(config, "MESH_UPLOAD_URL", "http://mesh"), \
             mock.patch.object(config, "UPLOAD_URL", "http://ac"), \
             mock.patch.object(service, "upload_records", return_value=DISPATCHED) as up:
            service.do_flush(state, force=True)
        self.assertEqual(up.call_args.kwargs["aircraft_url"], "http://ac")
        self.assertEqual(up.call_args.kwargs["mesh_url"], "http://mesh")

    def test_records_arriving_during_upload_survive(self):
        """Mid-upload ingests must not be wiped with the batch."""
        state = self._state()

        def slow_upload(aircraft, mesh, key, **urls):
            state["mesh_accumulator"]["n2"] = {"node_id": "n2"}     # new node
            state["accumulator"]["A"] = {"icao": "A", "alt_ft": 1}  # re-seen
            return DISPATCHED

        with mock.patch.object(service, "upload_records", side_effect=slow_upload):
            self.assertTrue(service.do_flush(state, force=True))

        self.assertIn("n2", state["mesh_accumulator"])
        self.assertIn("A", state["accumulator"])     # replaced, so not uploaded
        self.assertNotIn("B", state["accumulator"])  # uploaded, so dropped

    def test_retry_backoff_is_respected(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=NOTHING_LANDED):
            service.do_flush(state, force=True)
        state["window_start"] = 0  # window is long overdue
        with mock.patch.object(service, "upload_records", return_value=DISPATCHED) as up:
            self.assertFalse(service.do_flush(state))  # still inside backoff
            up.assert_not_called()

    def test_backoff_clears_after_a_success(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=NOTHING_LANDED):
            service.do_flush(state, force=True)
        with mock.patch.object(service, "upload_records", return_value=DISPATCHED):
            service.do_flush(state, force=True)
        self.assertEqual(runtime.next_flush_attempt, 0.0)

    def test_dispatch_summary_reports_each_feed(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", side_effect=self._record_failure):
            service.do_flush(state, force=True)

        runtime.reset()
        storage.load_state()
        self.assertTrue(runtime.last_upload["aircraft_success"])
        self.assertFalse(runtime.last_upload["mesh_success"])

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
    def _record_failure(aircraft, mesh, api_key, **urls):
        runtime.last_upload = {
            "timestamp": 1700000000.0, "aircraft_count": len(aircraft),
            "mesh_count": len(mesh), "aircraft_success": True,
            "mesh_success": False, "success": False,
        }
        return MESH_REJECTED

    def test_snapshot_is_written_before_upload(self):
        state = self._state()
        with mock.patch.object(service, "upload_records", return_value=DISPATCHED):
            service.do_flush(state, force=True)
        self.assertEqual(len(list(config.SNAPSHOT_DIR.glob("upload_*.json"))), 1)

    def test_the_snapshot_records_what_was_sent(self):
        state = self._state()
        sent = {}
        with mock.patch.object(service, "upload_records",
                               side_effect=lambda ac, mesh, key, **kw: sent.update(mesh=mesh)
                               or DISPATCHED):
            service.do_flush(state, force=True)
        snapshot = json.loads(next(config.SNAPSHOT_DIR.glob("upload_*.json")).read_text())
        self.assertEqual(snapshot["meshcore_nodes"], sent["mesh"])


class TestDispatchLogging(support.TempConfig):
    """Each flush leaves a record of what the server made of it."""

    def _state(self):
        state = runtime.default_state()
        state["accumulator"] = {"A": {"icao": "A"}}
        state["mesh_accumulator"] = {"n1": {"node_id": "n1", "lat": 1, "lon": 2}}
        return state

    def _upload(self, result, **summary):
        """Stand in for upload_records, which is what writes last_upload."""
        def run(*_args, **_kwargs):
            with runtime.lock:
                runtime.last_upload = {"timestamp": 1700000000.0, "aircraft_count": 1,
                                       "mesh_count": 1, "success": result.ok, **summary}
            return result
        return run

    def test_a_successful_dispatch_is_logged(self):
        with mock.patch.object(service, "upload_records", side_effect=self._upload(DISPATCHED)):
            service.do_flush(self._state(), force=True)
        entries = storage.load_dispatch_log()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["success"])
        self.assertEqual(entries[0]["polls"], 0)
        self.assertIn("window_start", entries[0])

    def test_a_failed_dispatch_is_logged_too(self):
        """The failures are exactly the ones worth looking back at."""
        with mock.patch.object(service, "upload_records",
                               side_effect=self._upload(NOTHING_LANDED)):
            service.do_flush(self._state(), force=True)
        entries = storage.load_dispatch_log()
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["success"])

    def test_refusal_reasons_reach_the_log(self):
        upload = self._upload(MESH_REJECTED, mesh_rejected=3,
                              mesh_reject_reasons={"bad_node_id": 3})
        with mock.patch.object(service, "upload_records", side_effect=upload):
            service.do_flush(self._state(), force=True)
        self.assertEqual(storage.load_dispatch_log()[0]["mesh_reject_reasons"],
                         {"bad_node_id": 3})

    def test_successive_flushes_build_a_history(self):
        for _ in range(3):
            with mock.patch.object(service, "upload_records",
                                   side_effect=self._upload(DISPATCHED)):
                service.do_flush(self._state(), force=True)
        self.assertEqual(len(storage.load_dispatch_log()), 3)

    def test_an_empty_window_logs_nothing(self):
        """Nothing was dispatched, so there is no verdict to record."""
        service.do_flush(runtime.default_state(), force=True)
        self.assertEqual(storage.load_dispatch_log(), [])


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


class TestRepeaterPoll(support.TempConfig):
    ADVERT = {"pubkey": "aabb0f1e2d3c4b5a" + "9" * 48, "contact_type": "Repeater",
             "latitude": 52.1, "longitude": 21.0, "rssi": -80, "last_seen": 1700000000.0}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.multiple(
            config, OPENHOP_REPEATER_URL="http://repeater/api",
            OPENHOP_REPEATER_API_KEY="sekret", OPENHOP_REPEATER_CONTACT_TYPES=["Repeater"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_disabled_without_url_or_key(self):
        state = runtime.default_state()
        with mock.patch.object(config, "OPENHOP_REPEATER_URL", ""), \
             mock.patch.object(service, "fetch_repeater_adverts") as fetch:
            service.do_repeater_poll(state, force=True)
        fetch.assert_not_called()

    def test_a_new_advert_is_merged_into_the_mesh_accumulator(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_repeater_adverts", return_value=[self.ADVERT]):
            service.do_repeater_poll(state, force=True)
        self.assertEqual(len(state["mesh_accumulator"]), 1)
        self.assertEqual(state["advert_cursor"][self.ADVERT["pubkey"]], 1700000000.0)

    def test_an_unchanged_advert_is_not_re_merged_on_the_next_poll(self):
        """The repeater's table holds one row per node — a re-poll of the same
        latest-known state must not keep re-adding it to the accumulator."""
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_repeater_adverts", return_value=[self.ADVERT]):
            service.do_repeater_poll(state, force=True)
            service.do_repeater_poll(state, force=True)
        self.assertEqual(len(state["mesh_accumulator"]), 1)

    def test_state_is_only_saved_when_something_new_landed(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_repeater_adverts", return_value=[]), \
             mock.patch.object(storage, "save_state") as save:
            service.do_repeater_poll(state, force=True)
        save.assert_not_called()

    def test_without_force_it_respects_its_own_poll_interval(self):
        state = runtime.default_state()
        runtime.next_repeater_poll = time.time() + 999
        with mock.patch.object(service, "fetch_repeater_adverts") as fetch:
            service.do_repeater_poll(state)
        fetch.assert_not_called()

    def test_an_unreachable_repeater_does_not_raise(self):
        state = runtime.default_state()
        with mock.patch.object(service, "fetch_repeater_adverts",
                               side_effect=urllib.error.URLError("no route")):
            service.do_repeater_poll(state, force=True)  # must not raise
        self.assertEqual(state["mesh_accumulator"], {})


class TestPreflight(support.TempConfig):
    def test_writable_directories_pass(self):
        self.assertTrue(service.preflight_storage())

    def test_unwritable_directory_fails(self):
        """The root-owned ./data bind mount must fail loudly at startup."""
        with mock.patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            self.assertFalse(service.preflight_storage())


if __name__ == "__main__":
    unittest.main()
