# 0016. Authenticate private rooms and separate household profiles

Accepted.

A new Compose room publishes to loopback and requires an owner login. An operator can supply the room's exact HTTP or HTTPS origin and choose an interface on their LAN, Tailscale, or WireGuard network. Listening uses that origin in invitations and checks browser writes against the request origin. The network and its keys belong to the operator. An HTTPS proxy configured for explicit owner mode preserves the Host header and can forward to loopback without granting owner privileges.

The local CLI sets the owner's password and can manage members through access to the host configuration directory. SQLite holds salted password hashes and revocable browser sessions. Owner and member sessions use separate credentials from existing device pairing. Existing standalone rooms can opt into explicit owner mode. Installations that require signed-in ownership set `LISTENING_ROOM_REQUIRE_OWNER=1` so the requirement also holds before first setup and when recovering from a missing credential database.

A member can play the shared music catalog and has private favorites, play history, and a queue that follows their login. Browser requests can assert the expected account so a tab stops using its old state after another tab signs into a different account. Paired devices retain the legacy shared room session. Member authentication covers the web player and web-based Apple clients. Android device pairing remains available as a shared listening path.

Host settings, credentials and household state belong in the private host backup. A portable collection snapshot holds the catalog and media and can restore into its own room with independent credentials. Private playlists store ordered catalog keys.

Owners can play their own playlists in a separate personal queue and switch back to the shared queue from the browser. A read-only OpenSubsonic API key can stream the shared verified library and see shared playlist metadata. The key has no household profile or owner permissions.

Nearby listening is opt-in for signed-in clients. A consenting listener shares their account name and current or last verified library song during a short-lived presence lease. The room groups direct private IPv4 peers by /24 and private IPv6 peers by /64. Authentication and the direct connection establish whom a listener can see. Forwarded addresses do not establish proximity. Paired devices do not publish account presence.

A Jam is an invitation to follow the host’s current library song and playback clock. Joining requires a listener action. The host controls their existing queue, and guests read only the current verified track and clock. Guest playback does not write to anyone’s queue or listening history. Closing the host tab, stopping presence sharing, or ending the Jam removes the invitation.

[Private deployment guide](../docs/private-install.md) · [Release roadmap](../docs/product-roadmap.md)
