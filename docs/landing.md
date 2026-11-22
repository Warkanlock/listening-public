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

Run `make verify-ui` with Playwright against the bundled disposable room. Review the captured screens in `verification/ui/`, preserving the test room's synthetic tracks and identities. Run `sh frontend/react/about/refresh-screenshots.sh` to convert the selected captures to WebP. It uses `cwebp`. Check the gallery descriptions and dimensions if a fixture viewport changes.

## Deploy on Vercel

Create a Vercel project for the standalone tour. Connect this repository in the project's Git settings. Set **Root Directory** to `frontend/react` and **Production Branch** to `main`.

The checked-in `frontend/react/vercel.json` builds the tour at `/` and `/about`. Merge landing changes through a pull request and verify that the production deployment matches the merge commit.

Choose the deployment's visibility in Vercel. For a public tour, check the page and screenshot URLs from a signed-out browser before sharing the link.
