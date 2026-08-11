"""Translation of tar1090 and MeshMapper payloads into WDGWars records."""
import unittest

import support
from deddrop import normalize

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs

# Ed25519 keys, 64 hex. A node's on-air id is the leading hex of its own key.
KEY = "aabb0f1e2d3c4b5a" + "9" * 48          # short id "aabb"
OTHER_KEY = "aa77c0ffee123456" + "1" * 48    # shares only "aa" with KEY
CCDD_KEY = "ccdd1234abcd5678" + "0" * 48


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

    def test_records_match_the_confirmed_meshcore_wire_shape(self):
        """node_id, node_type, name, lat, lon, rssi, first_seen, type."""
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "0CE8", "node_type": "R", "local_rssi": "-67"})[0]
        self.assertEqual(set(rec), {"node_id", "node_type", "name", "lat", "lon",
                                    "rssi", "first_seen", "type"})
        # The role goes in node_type; `type` is the constant envelope marker.
        # Swapping the two is accepted with meshcore_imported: 0.
        self.assertEqual(rec["type"], "MESHCORE")
        self.assertEqual(rec["node_type"], "REPEATER")
        # The server gates on lowercase hex; MeshMapper exports are uppercase.
        self.assertEqual(rec["node_id"], "0ce8")
        self.assertRegex(rec["first_seen"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_a_key_adds_public_key_and_nothing_else(self):
        """public_key is a live optional field; its absence never rejects."""
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "public_key": KEY})[0]
        self.assertEqual(set(rec), {"node_id", "node_type", "name", "lat", "lon",
                                    "rssi", "first_seen", "type", "public_key"})


class TestMeshNodeIdFromPublicKey(unittest.TestCase):
    """The on-air id is the key's leading hex, so the key is the same identity."""

    def test_node_id_is_the_first_eight_bytes_of_the_key(self):
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "AABB", "public_key": KEY.upper()})[0]
        self.assertEqual(rec["node_id"], KEY[:16])
        self.assertEqual(rec["name"], KEY[:16])
        self.assertEqual(rec["public_key"], KEY)

    def test_the_derived_id_clears_the_server_gate(self):
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "public_key": KEY})[0]
        self.assertEqual(normalize.predict_rejections([rec]), [])

    def test_a_key_that_does_not_start_with_the_short_id_is_refused(self):
        """A mispaired key must never rename a node into someone else's identity."""
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0,
            "repeater_id": "aabb", "public_key": CCDD_KEY})[0]
        self.assertEqual(rec["node_id"], "aabb")
        self.assertNotIn("public_key", rec)

    def test_only_a_full_64_hex_key_is_used(self):
        for key in (KEY[:32], KEY[:63], KEY + "aa", "z" * 64, "", None, "None"):
            with self.subTest(key=key):
                rec = normalize.normalize_mesh_ping({
                    "type": "DISC", "lat": 52.1, "lon": 21.0,
                    "repeater_id": "aabb", "public_key": key})[0]
                self.assertEqual(rec["node_id"], "aabb")

    def test_alternative_spellings_of_the_field_are_read(self):
        """The push payload is unobserved; a camelCase key must not be missed."""
        for field in ("public_key", "publicKey", "pubkey", "pub_key", "public_key_hex"):
            with self.subTest(field=field):
                rec = normalize.normalize_mesh_ping({
                    "type": "DISC", "lat": 52.1, "lon": 21.0,
                    "repeater_id": "aabb", field: KEY})[0]
                self.assertEqual(rec["node_id"], KEY[:16])

    def test_a_ping_without_a_key_is_unchanged(self):
        rec = normalize.normalize_mesh_ping({
            "type": "DISC", "lat": 52.1, "lon": 21.0, "repeater_id": "aabb"})[0]
        self.assertEqual(rec["node_id"], "aabb")
        self.assertNotIn("public_key", rec)


class TestMeshCaptureKeyResolution(unittest.TestCase):
    """heard_repeats tokens carry a short id and no key of their own."""

    def test_a_key_elsewhere_in_the_capture_names_a_heard_node(self):
        records = normalize.normalize_mesh_capture([
            {"type": "DISC", "lat": 52.1, "lon": 21.0,
             "repeater_id": "aabb", "public_key": KEY},
            {"type": "TRACE", "lat": 52.2, "lon": 21.1, "repeater_id": "none",
             "heard_repeats": "aabb(R)(-95.5)"},
        ])
        self.assertEqual([r["node_id"] for r in records], [KEY[:16], KEY[:16]])

    def test_an_ambiguous_prefix_keeps_the_short_id(self):
        """Two keys share "aa", so "aa" names neither of them."""
        records = normalize.normalize_mesh_capture([
            {"type": "DISC", "lat": 52.1, "lon": 21.0,
             "repeater_id": "aabb", "public_key": KEY},
            {"type": "DISC", "lat": 52.2, "lon": 21.1,
             "repeater_id": "aa77", "public_key": OTHER_KEY},
            {"type": "TRACE", "lat": 52.3, "lon": 21.2, "repeater_id": "none",
             "heard_repeats": "aa(-80)"},
        ])
        self.assertEqual(sorted(r["node_id"] for r in records),
                         sorted([KEY[:16], OTHER_KEY[:16], "aa"]))

    def test_a_key_seen_once_names_the_same_node_in_a_keyless_ping(self):
        records = normalize.normalize_mesh_capture([
            {"type": "DISC", "lat": 52.1, "lon": 21.0,
             "repeater_id": "aabb", "public_key": KEY},
            {"type": "DISC", "lat": 52.2, "lon": 21.1, "repeater_id": "aabb"},
        ])
        self.assertEqual({r["node_id"] for r in records}, {KEY[:16]})

    def test_capture_survives_non_dict_pings(self):
        self.assertEqual(normalize.normalize_mesh_capture(["nope", None]), [])

    def test_index_marks_shared_prefixes_unresolvable(self):
        index = normalize.index_public_keys([
            {"public_key": KEY}, {"public_key": OTHER_KEY}])
        self.assertEqual(index["aa"], "")
        self.assertEqual(index["aabb"], KEY)
        self.assertEqual(index["aa77"], OTHER_KEY)


class TestDescribeMeshIngest(unittest.TestCase):
    """Answers "does the push carry the key?" from a real push."""

    def _report(self, pings):
        return normalize.describe_mesh_ingest(pings, normalize.normalize_mesh_capture(pings))

    def test_reports_a_push_that_carries_keys(self):
        report = self._report([{"type": "DISC", "lat": 52.1, "lon": 21.0,
                                "repeater_id": "aabb", "public_key": KEY}])
        self.assertEqual(report["public_key_field"], "public_key")
        self.assertEqual(report["pings_with_public_key"], 1)
        self.assertEqual(report["nodes_passing_node_id_gate"], 1)
        self.assertEqual(report["nodes_short_id"], 0)
        self.assertIn("carry a usable public key", report["verdict"])

    def test_reports_a_push_with_no_key_field_at_all(self):
        report = self._report([{"type": "DISC", "lat": 52.1, "lon": 21.0,
                                "repeater_id": "aabb"}])
        self.assertIsNone(report["public_key_field"])
        self.assertEqual(report["nodes_short_id"], 1)
        self.assertIn("no public key field", report["verdict"])
        self.assertEqual(report["fields"], ["lat", "lon", "repeater_id", "type"])

    def test_distinguishes_a_present_but_unusable_key_field(self):
        report = self._report([{"type": "DISC", "lat": 52.1, "lon": 21.0,
                                "repeater_id": "aabb", "public_key": "aabb"}])
        self.assertEqual(report["public_key_field"], "public_key")
        self.assertEqual(report["pings_with_public_key"], 0)
        self.assertIn("no value in this push was a 64-hex key", report["verdict"])

    def test_the_sample_prefers_a_keyed_ping_and_hides_credentials(self):
        report = self._report([
            {"type": "DISC", "lat": 52.1, "lon": 21.0, "repeater_id": "ccdd"},
            {"type": "DISC", "lat": 52.2, "lon": 21.1, "repeater_id": "aabb",
             "public_key": KEY, "api_key": "s3cret", "note": "x" * 400},
        ])
        self.assertEqual(report["sample_ping"]["repeater_id"], "aabb")
        self.assertEqual(report["sample_ping"]["api_key"], "<redacted>")
        self.assertEqual(len(report["sample_ping"]["note"]), 257)

    def test_an_empty_push_is_described_without_failing(self):
        report = self._report([])
        self.assertEqual(report["pings"], 0)
        self.assertEqual(report["sample_ping"], {})


class TestPredictRejections(unittest.TestCase):
    """WDGWars' gates, mirrored so a refusal is explained before it happens."""

    def test_short_node_ids_are_flagged(self):
        warnings = normalize.predict_rejections([{"node_id": "0ce8", "lat": 1, "lon": 2}])
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad_node_id", warnings[0])

    def test_a_full_length_id_passes(self):
        self.assertEqual(
            normalize.predict_rejections([{"node_id": "a1b2c3d4", "lat": 1, "lon": 2}]), [])

    def test_uppercase_fails_the_lowercase_gate(self):
        self.assertTrue(
            normalize.predict_rejections([{"node_id": "A1B2C3D4", "lat": 1, "lon": 2}]))

    def test_short_ids_are_explained_as_a_missing_key_not_an_impossibility(self):
        """The warning stopped being "this can never be fixed" once keys arrived."""
        warning = normalize.predict_rejections([{"node_id": "0ce8", "lat": 1, "lon": 2}])[0]
        self.assertIn("without a public_key", warning)

    def test_a_mismatched_public_key_is_flagged(self):
        warnings = normalize.predict_rejections(
            [{"node_id": KEY[:16], "lat": 1, "lon": 2, "public_key": OTHER_KEY}])
        self.assertEqual(len(warnings), 1)
        self.assertIn("key_prefix_mismatch", warnings[0])

    def test_a_short_public_key_is_flagged(self):
        warnings = normalize.predict_rejections(
            [{"node_id": "aabb0f1e", "lat": 1, "lon": 2, "public_key": "aabb0f1e"}])
        self.assertIn("bad_public_key", warnings[0])

    def test_a_well_formed_key_passes(self):
        self.assertEqual(normalize.predict_rejections(
            [{"node_id": KEY[:16], "lat": 1, "lon": 2, "public_key": KEY}]), [])

    def test_missing_gps_is_flagged(self):
        warnings = normalize.predict_rejections([{"node_id": "a1b2c3d4", "lat": 0, "lon": 0}])
        self.assertEqual(len(warnings), 1)
        self.assertIn("no_gps", warnings[0])

    def test_nothing_to_say_about_an_empty_window(self):
        self.assertEqual(normalize.predict_rejections([]), [])


if __name__ == "__main__":
    unittest.main()
