"""Choosing the address the pairing link points at.

The pairing link has to work as copied, on whatever route the new
device will use to reach this host. The choice, in order:

1. The current request's own host, when it is a Tailscale address
   (100.64.0.0/10): the owner's browser already reached the host that
   way, so a device pairing from the same route can too.
2. The host machine's own Tailscale address, when it has one. It is
   detected by asking the OS which local source address it would use
   to reach Tailscale's DNS address (100.100.100.100): a UDP connect
   sends no traffic, it only makes the kernel pick a route. The answer
   counts only when it really is a Tailscale address.
3. The LAN source address, the original behavior.

The request host is only read here, never validated or rejected; host
validation is a separate concern (plan 021). Any detection failure
falls through silently to the next rule.
"""

import ipaddress
import os
import socket

from system.tailscale import IPV4, IPV6, local_identity

TAILNET_DNS = "100.100.100.100"
LAN_PROBE = "8.8.8.8"


def source_address(target, socket_factory=socket.socket):
    """Local source address the OS would use to reach `target`, or ""."""
    try:
        with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.connect((target, 80))
            return connection.getsockname()[0]
    except OSError:
        return ""


def is_tailnet(address):
    try:
        return ipaddress.ip_address(address) in IPV4
    except ValueError:
        return False


def host_address(host):
    """The bare IP in a Host header value, or "" when it is not an IP."""
    candidate = (host or "").strip()
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    else:
        candidate = candidate.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def tailscale_address(socket_factory=socket.socket):
    """This machine's Tailscale address, or "" when it has none."""
    address = source_address(TAILNET_DNS, socket_factory)
    return address if is_tailnet(address) else ""


def pairing_authority(request_host=None, port=None, socket_factory=socket.socket):
    """The host (with port) the pairing link should point at, or ""."""
    request_host = (request_host or "").strip()
    if is_tailnet(host_address(request_host)):
        if request_host == host_address(request_host):
            return f"{request_host}:{port}"
        return request_host
    address = (tailscale_address(socket_factory)
               or source_address(LAN_PROBE, socket_factory))
    return f"{address}:{port}" if address else ""


def can_listen_on(address):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


def home_server():
    """The explicit Debian Compose deployment, independent of container DNS."""
    return os.environ.get("LISTENING_ROOM_PROFILE") == "home-server"


def invitation_authority(request_host, port, bind_host="0.0.0.0", *, magicdns=True):
    """Advertise an address served by this listener. A bridged container
    cannot validate its host's MagicDNS Host header, so its host CLI uses IP."""
    if home_server():
        return "" if ipaddress.ip_address(bind_host).is_loopback else f"server:{port}"
    node = local_identity()
    if bind_host == "0.0.0.0":
        # Userspace-only daemons advertise an IP that a normal HTTP listener
        # cannot serve. The bind probe stays local and sends no packets.
        if not is_tailnet(host_address(request_host)):
            for ip in node.addresses:
                if ":" not in ip and can_listen_on(ip):
                    return f"{node.name if magicdns and node.name else ip}:{port}"
        authority = pairing_authority(request_host, port)
    else:
        address = ipaddress.ip_address(bind_host)
        if address.is_loopback or address.is_unspecified:
            return ""
        authority = f"[{bind_host}]:{port}" if address.version == 6 else f"{bind_host}:{port}"
    ip = host_address(authority)
    if magicdns and node.name and node.matches(ip):
        return f"{node.name}:{authority.rsplit(':', 1)[1]}"
    return authority


def is_tailnet_host(host):
    if host.endswith(".ts.net"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address in IPV4 or address in IPV6
