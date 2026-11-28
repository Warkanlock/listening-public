# Install and maintain versioned images

The standard Compose file defaults to localhost and requires an owner account. A fork can publish a versioned image for amd64 and arm64. You can also build from source or use a private release archive. [Private installation](private-install.md)

## Install from a registry

Download `docker-compose.yml` and `packaging/compose.import.yaml` from the same source tag as the image. Keep the import file under `packaging/` beside the main Compose file. You'll use it to mount the demo or your own music. Beside `docker-compose.yml`, create `.env` with the registry and version your maintainer provides.

```dotenv
LISTENING_IMAGE=ghcr.io/your-org/listening:1.2.0
```

You can pin the registry's image digest in place of a version for repeatable installs.

```sh
mkdir -p config library
```

The container runs as UID 1000. On Linux, if this UID cannot write to those directories, give it access with `sudo chown 1000:1000 config library` before starting the room.

```sh
docker compose pull
docker compose up -d --no-build
docker compose exec listening-room /app/listening account set-owner owner
```

Open `http://127.0.0.1:8765` and sign in. To hear a track and pair another device, follow [Your first run](../README.md#your-first-run). Keep the Compose file and data directories together. Before replacing an existing image, follow [Update and recover](private-install.md#update-and-recover).

## Publish from a public fork

1. Create a `vX.Y.Z` Git tag for the release. Normally it matches the server version in `pyproject.toml`. In the fork's Actions settings, set `LISTENING_PREVIOUS_RELEASE_REF` to the previous deployed source tag or commit. Keep the audited `public-baseline` tag reachable. The workflow uses it when no previous release ref is configured. Confirm this lookup before retiring any version tags.
2. From the public fork's checkout, use `gh workflow run verify.yml --ref vX.Y.Z -f publish_registry=true`. For a distribution that retains an existing server version, pass `-f distribution_tag=vX.Y.Z` with the same Git tag. The image tag identifies the distribution and the image labels record the bundled server version. The workflow runs fixture tests, the browser gate, native-architecture container boots, and an amd64 upgrade and restore into a second fixture room. Only the explicitly dispatched run on a tag in a public repository can publish to GHCR.
3. In GitHub Packages, set the image visibility to match the audience you intend to distribute it to. GitHub may create a registry package with private visibility. Verify anonymous pulls if you intend to offer public installation. Check that the public manifest has both `linux/amd64` and `linux/arm64`. Check the OCI version and revision labels for each image, then record the tag and digest in the release notes.

The workflow uses the built containers for the playback, pairing and restore smokes. A physical-device release check still covers pairing a phone, saving a playlist, and playing it with networking disabled.
