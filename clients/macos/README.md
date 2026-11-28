# Listening on macOS

On an Apple silicon Mac running macOS 13 or later, drag the app from the DMG to Applications and open it. Enter your server’s URL and tap **Connect**, then sign in or pair when prompted. You can also paste a pairing link from **Settings → Devices** in the initial form. The app saves the room address and WebKit keeps the device credential. Use **Connect to a room…** in the app menu to switch servers or replace a revoked credential.

If macOS blocks the first launch of a trusted download, compare its checksum with the release and follow Apple’s first-launch steps. [Apple’s Gatekeeper guide](https://support.apple.com/en-us/102445)

For local hosting, choose **Listening → Install terminal command…**. Open Terminal and run `~/.local/bin/listening setup` to choose a room and start the server. The bundled interpreter, FFmpeg, ffprobe, and web app run without Homebrew or development tools. The app uses the owner’s existing data folders. After upgrading, open the new app and confirm it can access your setup. If you host locally, choose **Listening → Install terminal command…** again before removing the older copy.

## Producing a personal disk image

In a Listening checkout on Apple silicon, run `listening build dmg`. Use `--output DIR` to select an artifact directory. Install Xcode command line tools, a current Node 22 LTS (22.12 or later) and Yarn Classic on the build machine. The first build needs a network connection. It fetches checksum-pinned runtime archives and builds the audio tools against the macOS SDK. Build downloads stay under `~/Library/Caches/listening-build/macos`. Set `LISTENING_MAC_BUILD_CACHE` to use another directory.

The builder rebuilds the frontend, compiles the native client, signs the bundle ad hoc, mounts the disk image read-only and checks the mounted signature, CLI version, and bundled tools. The output includes the versioned DMG and a SHA-256 file. This personal build uses ad-hoc signing and has no Apple notarization ticket.

See Notices in the app's Resources for the runtime licenses, FFmpeg sources and build options.

Install `pkg-config` on the build Mac to compile the bundled Opus encoder. The installed app ships with its own audio tools.
