"""Which Host headers the server answers to (plan 021b).

The Host header, not the source address, is the DNS-rebinding
signal: a rebinding page runs in a browser on a trusted machine and
arrives from a trusted address, but under an attacker-chosen
hostname. The server therefore only serves the hosts a deployment
means to use:

- IP literals, v4 or v6, in any range: loopback, LAN addresses, and
  Tailscale 100.64.0.0/10 addresses are all literals, and the
  pairing links plan 024 builds are made of them.
- `localhost`.
- Names ending in `.local` (mDNS household names).
- Names the owner lists in the `allowed_hosts` setting, which is
  where the public name of a reverse-proxy deployment goes (see
  "HTTPS through a reverse proxy" in the README).

Forwarding headers (`X-Forwarded-Host` and friends) are never
consulted: the proxy passes the public Host through as the
request's Host, and that name is what `allowed_hosts` lists.
"""

import ipaddress
import os
import re
from urllib.parse import urlsplit

from system.tailscale import local_identity


def configured_origin(value=None):
    """An operator-supplied browser origin, independent of incoming headers."""
    if value is None:
        value = os.environ.get("LISTENING_ROOM_ORIGIN", "")
    if not value:
        return ""
    error = ValueError("LISTENING_ROOM_ORIGIN must be a bare http:// or https:// origin.")
    if any(char.isspace() or not char.isascii() or not char.isprintable()
           or char in "\\%?#" for char in value):
        raise error
    try:
        url = urlsplit(value)
        port = url.port
        host = url.hostname
    except ValueError:
        raise error from None
    if (url.scheme not in {"http", "https"} or not host or url.username is not None
            or url.netloc.endswith(":") or port == 0 or url.path not in {"", "/"}):
        raise error
    if is_ip_literal(host):
        ip = ipaddress.ip_address(host)
        authority = f"[{ip}]" if ip.version == 6 else str(ip)
    elif len(host) <= 253 and all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.rstrip(".").split(".")
    ):
        authority = host
    else:
        raise error
    if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
        authority += f":{port}"
    return f"{url.scheme}://{authority}"


def parse_host(value):
    """The bare host in a Host header value: port stripped, IPv6
    brackets stripped, case folded. "" when the header is missing
    or carries no host."""
    candidate = (value or "").strip().casefold()
    if not candidate:
        return ""
    if candidate.startswith("["):
        return candidate[1:].split("]", 1)[0]
    if candidate.count(":") == 1:
        return candidate.split(":", 1)[0]
    if ":" in candidate:
        # An unbracketed IPv6 literal has no port to strip; anything
        # else with several colons is not a host this app serves.
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return ""
    return candidate


def is_ip_literal(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def host_permitted(name, allowed_hosts=()):
    """Whether a parsed (non-empty) host may be served."""
    if is_ip_literal(name):
        return True
    if name == "localhost" or name.endswith(".local"):
        return True
    if name.endswith(".ts.net") and name == local_identity().name:
        return True
    return name in {str(entry).strip().casefold()
                    for entry in allowed_hosts or ()}
