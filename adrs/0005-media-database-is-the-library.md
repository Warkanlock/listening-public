# 0005. Make SQLite the default home for audio

Accepted for database landing. The startup migration and source deletion described in the original decision were superseded by [ADR 0006](0006-collection-ownership.md).

The original file-first default made a fresh installation require managing a file library even though Listening Room could already play and export audio stored in SQLite. New rooms use database landing and preserve audio bytes under their original content hashes.

A previous implementation kept a separate `media.sqlite3` beside portable list files. It scheduled a one-time resumable migration on startup, storing the recording and replacing its reference before deleting the source file. That could leave the owner without an independent source for recovery. The replacement is an explicit, verified portable copy with an inventory of skipped files. Ordinary startup and catalog refresh preserve owner-provided media.
