# SPEC: the Listening Room library format

This document is the specification of how music information enters, lives in,
and leaves Listening Room.

## Principle: vendor-agnostic by design

Listening Room is not built around any music vendor. Its reason to exist is a
portable library: lists you own, in open formats, matched to audio files you
own, on a computer you control. Anything that can describe songs with the
fields in this spec can feed it, and everything in it can leave again in open
formats.

Listening Room supports Spotify playlist files and an optional account
connection.

- Its playlist CSV export shape is one of the CSV dialects the CSV adapter
  accepts (see the criteria below). A CSV from any other tool that matches
  the same criteria works exactly the same way.
- The OAuth connection mirrors liked songs and playlists into local sources.
  It also records the external identity of linked playlists. If the owner
  authorizes playlist writes, an explicit edit can update a linked Spotify playlist.
  Local files and portable playlists work without the account connection.

The same holds for every other source: a folder of M3U files, a hand-written
CSV, an XSPF list from another player, or a watched folder on disk are all
first-class inputs, equal to any vendor export.

A room can use the audio files it already has. For missing recordings,
it can use optional [download extensions](docs/download-engines.md).
Search and retrieval are independently selectable when the engines agree
on candidate sources. Every retrieved file passes audio verification
before it enters the library. An optional plugin can also share files
from a folder the operator authorizes on the plugin host.

## The interchange format

Listening Room uses [XSPF version 1](https://xspf.org/spec) as its interchange
format. XSPF is an open playlist specification with ordered tracks, human
readable song fields, persistent identifiers, optional artwork, and optional
file locations. A list can describe music that has not been downloaded yet.

This is a protocol for **library metadata and playlist order**. Audio files
stay in their original codecs. An XSPF file does not contain audio or grant
access to a streaming service. When a song has no local copy, Listening Room
can search for one through its configured engines.

## What a source must contain: the acceptance criteria

Every adapter reduces its input to the same track shape: title and artist
(required), plus album, duration, identifier, ISRC, artwork, and location
(all optional). A source is accepted when it can produce at least one track
with both a title and an artist. Tracks missing one of the two keep their
place in the playlist and are reported as metadata issues instead of being
dropped silently.

### CSV

- A header row is required. Header matching is forgiving: case, spaces, and
  punctuation are ignored, so `Artist Name(s)`, `artist_name`, and `ARTIST`
  style variants are all read the same way.
- Title column, any of: `Track Name`, `Title`, `Song`, `Song Title`, `Name`.
- Artist column, any of: `Artist Name(s)`, `Artist`, `Artists`, `Performer`,
  `Creator`.
- Album (optional): `Album Name` or `Album`.
- Duration (optional): milliseconds in `Track Duration (ms)`,
  `Duration (ms)`, or `Duration ms`; or seconds in `Duration`, `Length`, or
  `Seconds`.
- Identifier (optional): `Track URI`, `URI`, `Identifier`, or
  `MusicBrainz ID`. Any stable URI works; it does not have to come from a
  specific vendor.
- ISRC (optional): `ISRC`. Artwork (optional): `Album Image URL`, `Image`,
  or `Artwork`. Location (optional): `Location`, `File`, or `Path`.
- The minimal useful CSV is therefore two columns:

  ```csv
  Title,Artist
  Blue in Green,Miles Davis
  ```

### M3U and M3U8

- Each entry needs `Artist - Title`, either in its `#EXTINF` line or in the
  file name. The path or URL on the following line becomes the track's
  location hint. `#EXTINF` durations, in seconds, are kept when present.

### XSPF

- Must be XSPF version 1 in the standard namespace, with at least one track
  carrying both `title` and `creator`. Documents with `DOCTYPE` or `ENTITY`
  declarations are rejected. Field mapping is in the table below.

## Bringing information in, and taking it out

A portable room accepts lists through Sources, watched folders, linked files
and the optional Spotify adapter. CSV and M3U inputs can produce XSPF
documents for interchange. The room stores its list documents and ordered
memberships in SQLite. A watched or linked file stays at the location the
owner selected. A one-time browser import needs no ongoing access to its
original file.

There are three export contracts.

1. **Export library** downloads a metadata ZIP containing `bundle.json`,
   `lists/*.xspf`, `csv/*.csv` and generated `playlists/*.m3u8`. The recipient
   can import those lists on another host. Host paths, pairing credentials
   and audio bytes stay out of the ZIP.
2. An audio export materializes playable tracks in their stored formats.
   A playlist export writes its own folder and an M3U8 manifest in playlist
   order. A repeated entry can refer to the same exported file.
3. A collection snapshot contains owned audio, catalog identities, playlists,
   coverage, genres, favorites, requests and history. Use snapshot and
   restore to carry this state across machines. Importing a metadata ZIP
   into an empty room creates that room's own catalog indexes.

## Portable storage

A new room stores its collection in `collection.sqlite3`. Audio is keyed by
the SHA-256 of its original bytes. Lossless storage compression is used when
it saves space. Staging or exporting an audio object recovers the same bytes.
SQLite can create journal sidecars while the room is running. Use the
collection commands to produce a consistent standalone copy.

Working copies of list documents let filesystem adapters operate in a private
host workspace. The room can re-create missing copies from SQLite. Machine
configuration, pairing credentials, workers, caches and logs belong to the
host. Legacy installations can keep separate state, list and audio paths
until the owner explicitly migrates them. [Collection ownership](adrs/0006-collection-ownership.md)

## Track mapping

| XSPF track element | Listening Room meaning |
| --- | --- |
| `title`, `creator` | Required song title and artist |
| `album` | Optional album |
| `duration` | Milliseconds, if known |
| `identifier` | Stable provider or recording URI, if known |
| `image` | Optional artwork URL |
| `location` | Optional local or remote file reference; never required to import a list |
| `meta rel="https://isrc.ifpi.org/"` | Optional ISRC |

Tracks are matched by `identifier` when present, otherwise by a normalized
artist, title, and album tuple. Reimporting the same identifier preserves the
catalog index, downloaded match, and requests. Playlist order and repeated
entries are preserved. List names come from their file names; use separate
`.xspf` files for separate playlists.

An imported location is a hint. A recording becomes playable when verification
associates it with local audio, whether the verified copy is in the portable
database or a configured legacy audio directory. Other players need the audio
files referenced by an exported M3U8.

Minimal example:

```xml
<?xml version="1.0" encoding="utf-8"?>
<playlist version="1" xmlns="http://xspf.org/ns/0/">
  <title>Sunday listening</title>
  <trackList>
    <track>
      <title>Example Song</title>
      <creator>Example Artist</creator>
      <album>Example Album</album>
      <duration>201000</duration>
      <identifier>https://musicbrainz.org/recording/example</identifier>
    </track>
  </trackList>
</playlist>
```

## Import and export details

- Import XSPF directly, or let the app convert CSV (any tool's export that
  matches the criteria above) or M3U/M3U8 with `Artist - Title` information.
  `Duration` in a CSV is seconds; `Duration (ms)` is milliseconds.
- A watched source folder accepts dropped list files. Incomplete entries
  keep their place and appear as metadata issues until repaired. The
  original input stays available.
- **Export library** in Storage downloads a ZIP containing `bundle.json`,
  `lists/*.xspf`, `csv/*.csv` (one per list), and generated `playlists/*.m3u8`. It contains no host paths,
  credentials, or pairing tokens. Import that ZIP on another host using
  **Sources**. The new host chooses its own paths and engine configuration. **Export host settings** is a separate JSON download for
  people who want to copy path preferences and review them before saving.
- M3U8 exports contain only verified playable files. Inside the portable ZIP,
  paths are rewritten relative to an optional sibling `audio/` directory. A
  metadata-only XSPF list can include unavailable
  tracks; a player needs an audio copy before it can play them.

Use audio export for standalone music files, or snapshot and restore for
the full Listening Room collection. Only share files you have the right
to distribute.

## Multiple local sources

The Sources tab looks for likely playlist folders under Downloads, Music,
Documents, and Desktop on the host computer. It checks file headers locally
and skips the current library. Suggestions are not imported until you choose
**Watch folder**. You can also add an existing folder path directly or import
individual files from anywhere.

Watched folders can contain CSV, M3U/M3U8 and XSPF lists. The room imports
changes, preserving distinct names for inputs from different folders. Source
files remain untouched. Stopping a watch retains its imported lists.

For a single playlist that changes over time, use **Sources → Linked playlists**
and enter its path on the host computer. This imports it and remembers the path.
The next edit updates the same XSPF copy. A one-time browser file import has no
ongoing access to its original file. Unlinking or temporarily removing a linked
file leaves the last imported copy in place.

## Appendix: legacy migration from Spotify CSV directories

On first launch after the upgrade, Listening Room creates `lists/` and writes
XSPF copies of CSVs in the previously configured CSV directory. Originals
remain in place. The new `lists_folder` setting replaces `csv_folder`; existing
audio and M3U8 output paths remain configured. Catalog indexes and verified
audio are retained because Spotify track identifiers stay the same. Existing
clients may still send `csv_folder` or use `/api/import-csv`, but the UI and
portable export use XSPF and `lists_folder`. That compatibility surface is
deprecated: it is kept so existing clients keep working, and new setups
should not rely on it.

Spotify OAuth sync still writes a CSV mirror as an input adapter. The list
watcher converts it to XSPF before rebuilding the catalog. This keeps the
Spotify adapter isolated from the catalog and downloader.
