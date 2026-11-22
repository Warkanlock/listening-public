"""Pin the bootstrap registration before the web client reads room data."""

import unittest
from pathlib import Path

STORE = (Path(__file__).resolve().parent.parent
         / "frontend" / "react" / "src" / "store.tsx")


class StartupOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = STORE.read_text(encoding="utf-8")
        check = source.index("async checkRoom()")
        load = source.index("private async loadRoom()", check)
        end = source.index("await this.loadTracks(true);", load)
        cls.check = source[check:load]
        cls.load = source[load:end]

    def test_registration_precedes_the_room_probe_and_first_data_load(self):
        registered = self.check.index("await this.settingsSlice.registerDevice(")
        checked = self.check.index('("/api/room",')
        self.assertLess(registered, checked)
        self.assertIn('if (state === "ready" && previous !== "ready") void this.loadRoom()', self.check)

    def test_registration_is_not_part_of_the_data_batch(self):
        batch = self.load.index("Promise.allSettled([")
        self.assertNotIn("/api/device", self.load[batch:])
        self.assertNotIn("heartbeat(", self.load[batch:])
