# Listening plugins

Listening can import and play your collection with no engines installed. A search engine finds a candidate and a compatible download engine retrieves it into temporary storage. Listening checks the format, title, duration and bitrate, then commits verified recordings to the room library. Use an engine with sources you have permission to download from.

## Set up the manager on a Linux Docker host

1. Create a dedicated directory for engine state, a short private socket directory and a separate staging directory on the host. Use the same UID for the manager and the room container. The stock room container runs as UID 1000. Give that UID read and write access to staging. Keep credentials under the manager state directory and outside the room's mounts.
2. Choose a publisher with a signed plugin catalog, such as [Listening Plugins](https://github.com/Warkanlock/listening-plugins-public). Obtain the publisher's public key. Verify the signing key through an independent channel. You can also build your own catalog following the example below. A catalog author authorizes software to run on your host, so use a key you control or explicitly trust.
3. Start the manager as the room's host operator with Docker access. Run it under your host's existing service supervisor. Use absolute paths and replace the example version with the exact server release version.

   ```sh
   python3 scripts/engine_fleet.py \
     --socket /srv/listening/engines-socket/engine.sock \
     --state /srv/listening/engines-state \
     --staging /srv/listening/engines-staging \
     --repository https://github.com/Warkanlock/listening-plugins-public \
     --plugin-release RELEASE_TAG \
     --public-key /srv/listening/catalog-public.pem \
     --server-version 1.5.1
   ```

   Use `--repository` for publicly downloadable release assets. Set `RELEASE_TAG` to the release that lists your server version. While a plugin repository is private, use your authenticated GitHub session to download `catalog.json` and `catalog.sig` together, for example with `gh release download RELEASE_TAG --repo Warkanlock/listening-plugins-public --pattern 'catalog.*' --dir /srv/listening`. Replace `--repository` and `--plugin-release` with `--catalog /srv/listening/catalog.json`. Both paths verify the signature against the publisher key you supplied. If the catalog refers to private images, sign Docker in to that registry as the host user running the manager. A previously verified repository catalog can be reused if the host goes offline. The manager checks for a new repository catalog when it starts.

4. Set `LISTENING_ENGINE_SOCKET_DIR` and `LISTENING_ENGINE_STAGING_DIR` in the Compose environment. Append `-f packaging/compose.engines.yaml` to the files already used by your room. Keep the same Compose project and persistent mounts. Include the override in the host updater's Compose files. During a planned restart, the server mounts only the socket directory and shared staging, both separate from the engine configuration.
5. Open **Extensions** in the room and install the engine. Open its **Configure** form and enter any settings it requests. The manager stores values in that plugin's private credentials directory. Older workers can continue reading their credentials files there.
6. Run the readiness test and enable the engine. Choose which enabled engine handles search and which handles the download. The CLI can install, test, enable, and select engines through `listening extensions --help`. Enter setting values in the room UI. Use the Jobs screen to start background searches or process requests.

Each plugin runs in a separate container with constrained memory, CPU and process count. It receives its own staging and credentials. It can make network requests to its sources. Listening retains a 20 GiB reserve and initially processes one download at a time. If the engine becomes unavailable, queued work waits and the room continues to serve the library.

## Share a folder through a plugin

1. Create a dedicated music directory on the Linux Docker host. Give the manager's user read access. Use a Docker Engine that supports non-recursive bind mounts. Keep configuration, staging, and private files outside this directory.
2. Add `--share-root /absolute/path/to/shared-music` to the manager service and restart the manager. Place the files you intend to share in this directory. To share database-held tracks, use **Settings → Storage → Export library audio** first. The sharing process receives the selected folder as a read-only mount.
3. Under **Extensions → Listening plugins**, configure an installed plugin with the `share` method. Open its sharing controls. Enter a folder relative to the host's sharing directory or leave the field blank to share that directory.
4. Select **Start sharing**. Use **Stop sharing** before changing the folder. Disabling, replacing, or removing the plugin also stops sharing. Restart sharing after changing account settings.

Only share files you have permission to distribute.

## Find the source for a copy

Open **Extensions** to see which plugin searches for a recording and which one retrieves its audio. The room owner can install and configure plugins there. A paired device or household listener can see which sources the room uses. On Android, the room owner can open the room in a browser to manage the installation.

From **Browse catalogs**, explore a recording, then use **Find copy** to request audio in the format you want. Catalog results provide recording information. The selected search and download plugins handle the file. A ready plugin means the room can run it. An individual transfer still depends on the source responding and supplying audio that passes verification.

## Build and sign a catalog

Build a worker from [Listening Plugins](https://github.com/Warkanlock/listening-plugins-public) or implement the [engine protocol](../examples/engines/README.md). Push the worker image to a registry accessible by the host manager. Record its immutable image reference in the form `registry.example/listening/catalog@sha256:DIGEST`. The manager pulls only digest-pinned images from the verified catalog. Declare which candidate source namespaces each capability produces or accepts. A selected fetcher must accept every source declared by the searcher. When a package has no signed metadata for the current server release, refresh the catalog and update the extension before selecting it.

Use a catalog with this structure. Replace the image digest with the published image's actual digest, the version with the worker version and the compatibility list with the server releases you tested.

```json
{
  "version": 1,
  "engines": [
    {
      "id": "example-catalog",
      "version": "1.0.0",
      "architecture": "arm64",
      "server_versions": ["1.5.0", "1.5.1"],
      "capabilities": ["search", "fetch"],
      "sources": {"search": ["example-catalog"], "fetch": ["example-catalog"]},
      "image": "registry.example/listening/catalog@sha256:DIGEST"
    }
  ]
}
```

Sign the exact file bytes. Keep the private key on the signing machine. Attach `catalog.json` and `catalog.sig` to the plugin repository's release and distribute the public key separately.

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out catalog-private.pem
openssl pkey -in catalog-private.pem -pubout -out catalog-public.pem
openssl dgst -sha256 -sign catalog-private.pem -out catalog.sig catalog.json
```

## Upgrade a room with an existing engine

When an update adds required settings, pause affected jobs and disable that engine. Update it from the signed catalog, enter the settings, run the readiness test, then enable it and resume the jobs.

Before upgrading a room that used the bundled downloader or sharing client, install the matching plugin and follow its credential migration guide. Test the plugin on a fixture room before switching the live room. Select its download sources to resume queued work. Keep the previous installer or image until you verify the upgrade. Pin older managers to their existing signed catalog until they support the `share` method.

For an old server that predates the updater's idle protocol, schedule one maintenance window. Prepare a recovery point and verify playback and jobs are idle before stopping the service. Stop the updater service, stop just `listening-room` with its existing Compose invocation, and run `scripts/compose_updates.py` on the host with its normal flags and `--from-stopped-room`. The one-time command requires an existing stopped container and the same data mounts. If it fails before starting the update, use the pinned previous image to bring the room back. Restart the updater service after confirming playback, pairings, mixes and job settings. Future updates wait for an idle lease.


## Other integration seams

- Radio has per-device playback behavior and a built-in list of optional sources. A radio plugin would need a playback interface, separate from the verified audio retrieval protocol.
- Catalog discovery and metadata enrichment use provider-specific adapters and lookups. A discovery plugin would need pagination, recording identifiers, and attribution. It can offer a recording without owning how to retrieve it.
- Streaming-service playlist sync owns account authorization and playlist reconciliation. That behavior belongs behind its own interface, alongside the portable playlist importer.
- OpenSubsonic serves playback to other clients. Mastodon shares portable playlists. Both are separate interoperability adapters that use the room's library.

### When a source cannot sign in

Find copy keeps requests queued. Extensions shows when the source can try again in your device’s local time. Playback from the library continues. To use another installed source, open **Extensions → Song downloads → Change download sources**. Choose compatible extensions and select **Save sources**. Use the refresh icon beside **Listening plugins** to check availability.

See the [plugin protocol](../examples/engines/README.md#temporary-sign-in-failures) for the sign-in response.
