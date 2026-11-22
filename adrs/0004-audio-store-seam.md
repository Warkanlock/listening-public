# 0004. Resolve file and database audio through one interface

Accepted for playback, export and sharing. [ADR 0005](0005-media-database-is-the-library.md) changed the landing default. [ADR 0006](0006-collection-ownership.md) superseded the original storage layout and coverage-based retirement policy.

Introducing database storage into an existing file collection had to preserve playback for both types of reference. The landing setting controls incoming transfers. Playback resolves the reference held by each song, including database objects created before a setting change.

Soulseek downloads are verified in staging. The audio store keys the result by the SHA-256 of its original bytes. Several songs can reference one object. A playback buffer stages up to three originals on disk. Staged duplicates expire after five minutes without a request on Python 3.11 and newer. Expiry verifies the durable copy and leaves any open stream readable. Sharing materializes the chosen songs to a share folder. Export writes the original formats under portable file names. Reclaim returns freed pages to the filesystem in bounded increments.

The first design used an optional media database beside the library's own files. Coverage initially determined when a row could be reclaimed. Rebuilding coverage could then retire the only copy of a recording. Durable song ownership and explicit retirement now protect those bytes. Existing file-backed rooms retain a compatible read path.
