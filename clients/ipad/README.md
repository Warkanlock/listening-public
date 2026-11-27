# Listening for iPhone and iPad

Open your room invitation in Safari, or install the Listening app on iOS or iPadOS 17 or later. Both play from the same room and use the system media player for background listening. Connect to the room's Wi-Fi or Tailscale network before pairing.

## Use Safari on iPad

Connect to the room’s private network, open a device invitation in Safari, then choose **Share → Add to Home Screen**. Open Listening from the Home Screen and tap **Join audio** if prompted. Use the room’s Devices settings for a fresh invitation if needed.

The native source uses the same build for iPhone and iPad. Installation on a physical device requires signing.

## Install on an iPhone or iPad

1. Open `clients/ipad/Listening.xcodeproj` in Xcode on a Mac.
2. Select the Listening target. In Signing & Capabilities, select your Apple development team. If Xcode asks for a unique bundle identifier, choose one for your team.
3. Connect your iPhone or iPad, enable Developer Mode if prompted, select the device as the run destination, and press Run.
4. Enter your server’s URL and tap **Connect**. Use the address that reaches your server on Wi-Fi or Tailscale. Allow local network access if iOS asks. Sign in or pair when prompted. You can also paste a pairing link from **Settings → Devices** or `listening invite` into the initial form.

To switch servers, touch and hold the Listening icon on the Home Screen and choose **Change room**. Use the device volume buttons for playback.

## Build from the terminal

`listening build ios` writes one Simulator ZIP for both iPhone and iPad and a SHA-256 checksum to `dist/clients/`. Unzip it and drag the app onto a running Simulator. The `listening build ipad` command is an alias.

For a development IPA, configure the signing identity, bundle ID and registered devices in Xcode first. Run `LISTENING_APPLE_TEAM=YOUR_TEAM_ID listening build ios` to archive, sign and export the app for those devices. Install the IPA through Xcode’s Devices and Simulators window.
