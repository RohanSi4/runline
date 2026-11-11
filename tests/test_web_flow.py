"""Run the client-state regressions when Node is available to developers."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="client-state checks need Node")
def test_address_response_and_pin_state() -> None:
    result = subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("web_flow.mjs"))],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
