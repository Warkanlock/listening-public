"""Keep a developer's Listening overrides out of path-sensitive tests."""

import os
import unittest
from unittest.mock import patch


def isolate_room_environment():
    """Install as setUpModule so child processes inherit the same isolation."""
    prefixes = ("LISTENING_ROOM_", "OFFLINE_MUSIC_", "LISTENING_ENGINE_", "LISTENING_YOUTUBE_",
                "LISTENING_JELLYFIN_", "LISTENING_AUDIOBOOKSHELF_")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(prefixes)}
    override = patch.dict(os.environ, environment, clear=True)
    override.start()
    unittest.addModuleCleanup(override.stop)
