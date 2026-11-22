import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import unittest

from system.hosts import host_permitted, parse_host


class ParseHostTests(unittest.TestCase):
    def test_strips_port_and_folds_case(self):
        self.assertEqual(parse_host("Room.Local:8765"), "room.local")
        self.assertEqual(parse_host("LOCALHOST"), "localhost")
        self.assertEqual(parse_host("  example.com  "), "example.com")

    def test_ipv6_forms(self):
        self.assertEqual(parse_host("[::1]:8765"), "::1")
        self.assertEqual(parse_host("[2001:db8::1]"), "2001:db8::1")
        self.assertEqual(parse_host("::1"), "::1")

    def test_missing_or_empty(self):
        self.assertEqual(parse_host(None), "")
        self.assertEqual(parse_host(""), "")
        self.assertEqual(parse_host(":8765"), "")


class HostPermittedTests(unittest.TestCase):
    def test_ip_literals_in_any_range(self):
        for name in ("127.0.0.1", "192.168.1.20", "100.64.3.4",
                     "8.8.8.8", "::1", "2001:db8::1"):
            self.assertTrue(host_permitted(name), name)

    def test_localhost_and_local_names(self):
        self.assertTrue(host_permitted("localhost"))
        self.assertTrue(host_permitted("room.local"))
        self.assertFalse(host_permitted("notlocal"))
        self.assertFalse(host_permitted("local"))

    def test_allowed_hosts_is_case_insensitive(self):
        self.assertTrue(host_permitted("room.example", ["Room.Example"]))
        self.assertFalse(host_permitted("other.example", ["room.example"]))
        self.assertFalse(host_permitted("room.example", []))
        self.assertFalse(host_permitted("room.example"))


if __name__ == "__main__":
    unittest.main()
