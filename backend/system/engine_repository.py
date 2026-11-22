"""Resolve a GitHub release to a verified local engine catalog."""

import base64
import hashlib
import json
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from system.engine_fleet import LIMIT, verify_catalog
from system.private_files import private_write

REPOSITORY = re.compile(r"/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/?")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
ASSET_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class GitHubAssets(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc not in ASSET_HOSTS:
            raise ValueError("The engine repository redirected outside GitHub release storage.")
        return super().redirect_request(request, response, code, message, headers, url)


def release_assets(repository, tag):
    parsed = urlsplit(repository)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or not REPOSITORY.fullmatch(parsed.path)
            or parsed.query or parsed.fragment or parsed.path.rstrip("/").split("/")[-1] in {".", ".."}):
        raise ValueError("Enter an HTTPS GitHub repository URL.")
    if tag is not None and not TAG.fullmatch(tag):
        raise ValueError("Enter a GitHub release tag.")
    base = "https://github.com" + parsed.path.rstrip("/") + "/releases/"
    return base + (f"download/{tag}/" if tag else "latest/download/")


def download(opener, url):
    request = Request(url, headers={"User-Agent": "Listening Room plugin catalog"})
    with opener.open(request, timeout=10) as response:
        content = response.read(LIMIT + 1)
    if len(content) > LIMIT:
        raise ValueError("The engine repository asset is too large.")
    return content


def stage_repository(repository, tag, directory, state, public_key, architecture, version):
    base = release_assets(repository, tag)
    key = hashlib.sha256(public_key.read_bytes() + base.encode()).hexdigest()
    cache = state / f".repository-{key}.json"
    opener = build_opener(GitHubAssets())
    try:
        payload, signature = (download(opener, base + name) for name in ("catalog.json", "catalog.sig"))
        from_network = True
    except OSError as error:
        if (isinstance(error, HTTPError) and error.code < 500) or not cache.is_file():
            raise
        with cache.open() as source:
            saved = json.loads(source.read(3 * LIMIT))
        payload, signature = (base64.b64decode(saved[name], validate=True) for name in ("catalog", "signature"))
        from_network = False
    catalog = directory / "catalog.json"
    catalog.write_bytes(payload)
    catalog.with_suffix(".sig").write_bytes(signature)
    verify_catalog(catalog, public_key, architecture, version)
    if from_network:
        private_write(cache, json.dumps({"catalog": base64.b64encode(payload).decode(),
                                         "signature": base64.b64encode(signature).decode()}))
    return catalog
