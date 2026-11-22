"""Disposable playback copies of immutable, verified library audio."""
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

PROFILES = {
    "opus": (".opus", ("-c:a", "libopus", "-b:a", "128k", "-vbr", "on")),
    "aac": (".m4a", ("-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k", "-movflags", "+faststart")),
}
CACHE_VERSION = 1
CACHE_BYTES = 2 * 1024 ** 3
WAITERS = threading.BoundedSemaphore(8)


def digest(handle):
    handle.seek(0)
    checksum = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        checksum.update(chunk)
    handle.seek(0)
    return checksum.hexdigest()


def _tool(command, source, timeout):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=timeout,
                              pass_fds=(source.fileno(),)).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError("Compact audio could not be prepared. Check FFmpeg and the source audio.") from error


def _compatible(source, suffix, profile):
    raw = _tool(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                 "stream=codec_name,profile,bit_rate:format=bit_rate,format_name",
                 "-of", "json", f"/dev/fd/{source.fileno()}"], source, 30)
    data = json.loads(raw)
    stream = next(iter(data.get("streams", [])), {})
    container = data.get("format", {})
    codec = stream.get("codec_name")
    try:
        bitrate = int(stream.get("bit_rate") or container.get("bit_rate") or 0)
    except ValueError:
        return False
    formats = container.get("format_name", "").split(",")
    if profile == "opus" and suffix in {".opus", ".ogg"} and codec == "opus" and "ogg" in formats:
        return 0 < bitrate <= 140000
    limit = 140000 if profile == "opus" else 200000
    return 0 < bitrate <= limit and (
        (suffix == ".m4a" and "m4a" in formats and codec == "aac" and stream.get("profile") == "LC")
        or (suffix == ".mp3" and codec == "mp3" and "mp3" in formats)
    )


def _trim(folder, keep):
    entries = []
    for path in folder.iterdir():
        if path != keep and path.suffix in {".opus", ".m4a"} and path.is_file():
            info = path.stat()
            entries.append((info.st_mtime, info.st_size, path))
    remaining = max(0, CACHE_BYTES - keep.stat().st_size)
    total = sum(size for _, size, _ in entries)
    for _, size, path in sorted(entries):
        if total <= remaining:
            break
        path.unlink()
        total -= size


@contextmanager
def _conversion_lock(folder):
    if not WAITERS.acquire(blocking=False):
        raise OSError("Compact audio is busy. Try again.")
    try:
        with (folder / ".lock").open("ab") as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise OSError("Compact audio is busy. Try again.") from None
                    time.sleep(0.05)
            yield
    finally:
        WAITERS.release()


def _cached_copy(target):
    try:
        handle = target.open("rb")
    except FileNotFoundError:
        return None
    try:
        os.utime(handle.fileno())
    except OSError:
        # An open descriptor can outlive a cache eviction.
        pass
    return handle


@contextmanager
def open_copy(folder, source, name, profile, *, source_hash=None):
    """Yield an open, seekable copy. Eviction never interrupts open readers."""
    suffix = Path(name).suffix.lower()
    extension, encode = PROFILES[profile]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = os.fstat(source.fileno()) if source_hash is None else None
    source_hash = source_hash or digest(source)
    base = folder / f"{source_hash}-{suffix.lstrip('.')}-{profile}-v{CACHE_VERSION}"
    target = base.with_suffix(extension)
    direct = base.with_suffix(".direct")
    copy = _cached_copy(target)
    if copy is None and not direct.is_file():
        with _conversion_lock(folder):
            copy = _cached_copy(target)
            if copy is None and not direct.is_file():
                if _compatible(source, suffix, profile):
                    direct.touch()
                else:
                    # An inherited descriptor survives playback buffer eviction.
                    source.seek(0)
                    for leftover in folder.glob(".encode-*"):
                        if leftover.is_dir() and not leftover.is_symlink():
                            shutil.rmtree(leftover)
                    with tempfile.TemporaryDirectory(prefix=".encode-", dir=folder) as scratch:
                        staging = Path(scratch) / ("encoded" + extension)
                        _tool(["ffmpeg", "-nostdin", "-v", "error", "-xerror",
                               "-i", f"/dev/fd/{source.fileno()}", "-map", "0:a:0",
                               "-map_metadata", "0", "-fflags", "+bitexact", *encode, str(staging)], source, 300)
                        if original is not None:
                            current = os.fstat(source.fileno())
                            if (current.st_size, current.st_mtime_ns) != (original.st_size, original.st_mtime_ns):
                                raise OSError("The source changed while encoding. Try again.")
                        if not 0 < staging.stat().st_size <= CACHE_BYTES:
                            raise OSError("Encoded audio is empty or exceeds the cache size")
                        staging.replace(target)
                    _trim(folder, target)
                    copy = target.open("rb")
    if copy is None:
        source.seek(0)
        yield source, name
    else:
        with copy:
            yield copy, str(Path(name).with_suffix(extension))
