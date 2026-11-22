"""Start the macOS guard before plain unittest discovery imports app tests."""

import os
from pathlib import Path
import subprocess
import sys


def load_tests(_loader, tests, _pattern):
    if sys.platform != "darwin" or os.environ.get("LISTENING_TEST_AUDIT_ACTIVE") == "1":
        return tests
    script = Path(__file__).resolve().parents[1] / "scripts" / "audit_tests.py"
    spec = getattr(sys.modules["__main__"], "__spec__", None)
    if spec and spec.name == "unittest.__main__" and sys.argv[1:2] == ["discover"]:
        raise SystemExit(subprocess.call([sys.executable, str(script), *sys.argv[1:]]))
    raise SystemExit(f"Run this suite with {script}.")
