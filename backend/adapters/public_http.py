"""Bounded direct HTTP for public catalogs and publisher enclosures."""

from __future__ import annotations

from contextlib import closing
from http.client import HTTPConnection, HTTPException
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
from typing import NamedTuple
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit


MAX_URL = 4096
IDLE_TIMEOUT = 5
MAX_REDIRECTS = 4
_DNS_SLOTS = threading.BoundedSemaphore(4)
_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_IPV4_SUFFIX = re.compile(r"(?:[0-9]+|0x[0-9a-f]*)", re.ASCII)
_TRANSITION = tuple(ipaddress.ip_network(cidr) for cidr in
                    ("64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16"))
_SPECIAL_IPV4 = ipaddress.ip_network("192.0.0.0/24")
_USUAL_PORT = {"http": 80, "https": 443}
_PRIVATE_QUERY_FIELDS = {"token", "access_token", "auth", "authorization", "password",
                         "secret", "key", "api_key", "apikey", "signature", "credential", "session"}


class FetchResult(NamedTuple):
    url: str
    body: bytes | None
    etag: str = ""
    last_modified: str = ""


class _Response(NamedTuple):
    status: int
    body: bytes | None
    location: str
    etag: str
    last_modified: str


def _public_address(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        if "%" in value or address.is_site_local or any(address in network for network in _TRANSITION):
            raise ValueError("Sources need a public internet address")
        address = address.ipv4_mapped or address
    if (address in _SPECIAL_IPV4 or not address.is_global or address.is_multicast or address.is_reserved or address.is_private
            or address.is_loopback or address.is_link_local or address.is_unspecified):
        raise ValueError("Sources need a public internet address")
    return address


def normalize_url(value):
    """Canonicalize an HTTP(S) address and refuse local or credentialed literals."""
    if not value or len(value) > MAX_URL or any(char.isspace() or not char.isprintable() or char == "\\" for char in value):
        raise ValueError("Enter a public HTTP or HTTPS source URL")
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme not in _USUAL_PORT or not host or url.username is not None or url.fragment
                or url.port not in {None, _USUAL_PORT[url.scheme]}):
            raise ValueError
        if any(field.casefold() in _PRIVATE_QUERY_FIELDS or field.casefold().startswith(("x-amz-", "x-goog-"))
               for field, _ in parse_qsl(url.query, keep_blank_values=True, max_num_fields=64)):
            raise ValueError
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            host = host.rstrip(".").encode("idna").decode("ascii").lower()
            labels = host.split(".")
            if (len(host) > 253 or len(labels) < 2 or _IPV4_SUFFIX.fullmatch(labels[-1])
                    or not all(_HOST.fullmatch(label) for label in labels)
                    or host.endswith((".localhost", ".local", ".internal", ".home.arpa"))):
                raise ValueError
        else:
            _public_address(host)
            host = f"[{literal}]" if literal.version == 6 else str(literal)
    except ValueError as error:
        raise ValueError("Enter a public HTTP or HTTPS source URL on its standard port") from error
    path = quote(url.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(url.query, safe="?/%:@!$&'()*+,;=-._~[]")
    result = urlunsplit((url.scheme, host, path, query, ""))
    if len(result) > MAX_URL:
        raise ValueError("Source URL is too long")
    return result


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Source took too long to respond")
    return remaining


def _public_addresses(host, port, deadline):
    """Check every resolved address before connecting to any of them."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return (_public_address(host),)
    if not _DNS_SLOTS.acquire(timeout=_remaining(deadline)):
        raise TimeoutError("Source DNS lookup timed out")
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as error:
            result.put(error)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, daemon=True).start()
    try:
        resolved = result.get(timeout=_remaining(deadline))
    except queue.Empty as error:
        raise TimeoutError("Source DNS lookup timed out") from error
    if isinstance(resolved, OSError) or not resolved or len(resolved) > 32:
        raise ValueError("Could not resolve a public source address")
    return tuple(dict.fromkeys(_public_address(item[4][0]) for item in resolved))


def _connect_public(host, port, use_tls, addresses, deadline):
    for address in addresses:
        transport = socket.socket(socket.AF_INET if address.version == 4 else socket.AF_INET6, socket.SOCK_STREAM)
        try:
            transport.settimeout(min(IDLE_TIMEOUT, _remaining(deadline)))
            transport.connect((str(address), port))
            if use_tls:
                transport.settimeout(min(IDLE_TIMEOUT, _remaining(deadline)))
                transport = ssl.create_default_context().wrap_socket(transport, server_hostname=host)
            return transport
        except OSError:
            transport.close()
    raise ValueError("Could not connect to the public source")


def _stop_reading(transport):
    try:
        transport.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _safe_validator(value):
    return value if value and len(value) < 1024 and value.isascii() and value.isprintable() else ""


def _request_once(url, addresses, headers, deadline, *, limit, output=None):
    source = urlsplit(url)
    port = _USUAL_PORT[source.scheme]
    transport = _connect_public(source.hostname, port, source.scheme == "https", addresses, deadline)
    connection = HTTPConnection(source.hostname, port)
    connection.sock = transport
    watchdog = None
    try:
        watchdog = threading.Timer(_remaining(deadline), _stop_reading, args=(transport,))
        watchdog.daemon = True
        watchdog.start()
        target = source.path + ("?" + source.query if source.query else "")
        connection.request("GET", target, headers={**headers, "Host": source.netloc})
        with closing(connection.getresponse()) as response:
            location = response.getheader("Location", "")
            etag = _safe_validator(response.getheader("ETag"))
            last_modified = _safe_validator(response.getheader("Last-Modified"))
            if response.status != 200:
                return _Response(response.status, None, location, etag, last_modified)
            if response.getheader("Content-Encoding", "identity").casefold() != "identity":
                raise ValueError("Source host used an unsupported response encoding")
            declared_size = response.length
            if declared_size is not None and declared_size > limit:
                raise ValueError("Source response is too large")
            if output is None:
                body = response.read(limit + 1)
                received = len(body)
            else:
                body = None
                received = 0
                while chunk := response.read(128 * 1024):
                    received += len(chunk)
                    if received > limit:
                        raise ValueError("Source response is too large")
                    output.write(chunk)
            if received > limit:
                raise ValueError("Source response is too large")
            if declared_size is not None and received != declared_size:
                raise ValueError("The source host sent an incomplete response")
            _remaining(deadline)
            return _Response(200, body, location, etag, last_modified)
    except (OSError, HTTPException) as error:
        raise ValueError("Source response failed or timed out") from error
    finally:
        if watchdog is not None:
            watchdog.cancel()
        connection.close()
        transport.close()


def fetch(url, *, accept, limit, timeout, cached_url="", etag="", last_modified="", output=None):
    """Fetch public content without credentials, a proxy, or a DNS re-resolution."""
    url = normalize_url(url)
    deadline = time.monotonic() + timeout
    for redirect in range(MAX_REDIRECTS + 1):
        parsed = urlsplit(url)
        try:
            addresses = _public_addresses(parsed.hostname, _USUAL_PORT[parsed.scheme], deadline)
        except TimeoutError as error:
            raise ValueError("Source DNS lookup timed out") from error
        headers = {"Accept": accept, "Accept-Encoding": "identity",
                   "Connection": "close", "User-Agent": "ListeningRoom/1.2"}
        if url == cached_url:
            if validator := _safe_validator(etag):
                headers["If-None-Match"] = validator
            if validator := _safe_validator(last_modified):
                headers["If-Modified-Since"] = validator
        response = _request_once(url, addresses, headers, deadline, limit=limit, output=output)
        if response.status == 200 or (response.status == 304 and url == cached_url):
            return FetchResult(url, response.body, response.etag, response.last_modified)
        if response.status not in {301, 302, 303, 307, 308} or not response.location:
            raise ValueError(f"Source host returned HTTP {response.status}")
        if redirect == MAX_REDIRECTS:
            raise ValueError("Source redirected too many times")
        target = normalize_url(urljoin(url, response.location))
        if parsed.scheme == "https" and urlsplit(target).scheme != "https":
            raise ValueError("Source redirected from HTTPS to HTTP")
        url = target
