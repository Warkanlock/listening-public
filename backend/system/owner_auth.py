"""Host-private account credentials and revocable browser sessions."""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
from pathlib import Path
from uuid import uuid4


DATABASE_NAME = "owner-auth.sqlite3"
COOKIE_NAME = "lr_owner"
MEMBER_COOKIE_NAME = "lr_member"
SESSION_AGE = 365 * 24 * 60 * 60
SESSION_RENEWAL_WINDOW = 30 * 24 * 60 * 60
PASSWORD_ROUNDS = 600_000
RETRY_DELAY = 30
MAX_LOGIN_FAILURES = 5
USERNAME = re.compile(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,63}")
TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
MEMBER_TOKEN = re.compile(r"mbr_[A-Za-z0-9_-]{43}")
STREAM_TOKEN = re.compile(r"lr_sub_[A-Za-z0-9_-]{43}")
STREAM_SCHEMA = """CREATE TABLE IF NOT EXISTS subsonic_keys (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, digest BLOB NOT NULL UNIQUE)"""
SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_credentials (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    rounds INTEGER NOT NULL,
    cli_token TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS owner_sessions (
    token_hash BLOB PRIMARY KEY,
    expires_at REAL NOT NULL
);
"""

MEMBER_SCHEMA = """
CREATE TABLE IF NOT EXISTS household_members (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL,
    rounds INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1))
);
CREATE TABLE IF NOT EXISTS household_sessions (
    token_hash BLOB PRIMARY KEY,
    member_id TEXT NOT NULL REFERENCES household_members (id) ON DELETE CASCADE,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS household_sessions_member ON household_sessions (member_id);
"""


class AccountRole(str, Enum):
    OWNER = "owner"
    MEMBER = "member"


@dataclass(frozen=True)
class Account:
    id: str
    username: str
    role: AccountRole
    enabled: bool


class OwnerLoginThrottled(ValueError):
    """The owner login should be retried after its rate limit clears."""


def _password_key(password, salt, rounds):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).digest()


def _check_username(username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ValueError("Use 1–64 letters, numbers, dots, dashes or underscores for the username.")


def _password_fields(password):
    if not isinstance(password, str) or len(password) < 12 or len(password.encode("utf-8")) > 1024:
        raise ValueError("Use a password of at least 12 characters and at most 1,024 UTF-8 bytes.")
    salt = secrets.token_bytes(32)
    return salt, _password_key(password, salt, PASSWORD_ROUNDS), PASSWORD_ROUNDS


def _valid_login_input(username, password):
    return (isinstance(username, str) and isinstance(password, str) and len(username) <= 64
            and len(password.encode("utf-8", errors="replace")) <= 1024)


def _matches_password(password, record):
    # Derive a key for unknown accounts as well so they take the usual work.
    salt = record["salt"] if record is not None else bytes(32)
    rounds = record["rounds"] if record is not None else PASSWORD_ROUNDS
    try:
        key = _password_key(password, salt, rounds)
    except UnicodeEncodeError:
        return False
    return record is not None and hmac.compare_digest(key, record["password_hash"])


class OwnerAuth:
    """Keep account secrets in the host config, outside collection exports."""

    def __init__(self, config_dir):
        self.path = Path(config_dir).resolve() / DATABASE_NAME
        self._activated = self.path.exists()
        self._login_lock = threading.Lock()
        self._login_failures = 0
        self._retry_at = 0.0

    @property
    def enabled(self):
        self._activated = self._activated or self.path.exists()
        return self._activated

    def _connect(self):
        metadata = self.path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise OSError("The owner authentication database must be a private file owned by the server user.")
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @cached_property
    def household(self):
        return Household(self.path.parent, owner_auth=self)

    def create_stream_key(self, name):
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or not name.isprintable():
            raise ValueError("Choose a key name up to 80 characters.")
        if not self.enabled:
            raise ValueError("Set the room owner before creating a streaming key.")
        token = "lr_sub_" + secrets.token_urlsafe(32)
        identifier = uuid4().hex
        with closing(self._connect()) as db, db:
            if not db.execute("SELECT 1 FROM owner_credentials WHERE id=1").fetchone():
                raise ValueError("Set the room owner before creating a streaming key.")
            db.execute(STREAM_SCHEMA)
            db.execute("INSERT INTO subsonic_keys VALUES (?,?,?)", (identifier, name.strip(), _token_hash(token)))
        return identifier, token

    def _has_stream_keys(self, db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='subsonic_keys'").fetchone()

    def stream_keys(self):
        if not self.enabled:
            return []
        with closing(self._connect()) as db:
            if not self._has_stream_keys(db):
                return []
            return list(db.execute("SELECT id, name FROM subsonic_keys ORDER BY name, id"))

    def revoke_stream_key(self, identifier):
        if not self.enabled:
            return False
        with closing(self._connect()) as db, db:
            return bool(self._has_stream_keys(db) and db.execute(
                "DELETE FROM subsonic_keys WHERE id=?", (identifier,)).rowcount)

    def verify_stream_key(self, token):
        if not isinstance(token, str) or not STREAM_TOKEN.fullmatch(token) or not self.enabled:
            return False
        with closing(self._connect()) as db:
            return bool(self._has_stream_keys(db) and db.execute(
                "SELECT 1 FROM subsonic_keys WHERE digest=?", (_token_hash(token),)).fetchone())

    @staticmethod
    def _revoke_spoken(connection, account):
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='spoken_devices'").fetchone():
            connection.execute("DELETE FROM spoken_codes WHERE account=?", (account,))
            connection.execute("DELETE FROM spoken_devices WHERE account=?", (account,))

    def set_password(self, username, password):
        """Create or replace the owner and revoke all existing owner access."""
        _check_username(username)
        fields = _password_fields(password)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA + MEMBER_SCHEMA)
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("SELECT 1 FROM household_members WHERE username = ?", (username,)).fetchone():
                    raise ValueError("That username already belongs to a household member.")
                connection.execute("""INSERT OR REPLACE INTO owner_credentials
                    (id, username, salt, password_hash, rounds, cli_token) VALUES (1, ?, ?, ?, ?, ?)""",
                    (username, *fields, secrets.token_urlsafe(32)))
                connection.execute("DELETE FROM owner_sessions")
                self._revoke_spoken(connection, "owner")
        self._activated = True

    def cli_token(self):
        """Read the local control credential through filesystem authority."""
        if not self.enabled:
            return None
        with closing(self._connect()) as connection:
            record = connection.execute("SELECT cli_token FROM owner_credentials WHERE id = 1").fetchone()
            return record["cli_token"] if record else None

    def verify_cli(self, token):
        if not TOKEN.fullmatch(token):
            return False
        expected = self.cli_token()
        return bool(expected and hmac.compare_digest(token, expected))

    def login(self, username, password):
        """Validate credentials and return the canonical username and a new session."""
        result = self._login(username, password, include_members=False)
        return (result[0].username, result[1]) if result else None

    def sign_in(self, username, password):
        """Use one rate limit for both owner and member sign-ins over HTTP."""
        return self._login(username, password, include_members=True)

    def _login(self, username, password, *, include_members):
        if not _valid_login_input(username, password):
            return None
        if not self._login_lock.acquire(blocking=False):
            raise OwnerLoginThrottled
        try:
            if time.monotonic() < self._retry_at:
                raise OwnerLoginThrottled
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT * FROM owner_credentials WHERE id = 1").fetchone()
                if row is not None and username.lower() == row["username"].lower():
                    result = self._create_owner_session(connection, row, password)
                elif include_members:
                    result = self.household.login(username, password)
                else:
                    _matches_password(password, row)
                    result = None
            if result:
                self._login_failures = 0
                return result
            self._login_failures += 1
            if self._login_failures >= MAX_LOGIN_FAILURES:
                self._retry_at = time.monotonic() + RETRY_DELAY
                self._login_failures = 0
            return None
        finally:
            self._login_lock.release()

    def _create_owner_session(self, connection, row, password):
        if not _matches_password(password, row):
            return None
        token = secrets.token_urlsafe(32)
        now = time.time()
        with connection:
            connection.execute("DELETE FROM owner_sessions WHERE expires_at <= ?", (now,))
            inserted = connection.execute("""INSERT INTO owner_sessions (token_hash, expires_at)
                SELECT ?, ? FROM owner_credentials WHERE id = 1 AND salt = ?""",
                (_token_hash(token), now + SESSION_AGE, row["salt"]))
        return (Account("owner", row["username"], AccountRole.OWNER, True), token) if inserted.rowcount else None

    def session_username(self, token):
        if not TOKEN.fullmatch(token):
            return None
        with closing(self._connect()) as connection:
            row = connection.execute("""SELECT username FROM owner_credentials, owner_sessions
                WHERE id = 1 AND token_hash = ? AND expires_at > ?""",
                (_token_hash(token), time.time())).fetchone()
            return row["username"] if row else None

    def renew_session(self, token, *, member=False):
        """Extend a session near expiry and return its remaining cookie age."""
        pattern, table = (MEMBER_TOKEN, "household_sessions") if member else (TOKEN, "owner_sessions")
        if not pattern.fullmatch(token):
            return None
        now = time.time()
        digest = _token_hash(token)
        lookup = f"SELECT expires_at FROM {table} WHERE token_hash = ? AND expires_at > ?"
        with closing(self._connect()) as connection, connection:
            session = connection.execute(lookup, (digest, now)).fetchone()
            if session is None:
                return None
            if session["expires_at"] <= now + SESSION_RENEWAL_WINDOW:
                connection.execute(
                    f"UPDATE {table} SET expires_at = ? WHERE token_hash = ? AND expires_at > ? AND expires_at <= ?",
                    (now + SESSION_AGE, digest, now, now + SESSION_RENEWAL_WINDOW),
                )
                session = connection.execute(lookup, (digest, now)).fetchone()
        # Resend the actual expiry during the renewal window so a lost
        # response cannot leave the browser on the previous, shorter lease.
        remaining = session["expires_at"] - now if session is not None else 0
        return int(remaining) if remaining >= SESSION_AGE - SESSION_RENEWAL_WINDOW else None

    def logout(self, token):
        if TOKEN.fullmatch(token):
            with closing(self._connect()) as connection, connection:
                connection.execute("DELETE FROM owner_sessions WHERE token_hash = ?", (_token_hash(token),))


def _member(row):
    return Account(row["id"], row["username"], AccountRole.MEMBER, bool(row["enabled"]))


class Household:
    """Manage host-local members and verify only their own login sessions."""

    def __init__(self, config_dir, *, owner_auth=None):
        self._owner_auth = owner_auth or OwnerAuth(config_dir)
        self._initialized = False

    @contextmanager
    def _database(self):
        if not self._owner_auth.enabled:
            raise ValueError("Set the room owner before managing household accounts.")
        with closing(self._owner_auth._connect()) as connection:
            if not self._initialized:
                connection.executescript(MEMBER_SCHEMA)
                self._initialized = True
            yield connection

    def accounts(self):
        with self._database() as connection, connection:
            connection.execute("BEGIN")
            owner = connection.execute("SELECT username FROM owner_credentials WHERE id = 1").fetchone()
            members = connection.execute("SELECT id, username, enabled FROM household_members ORDER BY username")
            accounts = [Account("owner", owner["username"], AccountRole.OWNER, True)] if owner else []
            return [*accounts, *map(_member, members)]

    def add_member(self, username, password):
        _check_username(username)
        fields = _password_fields(password)
        account = Account(str(uuid4()), username, AccountRole.MEMBER, True)
        with self._database() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT username FROM owner_credentials WHERE id = 1").fetchone()
            if owner is None:
                raise ValueError("Set the room owner before managing household accounts.")
            if username.lower() == owner["username"].lower() or connection.execute(
                    "SELECT 1 FROM household_members WHERE username = ?", (username,)).fetchone():
                raise ValueError("That username is already in use.")
            connection.execute("""INSERT INTO household_members (id, username, salt, password_hash, rounds, enabled)
                                  VALUES (?, ?, ?, ?, ?, 1)""", (account.id, username, *fields))
        return account

    @staticmethod
    def _revoke(connection, member_id):
        OwnerAuth._revoke_spoken(connection, member_id)
        return connection.execute("DELETE FROM household_sessions WHERE member_id = ?", (member_id,)).rowcount

    def reset_password(self, member_id, password):
        fields = _password_fields(password)
        with self._database() as connection, connection:
            updated = connection.execute("UPDATE household_members SET salt = ?, password_hash = ?, rounds = ? WHERE id = ?",
                                         (*fields, member_id))
            if not updated.rowcount:
                raise ValueError("Unknown household member.")
            self._revoke(connection, member_id)

    def set_enabled(self, member_id, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("Choose whether the household member can sign in.")
        with self._database() as connection, connection:
            updated = connection.execute("UPDATE household_members SET enabled = ? WHERE id = ?", (enabled, member_id))
            if not updated.rowcount:
                raise ValueError("Unknown household member.")
            if not enabled:
                self._revoke(connection, member_id)

    def revoke_sessions(self, member_id):
        with self._database() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if not connection.execute("SELECT 1 FROM household_members WHERE id = ?", (member_id,)).fetchone():
                raise ValueError("Unknown household member.")
            return self._revoke(connection, member_id)

    def login(self, username, password):
        if not _valid_login_input(username, password):
            return None
        with self._database() as connection:
            record = connection.execute("SELECT * FROM household_members WHERE username = ?", (username,)).fetchone()
            if not _matches_password(password, record) or not record["enabled"]:
                return None
            token = "mbr_" + secrets.token_urlsafe(32)
            now = time.time()
            with connection:
                connection.execute("DELETE FROM household_sessions WHERE expires_at <= ?", (now,))
                row = connection.execute("""INSERT INTO household_sessions (token_hash, member_id, expires_at)
                    SELECT ?, id, ? FROM household_members WHERE id = ? AND enabled = 1 AND salt = ?""",
                    (_token_hash(token), now + SESSION_AGE, record["id"], record["salt"]))
            return (_member(record), token) if row.rowcount else None

    def session(self, token):
        if not isinstance(token, str) or not MEMBER_TOKEN.fullmatch(token):
            return None
        with self._database() as connection:
            record = connection.execute("""SELECT member.id, member.username, member.enabled
                FROM household_sessions AS session JOIN household_members AS member ON member.id = session.member_id
                WHERE session.token_hash = ? AND session.expires_at > ? AND member.enabled = 1""",
                (_token_hash(token), time.time())).fetchone()
            return _member(record) if record is not None else None

    def logout(self, token):
        if isinstance(token, str) and MEMBER_TOKEN.fullmatch(token):
            with self._database() as connection, connection:
                connection.execute("DELETE FROM household_sessions WHERE token_hash = ?", (_token_hash(token),))
