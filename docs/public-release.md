# Prepare and publish a public release

Use this runbook to publish Listening independently of any running room. A public release is intended for people who supply their own storage, accounts, and private network.

## Prepare the candidate

1. Choose the source commit and record the server, web, Android, and Apple versions. Use [client releases](client-releases.md) for version numbers and signing. Keep the audited `public-baseline` tag reachable. Set `LISTENING_PREVIOUS_RELEASE_REF` to the validated predecessor for the upgrade test. The workflow uses `public-baseline` when the variable is unset.
2. Complete the repository reviews and [verification checks](../CONTRIBUTING.md). Verify the private reporting path in [SECURITY.md](../SECURITY.md). Exercise a new room and an upgrade from the baseline on disposable data. Check owner setup, pairing, original audio playback, and snapshot restore. On a phone, save a playlist and play it with networking disabled.
3. Build and check the native installers and the portable site ZIP from the recorded source. Inspect app identity, signing, published names, files inside the archives, and embedded defaults. Verify checksums and scan source, binaries, screenshots, and archives for private network, device, account, and machine details.
4. Verify the signed extension catalog for the intended server version and each supported architecture. Use the already trusted publisher key. Check the signature against the exact catalog bytes and verify each referenced image digest. Test the catalog in a disposable install. Compatible signed metadata is required before switching the public release. [Catalog setup and signing](download-engines.md#build-and-sign-a-catalog)

## Publish in order

1. Establish the remote release tag at the reviewed commit. Create and push it when it is missing. If an existing tag points elsewhere, confirm the intended retag with the maintainer before moving it. Use `git ls-remote --tags origin "refs/tags/$TAG" "refs/tags/$TAG^{}"` to verify the commit. Use the peeled value ending in `^{}` for an annotated tag.
2. Build the amd64 and arm64 [Docker images](release-images.md#publish-from-a-public-fork) from that tag. Verify anonymous pulls and check the manifest, image revision, and server version against the reviewed commit.
3. Prepare the GitHub release with the signed APK, Mac DMG, client manifest, checksums, source revision, and notes. Include the Simulator ZIP and portable landing ZIP when they are part of the release. For an aligned launch, build all the release artifacts from the verified commit. When replacing an existing distribution, check the tag and current attachment inventory before uploading the reviewed files. Compare the resulting digests against the local builds. Verify the bytes at their intended download paths.
4. Update the [landing](landing.md) and README links after the downloads respond. Open the production site, wiki, and release page from a signed-out session. Copy the Docker quick-start commands and download the files the site advertises.
5. If this release becomes the Android update target, mark it Latest explicitly with `gh release edit "$TAG" --latest` after verifying the downloads. Check the signed-out GitHub Latest redirect and API response. Confirm the tag, APK filename, size, and digest. Check that the app reports its current version and recognizes a newer compatible version using disposable metadata.
6. Retire obsolete preview releases or version tags only after the replacement is live and the upgrade fixture points to a surviving ref. Keep the previous working artifacts available through verification. Delete old releases or tags only when the maintainer has authorized that cleanup.

## Refresh an independent attachment

A standalone site ZIP can change between software releases. Verify its replacement bytes, update its checksum, and record its source revision in the release notes. Keep the server and native installer provenance attached to the release.

## Move from a 2.x preview to the current release

A preview Android update check compares version names. It treats a 1.x release as older than 2.x. Download the current signed APK directly and install it over the existing app. Keep the installed app and its data in place. The replacement APK needs the same signing identity and application ID and a higher Android build number than the preview. After the move, use the built-in updater for later 1.x releases.

Owners upgrading a room should follow [Update and recover](private-install.md#update-and-recover). Refresh the signed catalog and update the installed extension metadata before re-enabling extensions on the release version.
