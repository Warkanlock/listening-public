"""Per-device pairing credentials: issued at registration, revocable.

Plan 021a: the pairing link is a one-time bootstrap. A browser that
arrives with it registers its device and is issued its own credential;
that credential is what its cookie holds from then on, and the host can
revoke it on its own. Only the SHA-256 hash of each credential is
persisted, so reading the store file does not yield a usable cookie.
"""

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

from system.private_files import private_write

# last_seen_at is persisted at most once per this interval per device,
# so the frontend's heartbeat cadence cannot become a write per poll.
SEEN_WRITE_INTERVAL = 5 * 60


def _credential_hash(credential):
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def room_proof(credential_hash, nonce):
    try:
        key = bytes.fromhex(credential_hash)
    except ValueError:
        return None
    return hmac.new(key, f"listening-room-wifi-v1:{nonce}".encode(), hashlib.sha256).hexdigest()


def invitation_room_proof(invitation_token, nonce):
    return room_proof(_credential_hash(invitation_token), nonce)


class DeviceStore:
    """The paired devices and their credential hashes, in one file.

    Constructed with a path (the server passes a file beside the
    config, never in the workspace). A missing or corrupt file reads
    as an empty store, the same posture as playback state: the
    consequence is devices re-register, never a crash.
    """

    def __init__(self, path, now=time.time):
        self._path = Path(path) if path else None
        self._now = now
        self._lock = threading.RLock()
        self._records = {}
        self._seen_written = {}
        if self._path:
            try:
                saved = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                saved = None
            if isinstance(saved, dict):
                for device_id, record in saved.items():
                    if not (isinstance(record, dict)
                            and isinstance(record.get("credential_hash"), str)):
                        continue
                    try:
                        entry = {"id": device_id,
                                 "name": str(record.get("name", "")),
                                 "credential_hash": record["credential_hash"],
                                 "offline_sync": record.get("offline_sync") is True,
                                 "playlist_access": record.get("playlist_access") is True,
                                 "can_invite": record.get("can_invite") is True,
                                 "paired_at": float(record.get("paired_at", 0)),
                                 "last_seen_at": float(record.get("last_seen_at", 0))}
                    except (TypeError, ValueError):
                        continue
                    self._records[device_id] = entry
                    self._seen_written[device_id] = entry["last_seen_at"]

    def _save(self):
        if self._path:
            private_write(self._path, json.dumps(self._records, indent=2))
        for device_id, record in self._records.items():
            self._seen_written[device_id] = record["last_seen_at"]

    def issue(self, device_id, name):
        """Create or replace the device's record; return the credential.

        The raw credential is returned exactly once, here, for the
        Set-Cookie on the registration response; only its hash is kept.
        """
        credential = secrets.token_urlsafe(32)
        with self._lock:
            now = self._now()
            self._records[device_id] = {"id": device_id, "name": name,
                                        "credential_hash": _credential_hash(credential),
                                        "paired_at": now, "last_seen_at": now}
            self._save()
            return credential

    def verify(self, credential):
        """The record a presented credential belongs to, or None."""
        if not credential:
            return None
        digest = _credential_hash(credential)
        with self._lock:
            for record in self._records.values():
                if hmac.compare_digest(digest, record["credential_hash"]):
                    return dict(record)
        return None

    def room_proof(self, device_id, nonce):
        """Prove room identity without the paired client sending its bearer token."""
        with self._lock:
            record = self._records.get(device_id)
            return room_proof(record["credential_hash"], nonce) if record else None

    def get(self, device_id):
        with self._lock:
            record = self._records.get(device_id)
            return dict(record) if record else None

    def touch(self, device_id, name):
        """Refresh a verified device's name and last-seen time.

        A name change persists immediately; last_seen_at reaches the
        file at most once per SEEN_WRITE_INTERVAL per device.
        """
        with self._lock:
            record = self._records.get(device_id)
            if record is None:
                return
            record["last_seen_at"] = self._now()
            renamed = bool(name) and name != record["name"]
            if renamed:
                record["name"] = name
            if renamed or (record["last_seen_at"] - self._seen_written.get(device_id, 0)
                           >= SEEN_WRITE_INTERVAL):
                self._save()

    def allow_invites(self, device_id, allowed):
        with self._lock:
            if device_id not in self._records or not isinstance(allowed, bool):
                raise ValueError("Choose a paired device and its invitation permission")
            self._records[device_id]["can_invite"] = allowed
            self._save()

    def allow_offline_sync(self, device_id, allowed):
        with self._lock:
            if device_id not in self._records or not isinstance(allowed, bool):
                raise ValueError("Choose a paired device and its offline sync permission")
            self._records[device_id]["offline_sync"] = allowed
            self._save()

    def allow_playlist_additions(self, device_id, enabled):
        with self._lock:
            if device_id not in self._records or not isinstance(enabled, bool):
                raise ValueError("Choose a paired device and its playlist permission.")
            self._records[device_id]["playlist_access"] = enabled
            self._save()

    def delete(self, device_id):
        with self._lock:
            if device_id not in self._records:
                return False
            del self._records[device_id]
            self._seen_written.pop(device_id, None)
            self._save()
            return True

    def records(self):
        with self._lock:
            return [dict(record) for record in self._records.values()]
