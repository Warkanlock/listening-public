import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import os
import subprocess
import tempfile
import unittest

from fixture_environment import isolate_room_environment as setUpModule

BACKEND = Path(__file__).resolve().parent.parent / "backend"


class ImportPurityTests(unittest.TestCase):
    def test_import_server_creates_nothing(self):
        """Importing the server with HOME on an empty temp dir must not
        create directories, write files, or otherwise touch state; all
        of that belongs to init()."""
        with tempfile.TemporaryDirectory() as home:
            env = dict(os.environ)
            env["HOME"] = home
            env["PYTHONPATH"] = str(BACKEND)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env.pop("LISTENING_ROOM_CONFIG", None)
            env.pop("OFFLINE_MUSIC_ROOT", None)
            result = subprocess.run([sys.executable, "-c", "import server"],
                                    cwd=home, env=env, capture_output=True, text=True,
                                    timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            created = [str(path) for path in Path(home).rglob("*")]
            self.assertEqual(created, [])


if __name__ == "__main__":
    unittest.main()
