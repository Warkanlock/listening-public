# 0012. Keep metadata enrichment separate from obtaining a recording

Accepted.

The catalog can describe any named recording. Third-party metadata can be slow, rate limited or unavailable. One metadata module handles provider queries, cache lifetimes, rate limits and retries. Deep Dive builds a weekly discovery shelf seeded from favorite artists. Recommendations and Explore results remain metadata until someone requests a copy. [External metadata](../backend/metadata/external_metadata.py)

Typing a query searches the local library with normalized substring matching. An explicit search of open catalogs selects Explore. The track table can filter by playlist, favorites, recently played, most played and text. Selecting several canonical genre tags includes recordings matching any selected tag. Genre correction uses the same vocabulary as filtering.

Genre categorization runs as background work. A stored file probe supplies tags for database-backed audio, just as a file probe does for a file-backed song. The genre pass can fall back to MusicBrainz and ListenBrainz. Store the genre, its source and any owner correction durably so a rebuild can reapply it. Produce coverage reports without starting a metadata crawl. Favorites and append-only play events refer to stable recording identities and survive changes in list membership. [Genre store](../backend/catalog/genres.py) · [Listening history](../backend/playback/play_events.py)
