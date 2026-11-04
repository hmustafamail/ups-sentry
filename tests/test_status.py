"""Tests for src/ups_sentry/status.py. pytest tests/test_status.py -v"""
from __future__ import annotations

import os

from ups_sentry.status import read_status, write_status


class TestStatusFile:
    def test_round_trip_making_the_folder(self, tmp_path):
        path = tmp_path / "state" / "status.json"
        write_status(path, {"power": "wall"})
        assert read_status(path) == {"power": "wall"}

    def test_replaces_and_leaves_no_temporary_file(self, tmp_path):
        path = tmp_path / "status.json"
        write_status(path, {"power": "wall"})
        write_status(path, {"power": "battery"})
        assert read_status(path) == {"power": "battery"}
        assert os.listdir(tmp_path) == ["status.json"]

    def test_readable_by_others(self, tmp_path):
        """Naive version: mkstemp's 0600 would hide it from a reader running as another user."""
        path = tmp_path / "status.json"
        write_status(path, {})
        assert path.stat().st_mode & 0o777 == 0o644

    def test_a_field_json_cant_represent_doesnt_stop_the_monitor(self, tmp_path):
        """A third-party driver could return anything in `fields`; only OSError is handled around this write."""
        path = tmp_path / "status.json"
        write_status(path, {"station_fields": {"odd": {1}}})
        assert read_status(path) == {"station_fields": {"odd": "{1}"}}

    def test_a_missing_or_broken_file_reads_as_none(self, tmp_path):
        assert read_status(tmp_path / "absent.json") is None
        broken = tmp_path / "broken.json"
        broken.write_text("{half a fi")
        assert read_status(broken) is None
        broken.write_text("[1, 2]")
        assert read_status(broken) is None
