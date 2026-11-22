"""The server's door to the media database (plan 061 step 1).

Plan 060 taught the server to serve, share, and report on
database-held audio through loose functions reading its globals;
this module is that cluster as one object. The server builds one
MediaAccess over its paths and a sources object bound to its live
state, and every route, snapshot, and job callback reads the
media database through it. Tests bind fake sources instead.

The one rule the interface carries: presence is existence. The
media database is there when its file is there, whatever the
audio_store landing setting says, so flipping the setting back
never strands a downloaded track: serving honors blob references
whenever the database exists.
"""

import logging
from pathlib import Path
import sqlite3

from catalog.audio_store import (MediaStore, PlaybackBuffer, blob_targets,
                                 custom_request_rows, materialize,
                                 read_export_status)


class MediaAccess:
    """Media database reads for one process: the serving pair, the
    settings facts, the jobs layer's dead bytes, the export and
    share work list, and the export's recorded progress.

    Constructed with the media database path, the playback buffer
    directory, the export-status path, and a sources object with
    five methods: coverage() (the coverage row dicts), records()
    (records by index), downloaded() (the downloaded-audio entry
    dicts), request(id) (one request dict or None), and
    share_folder(). A fresh MediaStore is built per call, matching
    the store's own one-connection-per-operation discipline.
    """

    def __init__(self, media_database, buffer_dir, export_status_path,
                 sources):
        self._media_database = Path(media_database)
        self._buffer = PlaybackBuffer(buffer_dir)
        self._export_status_path = Path(export_status_path)
        self._sources = sources

    def store(self):
        """The media database (plan 060) when it exists, else None."""
        if not self._media_database.is_file():
            return None
        return MediaStore(self._media_database)

    def serving(self):
        """The (store, buffer) pair the audio routes resolve
        through: both when the media database exists, neither
        when it does not."""
        store = self.store()
        if store is None:
            return None, None
        return store, self._buffer

    def release_playback_buffer(self, *, idle_only=False):
        store, buffer = self.serving()
        if buffer is not None:
            try:
                if idle_only:
                    buffer.expire(store)
                else:
                    buffer.release(store)
            except (OSError, sqlite3.Error) as error:
                logging.getLogger(__name__).warning("Could not clear the playback cache: %s", error)

    def facts(self):
        """The media database's facts for the settings snapshot, or
        None when the room has no media database."""
        store = self.store()
        if store is None:
            return None
        original, stored = store.byte_counts()
        return {"tracks": store.count(),
                "audio_bytes": original,
                "stored_bytes": stored,
                "dead_bytes": store.dead_bytes()}

    def dead_bytes(self):
        """The media database's dead bytes, or None when the room
        has no media database (the jobs layer reads None as: no
        reclaim card, the maintenance job does not exist for this
        room)."""
        store = self.store()
        return store.dead_bytes() if store is not None else None

    def targets(self):
        """The referenced blob tracks as (key, export name) pairs,
        from the sources' state (plan 060 steps 6-7); empty when
        the room has no media database."""
        store = self.store()
        if store is None:
            return []
        downloaded = self._sources.downloaded()
        customs = custom_request_rows(downloaded, self._sources.request)
        return blob_targets(store, self._sources.coverage(),
                            self._sources.records(), downloaded, customs)

    def materialize_share(self):
        """Write the blob tracks into the share folder (plan 060 Q1).

        Starting sharing materializes the database-held tracks
        beside the file-held ones, through the export machinery:
        incremental after the first pass, hash-checked as written.
        No media database or no share folder, no work. Returns how
        many files were written.
        """
        store = self.store()
        folder = self._sources.share_folder()
        if store is None or not folder:
            return 0
        written, _failed = materialize(store, self.targets(),
                                       Path(folder), preserve_existing=True)
        return written

    def export_status(self):
        """The last export run's recorded progress (plan 060 step 6)."""
        return read_export_status(self._export_status_path)
