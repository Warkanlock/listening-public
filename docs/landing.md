# About Listening

The server serves the product tour at `/about/`. The standalone build serves the same tour at `/` and `/about` on Vercel. It includes the demo recording and gallery from the test room. Its install links lead to instructions for connecting to a listener's own room.

## Test the tour

Run the normal frontend build and `make verify-ui` to exercise the server's About route. The browser checks the sleeve, screenshots, handoff demo, artist credits, Camping demo, and local-only page loads.

Build the standalone page from the same checkout.

```sh
cd frontend/react
yarn install --frozen-lockfile
yarn vite build --config vite.about.config.mjs
python3 -m http.server --bind 127.0.0.1 3000 --directory landing-dist
```

Check the root on a phone and desktop viewport. Open the screenshot gallery, try the keyboard and reduced motion paths, and reload with JavaScript disabled. The standalone output is limited to the marketing page and its assets. Room setup and support controls stay with the self-hosted app. The private About page also includes the existing e-ink screenshots.

## Refresh the screenshots

Run `make verify-ui` with Playwright against the bundled disposable room. Review the captured screens in `verification/ui/`, preserving the test room's synthetic tracks and identities. Run `sh frontend/react/about/refresh-screenshots.sh` to convert the selected captures, including Review copies, to WebP. It uses `cwebp`. Check the gallery descriptions and dimensions if a fixture viewport changes.

Capture Spotify settings in a disposable room with a simulated connected account and fixture playlists. Use an example server address for Connect a device and example paths for Storage. Review each screenshot for private account information and paths before adding it to the gallery.

## Deploy on Vercel

Create a Vercel project for the standalone tour. Connect this repository in the project's Git settings. Set **Root Directory** to `frontend/react` and **Production Branch** to `main`.

The checked-in `frontend/react/vercel.json` builds the tour at `/` and `/about`. The repository links to [the Listening Room tour on Vercel](https://listening-room-tour.vercel.app/). Merge landing changes through a pull request and verify that the production deployment matches the merge commit.

Keep the production domain public in Vercel. Check the production HTML and response headers for indexing directives from a signed-out session. `/` and both `/about` variants should declare the production root as their canonical URL.

The standalone build adds Open Graph and X card metadata. It serves `frontend/react/about/listening-room-social.jpg` at `/listening-room-social.jpg`. When you update the card, check that the live image URL returns a 1200 × 630 JPEG without authentication.
