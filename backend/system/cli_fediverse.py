"""Share portable playlists without opening or publishing the private room."""

from getpass import getpass
import os
from pathlib import Path
import sys

from adapters import mastodon
from library.playlist_sharing import load_shared_playlist, write_public_bundle
from system.cli_room import display


def add_commands(groups):
    fediverse = groups.add_parser("fediverse", help="prepare and publish playlist metadata")
    actions = fediverse.add_subparsers(dest="action", required=True)
    bundle = actions.add_parser("bundle", help="create a ZIP with a public web page and importable XSPF")
    bundle.add_argument("playlist", type=Path, metavar="PLAYLIST.xspf")
    bundle.add_argument("destination", type=Path, metavar="NEW_BUNDLE.zip")
    post = actions.add_parser("post", help="preview or publish a Mastodon post linking to a playlist page")
    post.add_argument("playlist", type=Path, metavar="PLAYLIST.xspf")
    post.add_argument("--instance", required=True, help="HTTPS origin of your Mastodon account")
    post.add_argument("--playlist-url", required=True, help="public HTTPS address of the uploaded playlist page")
    post.add_argument("--visibility", choices=("public", "unlisted"), default="unlisted")
    post.add_argument("--publish", action="store_true", help="send the post using LISTENING_MASTODON_TOKEN or a hidden prompt")


def run(args):
    preview = load_shared_playlist(args.playlist)
    if args.action == "bundle":
        write_public_bundle(preview, args.destination)
        print(f"Created {display(args.destination)}. Open index.html to review it before uploading both files.")
        return 0
    host = mastodon.instance_host(args.instance)
    text = mastodon.post_text(preview, args.playlist_url)
    print("\n".join(display(line) for line in text.splitlines()))
    if not args.publish:
        print(f"\nPreview for {host} ({args.visibility}). Add --publish to post it.")
        return 0
    token = os.environ.get("LISTENING_MASTODON_TOKEN")
    if not token and sys.stdin.isatty():
        token = getpass("Mastodon access token (hidden): ")
    if not token:
        raise ValueError("Set LISTENING_MASTODON_TOKEN or run the command in a terminal for a hidden prompt.")
    url = mastodon.publish(host, token, text, args.visibility, preview)
    print(f"\nPublished on Mastodon. {url}" if url else "\nMastodon accepted the post. Open your account to view it.")
    return 0
