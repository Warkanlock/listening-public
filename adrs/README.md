# Architecture decision records

These records explain why Listening works the way it does. See the [architecture overview](https://github.com/Warkanlock/listening-public/blob/main/ARCHITECTURE.md) for the current code map and data flow. The [library format](../SPEC.md) specifies the interchange contract. The [README](../README.md) covers installation and use.

| Record | Decision | Status |
| --- | --- | --- |
| [0001](0001-supervisor-replaces-tmux.md) | Supervise workers in separate processes | Accepted |
| [0002](0002-durable-job-queue.md) | Keep queued work durable on the host | Accepted |
| [0003](0003-radio-is-a-separate-per-device-surface.md) | Keep Radio local to the device | Accepted |
| [0004](0004-audio-store-seam.md) | Resolve file and database audio through one interface | Updated by 0005 and 0006 |
| [0005](0005-media-database-is-the-library.md) | Make the database the default home for audio | Automatic destructive migration superseded by 0006 |
| [0006](0006-collection-ownership.md) | Make the portable collection own music and playlists | Accepted |
| [0007](0007-backend-domains-and-state.md) | Share domain logic and transactional state | Accepted |
| [0008](0008-verify-and-locate-audio.md) | Verify recordings and resolve their audio consistently | Accepted |
| [0009](0009-portable-list-intake.md) | Import, merge and link portable lists | Accepted |
| [0010](0010-refresh-and-responsive-reads.md) | Keep refresh off the playback request path | Accepted |
| [0011](0011-paired-private-network-access.md) | Pair clients to a private home server | Accepted |
| [0012](0012-catalog-metadata-and-discovery.md) | Keep metadata enrichment and discovery separate from acquisition | Accepted |
| [0013](0013-local-playback-and-lyrics.md) | Share the queue and keep presentation local | Accepted |
| [0014](0014-web-client-and-rendered-verification.md) | Build and verify one React web application | Accepted |
| [0015](0015-server-installation-and-native-clients.md) | Use the listening CLI for hosting and client builds | Accepted |
| [0016](0016-private-installation-and-household-accounts.md) | Authenticate private rooms and keep member listening profiles separate | Accepted |

## Former plan numbers

Older code comments and commits use the following identifiers. Several plans implemented different parts of one decision. The decisions in this index document the current architecture.

| Former plans | Subject | Decision records |
| --- | --- | --- |
| 001, 002, 005, 007, 036, 052, 054 | Package layout, shared utilities, requests, configuration, catalog and SQLite stores | [0007](0007-backend-domains-and-state.md) |
| 003, 004, 009, 030, 058 | Verification, runner reliability, audio paths, supplied files and measured lengths | [0008](0008-verify-and-locate-audio.md) |
| 006, 018, 022, 025, 038, 055 | Refresh, watchdog, concurrency, paired-client load and the audio-folder signature | [0010](0010-refresh-and-responsive-reads.md) |
| 008, 029, 037, 047, 048 | Metadata providers, genre jobs, search filters and favorites | [0012](0012-catalog-metadata-and-discovery.md) |
| 010, 021a, 021b, 024 | Host-only actions, request validation, per-device credentials and reachable pairing links | [0011](0011-paired-private-network-access.md) |
| 011, 012, 013, 014, 027, 046, 053 | Import conflicts, owner naming, optional adapters, format documentation, links and merges | [0009](0009-portable-list-intake.md) |
| 015, 016, 017, 019, 042, 051, 056, 057 | Rendering, design tokens, service worker, test coverage, React conversion and settings panels | [0014](0014-web-client-and-rendered-verification.md) |
| 020, 023, 031, 032, 033, 034, 035, 041, 043, 044, 066 | Installation, updates, host state, service management, command-line entry points and native clients | [0015](0015-server-installation-and-native-clients.md), [0006](0006-collection-ownership.md) |
| 026, 028, 039, 045, 049 | Lyrics, artwork, transport, listening history and genre completion | [0013](0013-local-playback-and-lyrics.md), [0012](0012-catalog-metadata-and-discovery.md) |
| 040, 050 | Jobs console, supervisor, durable dispatch and release gates | [0001](0001-supervisor-replaces-tmux.md), [0002](0002-durable-job-queue.md), [0014](0014-web-client-and-rendered-verification.md) |
| 059 | Radio playback, settings and device isolation | [0003](0003-radio-is-a-separate-per-device-surface.md) |
| 060, 061, 062, 063 | Media adapters, default landing, export, stored probe tags, Radio controls and upkeep | [0004](0004-audio-store-seam.md), [0005](0005-media-database-is-the-library.md), [0006](0006-collection-ownership.md), [0010](0010-refresh-and-responsive-reads.md), [0013](0013-local-playback-and-lyrics.md) |
| 064, 065 | Collection assessment, source retention, ordered playlist linking, playback and restore verification | [0006](0006-collection-ownership.md), [0009](0009-portable-list-intake.md), [0014](0014-web-client-and-rendered-verification.md) |
