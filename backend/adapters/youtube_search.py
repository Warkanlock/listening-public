"""Find embeddable YouTube videos for the room's Radio stations."""

import os
import threading
from urllib.parse import urlencode

from adapters.service_http import read_json
from playback.radio import video_id_from
from metadata.external_metadata import search_cache_key

_LOCK = threading.Lock()


def available():
    return bool(os.environ.get("LISTENING_YOUTUBE_API_KEY", "").strip())


def _lookup(name, key, mode, token):
    args = {"part": "id", "type": "video", "q": query_for(name, mode),
            "videoEmbeddable": "true", "videoSyndicated": "true",
            "safeSearch": "strict", "maxResults": 12}
    if mode == "long":
        args["videoDuration"] = "long"
    if token:
        args["pageToken"] = token
    response = read_json(f"https://www.googleapis.com/youtube/v3/search?{urlencode(args)}", {"X-Goog-Api-Key": key})
    rows = response.get("items")
    if not isinstance(rows, list):
        raise ValueError("Unexpected YouTube search response.")
    videos = []
    for row in rows:
        identity = row.get("id") if isinstance(row, dict) else None
        video = video_id_from(identity.get("videoId")) if isinstance(identity, dict) else None
        if video and video not in videos:
            videos.append(video)
    token = response.get("nextPageToken")
    return tuple(videos), token if isinstance(token, str) and len(token) <= 200 else ""


MAX_PAGE = 2


def query_for(name, mode):
    return f"{name} continuous mix" if mode == "long" else f"{name} music mix"


def find(name, mode="mix", page=0, *, cached):
    if mode not in ("mix", "long") or type(page) is not int or not 0 <= page <= MAX_PAGE:
        raise ValueError("Choose a radio format and page.")
    key = os.environ.get("LISTENING_YOUTUBE_API_KEY", "").strip()
    if not key:
        raise ValueError("Open the station search on YouTube.")
    if not _LOCK.acquire(timeout=6):
        raise ValueError("The room is already searching YouTube. Try again shortly.")
    try:
        token = ""
        for number in range(page + 1):
            cache_key = search_cache_key("youtube", key, name, mode, token)
            videos, token = cached(cache_key, 6 * 3600, lambda cursor=token: _lookup(name, key, mode, cursor))
            if not token and number < page:
                return (), False
        return tuple(videos), bool(token) and page < MAX_PAGE
    finally:
        _LOCK.release()
