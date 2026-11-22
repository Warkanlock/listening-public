# Preparing a public release

Publish Listening and its add-ons separately from any running room. Operators choose their storage paths and trusted network settings when they install the server.

## Prepare the repositories

1. Publish from the reviewed source in new repositories. Use a project identity with a public-facing or no-reply address for new commits. Keep earlier private pull requests, release assets, and Actions runs in their original repositories.
2. Keep the audited `public-baseline` tag available for the upgrade test. Set the repository variable `LISTENING_PREVIOUS_RELEASE_REF` to a verified public release ref once one exists.
3. Configure pull requests for changes to `main`. Enable private vulnerability reporting, dependency alerts, and secret scanning and push protection where available. Verify the reporting path in [SECURITY.md](../SECURITY.md).

## Verify the release

1. Run the checks in [CONTRIBUTING.md](../CONTRIBUTING.md). Verify both a fresh install and an upgrade from the public baseline with owner setup, pairing, local audio playback, offline listening, and collection restore.
2. Build the [client installers](client-releases.md) from the audited release commit. Check signing identity, embedded defaults, media metadata, and the download links before publishing the new files. Use screened fixture data for landing-page images.
3. Build the signed add-on catalog for the intended release. Verify the catalog signature, Core version ranges, and image digests. Test installation and playback with an example catalog from a signed-out setup. See [download engines](download-engines.md) and the [engine protocol](../examples/engines/README.md).
4. Publish the intended source and release files, then check code, installers, Wiki links, and signed catalogs from a signed-out browser. Point any standalone landing deployment at the public downloads.
