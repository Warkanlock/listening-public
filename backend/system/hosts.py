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


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_IPV6_AUTHORITY = re.compile(r"\[([^\[\]]+)\](?::([0-9]{1,5}))?")


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
    except ValueError:
        raise error from None
    host = parse_host(url.netloc)
    if url.scheme not in {"http", "https"} or not host or url.path not in {"", "/"}:
        raise error
    authority = f"[{host}]" if ":" in host else host
    if port is not None and port != {"http": 80, "https": 443}[url.scheme]:
        authority += f":{port}"
    return f"{url.scheme}://{authority}"


def parse_host(value):
    """Return the bare host from an authority or legacy unbracketed IPv6 literal."""
    candidate = (value or "").strip(" \t").lower()
    if not candidate.isascii() or "%" in candidate:
        return ""
    if candidate.startswith("["):
        match = _IPV6_AUTHORITY.fullmatch(candidate)
        if not match or (match[2] and not 0 < int(match[2]) <= 65535):
            return ""
        try:
            return str(ipaddress.IPv6Address(match[1]))
        except ValueError:
            return ""
    if candidate.count(":") > 1:
        try:
            return str(ipaddress.IPv6Address(candidate))
        except ValueError:
            return ""
    name, separator, port = candidate.partition(":")
    if separator and (not re.fullmatch(r"[0-9]{1,5}", port) or not 0 < int(port) <= 65535):
        return ""
    if len(name) <= 253 and all(_DNS_LABEL.fullmatch(label) for label in name.removesuffix(".").split(".")):
        return name
    return ""


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
