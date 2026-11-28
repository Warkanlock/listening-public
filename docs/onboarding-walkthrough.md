# Set up Listening

This walkthrough uses a disposable room with a demo track. If you already have a room, review [backup and recovery](private-install.md#update-and-recover). For source-built Compose rooms, follow the [Compose update guide](private-install.md#update-a-source-built-compose-room-from-the-browser). Rooms with a download engine can follow the [engine upgrade guide](download-engines.md#upgrade-a-room-with-an-existing-engine).

## Start the server

1. Follow the [Docker installation](private-install.md#start-a-room-with-docker-compose) to build or load the image and create the persistent folders.
2. Run [the setup walkthrough](private-install.md#add-your-music-and-claim-the-room). Choose **host**, create the owner login, choose a read-only music folder, and confirm the import. You can skip the music folder and add it later.
3. Start the room and open the address printed by setup. If setup did not print an address, run `docker compose port listening-room 8765` with the same Compose files to find the published port. Sign in with the owner account you created.

[![Listening owner sign-in screen](../frontend/react/public/assets/about-showcase/onboarding/sign-in.webp)](../frontend/react/public/assets/about-showcase/onboarding/sign-in.webp)

## Play the first import

Open **Library** and select **Owned music**. Play a track and listen for a few seconds. If the list is empty, check that the music folder is mounted and contains tagged audio.

[![Library with an imported demo track](../frontend/react/public/assets/about-showcase/onboarding/owned-music.webp)](../frontend/react/public/assets/about-showcase/onboarding/owned-music.webp)

## Connect a second device

1. In the owner session, open **Settings → Connect a device**. The example uses a loopback address for the setup computer. To connect a second device, [configure the private network bind and advertised address](private-install.md#choose-how-devices-reach-the-room), recreate the room with the same Compose files, and open that address from the second device.
2. Choose **Open pairing settings**. Under **Pair a device**, choose **Copy pairing link** and open it on the new device. An invitation grants access to your room, so send it only to the person or device you want to pair.

[![Connect a device page with the server address and pairing settings](../frontend/react/public/assets/about-showcase/onboarding/connect-a-device.webp)](../frontend/react/public/assets/about-showcase/onboarding/connect-a-device.webp)

| Device | How to connect |
| --- | --- |
| Mac | On an Apple silicon Mac with macOS 13 or later, open the DMG and drag Listening into Applications. Open the app, then enter the room address or paste the full invitation. |
| Android | Install the APK and open Listening. Paste the full invitation into the app, or enter the room address and follow the pairing prompt. |
| iPhone or iPad | Open the address or invitation in Safari. Use **Share → Add to Home Screen** to open Listening from its own icon. Use the room’s trusted HTTPS address for offline Camping playback. |
| Other computers | Open the address or invitation in a browser on the room’s private network. |

If Android and Mac installers are missing from **Connect a device**, download the available public installers from [Releases](https://github.com/Warkanlock/listening-public/releases). The host can also [stage them on the server](private-install.md#stage-the-client-installers).

[![Pairing screen on a new browser](../frontend/react/public/assets/about-showcase/onboarding/pairing.webp)](../frontend/react/public/assets/about-showcase/onboarding/pairing.webp)

## Browse extensions from Room setup

As the room owner, open **Settings → Extensions**. From **Room setup**, you can also choose **Browse extensions** when that action is available. If your host has an engine manager, Listening shows the installed extensions and compatible plugins from the room’s signed catalog. Install and configure an extension, select **Check setup**, then enable it. Under **Song downloads**, select compatible search and download extensions and save the sources. [Set up the engine manager](download-engines.md#set-up-the-manager-on-a-linux-docker-host)

Select **Continue to library** to finish setup in this tab. You can return to **Room setup** or **Extensions** whenever you want.

## Add audio from a folder

Stop the room on the host before changing its music folder. For a local install, run `listening setup` to preview the folder and confirm the import. Use `listening setup --music PATH` to select the folder directly after checking its size.

For Docker, stop the `listening-room` service with your existing Compose files. Run the Compose setup command in the guide and enter `/imports` at the music folder prompt, even if the folder is already configured. Confirm the import when prompted and then start the room again. Find **Owned music** in Library. [Choose a music folder on the host](private-install.md#add-your-music-and-claim-the-room)

## Browse and import Bandcamp purchases

Find music on [Bandcamp](https://bandcamp.com/discover). The public catalog view in Listening can open Bandcamp in a separate tab with **Browse Bandcamp** or **Search on Bandcamp**. To add a purchase, download the audio from Bandcamp, extract it into the music folder on your host, and use the folder import above. [Find your Bandcamp download](https://get.bandcamp.help/en/articles/15263100-where-do-i-download-my-purchase)
