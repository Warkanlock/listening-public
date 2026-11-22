"""The one definition of a verified copy.

Every place that decides whether a file counts as the song it claims to
be calls judge() here: the library scan, the focused download, the
request pipeline, the ISRC sharing pass, and the coverage report. The
thresholds live here and nowhere else: at least 192 kbps, duration
within 5.5 seconds of the expected length when the length is known, and
a title tag that matches. Generic placeholder tags ("Track 01" and its
translations) and empty tags are accepted, because real libraries carry
them, but judge() records that acceptance as the coverage reason
"title tag generic" so it stays visible (A2 C-06).

New Soulseek downloads also verify that the encoding matches the advertised file type.
probe() captures descriptive fields that never feed the base judge():
the audio stream's codec, sample rate, and bit depth, and the file's
ReplayGain and R128 gain/peak tags (plan 042 stage 6). The player's
quality readout and volume normalization consume them; the verdict
vocabulary above is unchanged.
"""

import json
import re
import subprocess
from dataclasses import dataclass
from difflib import SequenceMatcher

from system.formats import LOSSLESS_DOWNLOAD_FORMATS, REQUEST_FORMAT_NAMES, REQUEST_SUFFIXES
from system.normalize import normalize_title

MIN_BITRATE = 192_000
MIN_BITRATE_KBPS = 192
DURATION_TOLERANCE = 5.5
TITLE_SIMILARITY = 0.55
# Sockseek's --length-tol takes whole seconds: it pre-filters candidates
# at 5 s, and judge() then applies the real 5.5 s rule to what returns.
CLI_LENGTH_TOLERANCE = 5
# ffprobe runs on files that come from untrusted peers; a hung probe
# must not stall the pipeline (A2 C-04).
PROBE_TIMEOUT = 30
FULL_DECODE_TIMEOUT = 45

# The probe cache's media shape version, owned here because probe()
# defines the shape: runner.cached_probe re-probes entries written
# under an older version once, so new fields are never missed.
# Version 7 includes recording identifiers and version tags.
# Version 6 records the container and reads case-insensitive title
# tags on both the container and the audio stream.
# Version 5 added the descriptive quality and gain fields (plan 042
# stage 6); version 4 was plan 030's artist and album tags, version 3
# plan 029's genre tag.
PROBE_CACHE_VERSION = 7

_GAIN_TAGS = ("replaygain_track_gain", "replaygain_album_gain",
              "replaygain_track_peak", "replaygain_album_peak",
              "r128_track_gain")
_NUMBER = re.compile(r"[+-]?\d+(?:\.\d+)?")


def _tag_value(tags, name):
    """The raw value of a tag, looked up case-insensitively.

    ffprobe preserves the case a file's tags were written with
    (Vorbis comments conventionally shout the ReplayGain names),
    so descriptive lookups fold case on both sides.
    """
    for key, value in tags.items():
        if key.lower() == name:
            return value
    return None


def _tag_number(tags, name):
    """A float from a tag value like "-7.89 dB" or "0.987654", or None."""
    raw = _tag_value(tags, name)
    if raw is None:
        return None
    match = _NUMBER.match(str(raw).strip())
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _r128_gain(tags):
    """The R128 track gain in dB, or None.

    The R128 tags are written as Q7.8 fixed-point integers (the
    gain in dB times 256) by the tools that write them, so an
    integer value reads as fixed point; a value written with a
    decimal point is already in dB.
    """
    raw = _tag_value(tags, "r128_track_gain")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text) / 256
        return float(text)
    except ValueError:
        return None

REASON_UNREADABLE = "unreadable audio"
REASON_TITLE_GENERIC = "title tag generic"

_GENERIC_TAG = re.compile(r"(?:track|traccia|pista|audio)\s*\d+")


@dataclass
class Verdict:
    ok: bool
    reason: str = ""


def decode_audio(path, *, probed_duration, expected_duration, timeout=FULL_DECODE_TIMEOUT):
    """Read every frame of a verified local candidate without writing an output file."""
    # Build the output timeline from decoded samples. A truncated stream can
    # reach EOF without a decoder error and still advertise its original duration.
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-progress", "pipe:1",
                             "-protocol_whitelist", "file", "-format_whitelist", "flac,mov", "-threads", "1",
                             "-i", str(path), "-map", "0:a:0", "-af", "asetpts=N/SR/TB", "-f", "null", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            timeout=timeout, check=True)
    progress = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    duration = int(progress.get("out_time_us", "0")) / 1_000_000
    if progress.get("progress") != "end" or duration <= 0 or any(
            value and abs(duration - float(value)) > DURATION_TOLERANCE
            for value in (probed_duration, expected_duration)):
        raise ValueError("Decoded audio did not reach the expected duration.")


def probe(path, *, local_audio_only=False, timeout=None):
    tag_entries = ",".join(("title", "artist", "album", "genre", "musicbrainz_trackid", "MusicBrainz Track Id", "version", "subtitle") + _GAIN_TAGS)
    try:
        restrictions = ["-protocol_whitelist", "file", "-format_whitelist", "mp3,flac,mov,aac"] if local_audio_only else []
        result = subprocess.run(["ffprobe", "-v", "error", *restrictions, "-show_entries",
                                 f"format=format_name,duration,bit_rate:format_tags={tag_entries}:"
                                 "stream=codec_type,codec_name,sample_rate,"
                                 "bits_per_raw_sample,bits_per_sample,bit_rate:"
                                 f"stream_tags={tag_entries}",
                                 "-of", "json", str(path)], capture_output=True, text=True,
                                timeout=PROBE_TIMEOUT if timeout is None else timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        info = json.loads(result.stdout)
        audio = next((s for s in info.get("streams", [])
                      if s.get("codec_type") == "audio"), None)
        if audio is None:
            return None
        duration = float(info.get("format", {}).get("duration", 0))
        bitrate = int(info.get("format", {}).get("bit_rate", 0) or 0)
        try:
            stream_bitrate = int(audio.get("bit_rate") or 0)
        except (ValueError, TypeError):
            stream_bitrate = 0
        if not bitrate and duration:
            bitrate = round(path.stat().st_size * 8 / duration)
        tags = info.get("format", {}).get("tags", {})
        if not tags:
            tags = next((stream.get("tags", {}) for stream in info.get("streams", [])
                         if stream.get("tags")), {})
        # Vorbis comment names are case-insensitive. Look for the actual
        # recording title even when the container carries only other tags.
        title = (_tag_value(info.get("format", {}).get("tags", {}), "title")
                 or _tag_value(audio.get("tags", {}), "title") or "")
        # The descriptive fields also read supplemental stream metadata.
        descriptive = [info.get("format", {}).get("tags", {}),
                       audio.get("tags", {})]
        descriptive += [s.get("tags", {}) for s in info.get("streams", [])]

        def gain(name):
            for source in descriptive:
                value = _tag_number(source, name)
                if value is not None:
                    return value
            return None

        r128 = None
        for source in descriptive:
            r128 = _r128_gain(source)
            if r128 is not None:
                break
        bit_depth = int(audio.get("bits_per_raw_sample") or
                        audio.get("bits_per_sample") or 0) or None
        identity_tags = {}
        for source in reversed(descriptive):
            for name in ("musicbrainz_trackid", "musicbrainz track id", "version", "subtitle"):
                if value := _tag_value(source, name):
                    identity_tags["musicbrainz_trackid" if name == "musicbrainz track id" else name] = value
        return {**identity_tags, "duration": duration, "bitrate": bitrate, "stream_bitrate": stream_bitrate, "title": title,
                "artist": _tag_value(tags, "artist") or _tag_value(audio.get("tags", {}), "artist") or "",
                "album": _tag_value(tags, "album") or _tag_value(audio.get("tags", {}), "album") or "",
                "genre": tags.get("genre", tags.get("Genre", "")),
                "codec": audio.get("codec_name", ""),
                "container": info.get("format", {}).get("format_name", ""),
                "sample_rate": int(audio.get("sample_rate") or 0),
                "bit_depth": bit_depth,
                "track_gain": gain("replaygain_track_gain"),
                "album_gain": gain("replaygain_album_gain"),
                "track_peak": gain("replaygain_track_peak"),
                "album_peak": gain("replaygain_album_peak"),
                "r128_track_gain": r128}
    except (ValueError, KeyError, TypeError):
        return None


def title_is_generic(actual):
    downloaded = normalize_title(actual)
    return not downloaded or re.fullmatch(_GENERIC_TAG, downloaded) is not None


def title_matches(expected, actual):
    target = normalize_title(expected)
    downloaded = normalize_title(actual)
    if not downloaded or re.fullmatch(_GENERIC_TAG, downloaded):
        return True
    return target in downloaded or SequenceMatcher(None, target, downloaded).ratio() >= TITLE_SIMILARITY


def judge(expected_title, expected_length, media):
    """Judge one probed file against the recording it claims to be.

    The reason strings are the coverage vocabulary the report writes:
    "unreadable audio", "low bitrate <bps>", "duration differs by <s>s",
    "title tag differs: <tag>", and, for an accepted copy whose tag is
    generic or empty, "title tag generic".
    """
    if media is None:
        return Verdict(False, REASON_UNREADABLE)
    if media["bitrate"] < MIN_BITRATE:
        return Verdict(False, f"low bitrate {media['bitrate']}")
    if expected_length and abs(media["duration"] - float(expected_length)) > DURATION_TOLERANCE:
        return Verdict(False, f"duration differs by {media['duration'] - float(expected_length):.1f}s")
    if not title_matches(expected_title, media["title"]):
        return Verdict(False, f"title tag differs: {media['title']}")
    if title_is_generic(media["title"]):
        return Verdict(True, REASON_TITLE_GENERIC)
    return Verdict(True, "")


def request_identity(row):
    if row.get("musicbrainz_id"):
        return {"recording_id": row["musicbrainz_id"], "version": row.get("version", ""),
                "expected_artist": row["artist"], "performance": row.get("performance", "")}
    return {}


def judge_claim(title, length, facts, suffix, request=None):
    if request and request.get("musicbrainz_id"):
        return judge_download(title, length, facts, suffix, request["requested_format"] or "any_lossless",
                              **request_identity(request))
    return judge(title, length, facts)


def recording_words(value):
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def recording_mismatch(media, title, artist, length, recording_id, version, performance):
    tagged = (media.get("musicbrainz_trackid") or "").lower()
    if tagged and recording_id != tagged:
        return "Audio identifies another recording"
    actual_title = recording_words(media["title"])
    expected = recording_words(title)
    if not actual_title or re.fullmatch(_GENERIC_TAG, actual_title) or not length:
        return "Missing recording title or expected duration"
    actual_artist = recording_words(media.get("artist") or "")
    if (actual_artist or not tagged) and actual_artist != recording_words(artist):
        return "The artist tag does not identify this performer"
    qualifier = recording_words(version)
    if actual_title not in {expected, (expected + " " + qualifier).strip()}:
        return "The title tag does not identify this recording"
    if not tagged:
        explicit = [recording_words(media.get(key) or "") for key in ("version", "subtitle")]
        for field in filter(None, explicit):
            if not qualifier or (f" {field} " not in f" {qualifier} " and f" {qualifier} " not in f" {field} "):
                return "The audio tags identify a different version"
        suffix = actual_title.removeprefix(expected).strip()
        album = recording_words(media.get("album") or "")
        evidence = [*explicit, suffix, album if album != expected else ""]
        if performance and not all(any(f" {recording_words(detail)} " in f" {field} " for field in evidence)
                                   for detail in performance.split(" · ")):
            return "The audio tags do not identify this performance"
        if qualifier and not any(f" {qualifier} " in f" {field} " for field in evidence):
            return "The audio tags do not identify this version"
    return ""


def judge_download(expected_title, expected_length, media, suffix, requested_format="any_lossless",
                   *, recording_id="", version="", expected_artist="", performance=""):
    """Verify the actual lossless stream and container before landing a peer transfer."""
    verdict = judge(expected_title, expected_length, media)
    if verdict.ok:
        if recording_id:
            reason = recording_mismatch(media, expected_title, expected_artist, expected_length, recording_id, version, performance)
            if reason:
                return Verdict(False, reason)
        codec = media.get("codec")
        containers = media.get("container", "").split(",")
        if suffix.lower() not in REQUEST_SUFFIXES[requested_format]:
            return Verdict(False, f"download requires {REQUEST_FORMAT_NAMES[requested_format]}")
        expected = LOSSLESS_DOWNLOAD_FORMATS[suffix.lower()]
        if codec != expected[0] or expected[1] not in containers:
            return Verdict(False, f"download requires {REQUEST_FORMAT_NAMES[requested_format]} audio")
    return verdict


def judge_bulk_download(expected_title, expected_length, media, suffix):
    """Check staged bulk downloads, including transfers left by an earlier policy."""
    if suffix.lower() != ".mp3":
        return judge_download(expected_title, expected_length, media, suffix)
    # Cover art and tags count toward the whole-file bitrate but not the audio floor.
    audio = {**media, "bitrate": media.get("stream_bitrate", 0)} if media is not None else None
    verdict = judge(expected_title, expected_length, audio)
    if verdict.ok and (media.get("codec") != "mp3" or "mp3" not in media.get("container", "").split(",")):
        return Verdict(False, "download requires MP3 audio")
    return verdict
