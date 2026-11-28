# Architecture of Listening

## Overview

Listening runs on a server you control. The server imports and stores the music collection, coordinates shared playback, and manages requests for missing recordings. Browsers and native clients use the same room API on a private network. A household account can keep its playlists and playback separate from the shared queue.

The core can import and play music on its own. An optional manager on a Linux Docker host runs extensions for searches, downloads, and folder sharing. External catalog, metadata, and playlist adapters have separate entry points.

## Code map

| Path | Responsibility |
| --- | --- |
| `backend/server.py` | HTTP entry point for the web app, API, audio streams, and access checks. |
| `backend/runner.py` | Runs catalog preparation, acquisition, and verification jobs. |
| `backend/catalog/` | Catalog projection, audio storage and lookup, collection snapshots, and offline replicas. `audio_store.py` and `collection_snapshot.py` handle stored audio and portable copies. |
| `backend/library/` | Portable playlist parsing, imports, linked playlists, and playlist edits. `library_protocol.py` defines the interchange rules. |
| `backend/downloads/` | Request state, candidate selection, the engine contract, and audio verification. Start with `requests_store.py`, `engine_contract.py`, and `verification.py`. |
| `backend/playback/` | Shared playback, outputs, Radio, and playback history. Radio runs independently of the shared queue. |
| `backend/devices/` | Paired device records, presence, and announcements. |
| `backend/metadata/` | Recording enrichment, discovery, lyrics, and related artists. |
| `backend/adapters/` | Integrations such as Spotify, public and private catalogs, Mastodon, and OpenSubsonic. |
| `backend/system/` | Host configuration, accounts, pairing, worker supervision, job state, updates, and extension management. |
| `frontend/react/` | React and TypeScript app. Vite builds it into `frontend/dist/`, which the server serves. `src/api.ts` is the web client API boundary. The standalone tour uses the same About page. |
| `clients/` | Native Android client, macOS wrapper, and iPhone and iPad app. The Apple clients render the web app. |
| `packaging/`, `scripts/` | Compose overrides, host and release commands, and test tooling. `listening` is the public command-line entry point. |
| `examples/engines/` | Extension protocol and worker examples. The Listening Plugins repository holds provider implementations and their SDK. |
| `tests/` | Backend unit and integration tests. `scripts/verify_ui.py` drives the built web app against a disposable room. |

## Storage and data flow

1. The library imports owned audio and parses CSV, M3U, and XSPF playlists. Playlist metadata and a playable recording are separate. Check `backend/library/music_intake.py` and `backend/library/list_intake.py` when changing intake.
2. A search extension returns candidates with an opaque ID and a source namespace. A compatible fetch extension places the selected file in staging. Core verifies the file and decides whether it belongs in the library. A sharing extension receives a read-only mount of the chosen folder and handles its own network protocol.
3. `collection.sqlite3` holds the portable collection, including original encoded audio keyed by SHA-256, catalog data, and ordered playlist memberships. Existing file-backed libraries use the same audio lookup interface. Private configuration, credentials, and device state live outside the portable collection.
4. The server streams verified originals to authorized clients. Compact audio uses derived encodings when requested. Offline saves copy selected audio to the device. Playback and library requests remain available while workers refresh the catalog or run jobs.

## Cross-cutting rules

- **Access.** The standard Compose setup binds to localhost and requires an owner login. Named household accounts and paired devices receive limited permissions. The server checks permissions at the HTTP boundary.
- **Extension trust.** The host manager verifies a signed catalog against the configured publisher key. Each entry names supported server versions and architectures and pins an image digest. The core container receives a manager socket and staging mount when extensions are enabled.
- **Long-running work.** The supervisor manages worker processes. The durable job queue records work so a restart can recover it. Audio streaming avoids holding the catalog write lock.
- **Verification.** Test domain behavior in `tests/`. Use the disposable browser room for UI behavior and `scripts/smoke_compose.py` for install, playback, pairing, upgrade, and restore checks.
- **Privacy.** Keep pairing data, credentials, databases, and installation details outside the public source and release artifacts. Use disposable data for screenshots and release fixtures.

## Architectural invariants

- For downloaded recordings, playback requires successful verification. The rules include codec and format policy, at least 192 kbps, duration within 5.5 seconds of the expected duration, and a matching title tag.
- Core owns verification, storage, retry state, and playback eligibility. Extensions supply candidates or files and can expose a folder the operator selects.
- Collection snapshots carry the catalog and original audio. Host backups carry private configuration and account state. Playlist exports carry portable lists and metadata.
- The shared queue, household playback, and per-device Radio maintain separate state. Switching Radio stations leaves the shared queue intact.

## References

`adrs/README.md` indexes the decisions behind this design. `SPEC.md` defines portable formats. `docs/public-release.md` describes how to verify and publish a release.
