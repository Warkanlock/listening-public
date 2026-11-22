# 0014. Serve one built React application

Accepted. This supersedes the initial framework-free shell and vendored component experiment.

The browser player, private-network clients and Mac web view need the same interface. Build the React 19 and TypeScript source using Vite and the pinned UI SDK. The server serves the resulting frontend distribution. A source install rebuilds those assets and a packaged install carries them prebuilt. The service worker precaches the content-versioned application shell. APIs and audio continue to require the room server.

Keep playback state and polling scoped to their consumers. Split the frontend store by domain and Settings into independently readable panels. One CSS token layer controls the palette, typography, spacing, focus, status contrast and artwork sizes. Preserve descriptive labels, visible status and usable controls at phone widths. Read the SDK's shipped component and rendering documentation when authoring components.

Type checking, the class-name contract and a headless browser walking a real fixture room cover different failure modes. The rendered gate checks that controls, results and audio progression work with the built assets. Record emulator, browser and physical-device results separately. The backend suite exercises domain behavior, failure handling and the HTTP access boundary. [Repository verification rules](../AGENTS.md)
