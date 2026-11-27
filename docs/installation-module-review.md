# Installation and module review

The 1.5 setup rehearsal exercised a clean Compose room, a server upgrade, recovery from the previous image, and a signed download worker. The room upgraded with the same storage mounted. A paired device kept access, and an imported track remained playable after removing the import folder. A separate download test let the worker fetch a public-domain track into staging and ran Listening’s audio checks on the result.

## Keep each interface small

| Module | Interface the caller can rely on | Decision |
| --- | --- | --- |
| Room setup | Choose an owner, room address, and optional music folder. The setup command probes the folder and creates the import list. | Keep the CLI as the setup entry point. The [illustrated guide](onboarding-walkthrough.md) follows its actual prompts and shows how to connect a device. |
| Library and storage | Import and play verified recordings. Portable track lists carry recording references, and collection snapshots carry the recordings for restore. | Keep one authoritative library on the server. Temporary downloads and offline copies have separate lifecycles. The verified recording remains the unit of storage. |
| Playback and devices | Choose an output, use the queue, and resume from the server’s saved playback state. | Keep shared playback logic on the server with client adapters for audio output and offline storage. A paired client can reconnect after a server upgrade. |
| Acquisition | Send a recording to search, then pass a returned candidate to retrieval. The worker writes into staging and the room verifies and stores the result. | Keep separate search and retrieval choices. The signed catalog, credential mount, and worker lifecycle belong to the host manager. The example plugins implement the same protocol. |
| Discovery | Find recordings, artists, and related music, then submit a recording reference for playback or retrieval. | Keep source lookup in discovery adapters. An artist graph, a mood, and a radio station can each have different discovery behavior. They can hand playable recordings to the same player. |
| Control and packaging | Inspect or configure a room as owner, and install the clients published for its release. | Keep owner actions together in Settings and expose release-matched installers from the room. The host retains Docker access and signing credentials. |

## Fixes from the rehearsal

1. Make release images work when the build context comes from a private directory that masks file access. Keep application files owned by root and give the unprivileged server or worker the read and traversal permissions it needs.
2. Put the verified setup screenshots on [About](../frontend/react/about/index.html) and link them from the install guide. Show the private network bind and the advertised address together, and name the exact pairing action.
3. Verify a worker in an isolated test first. Readiness checks the executable and configuration. An authenticated transfer and Listening’s audio checks verify retrieval into staging. Run a room job before moving a live queue, following [the engine upgrade guide](download-engines.md#upgrade-a-room-with-an-existing-engine).
4. Validate a rollback with the same collection, then use the room’s existing mounts for an upgrade. Keep the old image and the recovery copy until the upgraded room has passed its checks.

The next place to deepen is the owner’s extension interface. The CLI and browser already share it through the HTTP handler. When there is another lifecycle operation to support, move validation, readiness, and selection from that handler into the downloads module. Test both callers through the same interface. Keep Radio and metadata lookup separate because each has a different job and failure mode.
