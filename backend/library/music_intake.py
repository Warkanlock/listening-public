"""Create an import playlist from local, tagged audio."""

import csv
from collections import Counter
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from downloads.verification import judge, probe, title_is_generic
from library.library_protocol import track_key, write_xspf
from system.formats import AUDIO_FORMATS
from system.private_files import private_write


def audio_paths(folder):
    for directory, folders, files in os.walk(folder):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            path = Path(directory) / name
            if path.suffix.lower() in AUDIO_FORMATS and not path.is_symlink():
                yield path


@dataclass
class FolderScan:
    tracks: list
    skipped: Counter
    size: int


def scan_folder(folder):
    """Recognize local audio without writing to its source folder."""
    if not folder.is_dir() or not os.access(folder, os.R_OK | os.X_OK):
        raise ValueError("Choose an existing readable music folder.")
    if not shutil.which("ffprobe"):
        raise ValueError("Install FFmpeg to read audio tags.")
    tracks = {}
    skipped = Counter()
    size = 0
    for path in audio_paths(folder):
        if not path.is_file():
            continue
        facts = probe(path, local_audio_only=True)
        if not facts:
            skipped["Unreadable audio"] += 1
            continue
        if not facts.get("artist") or title_is_generic(facts.get("title", "")):
            skipped["Missing title or artist tags"] += 1
            continue
        verdict = judge(facts["title"], facts["duration"], facts)
        if not verdict.ok:
            skipped["Audio below the quality requirement"] += 1
            continue
        track = {**facts, "duration_ms": round(facts["duration"] * 1000)}
        identity = track_key(track)
        if identity in tracks:
            skipped["Duplicate recording tags"] += 1
            continue
        try:
            size += path.stat().st_size
        except OSError:
            skipped["Audio moved during the scan"] += 1
            continue
        tracks[identity] = track
    return FolderScan(list(tracks.values()), skipped, size)


def save_folder_index(destination, scan):
    """Regenerate the room-owned list without rewriting an identical index."""
    content = write_xspf("Owned music", scan.tracks)
    if not destination.is_file() or destination.read_text(encoding="utf-8") != content:
        private_write(destination, content)


def write_index(folder, playlist):
    if not folder.is_dir():
        raise ValueError("Choose an existing music folder.")
    if playlist.suffix.lower() != ".csv" or not playlist.parent.is_dir():
        raise ValueError("Choose a new .csv in an existing output folder.")
    if playlist.exists():
        raise ValueError("The playlist already exists. Choose a new output filename.")
    if not shutil.which("ffprobe"):
        raise ValueError("Install FFmpeg to read audio tags.")
    skipped = 0
    songs = []
    for path in audio_paths(folder):
        facts = probe(path, local_audio_only=True)
        if (not facts or not facts.get("title") or not facts.get("artist")
                or not judge(facts["title"], facts["duration"], facts).ok):
            skipped += 1
            continue
        songs.append((facts["title"], facts["artist"], facts.get("album", ""), facts["duration"]))
    if not songs:
        raise ValueError(f"No tagged, verified music to index ({skipped} skipped).")
    with playlist.open("x", encoding="utf-8", newline="") as handle:
        output = csv.writer(handle)
        output.writerow(("Title", "Artist", "Album", "Duration"))
        output.writerows(songs)
    return len(songs), skipped


def create_demo(folder):
    """Generate original CC0 audio for a deterministic, account-free demo."""
    if folder.exists() or not folder.parent.is_dir():
        raise ValueError("Choose a new demo folder in an existing directory.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("Install FFmpeg or run this command in the Listening container.")
    with tempfile.TemporaryDirectory(prefix=".listening-demo-", dir=folder.parent) as temporary:
        staging = Path(temporary)
        audio = staging / "First light.mp3"
        subprocess.run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "aevalsrc=0.12*sin(2*PI*220*t)+0.07*sin(2*PI*277.183*t)+0.04*sin(2*PI*329.628*t):s=44100:d=8",
            "-af", "afade=t=in:d=0.6,afade=t=out:st=6.5:d=1.5",
            "-c:a", "libmp3lame", "-b:a", "256k", "-metadata", "title=First light",
            "-metadata", "artist=Listening", "-metadata", "album=First listen", str(audio),
        ], check=True, timeout=30)
        playlist = staging / "Playlists"
        playlist.mkdir()
        write_index(staging, playlist / "First listen.csv")
        folder.mkdir()
        os.rename(audio, folder / audio.name)
        os.rename(playlist, folder / playlist.name)
