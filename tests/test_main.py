"""
Anti-Klepto Check (verifier SD): `src/main.py` starts the nonce verifier, never the
SeedSigner Controller. Replaces upstream's test_main.py, which drove the Controller (on
this image main() starts the verifier, and that test would hang).
"""
import os
import subprocess
import sys
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))


def test_main_starts_the_verifier():
    import main
    with patch("nonceverify.app.main") as verifier_main:
        main.main([])
    verifier_main.assert_called_once_with()


def test_main_does_not_load_the_signer():
    """
    The verifier's modules, as main() loads them at boot, pull in no SeedSigner
    controller, view or seed code, no embit and no urtypes
    """
    code = (
        "import sys; sys.path.insert(0, 'src'); import main, nonceverify.app, nonceverify.hw; "
        "bad = [m for m in sys.modules if m.startswith(('seedsigner.controller', 'seedsigner.views', "
        "'seedsigner.models.seed', 'embit', 'urtypes'))]; "
        "print(bad); sys.exit(1 if bad else 0)"
    )
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                            timeout=60, cwd=REPO)
    assert result.returncode == 0, result.stdout + result.stderr
