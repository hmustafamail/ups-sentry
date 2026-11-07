"""Keeps private values out of this public repo. pytest tests/test_private_data.py -v"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ups_sentry.cli import _setup_logging

ROOT = Path(__file__).parent.parent
PRIVATE = {
    "a macOS Bluetooth address or other UUID": r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
    "a Bluetooth MAC address": r"\b(?!AA:BB:CC:DD:EE:FF\b)[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b",
    "a heartbeat URL": r"hc-ping\.com/[0-9a-f]{8,}",
    "a logged session key": r"(?:Secure|Unsecure) (?:key|iv) +[0-9a-f]{16,}",
    "a Pushover credential": r"pushover_\w+ *= *\"[A-Za-z0-9]{20,}\"",
}


def _tracked_files() -> list[Path]:
    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    names = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    return [ROOT / name for name in names if (ROOT / name).is_file()]


@pytest.mark.parametrize("what", PRIVATE)
def test_no_tracked_file_holds(what):
    pattern = re.compile(PRIVATE[what])
    found = [str(path.relative_to(ROOT)) for path in _tracked_files()
             if pattern.search(path.read_text(errors="ignore"))]
    assert not found, f"{what} in {found}"


def test_session_keys_never_reach_the_logs(tmp_path):
    _setup_logging(str(tmp_path / "monitor.log"))
    encryption = logging.getLogger("bluetti_bt_lib.bluetooth.encryption")
    assert not encryption.isEnabledFor(logging.INFO)
    assert encryption.isEnabledFor(logging.WARNING)  # failed reads still say why
