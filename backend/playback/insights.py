"""A bounded recent window of playback payloads, held only in this process."""

from collections import OrderedDict, deque
from threading import Lock
from time import monotonic


class PlaybackTraffic:
    SECONDS = 5
    HISTORY_SECONDS = 60
    MAX_OUTPUTS = 128
    BUCKETS_PER_SECOND = 4

    def __init__(self, now=monotonic):
        self._now = now
        self._lock = Lock()
        self._outputs = OrderedDict()

    def record(self, key, *, received=0, sent=0):
        with self._lock:
            tick = int(self._now() * self.BUCKETS_PER_SECOND)
            samples = self._outputs.setdefault(
                key, deque(maxlen=self.HISTORY_SECONDS * self.BUCKETS_PER_SECOND + 1))
            if samples and samples[-1][0] == tick:
                _, previous_received, previous_sent = samples.pop()
                received += previous_received
                sent += previous_sent
            samples.append((tick, received, sent))
            self._outputs.move_to_end(key)
            if len(self._outputs) > self.MAX_OUTPUTS:
                self._outputs.popitem(last=False)

    def snapshot(self, key):
        with self._lock:
            tick = int(self._now() * self.BUCKETS_PER_SECOND)
            cutoff = tick - self.HISTORY_SECONDS * self.BUCKETS_PER_SECOND
            samples = self._outputs.get(key)
            while samples and samples[0][0] < cutoff:
                samples.popleft()
            if not samples:
                self._outputs.pop(key, None)
            windows = [{"download_bytes": 0, "upload_bytes": 0}
                       for _ in range(self.HISTORY_SECONDS // self.SECONDS)]
            ticks_per_window = self.SECONDS * self.BUCKETS_PER_SECOND
            # Ignore the in-progress bucket so every window covers five full seconds.
            for sample_tick, received, sent in samples or ():
                if sample_tick < tick:
                    window = windows[(sample_tick - cutoff) // ticks_per_window]
                    window["download_bytes"] += sent
                    window["upload_bytes"] += received
            return {"window_seconds": self.SECONDS, **windows[-1], "history": windows}
