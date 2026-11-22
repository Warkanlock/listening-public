"""JSON requests to endpoints selected by a room operator."""

from contextlib import closing
from http.client import HTTPConnection, HTTPSConnection, HTTPException
import json
from urllib.parse import urlsplit, urlunsplit


def base_url(value):
    if not value or any(char.isspace() or not char.isprintable() or char == "\\" for char in value):
        raise ValueError("Enter an HTTP or HTTPS service address.")
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or not url.hostname
            or url.username is not None or url.query or url.fragment or url.port == 0):
        raise ValueError("Enter an HTTP or HTTPS service address without credentials.")
    return urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))


def read_json(url, headers):
    target = urlsplit(url)
    connect = HTTPSConnection if target.scheme == "https" else HTTPConnection
    with closing(connect(target.hostname, target.port, timeout=6)) as connection:
        try:
            path = (target.path or "/") + ("?" + target.query if target.query else "")
            connection.request("GET", path, headers={"Accept": "application/json", **headers})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError("The service did not return a catalog response.")
            body = response.read(1024 ** 2 + 1)
        except HTTPException as error:
            raise ValueError("The service did not return a catalog response.") from error
    if len(body) > 1024 ** 2:
        raise ValueError("The catalog response is too large.")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Unexpected service response.")
    return data
