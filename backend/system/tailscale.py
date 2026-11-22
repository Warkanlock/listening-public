"""Read this server's MagicDNS identity from its local Tailscale daemon."""
import ipaddress
import json
import re
import subprocess
import threading
import time
from typing import NamedTuple


class TailnetNode(NamedTuple):
    name: str
    addresses: tuple

    def matches(self, host):
        if self.name and self.name == host:
            return True
        try:
            return str(ipaddress.ip_address(host)) in self.addresses
        except ValueError:
            return False


NO_NODE = TailnetNode("", ())
IPV4 = ipaddress.ip_network("100.64.0.0/10")
IPV6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

_lock = threading.Lock()
_cached = NO_NODE
_expires = 0.0
_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"


def identity(status):
    """Return the running node’s addresses and its exact MagicDNS name when enabled."""
    if not isinstance(status, dict) or status.get("BackendState") != "Running":
        return NO_NODE
    tailnet = status.get("CurrentTailnet") or {}
    node = status.get("Self") or {}
    if not isinstance(tailnet, dict) or not isinstance(node, dict):
        return NO_NODE
    suffix = str(tailnet.get("MagicDNSSuffix", "")).lower().rstrip(".")
    name = str(node.get("DNSName", "")).lower().rstrip(".")
    machine, _, domain = name.partition(".")
    if (not tailnet.get("MagicDNSEnabled") or not re.fullmatch(_label, machine) or domain != suffix
            or not re.fullmatch(_label + r"\.ts\.net", suffix)):
        name = ""
    addresses = node.get("TailscaleIPs")
    if not isinstance(addresses, list):
        return NO_NODE
    ips = []
    for item in addresses:
        try:
            address = ipaddress.ip_address(item)
        except ValueError:
            continue
        if address in IPV4 or address in IPV6:
            ips.append(str(address))
    return TailnetNode(name, tuple(ips)) if ips else NO_NODE


def local_identity():
    global _cached, _expires
    with _lock:
        if time.monotonic() >= _expires:
            try:
                result = subprocess.run(["tailscale", "status", "--json", "--peers=false"],
                                        capture_output=True, timeout=1.5, check=True)
                _cached = identity(json.loads(result.stdout))
            except (OSError, ValueError, subprocess.SubprocessError):
                _cached = NO_NODE
            _expires = time.monotonic() + 30
        return _cached
