"""Member listening state in host SQLite, keyed by room and account ID."""

import json
import sqlite3
import sys
import threading
import time
from contextlib import closing
from uuid import uuid4

from playback.playback import PlaybackSession
from system import state_database


DEFAULT_THEME = "sea-glass"
THEMES = frozenset((DEFAULT_THEME, "night", "paper"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS member_favorites (
    room TEXT NOT NULL,
    member TEXT NOT NULL,
    record_key TEXT NOT NULL,
    PRIMARY KEY (room, member, record_key)
);
CREATE TABLE IF NOT EXISTS member_play_events (
    id INTEGER PRIMARY KEY,
    room TEXT NOT NULL,
    member TEXT NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    ref INTEGER NOT NULL,
    record_key TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS member_play_events_recency ON member_play_events (room, member, kind, ts, id);
CREATE TABLE IF NOT EXISTS member_playback (
    room TEXT NOT NULL,
    member TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (room, member)
);
CREATE TABLE IF NOT EXISTS member_radio_resume (
    room TEXT NOT NULL,
    member TEXT NOT NULL,
    tuning TEXT NOT NULL,
    history TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY (room, member)
);
CREATE TABLE IF NOT EXISTS private_playlists (
    room TEXT NOT NULL, member TEXT NOT NULL, id TEXT NOT NULL,
    name TEXT NOT NULL, revision INTEGER NOT NULL, tracks TEXT NOT NULL,
    PRIMARY KEY (room, member, id)
);
CREATE TABLE IF NOT EXISTS member_appearance (
    room TEXT NOT NULL,
    member TEXT NOT NULL,
    theme TEXT NOT NULL,
    PRIMARY KEY (room, member)
);
"""


class ProfileConflict(ValueError):
    """An edit was based on an older copy from another device."""


class MemberProfiles:
    """Open private listening state and reuse one clock per member in each room."""

    def __init__(self, database, room, identity):
        self.database = database
        self._lock = threading.RLock()
        self._initialized = False
        self._sessions = {}
        self._room = self._identity = None
        self.select_room(room, identity)

    def select_room(self, room, identity):
        with self._lock:
            room = str(room)
            if (room, identity) != (self._room, self._identity):
                self._sessions.clear()
                self._room = room
                self._identity = identity

    def _connect(self):
        with self._lock:
            database = state_database.connect(self.database, None if self._initialized else SCHEMA)
            self._initialized = True
            return database

    def profile(self, member):
        with self._lock:
            return MemberProfile(self._connect, self._room, (self._identity, member))

    def selected(self, profile):
        with self._lock:
            return profile.room == self._room and profile.scope[0] == self._identity

    def playback_active(self):
        with self._lock:
            sessions = list(self._sessions.values())
        return any(player.snapshot()["session_playing"] for player in sessions)

    def session(self, profile, available, resolve, genre_order, record_key):
        with self._lock:
            player = self._sessions.get(profile.scope)
            if player is None:
                def record(kind, ref, _device, _listened):
                    key = record_key(ref)
                    if key is not None:
                        profile.record(kind, ref, key)

                player = PlaybackSession(available, resolve, genre_order=genre_order, state_store=profile,
                                         record_event=record)
                self._sessions[profile.scope] = player
            return player


class MemberProfile:
    def __init__(self, connect, room, scope):
        self._connect = connect
        self.room = room
        self.scope = scope

    def appearance(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT theme FROM member_appearance WHERE room=? AND member=?", self.scope).fetchone()
        return {"theme": row[0] if row and row[0] in THEMES else DEFAULT_THEME}

    def set_appearance(self, theme):
        if not isinstance(theme, str) or theme not in THEMES:
            raise ValueError("Choose Sea Glass, Night, or Paper.")
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO member_appearance (room, member, theme) VALUES (?, ?, ?)",
                       (*self.scope, theme))
        return {"theme": theme}

    def keys(self):
        with closing(self._connect()) as db:
            return {key for key, in db.execute(
                "SELECT record_key FROM member_favorites WHERE room = ? AND member = ?", self.scope)}

    def set_favorite(self, key, favorite, aliases):
        with closing(self._connect()) as db, db:
            if favorite:
                db.execute("INSERT OR IGNORE INTO member_favorites VALUES (?, ?, ?)", (*self.scope, key))
            else:
                db.executemany("DELETE FROM member_favorites WHERE room = ? AND member = ? AND record_key = ?",
                               ((*self.scope, alias) for alias in set(aliases) | {key}))

    def record(self, kind, ref, key):
        try:
            with closing(self._connect()) as db, db:
                db.execute("INSERT INTO member_play_events (room, member, ts, kind, ref, record_key) VALUES (?, ?, ?, ?, ?, ?)",
                           (*self.scope, time.time(), kind, ref, key))
        except sqlite3.Error as error:
            print(f"Member play event not recorded ({kind}): {error}", file=sys.stderr, flush=True)

    def stats(self, *keys):
        placeholders = ",".join("?" for _ in keys)
        with closing(self._connect()) as db:
            return db.execute(f"""SELECT COUNT(*), MAX(ts) FROM member_play_events
                WHERE room = ? AND member = ? AND kind = 'started' AND record_key IN ({placeholders})""",
                (*self.scope, *keys)).fetchone()

    def recent_started(self, limit):
        with closing(self._connect()) as db:
            return [{"ts": ts, "ref": str(ref), "device_id": None} for ts, ref in db.execute("""
                SELECT ts, ref FROM member_play_events WHERE room = ? AND member = ? AND kind = 'started'
                ORDER BY ts DESC, id DESC LIMIT ?""", (*self.scope, limit))]

    def history(self):
        with closing(self._connect()) as db:
            return {key: (started, completed) for key, started, completed in db.execute("""
                SELECT record_key, MAX(CASE WHEN kind = 'started' THEN ts END),
                    COUNT(CASE WHEN kind = 'completed' THEN 1 END)
                FROM member_play_events WHERE room = ? AND member = ? AND kind IN ('started', 'completed')
                GROUP BY record_key""", self.scope)}

    def load(self):
        with closing(self._connect()) as db:
            row = db.execute("SELECT state FROM member_playback WHERE room = ? AND member = ?", self.scope).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state):
        with closing(self._connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO member_playback VALUES (?, ?, ?)", (*self.scope, json.dumps(state)))

    def _radio_resume(self, db):
        row = db.execute("SELECT tuning, history, revision FROM member_radio_resume WHERE room=? AND member=?",
                         self.scope).fetchone()
        if row is None:
            return {"revision": 0, "history": None}
        tuning, stored, revision = row
        history = json.loads(stored)
        return {"revision": revision, "history": {"tuning": tuning, **history} if history else None}

    def radio_resume(self):
        with closing(self._connect()) as db:
            return self._radio_resume(db)

    def write_radio_resume(self, operation, tuning, history, expected_revision):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            state = self._radio_resume(db)
            owns_entry = state["history"] is not None and state["history"]["tuning"] == tuning
            revision = state["revision"]
            if operation == "tune" and not owns_entry:
                if expected_revision is not None and expected_revision != revision:
                    return {"accepted": False, "revision": revision}
                revision += 1
                history["saved_at"] = time.time()
                db.execute("INSERT OR REPLACE INTO member_radio_resume VALUES (?, ?, ?, ?, ?)",
                           (*self.scope, tuning, json.dumps(history), revision))
            elif owns_entry:
                if (history is not None and "sample" in state["history"]
                        and history.get("sample", -1) <= state["history"]["sample"]):
                    return {"accepted": True, "revision": revision}
                if history is not None:
                    history["saved_at"] = time.time()
                revision += operation == "clear"
                db.execute("UPDATE member_radio_resume SET history=?, revision=? WHERE room=? AND member=?",
                           (json.dumps(history), revision, *self.scope))
            else:
                return {"accepted": False, "revision": revision}
            return {"accepted": True, "revision": revision}

    def playlists(self):
        with closing(self._connect()) as db:
            return [{"id": identity, "name": name, "revision": revision, "count": len(json.loads(tracks))}
                    for identity, name, revision, tracks in db.execute(
                        "SELECT id,name,revision,tracks FROM private_playlists WHERE room=? AND member=? ORDER BY name COLLATE NOCASE,id",
                        self.scope)]

    def _playlist_row(self, db, identity):
        row = db.execute("SELECT name,revision,tracks FROM private_playlists WHERE room=? AND member=? AND id=?",
                         (*self.scope, identity)).fetchone()
        if row is None:
            raise KeyError(identity)
        name, revision, tracks = row
        return {"id": identity, "name": name, "revision": revision, "tracks": json.loads(tracks)}

    def playlist(self, identity):
        with closing(self._connect()) as db:
            return self._playlist_row(db, identity)

    def bind_recordings(self, playlist, bindings):
        """A draft that resolves once follows that catalog identity across later editions."""
        tracks = list(playlist["tracks"])
        for position, key in bindings.items():
            tracks[position] = key
        with closing(self._connect()) as db, db:
            db.execute("""UPDATE private_playlists SET tracks=?
                WHERE room=? AND member=? AND id=? AND revision=? AND tracks=?""",
                       (json.dumps(tracks), *self.scope, playlist["id"], playlist["revision"],
                        json.dumps(playlist["tracks"])))

    def edit_playlist(self, operation, body, track_key=None):
        name = body.get("name")
        if operation in {"create", "rename"}:
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100 or not name.strip().isprintable():
                raise ValueError("Choose a playlist name up to 100 characters.")
            name = name.strip()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if operation == "create":
                count = db.execute("SELECT count(*) FROM private_playlists WHERE room=? AND member=?", self.scope).fetchone()[0]
                if count >= 200:
                    raise ValueError("This profile can hold 200 playlists.")
                identity = uuid4().hex
                db.execute("INSERT INTO private_playlists VALUES (?,?,?,?,?,?)", (*self.scope, identity, name, 1, "[]"))
                return self._playlist_row(db, identity)
            identity = body.get("id")
            if not isinstance(identity, str):
                raise ValueError("Choose a playlist.")
            playlist = self._playlist_row(db, identity)
            if type(body.get("revision")) is not int or body["revision"] != playlist["revision"]:
                raise ProfileConflict("The playlist changed on another device. Reload it and try again.")
            tracks = playlist["tracks"]
            if operation == "delete":
                db.execute("DELETE FROM private_playlists WHERE room=? AND member=? AND id=?", (*self.scope, identity))
                return None
            if operation == "rename":
                playlist["name"] = name
            elif operation in {"add", "add_metadata"}:
                if operation == "add_metadata" and track_key in tracks:
                    return playlist
                if track_key is None or len(tracks) >= 500:
                    raise ValueError("Choose a song. Each playlist can hold up to 500 songs.")
                tracks.append(track_key)
            elif operation in {"remove", "move"}:
                position = body.get("position")
                target = body.get("target")
                if type(position) is not int or not 0 <= position < len(tracks):
                    raise ValueError("Choose a song in the playlist.")
                if operation == "move" and (type(target) is not int or not 0 <= target < len(tracks)):
                    raise ValueError("Choose a destination in the playlist.")
                key = tracks.pop(position)
                if operation == "move":
                    tracks.insert(target, key)
            else:
                raise ValueError("Unknown playlist action.")
            playlist["revision"] += 1
            db.execute("UPDATE private_playlists SET name=?,revision=?,tracks=? WHERE room=? AND member=? AND id=?",
                       (playlist["name"], playlist["revision"], json.dumps(tracks), *self.scope, identity))
            return playlist
