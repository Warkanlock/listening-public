"""Shared listening state with one small interface: snapshot, apply, reset."""

import math
import json
import random
import threading
import time
from pathlib import Path

from system.private_files import private_write
from playback.outputs import PlaybackOutputs


class PlaybackSession:
    """Keep one queue and clock for every connected listener.

    ``available(playlist)`` returns playable catalog IDs or raises ValueError for an
    unknown playlist. ``resolve(ref)`` returns a playable track or None. The
    session owns ordering, timing, and concurrency; callers only send actions.

    ``genre_order(refs, current_ref)`` is the server's playable-order
    assembly for Genre flow (plan 029): it returns the refs with the
    current track's genre first. The session only consumes that order
    at the points where it would otherwise shuffle; the grouping
    itself lives with the catalog, not in the queue mechanics.
    """

    _PERSISTED_KEYS = ("version", "current", "upcoming", "history", "order",
                       "shuffle", "playing", "position", "changed_at")

    def __init__(self, available, resolve, now=time.time, state_path=None, genre_order=None,
                 record_event=None, state_store=None):
        self._available = available
        self._resolve = resolve
        self._now = now
        self._state_path = Path(state_path) if state_path else None
        self._state_store = state_store
        self._genre_order = genre_order
        self._record_event = record_event
        self._open_ref = None
        self._lock = threading.RLock()
        self._outputs = PlaybackOutputs(now)
        self._state = {"version": 0, "occurrence": 0, "current": None, "upcoming": [], "manual_count": 0, "history": [],
                       "order": [], "shuffle": False, "genre_flow": False, "playing": False,
                       "position": 0.0, "changed_at": now(), "parked_at": None}
        if self._state_path or self._state_store is not None:
            try:
                saved = (self._state_store.load() if self._state_store is not None
                         else json.loads(self._state_path.read_text(encoding="utf-8")))
                if (isinstance(saved, dict) and all(key in saved for key in self._PERSISTED_KEYS)
                        and all(isinstance(saved[key], list) for key in ("upcoming", "history", "order"))):
                    self._state.update(saved)
                    manual = self._state["manual_count"]
                    self._state["manual_count"] = (
                        min(max(0, manual), len(saved["upcoming"])) if type(manual) is int else 0
                    )
                    if self._state["current"] is not None and not self._resolve(self._state["current"]):
                        self._state["current"] = None
                        self._state["playing"] = False
                    if self._state["playing"]:
                        # A session saved as playing restarts paused at
                        # its saved position (plan 045): position is
                        # wall-clock derived, so resuming as playing
                        # would silently add the downtime. The saved
                        # position is the honest resume point; clamp it
                        # to the track's length when that is known.
                        self._state["playing"] = False
                        self._state["changed_at"] = now()
                        length = self._resolved_length(self._state["current"])
                        if length is not None:
                            self._state["position"] = min(self._state["position"], length)
            except (OSError, ValueError, TypeError):
                pass
        if self._state["current"] is not None and self._state["position"] > 0:
            # A play was genuinely mid-flight when the state was saved;
            # it is still open (plan 045). A restored current at
            # position 0 is the queue-end shape and counts as closed.
            self._open_ref = self._state["current"]

    def _save(self, state=None):
        state = self._state if state is None else state
        if self._state_store is not None:
            self._state_store.save(state)
        elif self._state_path:
            private_write(self._state_path, json.dumps(state))

    # -- play events (plan 045) -------------------------------------------
    # The session emits play boundaries through the injected
    # record_event(kind, ref, device_id, listened) callable; it never
    # touches the store itself. Events are collected during an action
    # and flushed only on the success path, beside _save(), so a
    # failed action records nothing. Position ticks (the session
    # poll, snapshot()) are not events: only a track becoming current
    # opens a play, and only a boundary closes one.

    def _resolved_length(self, ref):
        """The resolved track's length in seconds, or None."""
        resolved = self._resolve(ref) if ref is not None else None
        duration = resolved.get("duration") if resolved else None
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) \
                and duration > 0:
            return float(duration)
        return None

    def _close_open_play(self, events, device_id, elapsed, natural_end=False,
                         force_skip=False):
        """Queue the open play's terminal event, if a play is open.

        The outcome is completed when the closing action is the
        playing client's own end-of-track report (a `next` carrying
        a matching expected_ref), or when the position reached the
        known length (a 3-second grace for the end post racing a
        poll or another device's action); anything else is a skip.
        `listened` is the ending position, clamped to the length.
        """
        if self._open_ref is None:
            return
        ref = self._open_ref
        self._open_ref = None
        length = self._resolved_length(ref)
        listened = min(elapsed, length) if length is not None else elapsed
        completed = not force_skip and (natural_end or (
            length is not None and elapsed >= length - 3))
        events.append(("completed" if completed else "skipped",
                       ref, device_id, listened))

    def _open_play(self, events, ref, device_id):
        events.append(("started", ref, device_id, None))
        self._open_ref = ref
        self._state["occurrence"] += 1

    def _flush_events(self, events):
        if self._record_event is None:
            return
        for kind, ref, device_id, listened in events:
            self._record_event(kind, ref, device_id, listened)

    def reset(self):
        with self._lock:
            events = []
            if self._open_ref is not None:
                # A library change is not a completion by any reading:
                # the open play closes as skipped (plan 045).
                self._close_open_play(events, None, self._position(),
                                      force_skip=True)
            shuffle = self._state["shuffle"]
            genre_flow = self._state["genre_flow"]
            self._state = {"version": self._state["version"] + 1, "current": None,
                           "occurrence": self._state["occurrence"],
                           "upcoming": [], "manual_count": 0, "history": [], "order": [], "shuffle": shuffle,
                           "genre_flow": genre_flow,
                           "playing": False, "position": 0.0, "changed_at": self._now()}
            self._outputs.clear_selection()
            self._save()
            self._flush_events(events)
            return self.snapshot()

    def _order_upcoming(self, state, *, restore=False):
        """Reorder the automatic lineup while keeping manual choices in place."""
        start = state["manual_count"]
        automatic = state["upcoming"][start:]
        if state["genre_flow"] and self._genre_order and state["current"] is not None:
            automatic = self._genre_order(automatic, state["current"])
        elif state["shuffle"]:
            random.shuffle(automatic)
        elif restore:
            order = {ref: index for index, ref in enumerate(state["order"])}
            automatic.sort(key=lambda ref: order.get(ref, len(order)))
        state["upcoming"][start:] = automatic

    def _position(self):
        state = self._state
        return max(0.0, state["position"] +
                   (self._now() - state["changed_at"] if state["playing"] else 0.0))

    def snapshot(self, *, output=None):
        with self._lock:
            self._outputs.touch(output)
            return self._snapshot(output)

    def observe(self, output_id):
        """Read the host's clock without extending their output presence."""
        with self._lock:
            state = self._state
            playing = state["playing"] and output_id in self._outputs.devices and self._outputs.audible((output_id,))
            return {"version": state["version"], "occurrence": state["occurrence"],
                    "current_ref": state["current"], "position": self._position(), "playing": playing}

    def _snapshot(self, output):
        state = self._state
        current = self._resolve(state["current"])
        return {"version": state["version"], "current": current,
                "buffer_reconciliation": True,
                "current_ref": state["current"], "occurrence": state["occurrence"],
                "upcoming": [self._resolve(ref) for ref in state["upcoming"][:20]],
                "queue_length": len(state["upcoming"]), "shuffle": state["shuffle"],
                "genre_flow": state["genre_flow"],
                "position": self._position(), "parked_at": state.get("parked_at") if current else None,
                **self._outputs.snapshot(state["playing"], output)}

    def _sample_position(self, action, output):
        state = self._state
        if (not state["playing"] or not self._outputs.sole_listener(output)
                or action.get("expected_version") != state["version"]
                or action.get("expected_occurrence") != state["occurrence"]
                or action.get("expected_ref") != state["current"]):
            return None
        position = action.get("position")
        return position if type(position) in (int, float) and 0 <= position <= 86400 else None

    def apply(self, action, device_id=None, *, output=None, mood_refs=None):
        op = action.get("op")
        with self._lock:
            state = self._state
            events = []
            if op == "leave":
                # A reload may register its new epoch before the departing page submits
                # its sample. The occurrence and version guard the queue independently.
                position = self._sample_position(action, output) if output and len(output) == 3 and output[2] else None
                if position is not None:
                    now = self._now()
                    updated = {**state, "playing": False, "position": position, "parked_at": now,
                               "changed_at": now, "version": state["version"] + 1}
                    self._save(updated)
                    self._state = updated
                    self._outputs.clear_selection()
                self._outputs.leave(output)
                return self._snapshot(output)
            if not self._outputs.touch(output):
                return self._snapshot(output)
            if op == "outputs":
                devices = self._outputs.validate_selection(action.get("devices"), output)
                updated = {**state, "version": state["version"] + 1}
                self._save(updated)
                self._state = updated
                self._outputs.select(devices)
                return self.snapshot(output=output)
            if op in {"queue_move", "queue_remove", "jump"} and action.get("expected_version") is not None:
                if action["expected_version"] != state["version"]:
                    raise ValueError("The queue changed on another device. Try again.")
            if op in {"next", "previous"} and action.get("expected_ref") is not None:
                if (action["expected_ref"] != state["current"]
                        or action.get("expected_occurrence", state["occurrence"]) != state["occurrence"]):
                    return self.snapshot(output=output)
                if not self._outputs.audible(output):
                    return self.snapshot(output=output)
            # Read the clock without mutating it until the action is valid.
            elapsed_position = self._position()
            if op in {"play", "play_available", "play_mood", "play_album"}:
                ready = mood_refs if op in {"play_mood", "play_album"} else self._available(action.get("playlist", "all"))
                if not ready:
                    raise ValueError("No ready songs match this mood" if op == "play_mood" else "No songs are ready in this playlist")
                selected = ready[0]
                if op == "play" or (op == "play_album" and "track_id" in action):
                    selected = action.get("track_id")
                allowed_types = (int, str) if op == "play_album" else (int,)
                if type(selected) not in allowed_types or selected not in ready:
                    raise ValueError("That song is not ready")
                position = ready.index(selected)
                state.update(current=selected, order=ready, upcoming=ready[position + 1:],
                             manual_count=0, history=ready[:position], position=0.0, playing=True)
                # A mood blend already has a weighted shuffle. An explicit genre flow
                # still takes precedence, and later transport changes work as usual.
                if op != "play_album" and (op != "play_mood" or not action.get("blend") or state["genre_flow"]):
                    self._order_upcoming(state)
                self._close_open_play(events, device_id, elapsed_position)
                self._open_play(events, state["current"], device_id)
            elif op == "resume_buffer":
                if (type(action.get("expected_version")) is not int
                        or action["expected_version"] != state["version"]
                        or action.get("expected_ref") != state["current"]
                        or action.get("expected_occurrence") != state["occurrence"]
                        or not self._outputs.audible(output)):
                    return self.snapshot(output=output)
                advances, playing = action.get("advances"), action.get("playing")
                finished = action.get("finished")
                position = action.get("position")

                def valid_position(value):
                    return type(value) is int or (type(value) is float and math.isfinite(value))

                def valid_boundary(boundary):
                    return (isinstance(boundary, dict) and type(boundary.get("natural")) is bool
                            and valid_position(boundary.get("position")))

                if (not isinstance(advances, list) or len(advances) > min(3, len(state["upcoming"]))
                        or not all(map(valid_boundary, advances)) or type(playing) is not bool
                        or not valid_position(position) or state["current"] is None
                        or (finished is not None and not valid_boundary(finished))):
                    raise ValueError("Invalid buffered position.")
                waiting_for_next = finished is not None and len(advances) < len(state["upcoming"])
                if waiting_for_next:
                    if not finished["natural"]:
                        raise ValueError("Invalid buffered position.")
                    advances = [*advances, finished]
                    finished = None
                    position, playing = 0, True
                consumed = state["upcoming"][:len(advances)]
                if any(not self._resolve(ref) for ref in consumed):
                    return self.snapshot(output=output)
                for ref, boundary in zip(consumed, advances):
                    self._close_open_play(events, device_id, max(0, min(boundary["position"], 86400)),
                                          natural_end=boundary["natural"])
                    state["history"].append(state["current"])
                    state["current"] = ref
                    self._open_play(events, ref, device_id)
                state["upcoming"] = state["upcoming"][len(advances):]
                state["manual_count"] = max(0, state["manual_count"] - len(advances))
                if finished is not None:
                    self._close_open_play(events, device_id, max(0, min(finished["position"], 86400)),
                                          natural_end=finished["natural"])
                    if playing or position > 0:
                        self._open_play(events, state["current"], device_id)
                duration = self._resolved_length(state["current"]) or 24 * 3600
                state.update(playing=playing, position=max(0, min(position, duration)))
            elif op == "play_request":
                request_id = action.get("request_id")
                if type(request_id) is not int:
                    raise ValueError("That request is not ready")
                ref = f"custom-{request_id}"
                if not self._resolve(ref):
                    raise ValueError("That request is not ready")
                state.update(current=ref, upcoming=[], manual_count=0, history=[], order=[], position=0.0, playing=True)
                self._close_open_play(events, device_id, elapsed_position)
                self._open_play(events, state["current"], device_id)
            elif op in {"enqueue", "play_next"}:
                track_id = action.get("track_id")
                if type(track_id) is not int or not self._resolve(track_id):
                    raise ValueError("Only ready songs can join the queue")
                state["upcoming"].insert(0 if op == "play_next" else state["manual_count"], track_id)
                state["manual_count"] += 1
                state["position"] = elapsed_position
            elif op in {"queue_move", "queue_remove", "jump"}:
                index = action.get("index")
                queue = state["upcoming"]
                if type(index) is not int or not 0 <= index < len(queue):
                    raise ValueError("That song is no longer in the queue")
                if op == "queue_move":
                    target = action.get("to")
                    if type(target) is not int or not 0 <= target < len(queue):
                        raise ValueError("Choose a position in the queue")
                    # Reordering changes what occupies each queue position.
                    queue.insert(target, queue.pop(index))
                    state["position"] = elapsed_position
                elif op == "queue_remove":
                    queue.pop(index)
                    if index < state["manual_count"]:
                        state["manual_count"] -= 1
                    state["position"] = elapsed_position
                else:
                    if state["current"] is not None:
                        state["history"].append(state["current"])
                    state["history"].extend(queue[:index])
                    state["current"] = queue[index]
                    state["upcoming"] = queue[index + 1:]
                    state["manual_count"] = max(0, state["manual_count"] - index - 1)
                    state["position"] = 0.0
                    state["playing"] = True
                    self._close_open_play(events, device_id, elapsed_position)
                    self._open_play(events, state["current"], device_id)
            elif op == "park":
                position = self._sample_position(action, output)
                if position is None:
                    return self.snapshot(output=output)
                state.update(playing=False, position=position, parked_at=self._now())
            elif op in {"toggle", "pause", "resume"}:
                if state["current"] is None:
                    raise ValueError("Choose a song first")
                sampled = self._sample_position(action, output) if op == "pause" else None
                state["playing"] = not state["playing"] if op == "toggle" else op == "resume"
                state["position"] = elapsed_position if sampled is None else sampled
                if sampled is not None:
                    state["parked_at"] = self._now()
                if (op in {"toggle", "resume"} and state["playing"]
                        and self._open_ref is None
                        and state["current"] is not None
                        and state["position"] == 0):
                    # A closed current (the queue-end shape) deliberately
                    # played again is a new play (plan 045). Resuming an
                    # open play crosses no boundary and records nothing.
                    self._open_play(events, state["current"], device_id)
            elif op == "seek":
                if action.get("expected_ref") is not None and action["expected_ref"] != state["current"]:
                    return self.snapshot(output=output)
                if state["current"] is None:
                    raise ValueError("Choose a song first")
                position = float(action.get("position", 0))
                if not math.isfinite(position):
                    raise ValueError("Choose a valid playback position")
                state["position"] = max(0.0, min(position, 24 * 3600))
            elif op in {"next", "previous"}:
                target = state["upcoming"] if op == "next" else state["history"]
                if target:
                    old = state["current"]
                    state["current"] = target.pop(0 if op == "next" else -1)
                    if op == "next" and state["manual_count"]:
                        state["manual_count"] -= 1
                    if old is not None:
                        if op == "next":
                            state["history"].append(old)
                        else:
                            state["upcoming"].insert(0, old)
                            # Previous explicitly puts the displaced track back at the front.
                            state["manual_count"] += 1
                    state["position"] = 0.0
                    state["playing"] = True
                    natural_end = (op == "next"
                                   and action.get("expected_ref") is not None
                                   and action.get("expected_ref") == self._open_ref)
                    self._close_open_play(events, device_id, elapsed_position,
                                          natural_end=natural_end)
                    self._open_play(events, state["current"], device_id)
                elif op == "next":
                    state["playing"] = False
                    state["position"] = 0.0
                    natural_end = (action.get("expected_ref") is not None
                                   and action.get("expected_ref") == self._open_ref)
                    self._close_open_play(events, device_id, elapsed_position,
                                          natural_end=natural_end)
                else:
                    state["position"] = elapsed_position
            elif op == "shuffle":
                state["position"] = elapsed_position
                state["shuffle"] = not state["shuffle"]
                self._order_upcoming(state, restore=not (state["shuffle"] or state["genre_flow"]))
            elif op == "genre_flow":
                state["position"] = elapsed_position
                state["genre_flow"] = not state["genre_flow"]
                if not state["genre_flow"] or self._genre_order and state["current"] is not None:
                    self._order_upcoming(state, restore=not state["genre_flow"])
            else:
                raise ValueError("Unknown player action")
            if state["playing"] or state["current"] is None:
                state["parked_at"] = None
            state["changed_at"] = self._now()
            state["version"] += 1
            self._save()
            self._flush_events(events)
            if op == "park" or (op == "pause" and sampled is not None):
                self._outputs.clear_selection()
            starts_here = op in {"play", "play_available", "play_request", "play_mood", "play_album", "resume", "toggle", "jump"}
            manual_skip = op in {"next", "previous"} and action.get("expected_ref") is None
            if state["playing"] and (starts_here or manual_skip):
                self._outputs.start(output)
            return self.snapshot(output=output)
