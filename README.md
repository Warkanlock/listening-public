<div align="center">
  <img src="frontend/react/public/assets/icons/favicon.svg" alt="" width="96" height="96">
  <h1>Listening</h1>
  <p><strong>Stream the records you own.</strong></p>
  <p>Listening is an open-source, self-hosted music player. Run it on your Mac or Linux server, import the music you own, and listen on your other devices.</p>
  <p>
    <a href="https://listening-music.vercel.app/">Website</a> ·
    <a href="#your-first-run">Get started</a> ·
    <a href="https://github.com/Warkanlock/listening-public/releases/latest">Downloads</a> ·
    <a href="#documentation">Docs</a>
  </p>
  <a href="frontend/react/public/assets/about-showcase/room/library.webp"><img src="frontend/react/public/assets/about-showcase/room/library.webp" alt="Listening library and playback queue with a demo collection" width="840"></a>
</div>

## What you can do

- Import MP3, AAC, M4A, and FLAC files from your own folders. Watch for new files and search by artist, album, song, or genre.
- Browse recording credits, read synced lyrics where available, and find music through Mood Map and Deep Dive.
- Sync your Spotify Liked Songs and playlists you own or collaborate on. Import portable playlists and group copies of the same playlist in your library.
- Start Library Radio, hand off playback to another device, or listen together with Nearby Jams. Give household listeners their own profiles.
- Save a Camping mix for offline listening. Android can also keep an offline copy of your room library, play files stored on the device, and switch to a high-contrast E Ink theme.
- Add optional plugins to find and retrieve missing recordings. Listening verifies audio before adding it to your library. Compatible plugins can also share a folder you choose.

## What's new

- **Better tablet listening.** On iPad, playlist artwork anchors the header, and the player background reaches the bottom edge.
- **A tour you can host anywhere.** The landing leads with installation and discovery features. Download the static site from the release.
- **Review copies at a glance.** Copy-review actions line up in a row on desktop and in two columns on web and Android phones.
- **Stop searching on web and Android.** In **Review copies**, choose **Stop searching** to mark a request **Unavailable** and remove it from automatic searches. The request stays visible. Select **Retry now** to search again.
- **Find your way around Settings.** Use a sidebar on a computer or scroll the tabs on a phone. Spotify account controls and playlist tools have their own sections. In **Sources → Playlist matches**, open a suggested match to compare recordings before linking playlists.
- **Manage sharing in Extensions.** Open the plugin that provides sharing to choose a folder and start or stop sharing. [Folder sharing setup](docs/download-engines.md#share-a-folder-through-a-plugin)
- **Search and update on Android.** Use the dedicated **Search** screen to find songs, artists, and albums. Check for a new APK from **Settings → App updates**. [Android guide](clients/android/README.md)

[Read the latest release notes](https://github.com/Warkanlock/listening-public/releases/latest)

## Your first run

Choose a computer to host Listening. It holds the library. Open the room on the other devices you want to listen on.

<details open>
<summary><strong>Docker</strong></summary>

With Docker running, pull the published image and start a room.

```sh
docker pull ghcr.io/warkanlock/listening-public:1.0.2
docker run -d --name listening --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -e LISTENING_ROOM_REQUIRE_OWNER=1 \
  -v listening-config:/config -v listening-library:/library \
  ghcr.io/warkanlock/listening-public:1.0.2
docker exec -it listening /app/listening account set-owner owner
```

Enter a password when prompted. On the Docker host, open [http://127.0.0.1:8765](http://127.0.0.1:8765) and sign in as `owner`. The two Docker volumes keep your settings and library when you replace the container.

[Published image tags](https://github.com/Warkanlock/listening-public/pkgs/container/listening-public) · [Music folders, private access, and Compose](docs/private-install.md#start-a-room-with-docker-compose)

</details>

<details>
<summary><strong>Mac</strong></summary>

1. Download the Mac DMG, drag the app to Applications, and open it. The Mac app runs on Apple silicon with macOS 13 or later.
2. To join a room, enter its address or paste your invitation.
3. To host a new room, choose **Install terminal command** from the app menu. Open Terminal, run `~/.local/bin/listening setup`, and choose a music folder.

[Download for Mac](https://github.com/Warkanlock/listening-public/releases/download/v1.0.2/listening-room-1.0.2.dmg) · [Mac setup guide](clients/macos/README.md)

</details>

<details>
<summary><strong>iPhone and iPad</strong></summary>

1. Open your room address or invitation in Safari.
2. Tap **Share → Add to Home Screen**, then open Listening from the new icon.

Use a trusted HTTPS room and save the mix from the Home Screen app for offline Camping. [iPhone and iPad guide](clients/ipad/README.md)

</details>

<details>
<summary><strong>Android</strong></summary>

1. Download and open the Listening APK. Allow installation from your browser or Files app if prompted.
2. Open Listening and enter your room address or invitation. You can also open music already on the device.

[Download for Android](https://github.com/Warkanlock/listening-public/releases/download/v1.0.2/listening-room-1.0.2.apk) · [Android guide](clients/android/README.md)

</details>

<details>
<summary><strong>Browser or Windows</strong></summary>

Open your room address or invitation in a browser on the room’s private network. Sign in or pair when prompted.

</details>

Already have a room? **Settings → Connect a device** shows the address, pairing options, and any installers available from that room. [Private network setup](docs/private-install.md#choose-how-devices-reach-the-room)

## Add a listening plugin

On a Linux Docker host, set up the plugin manager to add search, downloads, or folder sharing. Choose an engine for **Find copy** under **Settings → Extensions**. For folder sharing, open a plugin that supports sharing and choose a folder from the host's configured sharing directory. [Plugin setup guide](docs/download-engines.md) · [Listening Plugins](https://github.com/Warkanlock/listening-plugins-public)

## Preserve or move a collection

Use a collection snapshot to move your catalog and original audio to another host. Use a portable library ZIP to take playlists and metadata. Keep the server’s private settings and credentials in a separate backup. [Snapshot and restore](docs/operations.md#preserve-or-move-a-collection)

## Documentation

Start with the [documentation index](docs/README.md) or browse the [wiki](https://github.com/Warkanlock/listening-public/wiki).

| Guide | Covers |
| --- | --- |
| [Server installation](docs/private-install.md) | Owner setup, music import, private access, and updates |
| [Run from source](docs/operations.md#from-source-developers-and-forks) | Local development and forks |
| [Music storage and playback](docs/music-storage-and-playback.md) | Import, play, buffer, export, and sync |
| [Discovery and Radio](docs/operations.md#radio) | Library Radio, discovery, and optional sources |
| [Spotify setup](docs/operations.md#spotify-adapter-optional) | Connect Spotify and sync or edit eligible playlists |
| [Find copy states](docs/request-states.md) | Read request status and choose the next action |
| [Build an add-on](examples/engines/README.md) | Search and retrieval engines and a working example |
| [Operations](docs/operations.md) | CLI, backups, sharing, and troubleshooting |
| [Portable formats](SPEC.md) | CSV, M3U, XSPF, and other portable formats |
| [Client releases](docs/client-releases.md) | Build, sign, and verify installers |
| [Architecture](https://github.com/Warkanlock/listening-public/blob/main/ARCHITECTURE.md) | Code map, data flow, and extension boundaries |
| [Public release preparation](docs/public-release.md) | Review the source, history, and release assets before publication |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) to set up a development room. See [SECURITY.md](SECURITY.md) to report a vulnerability privately.

[MIT license](LICENSE) · [Third-party notices](THIRD_PARTY.md)
