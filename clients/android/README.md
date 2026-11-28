# Listening for Android

Download the signed Android APK from [GitHub Releases](https://github.com/Warkanlock/listening-public/releases). It includes room streaming, offline sync, and local file playback. After installing, enter your **Server URL** and tap **Connect**, then paste a pairing link from that room’s **Settings → Devices**. You can also paste the full pairing link in the first step, or choose **Device files** for local playback.

### Find library features

Open **Listen** for **Weekly Discovery**, **Deep Dive**, **Radio**, **Activities**, and **Moods**. **Search** finds songs, artists, and albums in the room and links to **Browse catalogs** for other sources. **Library** holds collections, listening history, and offline music.

Open **Library → Downloads** to check active transfers, review requests that need attention, or play recent downloads. To build an offline trip, open **Library → Camping mode**, pick a song count, and save the mixes to your selected music folder over Wi-Fi.

### Update the app

Open **Settings → App updates → Check for APK updates**. You can also check from the **Connect to a room** screen. When a newer release is available, tap **Update and install**. Listening downloads and verifies the APK, then opens Android's installation screen. Android may ask you to allow installs from Listening the first time.

If your phone requires a desktop installer, open the GitHub release on your computer and install the APK using that tool. On Mudita Kompakt, use Mudita Center.

### Add a song to a playlist

On the server, the owner can allow playlist additions for a paired phone under **Settings → Devices**. On the connected phone, open **Library → Playlists**, choose the playlist, and tap **Add song**. Enter the artist and title. The room saves the playlist entry even when the audio is still missing. You can also start from **Song information**, a **Deep Dive** pick, or a recording in **Search → Browse catalogs**.

A magnet identifies a playlist that follows a source. The label identifies Spotify, a linked source, or a watched folder. **Spotify · Two-way sync** means additions go to the linked Spotify playlist and Spotify changes come back to the room. A grouped playlist asks which linked destination should receive the addition. If several recordings match, paste the Spotify track link for the exact version. Editing uses the connected room. Saved playlists in **Device files** can be refreshed the next time you connect.

### Play or save device files

Open **Device files** and select a music folder on internal or removable storage. Choose a file or saved playlist to listen without a connection. To save room songs into a music folder, open **Device files → Save for offline** and connect to the room over Wi-Fi. The app can use a private VPN carried over Wi-Fi. If the saved hostname is unreachable, choose **Wi-Fi address or invitation** and enter the room’s local URL including its port. A paired phone verifies the room before reusing its device credential. Reopen the same playlist to resume a transfer or pick up newer tracks.

Existing Listening installations update in place. The old Listening Offline app has its own app ID. If you used it, open the same music folder in **Device files** and pair the unified app with a fresh room invitation. You can keep the old app installed while you check access to your files.

### Stream from your room

**Search** finds songs in the full room catalog. Tap a verified song to play it, or use its information button for release details. Tap a song without a copy to see its information and availability. **Radio** searches playable genres and live stations. **Library → Not downloaded** lists songs the room hasn’t downloaded. Use **Settings → Playback → Mobile audio** to select Original, Compact Opus, or Compact AAC for room streams.

Tap the song in the playback strip to open **Now playing**. It has the cover, track details, and playback controls. Use **Queue** to see what’s next or **More playback options** for lyrics, shuffle, and output. Back returns to the same view. Playback continues with the screen off.

### Use an E Ink display

Listening uses **E Ink** appearance automatically on Mudita Kompakt. On another Android device, select **Settings → Appearance → Theme → E Ink**.

E Ink appearance uses text tabs, high-contrast controls, and compact grayscale artwork. Tap **Show position** when you want to seek. Browse plain lists for moods and artist credits. To blend moods, open **Listen → Moods → Mood Map**. Scroll lyrics by hand or tap a timed line to seek. The app checks visible room screens less often between interactions.

Open **Settings → Extensions** to see the installed download extensions and the sources used by **Find a copy**. The format picker also offers **Extensions** so you can check the sources and return to the same request. The room owner can install extensions from the linked room page in a browser.

### Take a room offline

1. On the server, allow the paired phone under **Settings → Devices → Allow offline sync**.
2. On the phone, select **Settings → Offline library → Keep an offline library → Enable and sync**. Leave the app open for the first sync. The phone saves the catalog first and then copies each available original. Under **Library → Songs**, songs awaiting audio say **Not saved on phone**.
3. Disconnect the network and browse **Library** or use **Search**. Playback, seeking, and the phone’s media controls use its local SQLite database. The offline queue is private to the phone. **Settings → Offline library → Listen with the room** returns to the shared player.
4. Use **Settings → Offline library → Add song from phone** for MP3, FLAC, M4A or AAC files up to 512 MiB each. The picker preserves the source file. The next sync sends the original bytes. The server verifies the audio before adding the song to its catalog. A rejected upload stays on the phone, and Settings reports the reason.
5. **Sync now** retries interrupted transfers. Copies already saved are reused. With the app open, automatic sync runs on opening and every five minutes. Phone imports missing from an older version of the same collection are offered again. If the server loses its device credentials, choose **Settings → Room connection → Restore room access**, paste a fresh invitation, allow the new pairing on the server, and finish pairing on the phone. The app checks the collection identity before attaching the saved library. A different collection requires a separate phone copy.

**Turn off offline sync** stops synchronization and returns to streaming. Re-enable it to reuse the phone copy. **Disconnect** asks before removing the replica, including any pending phone imports. The room and the original files selected through Android’s picker keep their copies. Revoking a phone on the server stops future access. Its existing offline copy remains on the device.

## Building a personal release

On the build machine install a JDK 17 or 21, Android SDK platform 35 and build-tools 35.0.0. Set `ANDROID_HOME` to the SDK path. The checkout includes Gradle Wrapper 8.11.1 and verifies its distribution checksum. The first build downloads the Android Gradle plugin, Kotlin, Media3 and their dependencies.

Use your existing Android release signer for updates. If creating a new signer, the following example makes a private PKCS12 keystore and prompts for its password. Save that same password on the first line of the private file named `password` alongside it.

```sh
umask 077
mkdir -p ~/.config/listening-release/android
keytool -genkeypair -storetype PKCS12 -alias listening -keyalg RSA -keysize 3072 \
  -validity 10000 -keystore ~/.config/listening-release/android/release.p12
```

The default key alias is `listening`. Keep a secure backup of the signing key and its password so future builds can update an installed APK. To use an existing signer elsewhere, set `LISTENING_ANDROID_KEYSTORE`, `LISTENING_ANDROID_KEY_ALIAS`, and `LISTENING_ANDROID_STORE_PASSWORD_FILE`. If the key has a separate password, provide `LISTENING_ANDROID_KEY_PASSWORD_FILE`.

```sh
listening build apk
```

The command verifies the APK signature, writes the APK and a SHA-256 checksum to `dist/clients/`, and prints both paths. Use `--source DIR` to select a checkout and `--output DIR` to select another destination. The build works with the room server stopped.

## Move from a preview build

Download the signed 1.0.0 APK from the public release and install it over your 2.x preview once. Keep the preview installed during this step so Android preserves its data. Use **Check for APK updates** for later 1.x releases.
