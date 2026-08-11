"""The ingest probe: local self-test, and reading a live instance's report."""
import io
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import support
from deddrop import probe

setUpModule = support.quiet_logs
tearDownModule = support.restore_logs

KEY = "aabb0f1e2d3c4b5a" + "9" * 48


def run(*argv) -> tuple[int, str]:
    """Return (exit code, stdout) for one probe invocation.

    Several cases exercise the failure paths on purpose, so stderr is swallowed
    rather than left to scroll past the test output.
    """
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = probe.main(list(argv))
    return code, out.getvalue()


class TestSelfTest(unittest.TestCase):
    def test_the_synthetic_push_derives_both_nodes(self):
        code, out = run("--self-test")
        self.assertEqual(code, 0)
        # The DISC ping's own key, and the heard token resolved against it.
        self.assertEqual(out.count("derived from key"), 2)
        self.assertIn("carry a usable public key", out)

    def test_it_reports_the_node_it_cannot_widen(self):
        _, out = run("--self-test")
        self.assertIn("short id alone", out)

    def test_nothing_is_sent_anywhere(self):
        with mock.patch.object(probe.urllib.request, "urlopen") as urlopen:
            run("--self-test")
        urlopen.assert_not_called()


class TestReport(unittest.TestCase):
    def _run_against(self, report):
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=support.FakeResponse(report)):
            return run("--key", "k")

    def test_a_push_carrying_keys_exits_zero(self):
        code, out = self._run_against({
            "ok": True, "pings": 4, "fields": ["public_key", "repeater_id"],
            "public_key_field": "public_key", "pings_with_public_key": 4,
            "nodes": 4, "nodes_passing_node_id_gate": 4, "nodes_short_id": 0,
            "sample_ping": {"repeater_id": "aabb", "public_key": KEY},
            "verdict": "pings carry a usable public key (4/4)"})
        self.assertEqual(code, 0)
        self.assertIn("public_key", out)
        self.assertIn("aabb", out)

    def test_a_push_without_keys_exits_nonzero(self):
        code, out = self._run_against({
            "ok": True, "pings": 4, "fields": ["repeater_id"], "public_key_field": None,
            "pings_with_public_key": 0, "nodes": 4, "nodes_passing_node_id_gate": 0,
            "nodes_short_id": 4, "sample_ping": {"repeater_id": "aabb"},
            "verdict": "no public key field in this push"})
        self.assertEqual(code, 3)
        self.assertIn("(absent)", out)

    def test_no_push_yet_says_so(self):
        code, out = self._run_against({"ok": True, "verdict": "no MeshMapper push has arrived"})
        self.assertEqual(code, 3)
        self.assertIn("Point MeshMapper at this instance", out)

    def test_a_refused_key_is_named(self):
        error = urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, io.BytesIO(b"{}"))
        with mock.patch.object(probe.urllib.request, "urlopen", side_effect=error):
            code, _ = run("--key", "wrong")
        self.assertEqual(code, 2)

    def test_an_unreachable_instance_exits_two(self):
        with mock.patch.object(probe.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("refused")):
            code, _ = run("--key", "k")
        self.assertEqual(code, 2)

    def test_a_missing_key_is_refused_before_any_request(self):
        with mock.patch.object(probe.config, "MESHMAPPER_API_KEY", ""), \
                mock.patch.object(probe.config, "API_KEY", ""):
            code, _ = run()
        self.assertEqual(code, 2)

    def test_the_url_argument_is_used_verbatim(self):
        with mock.patch.object(probe.urllib.request, "urlopen",
                               return_value=support.FakeResponse({"ok": True})) as urlopen:
            run("--key", "k", "--url", "http://deddrop.local:8989/")
        self.assertEqual(urlopen.call_args.args[0].full_url,
                         "http://deddrop.local:8989/api/mesh-ingest-report")


if __name__ == "__main__":
    unittest.main()
