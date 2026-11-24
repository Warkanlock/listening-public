# Install Listening Room on a private network

The server keeps your catalog and media on its own disk. On the same computer, open the room in a browser. You can also connect a browser, an Android or Apple client, or Android’s offline player from another device on a private network. You can import and play audio you own without an account with a music service.

## Start a room with Docker Compose

The [illustrated setup walkthrough](onboarding-walkthrough.md) shows what a clean install looks like through the first track and device connection.

A new installation starts with your music folders. Use the core image on amd64 or arm64. Use a released image archive or the matching registry image. Existing rooms should follow [Update a source-built Compose room](#update-a-source-built-compose-room-from-the-browser) and keep the current files, secrets and installed extensions.

Load the release archive for your server architecture after comparing it with its matching SHA-256 checksum. Copy `release.env` to `.env`. If you are building from a checkout, run `docker compose build` instead. Use the release's Compose files with its image.

```sh
docker load -i listening-room-core-amd64.tar
cp release.env .env
mkdir -p config library
```

The container runs as UID 1000. On Linux give that UID write access to `config` and `library` if required. Store the private configuration and writable library on persistent local storage. Leave the port on loopback for use on the server itself, or set `LISTENING_BIND` to the intended LAN or VPN address and `LISTENING_ROOM_ORIGIN` to the full URL that clients can reach. For example, use `http://music-room.local:8765` for a room reachable under that mDNS name. Verify the URL from the second device.

### Add your music and claim the room

Add `LISTENING_IMPORT_DIR` to `.env` with the absolute path to a folder of tagged music. The Compose import override mounts this folder read-only at `/imports`. The setup walkthrough offers to probe it and writes a list called **Owned music** into the room's configuration directory. The server verifies and ingests the audio on startup, so allow storage for a verified library copy.

```sh
docker compose -f docker-compose.yml -f packaging/compose.import.yaml run --rm --no-deps listening-room /app/listening setup
docker compose -f docker-compose.yml -f packaging/compose.import.yaml up -d --no-build
```

Choose **host**, create the first owner, and accept the mounted music folder or enter `skip` to add music later. Use the same Compose overrides every time. If you are starting without music, omit the import override. On a source or Mac host, use `listening setup` from a terminal. The server-owner setup can be repeated without replacing the owner account. To specify the music folder and address directly, pass `--music PATH` and `--origin URL`. The Compose environment takes precedence over the saved URL. For a running installation, choose a maintenance window before changing the paths or advertised URL. To open someone else's invitation, run `listening join`.

Open the address printed by setup and sign in. If you did not supply a URL for Compose, run `docker compose port listening-room 8765` on the host with the same Compose files. Use the published port with the host’s reachable address. If the mapping binds to `127.0.0.1`, use that address from the host itself. Go to **Library → Owned music** and play a track. **Settings → Room setup** links to optional extensions. On the server, use `listening extensions list` to see the same choices. See `listening extensions --help` for engine lifecycle, selection and Radio controls. **Settings → Connect a device** shows the server address, local installers when staged, and Safari Home Screen instructions for iPhone and iPad.

### Stage the client installers

Place the release-matched signed APK, Mac DMG and `client-installers.json` together in a directory readable by Listening. Put them in `clients/` beside the room's private config file. For a separate read-only directory, use `packaging/compose.clients.yaml` and set `LISTENING_CLIENT_DIR` to its absolute path. Include the override with the same Compose commands above. The room checks each installer's name, version, size and SHA-256 before offering a download. A client on the room network can download a staged installer after signing in or pairing.

For an existing amd64 Compose room that uses bundled Sockseek, keep its compatible full image until the optional adapter has been verified on that room. The existing source build settings are `LISTENING_IMAGE_TARGET=full` and `LISTENING_IMAGE=listening-room:full-local`.

If the operator distributes images through a private registry, sign in and run `docker compose pull` before starting. The **Verify and build install images** workflow can produce a private archive, matching Compose files and checksums. [Install and maintain versioned images](release-images.md)

### Add download engines

The room can play, import and organize your local collection without an engine. Configure a supported worker from **Settings → Extensions** when you need background search and downloads. [Install and manage engines](download-engines.md)

## Update and recover

Before changing images, stop the service and back up `config/` and `library/` together. `config/` contains owner credentials and device and host state. For a portable copy of the catalog and audio without host settings, use [collection snapshot and restore](../README.md#preserve-or-move-a-collection). Keep the backups outside the Compose directories and test that a restored room can play a track before depending on them.

For a local report on the room, its disk space and the advertised connection address, run `docker compose exec listening-room /app/listening doctor`. Add `--check-database` to scan the SQLite collection.

## Install with an agent

> Install Listening Room from this private checkout using the instructions above. Create a localhost room first and verify owner sign-in, an imported track, and a disposable restore. Preserve any existing media and service configuration. Set the client origin and private bind only after I provide my network details. Use my existing LAN or VPN and keep the room off the public internet. Let me enter the owner password privately from the terminal.

### Update a source-built Compose room from the browser

The owner can install a newer revision under **Settings → Room setup → App updates** once the host updater is configured. The updater fetches `main` from a host-local clone, builds the chosen revision, and recreates only the Listening service. Compose checks that the service starts. If startup fails, the updater tries to restore the previous image against the same config and library mounts.

1. On a Linux host running Docker Engine, clone Listening into a private directory under a deployment account with Docker access. Use an SSH deploy key with read-only access to this repository for a private origin. Keep the key and clone on the host. The host needs Git and Python 3.12 or later.
2. Configure an owner login and require it with `LISTENING_ROOM_REQUIRE_OWNER=1`. Create a private socket directory owned by the account running the updater. Its UID must match the container's Listening UID, 1000 by default. Mount the directory read-only at `/run/listening-updates` and set `LISTENING_ROOM_UPDATE_SOCKET=/run/listening-updates/control.sock` in the Listening service. The supplied `packaging/compose.updates.yaml` override mounts the socket for the standard service using `LISTENING_UPDATE_SOCKET_DIR` from the Compose environment. For a custom service name, put these settings on that service.
3. Run `python3 scripts/compose_updates.py` on the host as a supervised service with startup at boot. Pass `--socket /path/to/socket-dir/control.sock`, `--repository /path/to/clone`, and the project's `--project-directory` and ordered `--compose-file` arguments. Also pass `--service listening-room`, `--image` with the local image tag used by that service, and `--target core` or `--target full` to match the installed image. Keep Docker access and Git credentials with this host process.
4. Rebuild and recreate the Listening service once from a revision that includes the updater, keeping its existing data mounts. Pass `LISTENING_ROOM_REVISION=$(git rev-parse HEAD)` as a Docker build argument to identify this initial image. Then sign in as the owner and check for updates from the room page.

### Check for updates on a schedule

Once the Compose host updater is running, use the same deployment account to schedule the one-shot helper with cron. Use absolute paths and keep the updater socket and logs private. For example, this checks for a newer revision at 4 a.m. in the host’s time zone:

```cron
0 4 * * * umask 077; /usr/bin/python3 /path/to/listening/scripts/compose_update_now.py --socket /path/to/control.sock >> /path/to/private/update.log 2>&1
```

The helper uses the existing updater and its rollback checks. It leaves the container running when it is already current. A newer revision is built before Listening restarts.

### Public artist rankings

Artist profiles can rank songs and albums by your completed plays. To add the
public ListenBrainz rankings, set `LISTENING_ROOM_LISTENBRAINZ_TOKEN` in the
server environment and restart Listening. With Compose, set it in the
project's private `.env` and recreate the service with `docker compose up -d`.
Use a user token from your
[ListenBrainz settings](https://listenbrainz.org/settings/). Listening sends
only public artist IDs to look up rankings. It keeps your playback history local.

### Connect catalogs and Radio search

Set these variables in the Listening server process environment. In Compose, pass them to the Listening service using its `environment` or private `env_file`. Recreate Listening after changing the environment.

| Service | Server connection | Optional settings |
| --- | --- | --- |
| Jellyfin music | `LISTENING_JELLYFIN_URL` and `LISTENING_JELLYFIN_TOKEN` | `LISTENING_JELLYFIN_USER_ID` scopes the item query to a Jellyfin user. `LISTENING_JELLYFIN_WEB_URL` is the browser address if it differs from the server address. |
| Radio | `LISTENING_YOUTUBE_API_KEY` for YouTube Data API v3 | Without a key, try SomaFM streams or generated noise, or open a genre search on YouTube. |

Give the service credentials access only to catalogs you want paired listeners to discover. **Private catalog** sends queries only to those configured services. Results open in the source app, which manages playback and its own sign-in. Listening keeps the service credentials on the server.

### Import a collection of existing audio

To try a room with account-free demo audio, generate a short original track and its playlist.

```sh
docker compose exec listening-room /app/listening music demo /library/demo
export LISTENING_IMPORT_DIR="$(pwd)/library/demo"
docker compose -f docker-compose.yml -f packaging/compose.import.yaml up -d --no-build
```

If you set `LISTENING_LIBRARY_DIR`, use that directory in the host path above. In **Settings → Sources**, add `/imports/Playlists` to the watched folders. Select **Check folders**, then open **First listen** from Library and press Play.

For a watched music folder, put a CSV, M3U or XSPF list in its `Playlists/` subfolder. Use a list whose artist, title, album and duration describe your tagged audio. Set `LISTENING_IMPORT_DIR` to the folder's absolute host path and add the import Compose file.

```sh
LISTENING_IMPORT_DIR=/path/to/music docker compose \
  -f docker-compose.yml -f packaging/compose.import.yaml up -d --no-build
```

The container mounts the folder read-only. To keep watching it after recreating the service, put `LISTENING_IMPORT_DIR` in `.env` and pass both Compose files again. For an existing room, add `/imports/Playlists` in **Settings → Sources** if its sources have already been configured. The room scans the tagged media and brings verified originals into its collection. See [music storage and playback](music-storage-and-playback.md) for what happens after import.

From a source installation with FFmpeg, `listening music index /path/to/music /path/to/music/Playlists/Owned.csv` generates a CSV from the tags. Create the `Playlists/` directory first. The command reports skipped files and leaves the music alone.

### Household profiles

The owner can add separate browser logins from the server terminal. Members share the music catalog and get their own favorites, play history, and queue. Existing paired devices continue to use the shared room session.

```sh
docker compose exec listening-room /app/listening account add-member alex
docker compose exec listening-room /app/listening account members
```

Members sign in by opening the room address in a browser. Use `account revoke-member MEMBER_ID` to sign a member out everywhere, or `account disable-member MEMBER_ID` to block sign-in. The account and its records stay in the installation, so include the private config directory in host backups.

The browser, Mac app, and iPhone or iPad app retain a signed-in session for up to a year. Opening the room again within 30 days of expiry renews a still-valid session. Signing out, changing the password, or revoking access invalidates the relevant sessions.

A paired browser can use **Sign in** to switch to a named account. Members can stream the shared music library and make their own playlists. Synced and linked music playlists, downloads, and room administration use the room's paired or owner views according to their permissions.


### Connect an OpenSubsonic client

Use a client that supports the `apiKeyAuthentication` extension and original audio streams. Set its server address to your room's private origin, then enter a key created for that client. Create a separate key for each client so you can revoke them independently.

```sh
docker compose exec listening-room /app/listening account add-stream-key "Music player"
docker compose exec listening-room /app/listening account stream-keys
docker compose exec listening-room /app/listening account revoke-stream-key KEY_ID
```

The CLI displays the key at creation. The adapter supports ping, artist and album browsing, song search, shared playlists, and original audio with byte-range seeking. Key access is limited to verified music. Use the key over private HTTPS or your VPN, and configure any proxy to omit or redact the query string on `/rest/` requests. OpenSubsonic carries the key in that query string. [OpenSubsonic API key authentication](https://opensubsonic.github.io/docs/extensions/apikeyauth/)

For connection drops, set **Buffer ahead** in Listening's playback settings on the device. The web player, Mac app and Android room app keep the current track and one to three upcoming tracks while the player is open. On Android, this buffering uses unmetered Wi-Fi or Ethernet. For OpenSubsonic players, use the player's own offline downloads or queue prefetch settings.

## Choose how devices reach the room

Use one stable address your devices can reach. `LISTENING_BIND` is an address assigned to the Docker host. `LISTENING_ROOM_ORIGIN` is the exact URL clients open, with the port if applicable. The configured origin is used in invitations, so use the same URL on each device. Recreate the container after editing `.env`.

```sh
docker compose up -d --force-recreate
```

The default Compose file publishes only to host loopback. For loopback isolation on a shared network, use Docker Engine 28 or newer. With an older engine, also verify that another machine cannot connect to the published port. When enabling access from other devices, restrict TCP 8765 to the intended network and confirm from a device on that network that the configured origin opens the room. Sign in with the owner account to manage the room, then pair the devices that will use it.

### Home LAN

Reserve an address for the server on the router or use a stable private DNS name. In this example, `musicbox.local` resolves to the server's LAN address from the clients.

```dotenv
LISTENING_BIND=192.168.1.25
LISTENING_ROOM_ORIGIN=http://musicbox.local:8765
```

If local DNS is unavailable, use the reserved LAN address in both settings. Join the same trusted Wi-Fi or wired network from the client. Plain HTTP on a LAN relies on that network to protect the traffic.

A standalone room with both LAN and Tailscale access can advertise its Tailscale URL in the invitation. To pair a device that only reaches that same room on the LAN, the owner can copy the invitation and replace its server address with the room’s LAN URL. Keep the `/?token=…` portion and paste the resulting link into the client.

### Tailscale

Install and sign in to Tailscale on the Docker host and all devices that need access. Use your own tailnet and review its access rules. Copy the server's Tailscale IPv4 address with `tailscale ip -4`. With MagicDNS enabled, find the server's full `.ts.net` name in `tailscale status` or the Tailscale admin console.

```dotenv
LISTENING_BIND=100.64.10.25
LISTENING_ROOM_ORIGIN=http://musicbox.example-tailnet.ts.net:8765
LISTENING_ROOM_NETWORK=tailscale
```

Replace both example values. If you do not use MagicDNS, set the origin to the actual Tailscale address. Open the URL directly while Tailscale is connected. Tailscale can establish encrypted connections across networks without a router port forward. Android can play saved tracks after disconnecting from the tunnel. [Tailscale installation](https://tailscale.com/kb/1347/installation)

Set `LISTENING_ROOM_ABOUT_TAILNET_ONLY=1` in the Listening service environment to restrict `/about/` to Tailscale peers and loopback. The check uses the incoming connection address. Publish only on trusted interfaces and connect directly through Tailscale when using this option.

### WireGuard

Use your own WireGuard tunnel or an existing trusted WireGuard network. Each client must have a route to the server's WireGuard address. For a direct tunnel, start from the provided peer examples. [Server example](../packaging/wireguard/server.conf.example) · [Client example](../packaging/wireguard/client.conf.example)

1. Choose an unused private subnet and an address for each device. Create a separate key pair per device and exchange public keys. Place each device's private key in its own copy of the configuration and keep that file in its operating system's VPN storage.
2. On a Linux server, save the filled-in server configuration as `/etc/wireguard/listening.conf` with access limited to root. Bring it up with `sudo wg-quick up listening` and use `sudo systemctl enable wg-quick@listening` to start it on boot if the server uses systemd. Import the client configuration into the WireGuard app on the device. The example routes only the room's WireGuard address over the tunnel.
3. Once the tunnel is up on the host, set the bind and origin to the room's WireGuard address and recreate the container.

```dotenv
LISTENING_BIND=10.66.44.1
LISTENING_ROOM_ORIGIN=http://10.66.44.1:8765
LISTENING_ROOM_NETWORK=wireguard
```

If the tunnel terminates on a router instead of the Docker host, use the host address reachable through that router and permit access from the VPN subnet. A client that connects from outside your home needs a reachable WireGuard peer or hub. Carry Listening Room inside the tunnel. Leave the room's own TCP port on the private network. [WireGuard quick start](https://www.wireguard.com/quickstart/)

### Private HTTPS

For a browser on a private network, you can terminate HTTPS in a proxy while leaving the container's published port on loopback. Set the exact HTTPS URL as `LISTENING_ROOM_ORIGIN`, and keep `LISTENING_ROOM_REQUIRE_OWNER=1` in the container. Configure the proxy to preserve the request's Host header. Use a certificate trusted by your clients and bind the proxy to a private interface. Listening checks the configured origin for browser writes and issues Secure cookies for that origin. Keep VPN traffic and certificates under your own administration.
