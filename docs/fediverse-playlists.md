# Share a playlist on the Fediverse

People can follow your Mastodon account for playlist updates, open a public track list, and import its XSPF into their own room. Your server stays on its private network. Use any static HTTPS host you control for the two playlist files.

## Publish a playlist page

1. Sign in as the room owner, open a playlist from **Library**, and choose **Share playlist → Download XSPF**. The dialog previews the tracks in order, with MusicBrainz links where available.
2. From a computer with the `listening` command, package the downloaded file.

   ```sh
   listening fediverse bundle listening-playlist.xspf midnight.zip
   ```

3. Unzip `midnight.zip` and open `index.html` to review what will be public. Upload `index.html` and `playlist.xspf` to the same folder on your static host. Visit the public URL in a private browser window and check that the **Download playlist (XSPF)** link works. Recipients import that file under **Settings → Sources**.

The bundle contains the playlist title and ordered song metadata. The export omits audio, artwork URLs, local file locations, source IDs other than MusicBrainz recording IDs, room URLs, and credentials. The page makes no background requests. Review any private details you typed into playlist or track names before uploading it.

## Post from an existing account

You can paste the page URL into any Fediverse client. For Mastodon, you can also preview and publish a post from the terminal. Substitute your instance and the URL of the page you just uploaded.

```sh
listening fediverse post listening-playlist.xspf \
  --instance https://social.example.org \
  --playlist-url https://playlists.example.org/midnight/ \
  --visibility public
```

The command above prints the post without contacting the instance. To send it, run it again with `--publish`. Use a Mastodon user access token with the `write:statuses` scope. Paste it at the hidden prompt, or supply it through `LISTENING_MASTODON_TOKEN` in a private automation. Keep the token on the publishing computer. The room server doesn't need it.

The default visibility is `unlisted`. Use `--visibility public` to make the post eligible for public timelines. Anyone with the page URL can open the playlist either way. For a new edition, export the updated playlist, replace the two files under the same public URL, and publish another post. Followers follow the Mastodon account. A dedicated account gives playlists their own feed.

If publication times out, look at your account before retrying. Identical posts for an unchanged playlist reuse Mastodon’s idempotency key. How long Mastodon retains that key depends on the instance implementation.

The Mastodon instance handles follow requests and federation. Incoming traffic never needs to reach your server. The public page can be hosted elsewhere while all room access stays on your LAN or tailnet. [Mastodon posting API](https://docs.joinmastodon.org/methods/statuses/#create)
