"""Translation of tar1090 and MeshMapper payloads into WDGWars records."""
import unittest

import support
from deddrop import normalize

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs


class TestNormalizeAircraft(unittest.TestCase):
    TS = "2026-01-01 00:00:00"

    def test_accepts_a_well_formed_aircraft(self):
        result = normalize.normalize_one(
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
                self.assertIsNone(normalize.normalize_one(
                    {"hex": hex_id, "lat": 1.0, "lon": 1.0}, self.TS))

    def test_rejects_missing_or_out_of_range_position(self):
        for ac in ({"hex": "4CA7B1"},
                   {"hex": "4CA7B1", "lat": 52.1},
                   {"hex": "4CA7B1", "lat": 91.0, "lon": 21.0},
                   {"hex": "4CA7B1", "lat": 52.1, "lon": 181.0},
                   {"hex": "4CA7B1", "lat": "north", "lon": 21.0}):
            with self.subTest(ac=ac):
                self.assertIsNone(normalize.normalize_one(ac, self.TS))

    def test_missing_telemetry_is_null_not_zero(self):
        """Absent gs/track must not read as 0 kt heading due north."""
        _, rec = normalize.normalize_one(
            {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0}, self.TS)
        self.assertIsNone(rec["speed_kt"])
        self.assertIsNone(rec["heading"])
        self.assertIsNone(rec["alt_ft"])

    def test_ground_is_a_real_zero_altitude(self):
        _, rec = normalize.normalize_one(
            {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0, "alt_baro": "ground"}, self.TS)
        self.assertEqual(rec["alt_ft"], 0)

    def test_parse_snapshot_counts_skips(self):
        records, skipped = normalize.parse_snapshot({
            "now": 1700000000,
            "aircraft": [
                {"hex": "4CA7B1", "lat": 52.1, "lon": 21.0},
                {"hex": "BAD"},
                {"hex": "4CA7B2"},
                "not-a-dict",
            ],
        })
        self.assertEqual(len(records), 1)
        self.assertEqual(skipped, 3)

    def test_parse_snapshot_survives_a_bogus_now(self):
        records, _ = normalize.parse_snapshot({
            "now": 99999999999999,
            "aircraft": [{"hex": "4CA7B1", "lat": 52.1, "lon": 21.0}]})
        self.assertRegex(records["4CA7B1"]["first_seen"], r"^\d{4}-\d{2}-\d{2} ")

    def test_merge_preserves_first_seen(self):
        acc = {}
        normalize.merge_into(acc, {"A": {"icao": "A", "first_seen": "t1", "lat": 1}})
        normalize.merge_into(acc, {"A": {"icao": "A", "first_seen": "t2", "lat": 2}})
        self.assertEqual(acc["A"]["first_seen"], "t1")
        self.assertEqual(acc["A"]["lat"], 2)


class TestNormalizeMesh(unittest.TestCase):
    def test_disc_ping_becomes_a_repeater(self):
        recs = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "AABB", "node_type": "R", "local_rssi": "-95"})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["node_id"], "aabb")
        self.assertEqual(recs[0]["node_type"], "REPEATER")
        self.assertEqual(recs[0]["rssi"], -95)

    def test_unparseable_rssi_is_null(self):
        recs = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "local_rssi": "strong"})
        self.assertIsNone(recs[0]["rssi"])

    def test_rejects_null_island_and_bad_positions(self):
        for ping in ({"type": "DISC", "lat": 0, "lon": 0, "repeater_id": "aa"},
                     {"type": "DISC", "lat": None, "lon": 21.0, "repeater_id": "aa"},
                     {"type": "DISC", "lat": 91, "lon": 21.0, "repeater_id": "aa"}):
            with self.subTest(ping=ping):
                self.assertEqual(normalize.normalize_mesh_ping(ping), [])

    def test_heard_repeats_tokens(self):
        recs = normalize.normalize_mesh_ping({
            "type": "TRACE", "lat": 52.1, "lon": 21.0, "repeater_id": "none",
            "heard_repeats": "aabb(R)(-95.5), ccdd(-80), notatoken!"})
        self.assertEqual(sorted(r["node_id"] for r in recs), ["aabb", "ccdd"])

    def test_unusable_timestamp_falls_back_to_arrival(self):
        recs = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "timestamp": 99999999999999})
        self.assertEqual(len(recs), 1)
        self.assertRegex(recs[0]["first_seen"], r"^\d{4}-\d{2}-\d{2} ")

    def test_merge_preserves_first_seen(self):
        acc = {}
        normalize.merge_mesh_records(acc, [{"node_id": "n", "first_seen": "t1"}])
        normalize.merge_mesh_records(acc, [{"node_id": "n", "first_seen": "t2"}])
        self.assertEqual(acc["n"]["first_seen"], "t1")


if __name__ == "__main__":
    unittest.main()
