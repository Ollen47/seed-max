"""
SeedSigner Max 0024: runs tests/seedsigner_max_guard.py in its own interpreter,
because it replaces hardware modules with mocks.
"""
import os
import subprocess
import sys


def test_guard_toasts_hold():
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(here), "src"))
    result = subprocess.run([sys.executable, os.path.join(here, "seedsigner_max_guard.py")],
                            capture_output=True, text=True, env=env, timeout=600)
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
