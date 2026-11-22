"""Opt-in listening presence and invitations on the room's local network."""

from dataclasses import dataclass
import ipaddress
from secrets import token_hex
import time

from devices.presence import Presence


_LAN = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")))
_LEASE_SECONDS = 35


def nearby_network(address, host):
    """Use the direct peer address. A proxy's loopback connection is not a LAN."""
    peer = ipaddress.ip_address(address)
    if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped is not None:
        peer = peer.ipv4_mapped
    if peer.is_loopback and host in {"localhost", "127.0.0.1", "::1"}:
        return "loopback"
    if any(peer in network for network in _LAN):
        return str(ipaddress.ip_network((peer, 24 if peer.version == 4 else 64), strict=False))
    return None


@dataclass(frozen=True)
class Listener:
    name: str
    source: str
    song: dict | None
    playing: bool
    played_at: float


class JamDirectory:
    """The request handler holds the room lock around directory operations."""

    def __init__(self, now=time.monotonic):
        self._now = now
        self._listeners = Presence(now, _LEASE_SECONDS, 256)
        self._guests = Presence(now, _LEASE_SECONDS, 256)
        self._hosts = {}

    def _active(self):
        listeners = self._listeners.active()
        self._hosts = {key: jam for key, jam in self._hosts.items() if key in listeners}
        return listeners

    def publish(self, key, epoch, name, source, song, *, enabled=False):
        prior = self._active().get(key)
        if prior is None and not enabled:
            return
        accepted = self._listeners.arrive(key, epoch, Listener(
            name, source, song or (prior.song if prior else None), song is not None,
            self._now() if song else prior.played_at if prior else 0,
        ))
        if accepted and prior and prior.source != source:
            self._hosts.pop(key, None)

    def stop_sharing(self, key):
        self._listeners.forget(key)
        self._hosts.pop(key, None)

    def start(self, key):
        listener = self._active().get(key)
        if not listener or not listener.playing:
            raise ValueError("Play a library song and share your listening to start a Jam.")
        if key not in self._hosts:
            self._hosts[key] = token_hex(16)

    def end(self, key):
        self._hosts.pop(key, None)

    def _find(self, key, jam):
        listeners = self._active()
        for host, identity in self._hosts.items():
            if identity == jam and host[:2] == key[:2] and host[2] != key[2]:
                return host, listeners[host]
        return None

    def join(self, key, epoch, jam):
        host = self._find(key, jam)
        if not host or not host[1].playing or not self._guests.arrive(key, epoch, jam):
            return None
        return host

    def follow(self, key, epoch, jam):
        if self._guests.active().get(key) != jam:
            return None
        host = self._find(key, jam)
        return host if host and self._guests.arrive(key, epoch, jam) else None

    def leave(self, key, jam):
        if self._guests.active().get(key) == jam:
            self._guests.forget(key)

    def view(self, key):
        listeners = self._active()
        people = {}
        for peer, listener in listeners.items():
            if peer[:2] != key[:2] or peer[2] == key[2]:
                continue
            prior = people.get(peer[2])
            if prior is None or (listener.playing, listener.played_at) > (prior.playing, prior.played_at):
                people[peer[2]] = listener
        invites = [{"id": jam, "host": listeners[host].name, "song": listeners[host].song}
                   for host, jam in self._hosts.items()
                   if host[:2] == key[:2] and host[2] != key[2] and listeners[host].playing]
        return {"available": True, "sharing": key in listeners, "hosting": key in self._hosts,
                "people": [{"name": person.name, "playing": person.playing, "song": person.song}
                           for person in sorted(people.values(), key=lambda p: (not p.playing, p.name.casefold()))],
                "invites": invites}
