# 0008. Judge audio against the recording before marking it playable

Accepted.

Lists name recordings, including songs the owner has never downloaded. A successful transfer or a plausible filename does not establish a match. The downloads module probes the actual audio and judges it against the expected title and duration. This rule also applies when discovering an existing file in the owner's audio directory.

The verifier requires a readable audio stream, at least 192 kbps, a duration within 5.5 seconds when an expected length is known, and a matching title. A generic or empty title tag receives the established exception and an explicit reason. Persist verified, pending, unmatched and rejected coverage separately so an unsuccessful search can be distinguished from a bad copy. Retain a verified duration separately when the list omits one. [Verification](../backend/downloads/verification.py) · [Verified lengths](../backend/catalog/lengths.py)

Transferred audio must match the file type. Bulk downloads accept MP3 audio in MP3 files, FLAC audio in FLAC files, and ALAC audio in M4A containers. The room defaults to a preference for MP3 among matching results. The owner can set a room default or choose MP3 first, MP3 only, or lossless only for one job. Individual requests default to lossless and keep the format selected for that song. They can also search for MP3 only or prefer MP3 with AAC in M4A as a fallback. The room passes the allowed and preferred formats to its search extension. The runner checks the codec and container reported by ffprobe before it accepts the file. For MP3 and AAC downloads, the 192 kbps floor applies to the audio stream, independently of cover art and tags.

Portable rooms commit the original bytes to SQLite. Export reconstructs the same media bytes and original format, including metadata. Existing recordings and owner-provided audio use the recording verification rules above.

The runner bounds extension operations and ffprobe subprocess time. Bulk intake reserves 20 GiB of free space. It retains a cursor and deduplicates activity by song. The host accepts only a completed regular file directly inside the extension's staging directory. [Extension protocol](../examples/engines/README.md)

Catalog songs and custom requests use the same audio resolver. Legacy file references can be absolute, audio-directory relative or room relative. HTTP file resolution checks containment and allowed formats. The media adapter also accepts content-hash references and stages the original bytes for range playback. The original hash and stored probe remain useful after the staging file is evicted. [Audio resolution](../backend/catalog/audio_resolution.py) · [Collection ownership](0006-collection-ownership.md)
