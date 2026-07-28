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
