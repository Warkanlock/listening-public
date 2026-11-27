# Listening for Android refresh

## Goal

Give the 480 × 800 Mudita Kompakt a readable music app with one main job per screen. Keep playback available while browsing. Give the full player room for the song and its transport controls. Use the supplied Spotify screenshots for navigation and hierarchy. Listening uses its own type, ruled lists, white space, monochrome controls, and the room's artwork.

## Screen contract

| Screen | Main job | Interaction |
| --- | --- | --- |
| Listen | Decide what to hear next | Open Weekly Discovery, Deep Dive, Activities, Moods, or Radio. Each opens its own view. |
| Search | Find a song, artist, or album | Submit a query to the room or the phone library. A result starts available audio in place or opens song details. Catalog search has a separate entry. |
| Library | Open a collection or manage saved music | Open songs, playlists, favorites, listening history, genres, downloads, device files, and Camping mode. |
| Playlist and song lists | Browse one collection | Preserve playlist order. Show whether a copy is playable, offer Play playlist where supported, and keep editing and song details in their own actions. |
| Deep Dive and Weekly Discovery | Choose or request recommended recordings | Use one-column recording lists. Keep weekly editions, batch playback/download, review, and removal with their own playlist. |
| Activities, Moods, Radio | Start one kind of mix or station | Each has a separate view. On E Ink, show a plain mood list and open the draggable map on demand. Keep radio source choices and controls. |
| Catalogs, credits, artist, album, and song details | Inspect one source, recording, or related entity | Use readable rows and a small action area. Keep profiling, copy requests, and playlist links. |
| Downloads | Act on the download queue | Place unresolved requests and their next action first. Group active jobs and recent copies separately. |
| Device files and Save for offline | Play local files or choose something to copy | Preserve document picker grants and saved playback. Camping goes through a trip size, preview, and Save action. |
| Now playing | Control the current song | Give artwork and metadata a clear area. Keep previous, play/pause, next, and access to queue visible. Position opens on request on E Ink. More holds details, favorites if unavailable on the main view, lyrics, shuffle, and output routing. |
| Settings and Insights | Change one category of settings or inspect playback | Open focused categories for appearance, playback, offline sync, room connection, extensions, app updates, and playback diagnostics. |
| Connect to a room | Establish room access | Lead with a room address or invitation. Keep device files and APK updates available before pairing. |

## Layout and state

- Use native controls with touch targets of at least 48 dp, readable type, testable accessibility labels, and scrollable content where it can overflow. Keep an opaque black-on-white Kompakt palette, one depth of outlines, restrained rules, and no automatic motion on reading surfaces.
- Use three fixed destinations in the room shell. A playback strip above them offers song, artist, and one transport action. Open the full player on demand and return to the originating view. Keep the strip and bottom destinations out of the full player.
- Use a typed catalog selection for route filters, query, playlist, and page. Search and collection browsing keep independent pages. Preserve the selected view and query through the full player. Keep room-only destinations out of phone-library mode.
- Rebuild the native layouts on the existing Activity, RoomUi, and playback contracts. Keep Media3, room and replica state, catalog and download semantics, and signed in-app update handling at their current boundaries.

## Design review

The view reviews found three important boundaries. The full player returns to its entry screen, and starting a song never forces it open. Playlists retain stored track order. Room-only routes disappear when the phone library is active. System permission and copy-review flows keep their existing owners. Radio searches on submit on E Ink to avoid redrawing the list per keystroke.

## Work and acceptance

1. Inventory existing routes and agree on the screen map. Review this specification before writing UI code.
2. Build the shell, then integrate separate browse, player, and utility renderers. Each group owns disjoint source files. RoomActivity owns navigation and asynchronous callbacks.
3. Build the signed APK and drive it against a disposable paired room. Check Kompakt-sized screenshots, three tabs, search results, playlist order, Back from Now Playing, mini playback, system background playback, offline and download paths, and Settings access. Run the repository gates before any push.
4. Record actual device coverage and note anything that depends on physical E Ink.
