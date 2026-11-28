# Listening

The room's own vocabulary: the places it keeps things, the processes that do its long work, and the checks that guard its changes. Terms are what they are, not how they are built.

## Language

**Application folder**:
The per-user folder holding machine settings, credentials, worker state and caches. In a legacy room it also holds the state database.
_Avoid_: support folder, config directory

**Library**:
The room's collection folder. A portable room keeps its durable audio, memberships and listening metadata in collection.sqlite3. List adapters and explicitly requested exports can materialize files alongside it.
_Avoid_: workspace, music folder

**Source**:
A place a list comes from, such as a watched folder, a linked file, or a synced service list. Imports and background polling read the source. An explicit edit can add a recording to a writable Spotify playlist through its authenticated connection.
_Avoid_: origin, feed

**Worker**:
One of the background processes that do the room's long work: bulk downloads, song requests, genre categorization, library export, database reclaim, and the one-time library migration.
_Avoid_: session, tmux session, job

**Job**:
A unit of work as the person sees it in the Jobs console: one card, one primary action, one state. A job is carried out by a worker.
_Avoid_: task, process

**Supervisor**:
The part of the server that owns the workers' processes: starting them, stopping them, and re-adopting them across server restarts.
_Avoid_: manager, daemon, tmux

**Gate**:
A check that must pass before a change is pushed. A failing gate blocks the push.
_Avoid_: smoke test, sanity check

**Radio**:
Per-device background listening from verified library songs, live streams, generated noise, or a saved YouTube mix. Signed-in web clients can resume the same selection on another device. Radio leaves the room catalog and shared song queue untouched.
_Avoid_: streaming, web radio

**Station**:
A named source in Radio. A station can shuffle a library genre or play a live stream, generated noise, or a saved mix.
_Avoid_: channel, genre (as the name of the thing itself), playlist

**Audio store**:
The seam verified audio lands through. A portable collection stores each original byte sequence by its hash. Legacy libraries may keep file-backed tracks. Explicit adoption preserves the owner's source files.
_Avoid_: blob store, database mode (as the name of the seam)

**Media database**:
The database in the library that holds original audio, keyed by its SHA-256. A portable collection also keeps catalog identities, ordered memberships and listening metadata in this database. Older rooms may keep those facts in their separate state database. A blob reference is the string `blob:` plus the hash.
_Avoid_: audio database, blob database, library database


**Linked playlist**:
An owner-named playlist whose contents follow multiple sources. It keeps the base source's order and repetitions and appends recordings unique to the other sources. Unlinking restores the individual playlist views.
