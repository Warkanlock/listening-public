# Listening Room operations

[Install a room](private-install.md) · [Project overview](../README.md)

## Playback and devices

### Listen on the go

On Android, join the room’s private Wi-Fi and open **Save for offline → Connect over Wi-Fi** in Listening Room. Paste a private invitation from the room’s Devices settings on the first connection. Pick a playlist and grant write access to the microSD card’s **Music** folder. Once the playlist is ready, it plays from **Device files → Saved playlists**. Open the same playlist again to pick up changes or finish an interrupted save. Use an invitation for an address reachable from that Wi-Fi network.

**Mobile audio** defaults to **Original**. Select **Compact Opus** on Android for smaller files, or **Compact AAC** for broader player compatibility. In the browser and Apple clients, enable **Settings → Playback → Compact mobile audio** for AAC streams. As a rough guide, an hour of encoded music uses about 58 MB in Opus or 86 MB in AAC. Actual size depends on the source. The server keeps a bounded cache of Compact copies and can reuse a compatible, already-small original.

To make a compact copy from a playlist, select the format beside **Export playlist**. **Settings → Storage** offers the same choice for the library. Compact exports use their own format folders in the configured export destination. Playlist exports contain the audio and an M3U8 in playlist order. The original export still recovers the exact source files.

```sh
listening export start --playlist <playlist-key> --format opus
listening export status
```

### Run a development room

From an installed launcher, run:

```sh
listening server start
listening server status
listening server restart
listening server stop
```

Start prints the room address after the server answers. Use `listening server start --foreground` for a service or container. A checkout with the development prerequisites can install the launcher and built web app with `./listening setup`. The installer places the command in `~/.local/bin`. Existing systemd users can select their installed user service with `listening server configure systemd`.

The root Compose file publishes to localhost by default.

For an existing Compose installation, run `listening server configure compose --file /path/to/docker-compose.yml --project PROJECT` once on the host. Use the project's actual Compose name and add `--port PORT` for a nondefault published port. The lifecycle commands then operate on the Listening Room service in that project. Use `listening server configure local` to return to native hosting.

### Use the terminal

Run `listening` for the command guide or `listening COMMAND --help` for a specific command. `listening --version` prints just the installed version.

| On the server | What it does |
| --- | --- |
| `listening status` | Show server health, address, catalog song count, verified songs, queued requests, and paired device count. Exits with status 1 if stopped or unhealthy. |
| `listening invite` | Print a private pairing link for this room. Copy it to a trusted device. |
| `listening pair list` | List registered device IDs and their presence. |
| `listening pair rotate` | Replace the invitation. The old link expires immediately. Registered devices keep their access. |
| `listening pair revoke DEVICE_ID` | Disconnect that device. Rotate the invitation too if the device still has the link. |
| `listening settings` | Show the running room’s download, storage, and folder settings. |
| `listening settings get audio-store` | Print one setting. |
| `listening settings set auto-download off` | Change automatic downloading. Other writable keys are `audio-store` (`database` or `files`) and `share-folder` (a directory on the server). Portable rooms always store audio in SQLite. |
| `listening config` | Show the launcher's selected configuration path, manager, and service details, even while stopped. |
| `listening config manager systemd` | Select an installed server manager. Stop the current server before changing managers. |

On a phone or Mac, paste the invitation into **Connect to a room**. On a computer with the CLI, run `listening join` and paste the link at the hidden prompt to open the browser player. Treat the invitation as a password. The default bind address (`0.0.0.0`) supports both owner commands on the server and invitations on the network. A loopback-only listener serves local clients. A listener bound exclusively to a LAN address reports reachability, but requires a restart with loopback access for owner commands.

For scripting, append `--json` to status, pairing, settings, and configuration inspection commands. For example, `listening pair list --json` prints device records, and `listening config path` prints just the selected launcher path. With Compose hosting, room commands run inside the selected service and invitations use the published host port. Use a server image with the same CLI commands.

### Browse by mood and discover music

Open **Mood map**, or **Explore → Mood Map** on Android. Tap a mood to start playing, or drag between moods and release to play a blend. With a keyboard, focus **Blend moods**, use the arrow keys, and press Enter to play. On Android, TalkBack offers directional actions on the map. Use the regular player to pause, skip, or switch devices.

Open **Map settings** to choose a genre. On the web, Mac, and iPad, you can also adjust brightness and energy, play your selection, or change **Tune from** to YouTube or a public catalog. Room owners can select a single mood and use **Edit genres** in Map settings to change the genres it draws from.

**Deep Dive** offers weekly picks based on favorite artists. On Android it lives under **Explore → Deep Dive**. Tap a pick to see why it was recommended. **Find a copy** saves a request, and **Confirm and search** starts the search in the lossless format you selected.

### Build clients

From a checkout on a machine with the corresponding build tools:

```sh
listening build apk
listening build dmg
listening build ios
```

The builders print the versioned artifact path and write its SHA-256 checksum alongside it in `dist/clients/`. Use `--output DIR` for a different destination or `--source DIR` to select a checkout. Building clients works with the room stopped. See [the Android client](../clients/android/), [the macOS client](../clients/macos/), and [the iPhone and iPad client](../clients/ipad/) for platform requirements.

On iPhone or iPad, open the server invitation in Safari or install the app with Xcode. Both use iOS media playback for listening while the screen is locked or another app is open. The default iOS build produces a Simulator app. To install on a physical device, configure Apple signing and use Xcode or export a development IPA.

To let the browser reopen a saved Camping trip offline, provide a trusted HTTPS address using the [private reverse proxy setup](private-install.md#private-https). Require an explicit owner sign-in and preserve the incoming `Host` header when the proxy connects to Listening. Set `LISTENING_ROOM_HIKING_ORIGIN` to that bare HTTPS origin to offer the link on the HTTP room. Sign in on the HTTPS address with an owner or household account. To issue invitations for the HTTPS origin, use the canonical `LISTENING_ROOM_ORIGIN` setting from the reverse proxy setup. On iPhone or iPad, open the HTTPS address in Safari and add it to your Home Screen before downloading a trip.

### Preserve or move a collection

A new empty installation starts with a portable SQLite collection. Use `listening collection create /new/room` to choose another empty directory. Use `listening collection open /existing/room` to select a verified portable room. The next server start opens it using its own application folder.

Preview an existing room before copying it. The destination argument selects the disk for the free-space check. The command writes its report to standard output and leaves the source and destination alone.

```sh
listening collection preview /destination/backup.sqlite3 --check-audio > preview.json
```

The report separates missing references and unreadable audio from catalog entries still awaiting a copy. The payload estimate measures the distinct audio bytes after reversible compression. The free-space check adds the source input size, a second input-sized allowance for encoding and SQLite transactions, and a 20 GiB reserve. Only the finished database occupies space permanently. Checking an active SQLite journal can require a temporary copy in the system temp directory.

When ready to move, stop active jobs and the server, then run these commands in order. Snapshot verifies the stored audio and records skipped files. Restore requires a fresh destination. Opening the restored room selects it for the next server start.

```sh
listening collection snapshot /destination/backup.sqlite3
listening collection restore /destination/backup.sqlite3 /new/room
listening collection open /new/room
```

The original room and its source files remain available. If copying is interrupted, repeat the snapshot command with the same destination and unchanged sources.

### Your first run

The room creates its configuration at first start. For a private Compose room, [set the owner login and open the room](private-install.md). For a source installation, open <http://127.0.0.1:8765> and choose **Settings → Room setup**.

1. **Play your music.** Follow [Private installation](private-install.md) to bring in a music folder or generate a playable demo with `listening music demo`. If you already have a playlist, **Sources** accepts CSV, M3U, or XSPF.
2. **Connect another device.** Copy an invitation from **Settings → Devices** and open it on the device you want to use.
3. **Find missing music if you wish.** Set up Soulseek in **Settings → Downloads**, then request a copy from the catalog. The optional downloader is bundled in the full container image on amd64.

The listening directory may start empty. The app creates its folders when you
save setup.

### Bootstrap a home server

Run `scripts/bootstrap.sh` from a checkout with access to its origin. To select a different source repository, use `--repo GIT_URL`. It installs the current `main` branch under `~/listening-room`, builds the web app, and registers the CLI. Add `--downloads` for the checksum-verified Sockseek binary. Use `--dry-run` to inspect its actions. Subsequent installs fast-forward a clean checkout. Set the owner with `listening account set-owner OWNER`, then start with `listening server start --host 127.0.0.1`.

### From source (developers and forks)

Install Python 3.9 or newer, Node 22.12 or newer, and Yarn Classic. From the checkout:

```sh
./listening setup
listening server start
```

In a terminal, `listening setup` walks through hosting the server and browser player, building the Android APK, and building the Apple silicon DMG. Select one or more components. Hosting lets you choose a room and start it immediately. Existing rooms keep their settings. On a bundled Mac installation, the server and web player are already built.

For unattended setup, use `listening setup --client web --room /path/to/new-room --start`, or `listening setup --client apk --client dmg --output /path/to/installers` on a build machine with both toolchains. Add `--non-interactive` for the default host install without prompts.

Source setup builds the web app and registers `~/.local/bin/listening`. Add that directory to PATH as directed. To enable downloads, run `listening setup --downloads` to provision the checksum-verified Sockseek tool. Developers can still run the Make targets for development, testing and the rendered UI gate. The server accepts `--port` (default 8765) and `--host` (default 0.0.0.0).

### If something is missing

Three layers tell you what is wrong, in order:

1. The preflight and the launchers check before starting: `python3`
   and `ffprobe` are required or warned about, and each
   message names the fix.
2. The server starts regardless. Settings → Downloads shows the live
   dependency checklist.
3. The download and sharing workers refuse to start until every dependency
   is present and Soulseek is connected; Settings → Downloads and the
   Jobs view show which piece is missing.

### Upgrading and rolling back

New rooms keep their catalog and audio in the room’s `collection.sqlite3`. The application folder holds host settings, jobs, caches and credentials. Legacy rooms retain their existing layout until you select a verified copy.
How you upgrade depends on how you installed:

- **DMG:** Replace Listening Room.app on the server with the app from the new DMG.
- **Homebrew:** `brew upgrade listening-room`. The in-app update
  card defers to it.
- **Docker:** With the Compose host updater enabled, choose **Settings → Room setup → App updates** as the owner. The host builds the latest revision from `main` and restarts the room. Registry and archive installs use their existing image update procedure. [Compose updates](private-install.md#update-a-source-built-compose-room-from-the-browser)
- **A checkout (including a bootstrap install):** the App updates
  card in Settings → Room setup, or pull the new code and run
  `./listening setup` again.

**Upgrading from a pre-1.2.0 version:** stop the workers in the Jobs
view before you update. Earlier versions ran the workers in tmux
sessions; the new server supervises its workers itself and does not
adopt ones the old server started, so a worker left running would
keep going unmanaged alongside the new server.

The first run after upgrading moves any state still sitting in the
library folder into the application folder, and imports any
pre-database state files (manifest, coverage, genres, cursor,
probe cache) into the database, count-checked, keeping them in
the application folder's `state-backup/` as a backup. Rolling back
is returning to the previous package or code, and backing up is
copying the library and application folders.

### Settings at a glance

| Tab | What you do there |
| --- | --- |
| Room setup | Check playlist sources and download readiness, open Jobs, and update the app. |
| Sources | Find local list folders, watch folders, link a single playlist file, import files, and check for changes. |
| Downloads | Connect Soulseek and choose whether to download new songs automatically. |
| Spotify | Connect, sync, or disconnect your Spotify account. |
| Playback | Set crossfade and volume normalization on this device. |
| Radio | Choose the room’s SomaFM stations and manage saved YouTube mixes. |
| Devices | Pair browsers, name them, and send announcements. |
| Sharing | Offer a folder back to Soulseek with Nicotine+. |
| Storage | Change the Library folder and export or import host settings. |

Paired web and Mac clients have access to Playback, Radio, and Devices. Open Settings on the server to manage the room.

Source discovery checks common locations under Downloads, Music, Documents,
and Desktop on the **server computer**. It reads likely list headers locally
and excludes the app's own library folder. You choose which found folder to watch;
discovery does not automatically import unrelated files. You can watch up to
20 folders. Stopping a watch keeps the lists already copied into your library.
To keep a single file linked, enter its path in **Sources → Linked playlists**.
When you stop linking or the source file disappears, the last imported copy stays.

### Upgrading an existing CSV library

On the first launch after upgrading, Listening Room copies CSV lists from the
old configured CSV directory into XSPF files under the library's `lists/`
directory. The source CSV files remain where they were. Existing audio and
M3U8 export paths stay configured; catalog IDs and verified matches remain
attached to the same tracks. No path changes are needed unless you move files.
See [SPEC.md](../SPEC.md) for the field mapping
and import behavior. This upgrade path is legacy; the migration details live
in the appendix of [SPEC.md](../SPEC.md).

## Bring in your music

Every route in ends in the same place: portable XSPF lists in your `lists/`
folder and one shared catalog.

1. **Import files in the app.** In **Sources**, import CSV (any tool's
   export that matches the criteria in SPEC.md), M3U/M3U8, or XSPF files,
   or a whole Listening Room ZIP bundle. A browser file import is a
   one-time copy.
2. **Drop files into a folder.** Put list files in the `lists/` folder, or
   in any folder you ask **Sources** to watch. Listening Room recognizes
   song-list files by their contents, copies them into the portable
   library, and checks watched folders for changes automatically. The
   original files stay in place. You can watch up to 20 folders.
3. **Link one playlist file.** Enter the host path of a CSV, M3U, or XSPF
   file under **Sources → Linked playlists** when you want edits to that
   file to keep updating the local playlist. When you stop linking or the
   source file disappears, the last imported copy stays.
4. **Connect an adapter.** An adapter mirrors an account into list files
   on a schedule. The Spotify adapter below is the one that exists today.

### Adding a song before you have the audio

Open the playlist in **Library**, select **Add song**, and enter the artist and title. An album helps distinguish recordings with the same name. You can also add an existing library song from its actions or a song result from **Browse catalogs**. The playlist stores the recording details immediately. A song becomes playable when the room verifies an audio copy. **My playlists** also accepts entries that are still awaiting audio.

A magnet labeled **Spotify** shows the connection state. **Two-way** means this room can send an explicit addition to Spotify and import later changes from Spotify. If the artist and title identify several recordings, paste the intended Spotify track link. A grouped playlist with several writable Spotify sources asks where to send the addition. A read-only, paused or disconnected source shows its state. Owner Settings provides account reconnection and per-playlist linking. Watched files and folders have a different source label.

Owners can add from any browser or Apple client signed in to their account. To authorize a paired player such as Android, open **Settings → Devices**, locate the device, and allow playlist additions. That device can add to shared editable playlists, including the owner's writable Spotify links. Revoking the permission takes effect on the next request. Edits need a connection to the room. Offline saves continue to play their previously downloaded contents.

### Merging playlists

If imports leave you with overlapping lists (the same playlist exported
twice, a liked-songs list beside the playlist it came from), open the
playlist shelf and choose **Merge**. Select the lists to combine, then
merge into one of your own lists or a new one. The merged list holds each
song once, in order, and is an ordinary portable list file. Playlists
with a 🧲 beside the name are kept in sync from their source (an adapter,
a linked file, or a watched folder): they can be merged from, but not
into, because the next sync would overwrite whatever was merged into
them. Source lists are never changed or deleted by a merge.

### Already have the audio files?

If the songs on your lists already live on this computer, you do not
need to download anything. Import a list (any route above), then point
**Audio files** in Settings at the folder holding your audio. Choose
**Check for changes** in Sources, or just wait for the next automatic
check. Listening Room probes each file's tags, matches it to a catalog
song by artist, title, and album, and applies the same verification as
a download: a matched song that passes becomes playable where it sits.
Files are never renamed or moved, files that match no song are left
alone, and a file whose tags fit two different songs is skipped rather
than guessed.

### Spotify adapter (optional)

To leave Spotify without connecting its Web API, request your personal account data and convert the ZIP or its Playlist and YourLibrary JSON files locally. Import the resulting ZIP in **Settings → Sources**. Saved tracks appear as a playlist. The converter reports unsupported entries and keeps their source export intact.

```sh
listening spotify convert spotify-account-data.zip listening-playlists.zip
```

The library works fully without this adapter; connect it only if you want
your Spotify account mirrored into the local catalog. Spotify sync is
optional. In the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard),
create a Web API app for your own account. Add the Redirect URI shown in
Listening Room's **Spotify** tab exactly, such as
`http://127.0.0.1:8765/api/spotify/callback`. Enter the app's Client ID in
the **Spotify** tab and choose **Connect Spotify** on the host computer. Each person
running a fork can enter their own Client ID and connect their own account.
Spotify's development mode requires a Premium app owner and allowlisted users.

The adapter reads your saved tracks and playlists you own or collaborate on. To edit them from Listening, reconnect once after upgrading and grant Spotify’s library and playlist modification permissions. Older read-only connections can keep importing. Authorization uses PKCE. Tokens stay in the host’s private application folder.

On the server computer, open **Settings → Spotify → Manage playlists**. Choose **Liked Songs** or an individual playlist. **Unlink playlist** keeps the last local copy and pauses changes in both directions for that playlist. **Link playlist** refreshes it from Spotify. New eligible playlists are linked on discovery. The watchdog checks every 15 minutes, or you can choose **Sync now**. Followed playlists that Spotify restricts to their owner or collaborators can still be imported from a playlist export.

From the same panel you can create a private Spotify playlist, rename one you own, and add or remove songs. Search for a catalog recording with a Spotify ID or paste its Spotify track link. Adding an existing song leaves its position unchanged. Removing a song removes its occurrences from the selected playlist. Editing **Liked Songs** saves or unsaves the track in your Spotify library. The heart in Listening continues to control your room’s favorites and Deep Dive seeds.

Listening refreshes its mirror after an edit. Spotify-originated changes are detected by playlist snapshots and liked-song checks, with a daily full liked-song read. A temporary empty playlist listing preserves previously imported sources.

For an imported copy of the same playlist, **Settings → Sources → Playlist matches** shows the available groupings. The same explicit Spotify playlist ID groups automatically. Similar membership produces a suggestion. You can also group two sources manually or undo a grouping. Grouping combines their songs on the Listening shelf and preserves both original sources. It does not publish the imported list to Spotify.

The existing bulk-download setting still controls whether the downloader
automatically searches new catalog entries.

## Radio

Choose a library genre to shuffle verified songs on this device. In the browser and Mac app, the station plays each song once before reshuffling. Android can play a shuffled library station while connected to a room. Generated white or brown noise is available in the web player.

Room owners can enable **YouTube mixes** and **SomaFM** independently in **Settings → Extensions** or with `listening extensions radio`. To choose SomaFM channels, open **Radio → Manage SomaFM** or **Settings → Radio**. Add a channel, use the menu next to a station to change its stream, or remove a station, then choose **Save SomaFM settings**. On Android, the Radio shortcut opens the room’s settings in your browser.

For a YouTube mix, add the videos in the room’s Radio settings. Configuring a YouTube API key also enables search for a room that has opted into YouTube. Disabling a provider hides its stations and blocks starting or resuming them. Saved station definitions are available again when that provider is re-enabled.

## Browse catalogs

Open **Browse catalogs** and search for music by title, artist, or genre. Choose **Public catalogs** for Apple Music, Internet Archive, and PeerTube, or **Private catalog** for a connected Jellyfin library. Some catalogs accept **Browse** with an empty search field. Results open at the source. [Connect a private catalog](private-install.md#connect-catalogs-and-radio-search)

From an owner session, open **Manage discovery sources** to add an Internet Archive collection or a public PeerTube or Funkwhale server. Custom network sources use HTTPS and must support access without signing in. Paired devices and household members can browse the configured sources.

The room contacts a public catalog when someone submits a search or browses it. Public search APIs may receive the entered terms. Selecting a result opens its source.

## Profiles and playlist sharing

Use **My playlists** to build playlists from the library that only your household login can see. **Play privately** loads the playable tracks into your own queue. Owners can select **Use shared queue** to switch back to the room's queue. Household profiles are included in a host backup.

Sign in as the owner and open a playlist from **Library**. Choose **Share playlist** to preview its tracks, download an XSPF for another room, or copy text for a post. To publish a playlist page people can import and announce updates through an existing Mastodon account, follow [Share a playlist on the Fediverse](fediverse-playlists.md).

Clients that support OpenSubsonic API keys can browse and stream verified music on the same private network. [OpenSubsonic setup](private-install.md#connect-an-opensubsonic-client)

## Library layout

A new portable room keeps its catalog, ordered playlists, coverage, genres, play history and owned audio in one file. Identical audio bytes share a content hash. SQLite stores a losslessly compressed payload when that saves space. Playback restores its original bytes. SQLite can create journal sidecars while the server is running.

```text
Listening/collection/
  collection.sqlite3
```

The **application folder** holds what belongs to this host, including its settings, pairing credentials, jobs, logs and playback cache. Rooms selected with the CLI use a private folder under `rooms/`. The working lists, audio inbox, generated playlists and reports of a new room live in its host workspace. Watched folders and explicit exports stay at the paths you choose.

- New macOS installations keep the collection under `~/Library/Application Support/Listening/collection/` and private runtime data under `Listening/host/`. An existing `ListeningMusic` host or `~/Music/ListeningRoom` library retains its configured paths until cutover. The Mac app bundles its interpreter, web player and audio tools.
- On Linux the default is `~/.config/listening-room/`.

Existing rooms can still have `library/`, `lists/`, `playlist/`, `offline_playlists/` and `reports/` alongside an application-folder `requests.sqlite3`. Listening reads those paths using the saved settings. To consolidate an existing room, follow **Preserve or move a collection** above. The original media, old exports, backups and machine-specific credentials stay where they were.

The config path can be overridden with the `LISTENING_ROOM_CONFIG` environment
variable. Credential files are written with `0600` permissions. Never commit a
personal application folder, audio library, credentials file, or server log.

## Moving to a new computer

1. On the old host, choose **Export library** in Settings → Storage and
   save the ZIP.
2. On the new host, start the app and import the ZIP in **Sources**. This
   restores the XSPF playlists without old computer paths.
3. If you also want to reuse settings, choose **Export host settings** on the
   old host, then **Load settings JSON** on the new one. Review every path
   before saving it.
4. Enter that host's Soulseek credentials, because credentials and
   the pairing token are never exported. Sockseek itself is
   provisioned by the install.
5. Copy your music directory separately if you want to keep the downloaded
   library. The export intentionally contains no audio.

## Sharing with Soulseek

Sockseek 3.x [does not upload files](https://github.com/fiso64/sockseek/blob/master/README.md),
so downloading and sharing are two separate mechanisms here.

To share: install [Nicotine+](https://nicotine-plus.org/), pick a share folder,
and enter a **separate sharing account** in Settings → Sharing. Listening Room runs an
isolated headless Nicotine+ profile for that folder only, and Settings reports
whether it is running. A separate account avoids the connection conflict that
occurs when two clients use one Soulseek login at the same time.

Only share files you have the right to distribute.

## Devices and installation

The server owns the collection and performs imports, exports, and other file operations. Sign in as the owner to administer the room. Paired devices and household members have their own access limits.

Copy an invitation from **Settings → Devices** to a device on your private network. The link carries a pairing token that the server exchanges for an
`HttpOnly` cookie. The token remains valid while it is in the host config, so
treat the link as access to your library. After pairing, that browser uses its
cookie. Paired devices can browse, search, request songs, and control
playback. Browsers may require one local tap on Play before audio is allowed
to start.

In an owner session, **Settings → Devices → Send an announcement** sends a
short message to everyone or one currently connected device. Paired browsers
show new messages in an in-app notification; the envelope in the top bar opens
recent announcements. The server keeps the latest 50 messages for up to 24
hours in memory, so announcements clear when the server restarts. Notifications
appear while the Listening Room page is open; they do not require browser or
OS notification permission. Device selection routes a message to that
browser's inbox, but it is not a private-messaging system; do not send secrets.

The shared queue supports drag to reorder, move and remove buttons, and
**Play next** from each song’s menu. The server saves the current song,
position, and full queue in its private configuration folder so a restart
can resume the session. Press `?` for shortcuts: Space toggles playback,
`Ctrl/⌘ K` focuses search, left and right arrows skip tracks, and `Q`
toggles the queue.

Listening Room is a PWA. On the host, open the localhost page in Chrome or
Edge and choose **Install app** from the browser menu. The app shell installs
and is cached by a service worker, while audio and library APIs still need the
local server to be running. PWA installation on other devices generally
requires an HTTPS origin; an ordinary LAN `http://` link still works in a
browser.

The server only answers requests under hosts it means to serve: IP addresses,
`localhost`, `.local` names, the configured room origin, and any names listed in the `allowed_hosts`
setting in the config file. Anything else is refused before pairing or
authorization, which closes the DNS-rebinding hole where a hostile website
drives a browser at your server under the site's own hostname.

## Private network access

Pair on home Wi-Fi or on a private Tailscale or WireGuard network you manage. Set `LISTENING_ROOM_ORIGIN` to the address clients can reach. New Compose rooms require an explicit owner login. To opt in from another installation, run `listening account set-owner OWNER` on the server. Keep the room port private. Configure an HTTPS origin for browser administration away from localhost and follow the private deployment guide before using a proxy. [Private installation](private-install.md)

## What leaves this computer

Listening stores the music collection, host settings, and account data on your server. It makes network requests for the features you use:

- **MusicBrainz** provides recording metadata, search, and Song DNA. **ListenBrainz** provides related artists for weekly Deep Dive recommendations.
- **LRCLIB** receives track names, artists, albums, and durations when you look up lyrics.
- **Public catalogs** receive search or browse queries you submit. Private catalog sends queries to the Jellyfin server the owner configured.
- **Radio** streams library songs from your room or connects to SomaFM. A saved YouTube mix opens a YouTube embed. If YouTube discovery is configured, the server sends Mood map discovery queries to the YouTube Data API. White and brown noise are generated on the device.
- **Artwork hosts** serve images when imported metadata includes album-cover URLs.
- **Spotify** receives authorized requests after you connect an app for playlist sync and editing.
- **Optional downloading and sharing** use Soulseek through the external tools you configure. Searches may reach other peers, and enabling sharing makes files from the selected directory available to those peers.
- **Fediverse sharing** is an explicit export. Publishing with the CLI sends the playlist title, track count, and public page link to your chosen Mastodon instance. [Fediverse sharing](fediverse-playlists.md)

## Troubleshooting

- **Settings says a dependency is missing.** The Downloads tab shows a live
  checklist for `ffprobe`, Sockseek, and Nicotine+. Downloads only
  report ready when credentials exist, the Sockseek binary is present and
  executable, and `ffprobe` is found.
- **Nothing is downloading.** Open the Jobs view. It shows
  whether the bulk and request jobs are running; the activity view
  (click the progress strip) shows their latest output. Bulk also pauses itself when free space approaches the 20 GiB
  reserve, and the activity bar says so.
- **A song will not play.** Only verified files can play. Find the track in
  the coverage table of the application folder's `requests.sqlite3` (or in the status report
  in `reports/`) to see the exact rejection reason (bitrate, duration,
  or title mismatch are the usual ones), or use **Retry search** on the
  failed request.
- **A familiar song is missing.** Search the library and use **Find copy** on
  its row, or **Find copies shown** for up to 50 visible missing tracks. A
  request first looks for a specific album and track, checks candidate duration
  and bitrate, then tries a broader search when needed. Transfers that exhaust
  Sockseek's memory, file handles, or connections appear as retryable errors
  instead of “not found.” Tracks with the same ISRC share a verified local copy
  across imported and Spotify lists when their title and duration agree.
- **A paired device gets "Open this app using the paired link".** Open the
  exact link from Settings once on that device and browser, so it can set its
  pairing cookie.
- **Downloads seem stuck after a crash.** Restarting the server is safe. Its
  background watchdog re-syncs changed lists, returns interrupted requests to
  the queue, and resumes bulk from the saved cursor instead of starting over.
- **Two Soulseek clients fight over one login.** That is expected. Keep the
  searching account and the sharing account different, as described above.

## Optional downloads

### To find new audio

| Need | Why |
| --- | --- |
| FFmpeg (`ffprobe`) | Verifies every downloaded file before it can be played. |
| Sockseek executable | The Soulseek search and download CLI. A separate project; the package routes fetch the pinned release and verify its checksum during installation. Developed against Sockseek 3.x (current release: 3.0.6). |
| A Soulseek account | Used by Sockseek for searching and downloading. There is no sign-up form: choose a username and password and sign in with the official client from [slsknet.org](https://www.slsknet.org), or with Sockseek itself; if the name is free, that first sign-in creates the account. Sharing needs a second, separate account. |
| Nicotine+ (optional) | Only needed if you want to share a folder back to Soulseek. |

The full amd64 server image ships with Sockseek at `/opt/sockseek/sockseek`. Add your Soulseek credentials in **Settings → Downloads**. The CLI transfers into private staging under `/config/bulk-incoming` for bulk jobs and `/config/requests` for individual requests. SQLite rooms release the transfer file after verifying the stored copy. Identical audio shares one SQLite payload. Playback uses a small rolling buffer, and exports create files at the destination you choose.

When a song has no playable copy, **Find on YouTube** opens an artist-and-title search from its details. You can also use it on a failed request or a Deep Dive pick.

For an individual song, choose **Find copy** or enter its artist and title in **Requests**. The room saves the request immediately. Choose **Confirm and search** to use the selected format. New requests start with **Any lossless (prefer FLAC)** selected. Choose **MP3 or AAC (prefer MP3)** to accept MP3 or AAC in an M4A file for that song. You can also select **FLAC only**, **Apple Lossless (ALAC) only**, or **MP3 only**. Close the picker to choose later from **Requests**, including after a restart. After an unsuccessful search, **Change format** lets you choose again. **Try MP3** is a shortcut after a lossless search finds no copy.

Bulk downloads prefer MP3 among matching results and also accept FLAC or Apple Lossless (ALAC in M4A). Change the room default in **Settings → Downloads**. In **Jobs**, choose a format for one run and the playlists to search. The room default applies to future batches when a job uses **Room default**. A job with a format override keeps that choice while queued and running. Existing recordings stay in their original format.

Individual requests keep their confirmed format. Older requests without a format choice still search for FLAC or Apple Lossless. The room checks the downloaded stream’s codec and container with ffprobe, then verifies the bitrate, expected duration, and title. Portable rooms store the original encoded file and its tags in SQLite. Legacy rooms use their configured landing mode. A peer may have transcoded a recording before sharing it.

Use **Settings → Storage → Export library audio** to recover the original media bytes with their original format extensions. A whole portable-room export includes stored audio without a current track reference under `exported/originals/`. Playlist exports follow the selected playlist’s track order.

Choose the full container or provision the optional downloader with
`listening setup --downloads` in a source checkout. On
macOS, `brew install ffmpeg` supplies the audio tools. On
Debian or Ubuntu, `apt install ffmpeg`; on Fedora, `dnf install
ffmpeg`. Windows is not supported natively; WSL2 works and is
treated as Linux.
