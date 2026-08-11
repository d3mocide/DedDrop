"""State persistence and the snapshot archive."""
import json
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import support
from deddrop import runtime, storage

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs


class TestState(support.TempConfig):
    def test_round_trip(self):
        state = runtime.default_state()
        state["accumulator"]["A"] = {"icao": "A"}
        state["poll_count"] = 7
        storage.save_state(state)
        loaded = storage.load_state()
        self.assertEqual(loaded["poll_count"], 7)
        self.assertEqual(loaded["accumulator"]["A"]["icao"], "A")

    def test_missing_file_yields_defaults(self):
        self.assertEqual(storage.load_state()["accumulator"], {})

    def test_corrupt_file_does_not_raise(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("{not json")
        self.assertEqual(storage.load_state()["accumulator"], {})

    def test_wrong_types_are_repaired(self):
        """A list accumulator must not blow up on the next merge."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "accumulator": ["not", "a", "dict"],
            "mesh_accumulator": {"good": {"node_id": "good"}, "bad": "string"},
            "poll_count": "seven",
        }))
        state = storage.load_state()
        self.assertEqual(state["accumulator"], {})
        self.assertEqual(list(state["mesh_accumulator"]), ["good"])
        self.assertEqual(state["poll_count"], 0)

    def test_top_level_non_object_is_repaired(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("[1, 2, 3]")
        self.assertEqual(storage.load_state()["accumulator"], {})

    def test_future_window_start_is_reset(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"window_start": time.time() + 999999}))
        self.assertLessEqual(storage.load_state()["window_start"], time.time() + 1)

    def test_last_upload_survives_a_restart(self):
        """The dashboard must still show the last dispatch after a restart."""
        runtime.last_upload = {
            "timestamp": 1700000000.0, "aircraft_count": 42, "mesh_count": 0,
            "aircraft_imported": 12, "aircraft_seen": 30, "mesh_imported": 0,
            "mesh_seen": 0, "success": True, "dry_run": False,
        }
        storage.save_state(runtime.default_state())

        runtime.last_upload = {}
        storage.load_state()
        self.assertEqual(runtime.last_upload["aircraft_count"], 42)
        self.assertEqual(runtime.last_upload["aircraft_imported"], 12)
        self.assertEqual(runtime.last_upload["timestamp"], 1700000000.0)
        self.assertTrue(runtime.last_upload["success"])

    def test_last_upload_is_absent_before_any_dispatch(self):
        storage.save_state(runtime.default_state())
        storage.load_state()
        self.assertEqual(runtime.last_upload, {})

    def test_malformed_last_upload_is_discarded(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"last_upload": {"aircraft_count": 5}}))
        storage.load_state()
        self.assertEqual(runtime.last_upload, {})

        self.state_file.write_text(json.dumps({"last_upload": "nope"}))
        storage.load_state()
        self.assertEqual(runtime.last_upload, {})

    def test_bad_last_upload_field_does_not_lose_the_summary(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({
            "last_upload": {"timestamp": 1700000000.0, "aircraft_count": "many"},
        }))
        storage.load_state()
        self.assertEqual(runtime.last_upload["timestamp"], 1700000000.0)
        self.assertNotIn("aircraft_count", runtime.last_upload)

    def test_last_upload_does_not_leak_into_state(self):
        runtime.last_upload = {"timestamp": 1700000000.0}
        storage.save_state(runtime.default_state())
        self.assertNotIn("last_upload", storage.load_state())

    def test_concurrent_saves_never_corrupt_the_file(self):
        """Every writer must use its own temp file."""
        state = runtime.default_state()
        state["accumulator"] = {f"K{i}": {"icao": f"K{i}"} for i in range(500)}
        errors = []

        def writer():
            try:
                for _ in range(20):
                    storage.save_state(state)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(storage.load_state()["accumulator"]), 500)
        leftovers = list(self.state_file.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")

    def test_atomic_write_leaves_no_temp_on_failure(self):
        target = self.root / "out.json"
        with mock.patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                storage.atomic_write(target, "{}")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertFalse(target.exists())


def dispatch(timestamp=1700000000.0, **overrides):
    return {"timestamp": timestamp, "window_start": timestamp - 21600,
            "window_end": timestamp, "polls": 720, "aircraft_count": 400,
            "aircraft_imported": 390, "mesh_count": 12, "mesh_imported": 12,
            "success": True, **overrides}


class TestDispatchLog(support.TempConfig):
    """History of what WDGWars made of each window, one entry per dispatch."""

    def test_entries_round_trip(self):
        storage.append_dispatch(dispatch())
        entries = storage.load_dispatch_log()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["polls"], 720)
        self.assertEqual(entries[0]["aircraft_imported"], 390)

    def test_entries_accumulate_oldest_first(self):
        for ts in (1700000000.0, 1700021600.0, 1700043200.0):
            storage.append_dispatch(dispatch(ts))
        self.assertEqual([e["timestamp"] for e in storage.load_dispatch_log()],
                         [1700000000.0, 1700021600.0, 1700043200.0])

    def test_the_log_is_bounded(self):
        with mock.patch.object(storage.config, "DISPATCH_LOG_LIMIT", 3):
            for ts in range(1700000000, 1700000006):
                storage.append_dispatch(dispatch(float(ts)))
            entries = storage.load_dispatch_log()
        self.assertEqual(len(entries), 3)
        # The three most recent survive; the oldest fall off.
        self.assertEqual([e["timestamp"] for e in entries],
                         [1700000003.0, 1700000004.0, 1700000005.0])

    def test_reject_reasons_survive_the_round_trip(self):
        storage.append_dispatch(dispatch(mesh_rejected=3,
                                         mesh_reject_reasons={"bad_node_id": 3}))
        self.assertEqual(storage.load_dispatch_log()[0]["mesh_reject_reasons"],
                         {"bad_node_id": 3})

    def test_an_undated_entry_is_refused(self):
        storage.append_dispatch({"aircraft_count": 5})
        self.assertEqual(storage.load_dispatch_log(), [])

    def test_a_missing_file_is_an_empty_history(self):
        self.assertEqual(storage.load_dispatch_log(), [])

    def test_a_corrupt_file_does_not_raise(self):
        self.dispatch_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch_log_file.write_text("{not json")
        self.assertEqual(storage.load_dispatch_log(), [])

    def test_a_non_array_document_is_repaired(self):
        self.dispatch_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch_log_file.write_text(json.dumps({"nope": True}))
        self.assertEqual(storage.load_dispatch_log(), [])

    def test_malformed_entries_are_dropped_individually(self):
        self.dispatch_log_file.parent.mkdir(parents=True, exist_ok=True)
        self.dispatch_log_file.write_text(json.dumps(
            [dispatch(), "not-a-dict", {"no": "timestamp"}, dispatch(1700021600.0)]))
        self.assertEqual(len(storage.load_dispatch_log()), 2)

    def test_wrong_field_types_are_dropped_not_fatal(self):
        storage.append_dispatch(dispatch(polls="seven", aircraft_count=400))
        entry = storage.load_dispatch_log()[0]
        self.assertNotIn("polls", entry)
        self.assertEqual(entry["aircraft_count"], 400)

    def test_an_unwritable_path_does_not_break_a_flush(self):
        """History is a nicety; losing it must not fail the dispatch."""
        with mock.patch.object(storage, "atomic_write", side_effect=OSError("read-only")):
            storage.append_dispatch(dispatch())
        # It is still in memory for the dashboard, just not on disk.
        self.assertEqual(len(runtime.dispatch_log), 1)


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.dir = __import__("tempfile").TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)
        storage._snapshot_cache.clear()
        self.addCleanup(storage._snapshot_cache.clear)

    def _write(self, name, aircraft=3, mesh=1):
        (self.path / name).write_text(json.dumps({
            "aircraft_count": aircraft, "mesh_nodes_count": mesh,
            "window_start": "s", "window_end": "e",
            "aircraft": [{"icao": "4CA7B1"}] * aircraft, "meshcore_nodes": []}))

    def test_lists_newest_first(self):
        for name in ("upload_20260101T000000Z.json", "upload_20260102T000000Z.json"):
            self._write(name)
        names = [s["name"] for s in storage.list_snapshots(self.path)]
        self.assertEqual(names[0], "upload_20260102T000000Z.json")

    def test_second_call_is_served_from_cache(self):
        """Archived snapshots must not be re-parsed on every dashboard poll."""
        self._write("upload_20260101T000000Z.json")
        first = storage.list_snapshots(self.path)
        real_read = Path.read_text
        with mock.patch.object(Path, "read_text", autospec=True) as reader:
            reader.side_effect = real_read
            second = storage.list_snapshots(self.path)
            reader.assert_not_called()
        self.assertEqual(first, second)

    def test_cache_evicts_pruned_files(self):
        self._write("upload_20260101T000000Z.json")
        storage.list_snapshots(self.path)
        self.assertEqual(len(storage._snapshot_cache), 1)
        (self.path / "upload_20260101T000000Z.json").unlink()
        self.assertEqual(storage.list_snapshots(self.path), [])
        self.assertEqual(len(storage._snapshot_cache), 0)

    def test_corrupt_snapshot_is_listed_not_fatal(self):
        (self.path / "upload_20260101T000000Z.json").write_text("{broken")
        result = storage.list_snapshots(self.path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["aircraft_count"], 0)

    def test_missing_directory_is_empty(self):
        self.assertEqual(storage.list_snapshots(self.path / "nope"), [])

    def test_respects_the_limit(self):
        for i in range(5):
            self._write(f"upload_2026010{i}T000000Z.json")
        self.assertEqual(len(storage.list_snapshots(self.path, limit=2)), 2)

    def test_prune_keeps_newest(self):
        for i in range(5):
            self._write(f"upload_2026010{i}T000000Z.json")
        storage.prune_snapshots(self.path, keep=2)
        self.assertEqual(sorted(p.name for p in self.path.glob("upload_*.json")),
                         ["upload_20260103T000000Z.json", "upload_20260104T000000Z.json"])

    def test_prune_with_non_positive_keep_is_a_noop(self):
        self._write("upload_20260101T000000Z.json")
        storage.prune_snapshots(self.path, keep=0)
        self.assertEqual(len(list(self.path.glob("upload_*.json"))), 1)

    def test_save_snapshot_round_trips(self):
        path = storage.save_snapshot([{"icao": "A"}], [{"node_id": "n"}],
                                     1700000000, 1700003600, 12, self.path)
        data = json.loads(path.read_text())
        self.assertEqual(data["aircraft_count"], 1)
        self.assertEqual(data["mesh_nodes_count"], 1)
        self.assertEqual(data["polls"], 12)


if __name__ == "__main__":
    unittest.main()
