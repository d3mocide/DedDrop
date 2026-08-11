"""Shared test setup.

deddrop.config reads the environment at import time, so every test module
imports this first to pin config at safe temp paths before that happens.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="deddrop-tests-")
os.environ.setdefault("TAR1090_URL", "http://127.0.0.1:9/aircraft.json")
os.environ.setdefault("WDGWARS_API_KEY", "a" * 64)
os.environ["STATE_FILE"] = str(Path(_TMP) / "state" / "accumulator.json")
os.environ["SNAPSHOT_DIR"] = str(Path(_TMP) / "snapshots")
os.environ["WEB_ENABLED"] = "false"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from deddrop import config, runtime  # noqa: E402,F401


def quiet_logs():
    """Several tests exercise warning/error paths on purpose."""
    logging.disable(logging.CRITICAL)


def restore_logs():
    logging.disable(logging.NOTSET)


class FakeResponse:
    """Stands in for the object urlopen() yields, reusable across calls."""

    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status
        self.headers = {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RuntimeIsolated(unittest.TestCase):
    """Restores shared runtime state between tests."""

    def setUp(self):
        runtime.reset()
        self.addCleanup(runtime.reset)


class TempConfig(RuntimeIsolated):
    """RuntimeIsolated plus STATE_FILE/SNAPSHOT_DIR pointed at a temp dir."""

    def setUp(self):
        super().setUp()
        from unittest import mock

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.state_file = self.root / "state" / "accumulator.json"
        self.snapshot_dir = self.root / "snapshots"
        self.dispatch_log_file = self.root / "state" / "dispatch_log.json"
        for attr, value in (("STATE_FILE", self.state_file),
                            ("SNAPSHOT_DIR", self.snapshot_dir),
                            ("DISPATCH_LOG_FILE", self.dispatch_log_file)):
            patcher = mock.patch.object(config, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
