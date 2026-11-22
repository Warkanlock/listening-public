"""Transient audio destinations for one playback session."""

import re
from urllib.parse import unquote

from devices.presence import Presence

_ID = re.compile(r"[A-Za-z0-9_-]{8,80}")
_PRESENCE_SECONDS = 120
_MAX_DEVICES = 32


def valid_output_id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def output_from(headers):
    identity = headers.get("X-Listening-Output", "")
    if not valid_output_id(identity):
        return None
    label = unquote(headers.get("X-Listening-Output-Name", "")[:800])
    name = "".join(char for char in label if char.isprintable()).strip()[:60]
    raw_epoch = headers.get("X-Listening-Output-Epoch", "")
    epoch = int(raw_epoch) if re.fullmatch(r"[1-9][0-9]{0,15}", raw_epoch) else None
    return identity, name or "Listening client", epoch


class PlaybackOutputs:
    def __init__(self, now):
        self.presence = Presence(now, _PRESENCE_SECONDS, _MAX_DEVICES * 4)
        self.clear_selection()

    def clear_selection(self):
        self.selected = None
        self.explicit = False

    @property
    def devices(self):
        return self.presence.active()

    def touch(self, output):
        if output is None:
            return True
        identity, name, *epoch = output
        if not self.presence.arrive(identity, epoch[0] if epoch else None, {"name": name}):
            return False
        devices = self.devices
        if len(devices) > _MAX_DEVICES:
            self.presence.forget(next(iter(devices)))
        return True

    def leave(self, output):
        return bool(output and len(output) == 3 and self.presence.leave(output[0], output[2]))

    def validate_selection(self, value, output):
        devices = self.devices
        if output is None:
            raise ValueError("Open the device picker from a current Listening client.")
        if (not isinstance(value, list) or not 1 <= len(value) <= _MAX_DEVICES
                or any(not isinstance(key, str) or key not in devices for key in value)):
            raise ValueError("Choose at least one connected playback device.")
        return frozenset(value)

    def select(self, devices):
        self.selected = devices
        self.explicit = True

    def start(self, output):
        if output is not None and (not self.explicit or not self.selected.intersection(self.devices)):
            self.selected = frozenset((output[0],))
            self.explicit = False

    def audible(self, output):
        return self.selected is None or (output is not None and output[0] in self.selected and output[0] in self.devices)

    def sole_listener(self, output):
        if output is None or self.selected is None or output[0] not in self.selected:
            return False
        return not (self.selected.intersection(self.devices) - {output[0]})

    def snapshot(self, playing, output):
        audible = self.audible(output)
        return {
            "playing": playing and audible,
            "session_playing": playing,
            "output_selected": audible,
            "outputs_explicit": self.explicit,
            "outputs": [
                {"id": key, "name": device["name"], "selected": key in (self.selected or ())}
                for key, device in sorted(self.devices.items())
            ] if output else [],
        }
