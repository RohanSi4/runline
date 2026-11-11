"""Exercise GPS session behavior without collecting anyone's location."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="session checks need Node")
def test_foreground_run_session():
    result = subprocess.run(["node", "--test", str(Path(__file__).with_name("run_session.mjs"))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
