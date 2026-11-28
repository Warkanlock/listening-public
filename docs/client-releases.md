# Client releases

GitHub Releases holds the installers, checksums, versioned source, and change notes together. Releases follow the repository's current visibility. The unified APK uses the Listening application ID. A signed update retains the app’s local data and pairing.

## Build and publish

1. Update the matching versions in `pyproject.toml`, `frontend/react/package.json`, the Android app's Gradle file, and the Apple project. Increase the Android version code and Apple build number.
2. Run the backend suite, browser verification, and code review before pushing the release commit to `main`.
3. On an Apple silicon Mac with Python 3.12 or newer, the client build prerequisites, and access to the Android signing key, build the installers. The signing files stay on the build machine.

   ```sh
   python3 scripts/release_clients.py
   ```

4. Write the release notes and publish the checked files. The command checks the commit and checksums, uploads to a GitHub draft, verifies the draft's assets, and publishes it.

   ```sh
   python3 scripts/release_clients.py --publish /path/to/release-notes.md
   ```

The helper creates a new draft release. To refresh an existing tag or its attachments, follow the [public release runbook](public-release.md#publish-in-order).

The helper builds one signed Android APK and one Apple silicon DMG. It writes the manifest, SHA-256 checksums, and the source commit. The Mac build uses ad-hoc signing. For Simulator, run `listening build ios` and verify that ZIP separately before publishing it. For physical iPhones and iPads, use the web app or build the Apple project with your signing identity. Build the container images through the [image workflow](release-images.md).

## Verify an Android update

Keep the application ID and signing certificate across releases. Increase `versionCode` before publishing the next APK. The updater reads GitHub Latest, validates a matching `listening-room-VERSION.apk` asset with its reported size and SHA-256, then checks the APK's identity before opening Android's installer.

For the move from a 2.x preview to 1.0.0, use the [direct installation procedure](public-release.md#move-from-a-2x-preview-to-100). Verify that an installed 1.x build recognizes a newer 1.x version before relying on automatic discovery.


## Offer the release from a room

The release command also writes and publishes `client-installers.json`, a list of the two artifacts with their size and SHA-256 bound to the matching server version. Download all three release files as the operator and check `SHA256SUMS` before staging. Leave GitHub credentials on the build or deployment machine.

Stage them in `clients/` beside Listening's active configuration, or mount a separate read-only artifact directory using `packaging/compose.clients.yaml`. Be sure the server user can read the manifest and both files. Clients signed in or paired to the room can open **Settings → Connect a device**, or visit `/?setup=clients` at their room address. The room omits a missing, modified or different-version installer and shows how to repair the installation.
