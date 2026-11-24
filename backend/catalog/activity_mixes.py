"""Local tempo analysis and queues for listening by activity."""

from __future__ import annotations

from array import array
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from math import isfinite, log1p, sqrt
from pathlib import Path
import json
import os
import random
import subprocess
import sys
import wave

from catalog.audio_resolution import resolve_stored
from catalog.audio_store import blob_key, is_blob_ref, source_stamp
from system import state_database

# Overlap lets steady songs suit both a slower and a more active mix.
PRESETS = {
    "walking": ("Walking", 95, 130),
    "studying": ("Studying", 65, 95),
    "meditation": ("Meditation", 45, 70),
}
MIX_LIMIT = 40
# Bump the version when tag rules or the estimator change.
VERSION = 1
SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_tempos (
    identity TEXT NOT NULL,
    version INTEGER NOT NULL,
    bpm REAL,
    PRIMARY KEY (identity, version)
);
"""

SAMPLE_RATE = 4000
FRAMES_PER_SECOND = 100
FRAME_SIZE = SAMPLE_RATE // FRAMES_PER_SECOND
READ_SECONDS = 32
CONTAINERS = "aac,flac,mov,mp3"


@dataclass(frozen=True)
class Recording:
    index: int
    identity: str
    path: Path | None
    blob: str | None
    duration: float
    stamp: list[int] | None = None


def recordings(records, coverage, library, *, has_media):
    """Use verified active recordings and the same file boundary as playback."""
    for index, record in enumerate(records, 1):
        if not record.get("active", True):
            continue
        row = coverage.get(index, {})
        reference = row.get("path", "")
        if not reference or row.get("status") != "verified":
            continue
        duration = record.get("length") or 0
        if is_blob_ref(reference):
            if has_media:
                yield Recording(index, reference, None, blob_key(reference), duration)
            continue
        path = resolve_stored(reference, library)
        if path is not None:
            try:
                stamp = source_stamp(path.stat())
            except OSError:
                continue
            yield Recording(index, "file:" + ":".join(map(str, stamp)), path, None, duration, stamp)


def read_tempos(database):
    with closing(state_database.connect(database, SCHEMA)) as db:
        return dict(db.execute("SELECT identity, bpm FROM activity_tempos WHERE version=?", (VERSION,)))


def save_tempo(database, identity, bpm):
    with closing(state_database.connect(database, SCHEMA)) as db, db:
        db.execute("INSERT OR REPLACE INTO activity_tempos VALUES (?, ?, ?)", (identity, VERSION, bpm))


def unique_recordings(items):
    unique = {}
    for item in items:
        unique.setdefault(item.identity, item)
    return unique.values()


class MixIndex:
    """A prepared view of the stored tempos, published as one snapshot."""

    def __init__(self, items, tempos):
        self.total = len(items)
        self.indexed = sum(item.identity in tempos for item in items)
        classified = [(item.index, bpm) for item in items if (bpm := tempos.get(item.identity)) is not None]
        self.matches = {key: tuple(index for index, bpm in classified if low <= bpm <= high)
                        for key, (_, low, high) in PRESETS.items()}

    def choices(self):
        return [{"id": key, "name": name, "min_bpm": low, "max_bpm": high, "count": len(self.matches[key])}
                for key, (name, low, high) in PRESETS.items()]

    def queue(self, activity):
        if not isinstance(activity, str) or activity not in PRESETS:
            raise ValueError("Choose a listening activity.")
        matches = self.matches[activity]
        if not matches:
            raise ValueError(f"No songs in the {PRESETS[activity][0].lower()} tempo range yet.")
        return random.sample(matches, min(len(matches), MIX_LIMIT))


def _tagged_bpm(metadata):
    for group in [metadata.get("format", {}), *metadata.get("streams", [])]:
        for key, raw in group.get("tags", {}).items():
            if key.lower() not in {"bpm", "tbpm", "tempo"}:
                continue
            try:
                bpm = float(str(raw).lower().removesuffix("bpm").strip())
            except ValueError:
                continue
            if isfinite(bpm) and 30 <= bpm <= 300:
                return round(bpm, 1)
    return None


def estimate_bpm(pcm):
    """Find a periodic rise in short-window energy, or leave the track unclassified."""
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) < 10 * SAMPLE_RATE:
        return None
    frame_rms = [log1p(sqrt(sum(value * value for value in samples[start:start + FRAME_SIZE]) / FRAME_SIZE))
                 for start in range(0, len(samples) - FRAME_SIZE + 1, FRAME_SIZE)]
    # Three-frame smoothing tolerates minor performance timing variation.
    rises = [max(0.0, right - left) for left, right in zip(frame_rms, frame_rms[1:])]
    strength = [sum(rises[i:i + 3]) for i in range(len(rises) - 2)]
    mean = sum(strength) / len(strength)
    centered = [value - mean for value in strength]
    rms = sqrt(sum(value * value for value in centered) / len(centered))
    if rms < 0.025 or sum(value > mean + 2 * rms and value >= strength[i - 1] and value > strength[i + 1]
                         for i, value in enumerate(strength[1:-1], 1)) < 8:
        return None
    energy = sum(value * value for value in centered)
    low_lag = round(60 * FRAMES_PER_SECOND / 200)
    high_lag = round(60 * FRAMES_PER_SECOND / 45)
    correlation = {
        lag: sum(left * right for left, right in zip(centered, centered[lag:]))
        / (energy * (1 - lag / len(centered)))
        for lag in range(low_lag - 1, high_lag + 2)
    }
    peaks = [lag for lag in range(low_lag, high_lag + 1)
             if correlation[lag] >= correlation[lag - 1] and correlation[lag] > correlation[lag + 1]]
    if not peaks:
        return None
    best = max(peaks, key=correlation.__getitem__)
    if correlation[best] < 0.28:
        return None
    # Prefer the fundamental to an equally strong peak at twice/three times its period.
    lag = min([best, *(candidate for candidate in peaks if candidate < best
                       and abs(best / candidate - round(best / candidate)) < 0.06
                       and correlation[candidate] >= 0.88 * correlation[best])])
    before, peak, after = (correlation[lag + offset] for offset in (-1, 0, 1))
    curve = before - 2 * peak + after
    correction = 0.5 * (before - after) / curve if curve < 0 else 0
    return round(60 * FRAMES_PER_SECOND / (lag + correction), 1)


def analyze(handle, duration):
    descriptor = handle.fileno()
    source = ["-protocol_whitelist", "file", "-format_whitelist", CONTAINERS, "-i", f"/dev/fd/{descriptor}"]
    tags = "bpm,tbpm,tempo"
    probe = subprocess.run(["ffprobe", "-v", "error", *source, "-show_entries",
                            f"format_tags={tags}:stream_tags={tags}", "-of", "json"],
                           capture_output=True, check=True, timeout=8, pass_fds=(descriptor,))
    bpm = _tagged_bpm(json.loads(probe.stdout))
    if bpm is not None:
        return bpm
    handle.seek(0)
    start = ["-ss", "12"] if duration >= READ_SECONDS + 12 else []
    decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
                              *start, *source, "-t", str(READ_SECONDS), "-map", "0:a:0", "-ac", "1",
                              "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
                             capture_output=True, check=True, timeout=12, pass_fds=(descriptor,))
    with wave.open(BytesIO(decoded.stdout), "rb") as samples:
        return estimate_bpm(samples.readframes(SAMPLE_RATE * READ_SECONDS))


def recording_tempo(item, media):
    if item.blob is not None:
        with media.open_original(item.blob) as (handle, _):
            return analyze(handle, item.duration)
    with item.path.open("rb") as handle:
        if source_stamp(os.fstat(handle.fileno())) != item.stamp:
            raise OSError("Recording changed before tempo analysis.")
        bpm = analyze(handle, item.duration)
        if source_stamp(os.fstat(handle.fileno())) != item.stamp:
            raise OSError("Recording changed during tempo analysis.")
        return bpm
