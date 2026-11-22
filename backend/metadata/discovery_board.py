"""Room listening signals and the public music desk for Deep Dive."""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import threading
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from adapters import public_http


FEED = "https://www.theguardian.com/music/rss"
_news_lock = threading.Lock()


def listening_activity(catalog, favorites, events):
    # Resolve durable source keys through the current build, including linked
    # copies of a recording. Never expose an obsolete positional track ID.
    likes = favorites.load().copy()
    history = events.history()
    liked, played = [], []
    seen = set()
    for record in catalog.records:
        identity = record.get("linked_recording") or record["key"]
        if identity in seen or not record.get("active", True):
            continue
        seen.add(identity)
        aliases = catalog.recording_keys(record)
        liked_at = max((likes.get(key, 0) for key in aliases), default=0)
        played_at = max((history.get(key, (0, 0))[0] or 0 for key in aliases), default=0)
        if liked_at or played_at:
            song = {"id": record["index"], "title": record["title"], "artist": record["artist"]}
            if liked_at:
                liked.append((liked_at, song))
            if played_at:
                played.append((played_at, song))

    def latest(rows):
        return [song for _, song in sorted(rows, key=lambda entry: (-entry[0], entry[1]["id"]))[:3]]

    return {"liked": latest(liked), "played": latest(played)}


def headlines(body):
    if b"<!DOCTYPE" in body.upper():
        raise ValueError("Unexpected feed format")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ValueError("Invalid music feed") from error
    if root.find("channel") is None:
        raise ValueError("Missing music feed channel")
    stories = {}
    for item in root.findall("channel/item"):
        url = (item.findtext("link") or "").strip()
        title = " ".join((item.findtext("title") or "").split())
        parsed = urlsplit(url)
        if not title or parsed.scheme != "https" or parsed.netloc != "www.theguardian.com":
            continue
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None:
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        stories[url] = {"title": title[:300], "url": url, "published": published.astimezone(timezone.utc).isoformat()}
    return sorted(stories.values(), key=lambda story: story["published"], reverse=True)[:50]


def weekly_press(metadata, now=None):
    def fetch():
        feed = public_http.fetch(FEED, accept="application/rss+xml, application/xml", limit=512 * 1024, timeout=6)
        return headlines(feed.body or b"")
    if not _news_lock.acquire(timeout=7):
        raise TimeoutError("Music desk busy")
    try:
        stories = metadata.cached("discovery-board:guardian-music:v1", 6 * 3600, fetch)
    finally:
        _news_lock.release()
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=7)).isoformat()
    return {"stories": [item for item in stories if cutoff <= item["published"] <= now.isoformat()]}
