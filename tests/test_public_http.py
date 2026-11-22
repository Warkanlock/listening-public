import ipaddress
from io import BytesIO
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from adapters import public_http
from fixture_environment import isolate_room_environment as setUpModule

_FEED = "https://catalog.example/feed.xml"
_CDN = "https://cdn.example/catalog.json"


class PublicTransportTests(unittest.TestCase):
    def test_rejects_local_literal_credentials_and_non_http(self):
        for url in ("http://127.0.0.1/a", "http://127.0.0.1./a", "http://0x7f.0.0.1/a", "http://0177.0.0.1/a",
                    "http://[::ffff:127.0.0.1]/a", "http://localhost/a",
                    "http://home.local/a", "https://name:secret@example.org/rss", "ftp://example.org/rss",
                    "https://public.example/rss?access_token=private", "https://public.example/rss?X-Amz-Signature=private",
                    "http://100.64.0.1/a", "http://224.0.0.1/a", "http://8.8.8.8:8765/a",
                    "http://[64:ff9b::7f00:1]/a", "http://[2002:7f00:1::]/a", "http://[fec0::1]/a", "http://192.0.0.9/a"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_http.normalize_url(url)

    def test_redirect_is_revalidated_and_mixed_dns_answers_never_connect(self):
        dns = [[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))],
               [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443))]]
        with patch.object(public_http.socket, "getaddrinfo", side_effect=dns) as resolve, \
                patch.object(public_http, "_request_once", return_value=public_http._Response(302, None, "/second", "", "")) as req:
            with self.assertRaisesRegex(ValueError, "public internet address"):
                public_http.fetch(_FEED, accept="application/json", limit=8192, timeout=2)
        self.assertEqual(resolve.call_count, 2)
        req.assert_called_once()
        self.assertEqual(req.call_args.args[1], (ipaddress.ip_address("8.8.8.8"),))

    def test_conditional_headers_only_go_to_cached_effective_address(self):
        responses = [public_http._Response(302, None, _CDN, "", ""), public_http._Response(304, None, "", '"tag"', "")]
        with patch.object(public_http, "_public_addresses", return_value=(ipaddress.ip_address("8.8.8.8"),)), \
                patch.object(public_http, "_request_once", side_effect=responses) as requests:
            result = public_http.fetch(_FEED, accept="application/json", limit=8192, timeout=2, cached_url=_CDN, etag='"tag"')
        first, redirected = [call.args for call in requests.call_args_list]
        self.assertNotIn("If-None-Match", first[2])
        self.assertEqual(redirected[2]["If-None-Match"], '"tag"')
        self.assertEqual(result, public_http.FetchResult(_CDN, None, '"tag"'))

    def test_audio_redirect_cannot_reach_a_private_address_or_forward_session_headers(self):
        with patch.object(public_http, "_public_addresses", return_value=(ipaddress.ip_address("8.8.8.8"),)), \
                patch.object(public_http, "_request_once", return_value=public_http._Response(
                    302, None, "https://10.0.0.1/hidden", "", "")) as request:
            with self.assertRaises(ValueError):
                public_http.fetch(_FEED, accept="audio/*", limit=8192, timeout=2, output=BytesIO())
        request.assert_called_once()
        headers = request.call_args.args[2]
        self.assertIn("audio/*", headers["Accept"])
        self.assertFalse({"Cookie", "Authorization", "Referer", "Origin"} & set(headers))

    def test_audio_growing_past_limit_is_rejected_before_writing_the_excess(self):
        remote, client = socket.socketpair()
        with remote, client:
            remote.sendall(b"HTTP/1.0 200 OK\r\n\r\n" + b"+" * 32)
            remote.shutdown(socket.SHUT_WR)
            output = BytesIO()
            with patch.object(public_http, "_connect_public", return_value=client):
                with self.assertRaisesRegex(ValueError, "too large"):
                    public_http._request_once(_FEED, (), {}, time.monotonic() + 2, limit=16, output=output)
            self.assertEqual(output.getvalue(), b"")

    def test_transport_dials_numeric_address_and_verifies_original_tls_name(self):
        transport = Mock()
        tls = Mock()
        address = ipaddress.ip_address("8.8.8.8")
        with patch.object(public_http.socket, "socket", return_value=transport), \
                patch.object(public_http.ssl, "create_default_context", return_value=tls):
            connected = public_http._connect_public("catalog.example", 443, True, (address,), time.monotonic() + 2)
        self.assertIs(connected, tls.wrap_socket.return_value)
        transport.connect.assert_called_once_with(("8.8.8.8", 443))
        tls.wrap_socket.assert_called_once_with(transport, server_hostname="catalog.example")

    def test_response_body_limit_is_enforced_over_real_http_framing(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        request = bytearray()

        def reply():
            with server:
                server.settimeout(2)
                while b"\r\n\r\n" not in request:
                    request.extend(server.recv(4096))
                server.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"+" * 32)

        thread = threading.Thread(target=reply, daemon=True)
        thread.start()
        client.settimeout(2)
        with patch.object(public_http, "_connect_public", return_value=client):
            with self.assertRaisesRegex(ValueError, "too large"):
                public_http._request_once(_FEED, (), {}, time.monotonic() + 2, limit=16)
        thread.join(timeout=2)
        self.assertIn(b"GET /feed.xml HTTP/1.1", request)
        self.assertIn(b"Host: catalog.example", request)
