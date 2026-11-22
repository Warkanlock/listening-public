"""Publish an explicitly selected playlist through an existing Mastodon account."""

import hashlib
import http.client
import json
import re
from urllib.parse import urlsplit

from library.playlist_sharing import playlist_xspf, track_count


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+(?:[a-z]{{2,63}}|xn--{_LABEL})", re.ASCII)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".arpa", ".lan")
_STATUS_LIMIT = 500
_RESPONSE_LIMIT = 64 * 1024


def https_address(value):
    message = "Use an HTTPS address with a public hostname and no credentials, query, or fragment."
    if not value.isascii() or any(not char.isprintable() or char.isspace() or char in "\\?#" for char in value):
        raise ValueError(message)
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        if (url.scheme != "https" or len(host) > 253 or not _DOMAIN.fullmatch(host)
                or host.endswith(_PRIVATE_SUFFIXES) or url.netloc.lower() not in {host, f"{host}:443"}):
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    return url


def instance_host(value):
    url = https_address(value)
    if url.path not in ("", "/"):
        raise ValueError("Use the Mastodon instance's HTTPS origin, without a path.")
    return url.hostname


def post_text(preview, link):
    https_address(link)
    if not preview["tracks"]:
        raise ValueError("Add a track before posting the playlist.")
    suffix = f"\n{track_count(preview)} · Playlist and XSPF\n{link}"
    available = _STATUS_LIMIT - len(suffix)
    if available < 2:
        raise ValueError("Use a shorter public playlist address.")
    title = " ".join(preview["name"].split()) or "Playlist"
    if len(title) > available:
        title = title[:available - 1].rstrip() + "…"
    return title + suffix


def publish(host, token, text, visibility, preview):
    if not re.fullmatch(r"[!-~]{1,1024}", token):
        raise ValueError("Enter a valid Mastodon access token.")
    body = json.dumps({"status": text, "visibility": visibility}, ensure_ascii=False).encode("utf-8")
    key = hashlib.sha256(body + b"\0" + playlist_xspf(preview)).hexdigest()
    connection = http.client.HTTPSConnection(host, timeout=15)
    try:
        connection.request("POST", "/api/v1/statuses", body, {
            "Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json",
            "Idempotency-Key": key,
        })
        with connection.getresponse() as response:
            if response.status not in (200, 201):
                raise RuntimeError(f"Mastodon refused the post (HTTP {response.status}). Check the instance and token scope.")
            data = response.read(_RESPONSE_LIMIT + 1)
            if len(data) > _RESPONSE_LIMIT:
                return None
            try:
                result = json.loads(data)
                url = result.get("url") if isinstance(result, dict) else None
                if isinstance(url, str) and len(url) < 2048:
                    return https_address(url).geturl()
            except ValueError:
                pass
            return None
    except (OSError, http.client.HTTPException):
        raise RuntimeError("Could not confirm publication. Check the Mastodon account before retrying.") from None
    finally:
        connection.close()
