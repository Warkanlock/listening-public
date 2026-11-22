# Radio is a separate, per-device playback surface

Radio (plan 059) does not join the shared playback session. The session is
the room's together mode: one queue, each selected output hearing the same
library songs. Radio is background listening, usually one person
half-listening, so in its first version it plays per device: tuning in on
one device changes nothing on the others, and the shared queue is never
touched.

On the device where Radio plays, the two engines exclude each other:
starting Radio pauses that device's local playback, and starting a local
track stops Radio. Two things never sound together on one device.

The browser station picker searches verified library genres, SomaFM streams,
generated noise, and previously saved YouTube mixes. A library station shuffles
the ready songs in that genre, streams their ordinary authenticated audio URLs,
and reshuffles after each pass. Tuning leaves the room session untouched.
The Mood map can request YouTube discovery independently. YouTube owns playback
and ads when listeners explicitly select that source.

Clients remember the latest tuned station in their own storage. Signed-in web
clients also save one Radio selection per room and account, so you can pick up
a station on another browser, iPad, or Mac. Pairing alone gives access to the
room, so guest and native Android history stays on its device. Opening another
client shows the station paused. Playing resumes the remembered library track or YouTube video at its last
checkpoint. The player samples the media clock every ten seconds and saves
when paused or leaving the page. Live streams reconnect at the live edge.
The YouTube source link opens the video at the current position. Playback in
the external YouTube app follows its own history and background settings. Android delegates saved YouTube
mixes to YouTube and can reopen the mix on request.

The most recently tuned station owns the shared resume entry. Track changes
and explicit Radio Stop can update or clear that entry only while it still
refers to the same tuning. Starting a library song silences Radio locally.
When the latest saved selection is a parked library song, it takes precedence
in the player and the Radio station stays available in the account.
Taking over the resume entry sends no transport commands to
the other client. Radio reads the catalog without editing the catalog,
coverage, or play events.
