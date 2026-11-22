# 0011. Pair devices to a private home server

Accepted.

The home server owns the catalog, queue, verified audio streams and host configuration. A paired client can browse and play over the LAN or a private VPN. Imports, filesystem selection, credentials, updates and process control require the host context. Paired views redact host-only settings.

The server chooses a reachable LAN or Tailscale address for its pairing link. A token bootstraps registration. The device receives its own revocable credential and reports presence. Rotation and revocation must take effect on subsequent authenticated requests. An install must avoid distributing pairing links or machine secrets in portable exports. [Devices](../backend/devices/) · [Pairing addresses](../backend/system/pairing.py)

Validate the request Host and the origin of state-changing browser requests independently of pairing. Legacy rooms grant owner authority to loopback. New Compose rooms require explicit authentication, as described in [ADR 0016](0016-private-installation-and-household-accounts.md). [Host validation](../backend/system/hosts.py)
