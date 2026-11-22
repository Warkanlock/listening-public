"""Staged clients are offered only when the matching files verify."""

import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from system import client_artifacts


class ClientArtifactsTests(unittest.TestCase):
    def test_release_selection_rejects_missing_wrong_version_modified_and_symlink_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            version = "1.2.3"
            name, other = client_artifacts.names(version)
            self.assertTrue(all("url" not in item for item in
                                client_artifacts.status(directory, version, "")["installers"]))
            for file in (name, other):
                (directory / file).write_bytes(b"artifact fixture")
            client_artifacts.build_manifest(directory, version)
            clients = client_artifacts.status(directory, version, "http://room.test")
            self.assertEqual(clients["origin"], "http://room.test")
            self.assertTrue(all("url" in item for item in clients["installers"]))
            self.assertTrue(all("url" not in item for item in
                                client_artifacts.status(directory, "1.2.4", "")["installers"]))
            (directory / name).write_bytes(b"modified fixture")
            (directory / other).rename(directory / "original")
            (directory / other).symlink_to(directory / "original")
            self.assertTrue(all("url" not in item for item in
                                client_artifacts.status(directory, version, "")["installers"]))
