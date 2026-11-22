import argparse
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from adapters import mastodon
from fixture_environment import isolate_room_environment as setUpModule
from library.library_protocol import write_xspf, xspf_tracks
from system import cli_fediverse


INSTANCE = "https://social.example.org"
PUBLIC_PAGE = "https://playlists.example.org/midnight/"
RECORDING = "https://musicbrainz.org/recording/8ad4d753-db52-4879-9f0c-f2c1dd893d0a"


class FediverseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "songs.xspf"
        self.source.write_text(write_xspf("Midnight & <script>", [
            {"title": "Björk", "artist": "Artist", "identifier": RECORDING, "duration_ms": 5000},
            {"title": "Finale", "artist": "Artist", "album": "<Finale>",
             "location": "file:///private/song.flac", "image": "http://server:8765/art?token=private",
             "identifier": "https://musicbrainz.org/recording/8ad4d753-db52-4879-9f0c-f2c1dd893d0a?token=private",
             "isrc": "SECRET"},
        ], identifier="http://server:8765/private"), encoding="utf-8")

    def run_cli(self, *arguments):
        parser = argparse.ArgumentParser()
        cli_fediverse.add_commands(parser.add_subparsers(dest="group", required=True))
        output = io.StringIO()
        with redirect_stdout(output):
            cli_fediverse.run(parser.parse_args(["fediverse", *map(str, arguments)]))
        return output.getvalue()

    def post(self, *flags, instance=INSTANCE, url=PUBLIC_PAGE):
        return self.run_cli("post", self.source, "--instance", instance, "--playlist-url", url, *flags)

    def test_bundle_can_be_hosted_or_imported_and_only_includes_selected_metadata(self):
        destination = self.root / "share.zip"
        self.run_cli("bundle", self.source, destination)
        with ZipFile(destination) as bundle:
            self.assertEqual(bundle.namelist(), ["index.html", "playlist.xspf"])
            page, playlist = (bundle.read(name).decode("utf-8") for name in bundle.namelist())
        self.assertIn("Midnight &amp; &lt;script&gt;", page)
        self.assertIn('href="playlist.xspf" download', page)
        self.assertIn(f'href="{RECORDING}"', page)
        self.assertLess(page.index("Björk"), page.index("&lt;Finale&gt;"))
        for private in ("file:///", "server:8765", "token=", "SECRET", "<script>"):
            self.assertNotIn(private, page)
            self.assertNotIn(private, playlist)
        rows = xspf_tracks(playlist)
        self.assertEqual([row["title"] for row in rows], ["Björk", "Finale"])
        self.assertEqual([row["identifier"] for row in rows], [RECORDING, ""])
        self.assertEqual(rows[0]["duration_ms"], 5000)
        with self.assertRaises(FileExistsError):
            self.run_cli("bundle", self.source, destination)

    def test_preview_is_offline_and_invalid_addresses_do_not_send_credentials(self):
        with mock.patch("adapters.mastodon.http.client.HTTPSConnection") as connect:
            output = self.post()
            self.assertIn(PUBLIC_PAGE, output)
            self.assertIn("Preview for social.example.org (unlisted)", output)
            for unsafe in ("http://social.example.org", "https://owner:secret@social.example.org",
                           "https://musicbox.local", "https://127.0.0.1", "https://server.example.org/?token=secret",
                           "https://social.example.org/api"):
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    self.post("--publish", instance=unsafe)
            with self.assertRaises(ValueError):
                self.post("--publish", url="https://playlists.example.org/?token=secret")
            connect.assert_not_called()

    def test_publish_posts_metadata_to_mastodon_and_changes_key_on_playlist_updates(self):
        with mock.patch.dict("os.environ", {"LISTENING_MASTODON_TOKEN": "disposable-secret"}), \
                mock.patch("adapters.mastodon.http.client.HTTPSConnection") as connect:
            client = connect.return_value
            response = client.getresponse.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"url":"https://social.example.org/@fixture/123"}'
            output = self.post("--publish", "--visibility", "public")
            self.assertIn("https://social.example.org/@fixture/123", output)
            self.assertNotIn("disposable-secret", output)
            connect.assert_called_once_with("social.example.org", timeout=15)
            method, path, body, headers = client.request.call_args.args
            self.assertEqual((method, path), ("POST", "/api/v1/statuses"))
            self.assertEqual(json.loads(body), {"status": f"Midnight & <script>\n2 tracks · Playlist and XSPF\n{PUBLIC_PAGE}",
                                                "visibility": "public"})
            self.assertEqual(headers["Authorization"], "Bearer disposable-secret")
            first_key = headers["Idempotency-Key"]
            self.post("--publish", "--visibility", "public")
            self.assertEqual(client.request.call_args.args[3]["Idempotency-Key"], first_key)
            self.source.write_text(self.source.read_text().replace("Finale", "Encore"), encoding="utf-8")
            self.post("--publish", "--visibility", "public")
            self.assertNotEqual(client.request.call_args.args[3]["Idempotency-Key"], first_key)
            response.status = 302
            with self.assertRaisesRegex(RuntimeError, "HTTP 302"):
                self.post("--publish")
            self.assertEqual(connect.call_count, 4)

    def test_post_limits_long_titles_and_refuses_empty_playlists(self):
        preview = {"name": "z" * 600, "tracks": [{}]}
        text = mastodon.post_text(preview, PUBLIC_PAGE)
        self.assertEqual(len(text), 500)
        self.assertIn("…\n1 track", text)
        with self.assertRaisesRegex(ValueError, "Add a track"):
            mastodon.post_text({"name": "Empty", "tracks": []}, PUBLIC_PAGE)
