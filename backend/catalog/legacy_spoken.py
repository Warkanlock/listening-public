"""Preserve retired audiobook and podcast tables in portable collection snapshots."""

BOOK_TABLES = ("audiobooks", "audiobook_chapters")
PODCAST_TABLES = ("podcast_feeds", "podcast_episodes")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audiobooks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL,
    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0)
);
CREATE TABLE IF NOT EXISTS audiobook_chapters (
    book_id TEXT NOT NULL REFERENCES audiobooks(id) ON DELETE CASCADE,
    number INTEGER NOT NULL CHECK(number > 0),
    title TEXT NOT NULL,
    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
    audio_hash TEXT NOT NULL REFERENCES audio(hash),
    PRIMARY KEY(book_id, number)
);

CREATE TABLE IF NOT EXISTS podcast_feeds (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    effective_url TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    etag TEXT NOT NULL,
    last_modified TEXT NOT NULL,
    refreshed_at INTEGER NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS podcast_episodes (
    id TEXT PRIMARY KEY,
    feed_id TEXT NOT NULL REFERENCES podcast_feeds(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    enclosure_url TEXT NOT NULL,
    duration_seconds INTEGER,
    published_at INTEGER,
    ordinal INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS podcast_episode_feed ON podcast_episodes(feed_id, published_at, ordinal);
"""
