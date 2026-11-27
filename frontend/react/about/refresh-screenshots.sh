#!/bin/sh
# Refresh the public-safe gallery from the disposable room in make verify-ui.
set -eu
cd "$(dirname "$0")/../../.."

source=verification/ui
destination=frontend/react/public/assets/about-showcase/room
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

# Credits and Song information use curated detail crops. To update them,
# capture the graph and recording details from a local fixture library, then
# update their intrinsic image dimensions in about/index.html.
# Refresh Storage separately using example paths in both the fields and help text.
# An unmodified fixture capture shows the host's temporary workspace paths.
while read -r filename name; do
    case "$name" in
        library|catalogs|radio) set -- -lossless ;;
        *) set -- -q 87 ;;
    esac
    cwebp -quiet "$@" "$source/$filename.png" -o "$staging/$name.webp"
done <<'SCREENS'
00-library library
23-now-playing-phone player
01f-mood-map mood-map
12-deep-dive deep-dive
14-lyrics-stage lyrics
16-radio radio
21-discovery-mobile catalogs
29-camping-mode camping
34-nearby-jams-phone jams
01b-playlist-share share
11-settings-devices devices
13-requests requests
13-copy-review copy-review
30-insights insights
SCREENS

mkdir -p "$destination"
cp "$staging/"*.webp "$destination/"
