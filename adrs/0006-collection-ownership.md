# Audio belongs to the collection

Accepted. This decision supersedes automatic destructive migration in [ADR 0005](0005-media-database-is-the-library.md) and coverage-driven deletion in [ADR 0004](0004-audio-store-seam.md). A catalog refresh computes coverage. It has no authority to retire a song. Committing incoming bytes and their stable song owner in one transaction lets a retry recover the recording even if coverage publication was interrupted. An explicit copy leaves original and skipped files in place. Only app-owned download staging is disposable.

A new or explicitly restored portable room keeps music, catalog identities, ordered list memberships, playlist links, corrected genres, favorites, request history and play events in `collection.sqlite3`. Its stored audio is keyed by the original SHA-256. Each row uses raw bytes or a smaller reversible representation. Older raw rows remain readable. Export and playback recover the original file.

The application folder keeps machine settings, credentials, workers and disposable caches. Existing rooms retain their database locations until the owner copies and verifies a portable snapshot. The snapshot includes the approved collection tables and original list contents. The restore validates SQLite integrity, audio references and decoded hashes in a fresh destination. Opening a portable collection with a missing lists directory reconstructs it from the database. Files used by the existing list adapters are synchronized at catalog build time.

A playlist source keeps its own ordered membership. A linked playlist projects current memberships under its own stable identifier and owner-selected name. An explicit shared provider playlist ID can establish a link. Similar contents generate a reviewable suggestion. Dismissals and unlink decisions persist across later imports and syncs. The owner can reconsider either decision in Sources.

A fresh portable room keeps its list, audio landing, playlist and report folders
in a per-room cache under the application folder. An explicitly configured list folder stays an input. Reconciliation and
canonicalization write to the private cache and preserve the owner’s original
files. On open, the server compares cached list contents with their last
materialized hashes. SQLite supplies changes from another host. Offline edits
whose database version has stayed the same enter the next build. When both
versions changed, the cache keeps the local bytes under `retained-lists` and
presents the database version. Snapshots take portable list contents and names
directly from SQLite.

A missing private cache file is re-created from SQLite. An explicit source or
Spotify adapter removal checks the stored content hash before retiring the
canonical document and its unchanged generated XSPF. An independently edited
playlist remains available.
