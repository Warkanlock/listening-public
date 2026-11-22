# 0013. Share the playback queue and keep presentation preferences per device

Accepted.

Paired devices operate one ordered queue. Persist the current track, position and upcoming tracks. Restore a previously playing session paused, so restarting a host or reopening a browser cannot unexpectedly start audio. Recording a play event against its stable song identity lets recently played and most played survive a catalog rebuild.

Playing targets the initiating client by default. Listeners can choose one or more connected outputs for a queue. Each selected client streams the same track from the room and follows its playback position. Output presence and selection expire with the session and are separate from paired-device credentials. Navigation keeps music playing. A previous output retains its buffered songs for five minutes after it is deselected. The web player detaches its media elements at handoff. Its standby element reads the same client-buffered copy used for offline playback.

The local web player prepares a second audio element for the next song. Each device can choose zero, two or five seconds of crossfade and can use system playback controls through Media Session. A song dialog shows the recording and available quality metadata. Artwork cycles between small, medium, large and CD-case views. Keep the chosen size on the device and respect reduced motion.

On iPhone, iPad, and the macOS app, the web player uses one media element and the system playback session so audio can continue through background transitions. Volume uses the device controls. Crossfade and ReplayGain stay available on clients that use the Web Audio gain chain.

Fetch lyrics through the metadata layer and cache the response. Timestamped lyrics drive the full-screen stage. Selecting a line seeks the active song. Plain lyrics remain readable when timestamps are unavailable. Radio owns its own device-local player, keyboard controls and optional corner view. It yields playback and system controls when local music resumes. [Radio](0003-radio-is-a-separate-per-device-surface.md)
