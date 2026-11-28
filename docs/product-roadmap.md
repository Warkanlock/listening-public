# Listening private release roadmap archive

## Goal

This document records the private preview plan. For the current feature set, see the [README](../README.md). For current release steps, use the [public release runbook](public-release.md).

Give a household its own music across browsers and phones, with separate listening profiles and playback that works away from the server. The software should be installable from a private checkout or private release artifact. The operator supplies the server, the music they can use, and any network or service accounts they choose.

The first release should prove that a new household can create a room, hear its own music, pair a phone, save a playlist offline, upgrade, and recover the collection on another machine. The second should prove that two people can use the room without mixing their favorites or playback history. These are acceptance targets. We have no external usability or adoption measurements yet.

## Priorities and release checks

Effort estimates are relative. Reach reflects how much of the intended household audience benefits. Confidence reflects what the current implementation already supports.

| Stage | Initiative | Reach · confidence · effort | Release check |
| --- | --- | --- | --- |
| Now | Private installation and networking | Everyone · high · medium | Build and start on amd64 and ARM64, play from a desktop and another device, and verify that the default installation publishes only on loopback. The operator chooses their own hostname and LAN, Tailscale, or WireGuard setup. |
| Now | Bring your own music | Everyone · high · medium | A new room plays the bundled demo and an imported tagged track before any third-party account is configured. Artist, title, album, and original audio survive export. |
| Now | Diagnostics and recovery | Everyone · high · medium | `listening doctor` provides actionable checks without printing secrets. Automated verification plays after a restart, upgrades the previous release, and restores a disposable collection into a fresh room. |
| Next | Explicit owner authentication | Every networked room · high · large | Requests forwarded to loopback have no owner access without an owner session. Existing paired clients can still listen. Setup, recovery, session revocation, and HTTPS are verified. |
| Next | Household accounts | Multi-person homes · high · large | Two users share stored audio while their likes, history, private playlists, resume position, devices, and integrations stay separate. The existing profile migrates to the first owner without losing data. A shared room queue is a deliberate mode. |
| Next | Portable playlist sharing and listening integrations | People migrating or sharing · high · medium | Export a selected playlist's metadata and import it in another room. Spotify remains optional for migration. ListenBrainz recording and playlist exchange are opt-in and scoped to the signed-in member. |
| Later | OpenSubsonic compatibility | Third-party client users · medium · large | A read-only scoped credential browses the music catalog and streams or seeks with an existing client. It cannot use room administration, private profile data, or unpublished collection endpoints. |
| Available through Mastodon | Follow public playlist updates | Fediverse users | Publish a selected metadata-only playlist page and announce editions through an existing account. Followers receive posts through their Fediverse client. |

Build the install, demo, and recovery work first because every path depends on it. Playlist export requires access only to the selected metadata. Explicit authentication precedes proxy support and member-scoped integrations. OpenSubsonic depends on authenticated streaming credentials. A public playlist page and an existing Fediverse account let people follow updates without connecting to the room.

## Product and implementation decisions

- Keep the default room usable with internet access disconnected. External services require an explicit action or configuration. For Tailscale and WireGuard, use the operator's existing private network and client apps. Listening stores an origin and validates hostnames. VPN installation, keys, network membership, firewall policy, and router configuration stay with the operator.
- Run release automation against disposable rooms. Keep private install archives restricted to authorized users. The versioned image workflow publishes from a public fork only after an explicit tagged release run. Verify source and container installs with an original byte-for-byte export and an offline phone save before calling the release ready for new households.
- Keep the shared media payload content-addressed. Give owner and member identities stable IDs. Migrate unscoped personal data to the first owner and make the migration resumable. Store salted password hashes in the private application database and use separately revocable sessions and device credentials. Verify role boundaries across HTML, the API, native clients, playback, and offline replicas.
- Start Fediverse sharing with an explicit export. Include a preview, ordered track metadata, and public recording identifiers where available. Let the owner upload the result or post using an existing Fediverse account. An export contains no room address, credentials, audio, or listening history.

## Verification before private distribution

Run the repository's backend, database isolation, browser, and native client checks for the changed surfaces. Exercise a new and upgraded install with fresh volumes, Android offline playback, and a room restored from a verified snapshot. Record which platforms and network routes were tested on physical devices. Test automation and local builds use disposable fixtures. Publication and changes to a deployed household room require a separate release action.

## Current delivery

The private release includes the read-only OpenSubsonic API, account-only playlists, and offline music on compatible devices. [Fediverse playlist sharing](fediverse-playlists.md) uses a static page and an existing Mastodon account. [Versioned images](release-images.md) can be published by maintainers of public forks.
