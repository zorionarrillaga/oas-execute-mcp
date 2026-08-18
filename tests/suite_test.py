"""pytest bridge over the script-style suite.

Runs each `tests/test_*.py` as a subprocess and asserts it exits 0. The scripts
remain the source of truth — this only makes `pytest` a working entry point for
someone whose reflex is to type it.
"""

import os
import pathlib
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPTS = sorted(p.name for p in HERE.glob("test_*.py"))


@pytest.mark.parametrize("script", SCRIPTS)
def test_script_exits_clean(script):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run(
        [sys.executable, str(HERE / script)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, (
        f"{script} exited {proc.returncode}\n"
        f"--- stdout ---\n{proc.stdout[-4000:]}\n"
        f"--- stderr ---\n{proc.stderr[-4000:]}"
    )
