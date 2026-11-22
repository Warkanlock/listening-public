"""Short-lived presence for device registrations and playback outputs."""

from dataclasses import dataclass


@dataclass(frozen=True)
class _Lease:
    seen_at: float
    epoch: int | None
    data: dict | None


class Presence:
    """Keep departures briefly so a delayed heartbeat cannot reconnect a closed tab.

    The caller holds its domain lock when reading or mutating these records.
    """

    def __init__(self, now, ttl, limit):
        self._now = now
        self._ttl = ttl
        self._limit = limit
        self._leases = {}

    def _prune(self):
        cutoff = self._now() - self._ttl
        self._leases = {key: lease for key, lease in self._leases.items() if lease.seen_at >= cutoff}

    def _put(self, key, epoch, data):
        self._leases.pop(key, None)
        self._leases[key] = _Lease(self._now(), epoch, data)
        if len(self._leases) > self._limit:
            del self._leases[next(iter(self._leases))]

    def arrive(self, key, epoch, data):
        self._prune()
        current = self._leases.get(key)
        if current and current.epoch is not None and (
            epoch is None or epoch < current.epoch or (epoch == current.epoch and current.data is None)
        ):
            return False
        self._put(key, epoch, data)
        return True

    def leave(self, key, epoch):
        self._prune()
        current = self._leases.get(key)
        if epoch is None or (current and current.epoch is not None and epoch < current.epoch):
            return False
        self._put(key, epoch, None)
        return True

    def active(self):
        self._prune()
        return {key: lease.data for key, lease in self._leases.items() if lease.data is not None}

    def forget(self, key):
        self._leases.pop(key, None)
