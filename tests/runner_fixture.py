"""Give in-process runner tests disposable defaults before importing the CLI."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from system import config

_workspace = TemporaryDirectory(prefix="listening-test-runner-")
_root = Path(_workspace.name).resolve()
_settings = _root / "state" / "settings.json"
def _settings_path():
    return _settings


# runner resolves its paths on import and keeps its own settings resolver.
# Individual tests can still override those paths with their own fixtures.
with patch.dict(os.environ, {
    "OFFLINE_MUSIC_ROOT": str(_root / "room"),
}, clear=True), patch.object(config, "resolve_config_path", _settings_path):
    import runner
