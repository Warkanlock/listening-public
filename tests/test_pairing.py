import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import unittest

from system.tailscale import NO_NODE, TailnetNode
from system.pairing import (host_address, is_tailnet, pairing_authority,
                            source_address, tailscale_address)


class FakeSocket:
    """A UDP socket whose route answers come from a fixed mapping.

    The mapping keys are probe target hosts; a value is either the local
    source address to report or an exception to raise from connect().
    """

    def __init__(self, routes):
        self.routes = routes
        self.source = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, target):
        answer = self.routes[target[0]]
        if isinstance(answer, BaseException):
            raise answer
        self.source = answer

    def getsockname(self):
        return (self.source, 0)


def factory_for(routes):
    def factory(family, kind):
        return FakeSocket(routes)
    return factory


def forbidden_factory(family, kind):
    raise AssertionError("no socket should be created")


TAILNET_ROUTES = {"100.100.100.100": "100.64.0.10", "8.8.8.8": "192.0.2.10"}
NO_TAILNET_ROUTES = {"100.100.100.100": "192.0.2.10", "8.8.8.8": "192.0.2.10"}
BROKEN_TAILNET_ROUTES = {"100.100.100.100": OSError("no route"), "8.8.8.8": "192.0.2.10"}
ALL_BROKEN_ROUTES = {"100.100.100.100": OSError("no route"), "8.8.8.8": OSError("no route")}


class TailnetMembershipTests(unittest.TestCase):
    def test_range_boundaries(self):
        self.assertTrue(is_tailnet("100.64.0.1"))
        self.assertTrue(is_tailnet("100.127.255.254"))
        self.assertFalse(is_tailnet("100.63.255.255"))
        self.assertFalse(is_tailnet("100.128.0.1"))

    def test_non_tailnet_and_garbage(self):
        self.assertFalse(is_tailnet("192.0.2.10"))
        self.assertFalse(is_tailnet("127.0.0.1"))
        self.assertFalse(is_tailnet("not-an-address"))
        self.assertFalse(is_tailnet(""))


class HostAddressTests(unittest.TestCase):
    def test_ip_with_and_without_port(self):
        self.assertEqual(host_address("100.64.0.11:8765"), "100.64.0.11")
        self.assertEqual(host_address("100.64.0.11"), "100.64.0.11")

    def test_names_and_blanks_have_no_address(self):
        self.assertEqual(host_address("room.example.local:8765"), "")
        self.assertEqual(host_address(""), "")
        self.assertEqual(host_address(None), "")


class SourceAddressTests(unittest.TestCase):
    def test_reports_the_route_source(self):
        factory = factory_for(TAILNET_ROUTES)
        self.assertEqual(source_address("8.8.8.8", factory), "192.0.2.10")

    def test_failure_is_empty_not_an_exception(self):
        factory = factory_for(ALL_BROKEN_ROUTES)
        self.assertEqual(source_address("8.8.8.8", factory), "")


class TailscaleDetectionTests(unittest.TestCase):
    def test_tailnet_source_is_accepted(self):
        self.assertEqual(tailscale_address(factory_for(TAILNET_ROUTES)), "100.64.0.10")

    def test_non_tailnet_source_is_rejected(self):
        self.assertEqual(tailscale_address(factory_for(NO_TAILNET_ROUTES)), "")

    def test_detection_failure_is_empty(self):
        self.assertEqual(tailscale_address(factory_for(BROKEN_TAILNET_ROUTES)), "")


class PairingAuthorityTests(unittest.TestCase):
    def test_request_under_a_tailnet_host_wins(self):
        authority = pairing_authority("100.64.0.11:8765", 8765,
                                      socket_factory=forbidden_factory)
        self.assertEqual(authority, "100.64.0.11:8765")

    def test_tailnet_host_without_a_port_gains_the_server_port(self):
        authority = pairing_authority("100.64.0.11", 8765,
                                      socket_factory=forbidden_factory)
        self.assertEqual(authority, "100.64.0.11:8765")

    def test_detected_tailnet_beats_lan_without_a_request(self):
        authority = pairing_authority(None, 8765, socket_factory=factory_for(TAILNET_ROUTES))
        self.assertEqual(authority, "100.64.0.10:8765")

    def test_detected_tailnet_beats_a_lan_request_host(self):
        authority = pairing_authority("192.0.2.10:8765", 8765,
                                      socket_factory=factory_for(TAILNET_ROUTES))
        self.assertEqual(authority, "100.64.0.10:8765")

    def test_detected_tailnet_beats_a_loopback_request_host(self):
        authority = pairing_authority("127.0.0.1:8765", 8765,
                                      socket_factory=factory_for(TAILNET_ROUTES))
        self.assertEqual(authority, "100.64.0.10:8765")

    def test_lan_fallback_when_the_machine_has_no_tailnet(self):
        authority = pairing_authority(None, 8765, socket_factory=factory_for(NO_TAILNET_ROUTES))
        self.assertEqual(authority, "192.0.2.10:8765")

    def test_lan_fallback_when_detection_fails(self):
        authority = pairing_authority("room.example.local:8765", 8765,
                                      socket_factory=factory_for(BROKEN_TAILNET_ROUTES))
        self.assertEqual(authority, "192.0.2.10:8765")

    def test_no_address_anywhere_is_empty(self):
        authority = pairing_authority(None, 8765, socket_factory=factory_for(ALL_BROKEN_ROUTES))
        self.assertEqual(authority, "")


class MagicDNSTests(unittest.TestCase):
    def setUp(self):
        self.status = {"BackendState": "Running",
                       "Self": {"DNSName": "room.family.ts.net.", "TailscaleIPs": ["100.83.1.2"]},
                       "CurrentTailnet": {"MagicDNSEnabled": True, "MagicDNSSuffix": "family.ts.net"}}

    def test_identity_uses_only_this_nodes_name_in_the_current_tailnet(self):
        from system.tailscale import identity
        self.assertEqual(identity(self.status), TailnetNode("room.family.ts.net", ("100.83.1.2",)))
        self.status["Self"]["DNSName"] = "room.other.ts.net."
        self.assertEqual(identity(self.status), TailnetNode("", ("100.83.1.2",)))
        self.status["BackendState"] = "Stopped"
        self.assertEqual(identity(self.status), NO_NODE)

    def test_invitation_uses_advertised_name_only_for_an_assigned_address(self):
        from unittest.mock import patch
        from system.pairing import invitation_authority
        with patch("system.pairing.local_identity", return_value=TailnetNode("room.family.ts.net", ("100.83.1.2",))), patch(
            "system.pairing.can_listen_on", return_value=True
        ) as listen:
            self.assertEqual(invitation_authority(None, 8765), "room.family.ts.net:8765")
            self.assertEqual(invitation_authority(None, 1234, magicdns=False), "100.83.1.2:1234")
            self.assertEqual(invitation_authority(None, 1234, "192.0.2.8"), "192.0.2.8:1234")
            self.assertEqual(invitation_authority(None, 1234, "127.0.0.1"), "")
            listen.return_value = False
            with patch("system.pairing.pairing_authority", return_value="192.0.2.8:8765"):
                self.assertEqual(invitation_authority(None, 8765), "192.0.2.8:8765")

    def test_host_check_accepts_only_the_discovered_self_name(self):
        from unittest.mock import patch
        from system.hosts import host_permitted
        with patch("system.hosts.local_identity", return_value=TailnetNode("room.family.ts.net", ("100.83.1.2",))):
            self.assertTrue(host_permitted("room.family.ts.net"))
            self.assertFalse(host_permitted("other.family.ts.net"))
            self.assertFalse(host_permitted("room.another.ts.net"))


if __name__ == "__main__":
    unittest.main()
