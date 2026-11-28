# Contributing to Listening

Use a disposable room for development. Keep collections, credentials, pairing links, and application settings outside the checkout. The project accepts fixes, install improvements, accessibility work, and new integrations that fit the existing domains.

## Build and verify

Install Python 3.9+, Node.js 22.12+, Yarn Classic, and FFmpeg. From the checkout:

```sh
yarn --cwd frontend/react install --frozen-lockfile
yarn --cwd frontend/react build
python3 -m unittest discover -s tests
```

For changes to the web app, install Playwright and Chromium, then run the rendered checks against a fixture room.

```sh
python3 -m pip install playwright
python3 -m playwright install chromium
make verify-ui
```

Use an isolated Python environment for the Playwright tools. On macOS, the test and UI audit can verify that tests do not read the installed room’s data. See [AGENTS.md](AGENTS.md) for the repository’s verification rules and the [architecture overview](https://github.com/Warkanlock/listening-public/blob/main/ARCHITECTURE.md) for the code map.

To work on the landing page without a room, run `yarn --cwd frontend/react dev` and open [the Vite preview](http://localhost:5173/about/). It uses bundled artwork and example moods.

## Submit a change

Describe the user-facing behavior and the checks you ran. For a bug, include the platform and Listening version, steps to reproduce, and the result you expected. Use generated audio or a minimal sample you have permission to share. Remove account names, invite links, addresses, tokens, file paths, and library data from reports and screenshots. [Private vulnerability reporting](SECURITY.md)
