"""Local tempo and intensity analysis for activity mixes."""

from __future__ import annotations

from array import array
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from math import isfinite, log10, log1p, sqrt
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

@dataclass(frozen=True)
class Preset:
    name: str
    bpm: tuple[int, int]
    energy_db: tuple[float, float]
    energy_label: str


PRESETS = {
    "walking": Preset("Walking", (95, 130), (-32, 0), "Steady to high energy"),
    "studying": Preset("Studying", (65, 95), (-90, -12), "Low to moderate energy"),
    "meditation": Preset("Meditation", (45, 70), (-90, -19), "Low energy"),
}
MIX_LIMIT = 40
# Independent versions let us backfill energy without discarding saved BPMs.
TEMPO_VERSION = 1
ENERGY_VERSION = 1
NEEDS_TEMPO = object()
SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_tempos (
    identity TEXT NOT NULL,
    version INTEGER NOT NULL,
    bpm REAL,
    PRIMARY KEY (identity, version)
);
CREATE TABLE IF NOT EXISTS activity_energy (
    identity TEXT NOT NULL,
    version INTEGER NOT NULL,
    energy_db REAL,
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


@dataclass(frozen=True)
class Analysis:
    bpm: float | None
    energy_db: float | None


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


def read_analysis(database):
    with closing(state_database.connect(database, SCHEMA)) as db:
        db.execute("BEGIN")
        tempos = dict(db.execute("SELECT identity, bpm FROM activity_tempos WHERE version=?", (TEMPO_VERSION,)))
        energy = dict(db.execute("SELECT identity, energy_db FROM activity_energy WHERE version=?", (ENERGY_VERSION,)))
        return tempos, energy


def save_analysis(database, identity, analysis):
    with closing(state_database.connect(database, SCHEMA)) as db, db:
        db.execute("INSERT OR IGNORE INTO activity_tempos VALUES (?, ?, ?)", (identity, TEMPO_VERSION, analysis.bpm))
        db.execute("INSERT OR REPLACE INTO activity_energy VALUES (?, ?, ?)", (identity, ENERGY_VERSION, analysis.energy_db))


def unique_recordings(items):
    unique = {}
    for item in items:
        unique.setdefault(item.identity, item)
    return unique.values()


class MixIndex:
    """A prepared view of the stored audio analysis, published as one snapshot."""

    def __init__(self, items, tempos, energy):
        self.total = len(items)
        self.indexed = sum(item.identity in tempos and item.identity in energy for item in items)
        classified = [(item.index, bpm, level) for item in items
                      if (bpm := tempos.get(item.identity)) is not None
                      and (level := energy.get(item.identity)) is not None]
        self.matches = {
            key: tuple(index for index, bpm, level in classified
                       if preset.bpm[0] <= bpm <= preset.bpm[1] and preset.energy_db[0] <= level <= preset.energy_db[1])
            for key, preset in PRESETS.items()
        }

    def choices(self):
        return [{"id": key, "name": preset.name, "min_bpm": preset.bpm[0], "max_bpm": preset.bpm[1],
                 "energy_label": preset.energy_label, "count": len(self.matches[key])}
                for key, preset in PRESETS.items()]

    def queue(self, activity):
        if not isinstance(activity, str) or activity not in PRESETS:
            raise ValueError("Choose a listening activity.")
        matches = self.matches[activity]
        if not matches:
            raise ValueError(f"No songs match {PRESETS[activity].name} yet.")
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


def _frame_power(pcm):
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    return [sum(value * value for value in samples[start:start + FRAME_SIZE]) / FRAME_SIZE
            for start in range(0, len(samples) - FRAME_SIZE + 1, FRAME_SIZE)]


def _estimate_energy(frame_power):
    """Use sustained levels and recurring short bursts so a slow beat can still be intense."""
    if len(frame_power) < FRAMES_PER_SECOND:
        return None
    block = FRAMES_PER_SECOND // 10
    windows = sorted(sum(frame_power[start:start + block]) / block
                     for start in range(0, len(frame_power) - block + 1, block))
    sustained = windows[int(0.90 * (len(windows) - 1))]
    attacks = sorted(frame_power)[int(0.98 * (len(frame_power) - 1))]
    power = max(sustained, attacks / 3)
    return round(10 * log10(power / 32768 ** 2), 1) if power else None


def _estimate_bpm(frame_power):
    """Find a periodic rise in short-window energy, or leave the track unclassified."""
    if len(frame_power) < 10 * FRAMES_PER_SECOND:
        return None
    frame_rms = [log1p(sqrt(power)) for power in frame_power]
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


def analyze(handle, duration, saved_bpm=NEEDS_TEMPO):
    descriptor = handle.fileno()
    source = ["-protocol_whitelist", "file", "-format_whitelist", CONTAINERS, "-i", f"/dev/fd/{descriptor}"]
    bpm = saved_bpm
    if bpm is NEEDS_TEMPO:
        tags = "bpm,tbpm,tempo"
        probe = subprocess.run(["ffprobe", "-v", "error", *source, "-show_entries",
                                f"format_tags={tags}:stream_tags={tags}", "-of", "json"],
                               capture_output=True, check=True, timeout=8, pass_fds=(descriptor,))
        bpm = _tagged_bpm(json.loads(probe.stdout))
        if bpm is None:
            bpm = NEEDS_TEMPO
    handle.seek(0)
    start = ["-ss", "12"] if duration >= READ_SECONDS + 12 else []
    decoded = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-filter_threads", "1",
                              *start, *source, "-t", str(READ_SECONDS), "-map", "0:a:0", "-ac", "1",
                              "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", "-f", "wav", "pipe:1"],
                             capture_output=True, check=True, timeout=12, pass_fds=(descriptor,))
    with wave.open(BytesIO(decoded.stdout), "rb") as samples:
        power = _frame_power(samples.readframes(SAMPLE_RATE * READ_SECONDS))
    return Analysis(_estimate_bpm(power) if bpm is NEEDS_TEMPO else bpm, _estimate_energy(power))


def recording_analysis(item, media, saved_bpm):
    if item.blob is not None:
        with media.open_original(item.blob) as (handle, _):
            return analyze(handle, item.duration, saved_bpm)
    with item.path.open("rb") as handle:
        if source_stamp(os.fstat(handle.fileno())) != item.stamp:
            raise OSError("Recording changed before activity analysis.")
        analysis = analyze(handle, item.duration, saved_bpm)
        if source_stamp(os.fstat(handle.fileno())) != item.stamp:
            raise OSError("Recording changed during activity analysis.")
        return analysis
