"""
SeedSigner Max 0022: runs tests/seedsigner_max_word_taps.py in its own interpreter,
because it replaces hardware modules with mocks.
"""
import os
import subprocess
import sys


def test_seed_word_taps():
    here = os.path.dirname(os.path.abspath(__file__))
    env = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(here), "src"))
    result = subprocess.run([sys.executable, os.path.join(here, "seedsigner_max_word_taps.py")],
                            capture_output=True, text=True, env=env, timeout=300)
    print(result.stdout)
    assert result.returncode == 0, result.stdout + result.stderr
