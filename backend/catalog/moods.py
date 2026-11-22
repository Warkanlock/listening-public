"""Room-wide listening moods group canonical catalog genres without retagging tracks."""

from contextlib import closing
import json
import random

from catalog.genres import canonical_genre
from system import state_database

DEFAULTS = {
    "energetic": ("Energetic", (
        "drum and bass", "dubstep", "electronic", "hip-hop", "house", "metal",
        "punk", "rock", "techno", "trance")),
    "calm": ("Calm", (
        "ambient", "classical", "electronic", "folk", "instrumental", "jazz",
        "latin", "soundtrack")),
    "focused": ("Focused", (
        "ambient", "classical", "electronic", "indie", "instrumental", "jazz",
        "minimal", "soundtrack")),
    "upbeat": ("Upbeat", (
        "afrobeat", "electronic", "hip-hop", "house", "latin", "pop",
        "r&b", "reggae", "trance")),
    "reflective": ("Reflective", (
        "ambient", "blues", "country", "folk", "indie", "jazz", "r&b",
        "soundtrack")),
}
# Coordinates are percentages of the same energy/brightness plane on every client.
LANDMARKS = {
    "energetic": (29, 22, "Fast tempos, distortion, and heavy low end."),
    "upbeat": (76, 29, "Bright grooves with room to move."),
    "focused": (54, 51, "Steady rhythms and space between the notes."),
    "calm": (75, 78, "Soft textures and unhurried arrangements."),
    "reflective": (22, 70, "Spacious guitars, soft voices, and songs to get lost in."),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS mood_genres (
    mood TEXT PRIMARY KEY,
    genres TEXT NOT NULL
);
"""


def groups(database):
    with closing(state_database.connect(database, SCHEMA)) as db:
        overrides = dict(db.execute("SELECT mood, genres FROM mood_genres"))
    return [{"id": key, "name": name, "x": LANDMARKS[key][0], "y": LANDMARKS[key][1],
             "description": LANDMARKS[key][2],
             "genres": json.loads(overrides[key]) if key in overrides
             else sorted({canonical_genre(tag) for tag in default})}
            for key, (name, default) in DEFAULTS.items()]


def save(database, mood, genres):
    if not isinstance(mood, str) or mood not in DEFAULTS:
        raise ValueError("Choose an existing mood")
    if not isinstance(genres, list) or len(genres) > 100:
        raise ValueError("Choose up to 100 genres")
    canonical = set()
    for tag in genres:
        if not isinstance(tag, str) or len(tag) > 120 or not canonical_genre(tag):
            raise ValueError("Enter a genre name")
        canonical.add(canonical_genre(tag))
    with closing(state_database.connect(database, SCHEMA)) as db, db:
        db.execute("INSERT OR REPLACE INTO mood_genres(mood, genres) VALUES (?, ?)",
                   (mood, json.dumps(sorted(canonical))))


class EmptyMix(ValueError):
    """The room currently has no playable songs in this mix."""


def resolve(groups, mood, blend, genre):
    by_id = {group["id"]: group for group in groups}
    if not isinstance(mood, str) or mood not in by_id or not isinstance(blend, str):
        raise ValueError("Choose a mood.")
    if blend and (blend not in by_id or blend == mood):
        raise ValueError("Choose two different moods.")
    first, second = by_id[mood], by_id.get(blend)
    if not isinstance(genre, str) or len(genre) > 120:
        raise ValueError("Choose a genre from this mix.")
    tag = canonical_genre(genre)
    if genre and tag not in first["genres"] and (not second or tag not in second["genres"]):
        raise ValueError("Choose a genre from this mix.")
    return first, second, tag


def shuffled_mix(first, second, blend_percent):
    """Draw from both genre pools without replaying a shared recording."""
    first, second = list(first), list(second)
    random.shuffle(first)
    if not second:
        return first
    random.shuffle(second)
    seen = set()
    order = []
    while first or second:
        pool = second if not first or (second and random.randrange(100) < blend_percent) else first
        while pool:
            ref = pool.pop()
            if ref not in seen:
                seen.add(ref)
                order.append(ref)
                break
    return order
