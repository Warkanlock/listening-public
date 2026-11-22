"""The one library refresh pipeline.

Owns the question "did the library sources change, and if so, rebuild
what depends on them". The cheap half is stat_signature(): path, size,
and mtime only. It never opens file contents and never writes, so read
paths may call it freely; the old signature ran the full watched-folder
sync (which content-sniffs every candidate file) from GET handlers and
from the watchdog every 10 seconds.

RefreshPipeline.refresh_if_changed() is the single entry point for the
expensive half. The process-specific steps (which paths to watch, how
to sync, how to rebuild, how to reload) are injected by the server, so
this module stays free of server globals and is testable in isolation.
"""

from pathlib import Path

from library.library_protocol import xspf_files

def stat_signature(paths):
    """Fingerprint files by path, size, and mtime; strictly read-only.

    Accepts files and directories (a directory contributes the files
    directly inside it). A path that cannot be stat'ed is simply
    absent, so a file appearing or disappearing changes the signature.
    Contents are never opened: an unreadable file still fingerprints.
    """
    signature = {}
    for raw in paths:
        path = Path(raw)
        candidates = [path]
        if path.is_dir():
            candidates = sorted((item for item in path.iterdir() if item.is_file()),
                                key=lambda item: item.name.casefold())
        for candidate in candidates:
            try:
                stat = candidate.stat()
            except OSError:
                continue
            signature[str(candidate)] = [stat.st_size, stat.st_mtime_ns]
    return signature


def manifest_signature(directory):
    """The signature shape the manifest stores: XSPF name -> [size, mtime_ns]."""
    signature = {}
    for path in xspf_files(directory):
        try:
            stat = path.stat()
        except OSError:
            continue
        signature[path.name] = [stat.st_size, stat.st_mtime_ns]
    return signature


def directory_signature(directory):
    """{name: [size, mtime_ns]} for the files directly inside a
    directory, any extension; {} when the directory is absent.

    The all-files sibling of manifest_signature (which is the
    XSPF-only shape the list build stores). The audio folder's
    fingerprint uses this shape on both sides of the refresh
    comparison, the runner's stored copy and the server's
    current one, so the two cannot drift apart."""
    signature = {}
    folder = Path(directory)
    if not folder.is_dir():
        return signature
    candidates = sorted((item for item in folder.iterdir() if item.is_file()),
                        key=lambda item: item.name.casefold())
    for candidate in candidates:
        try:
            stat = candidate.stat()
        except OSError:
            continue
        signature[candidate.name] = [stat.st_size, stat.st_mtime_ns]
    return signature


class RefreshPipeline:
    """Compares, syncs, and rebuilds, once, in one place.

    inputs:          callable returning the paths whose stat signature
                     gates the sync (watched candidates, linked files,
                     and the canonical list directory's files)
    canonical_dir:   callable returning the canonical XSPF directory
    stored_signature: callable returning the source signature the
                     stored catalog build was made from (plan 036: read
                     from the state database), or None when no build
                     exists
    audio_signature: callable returning the audio folder's current
                     directory_signature (plan 055: the audio folder
                     is a build input too, plan 030)
    stored_audio_signature: callable returning the audio signature
                     the stored catalog build was made from, or None
                     for a build that predates plan 055 (which reads
                     as unsettled exactly once, until a rebuild
                     records one)
    report_exists:   callable returning True once a report has run (a
                     missing report forces a rebuild, as the old
                     missing-summary check did)
    sync:            callable mirroring watched sources into the list
                     directory and canonicalizing it
    rebuild:         callable regenerating the catalog build and
                     report; raises on failure
    reload:          callable reloading the in-memory catalog after a
                     rebuild
    """

    def __init__(self, *, inputs, canonical_dir, stored_signature,
                 audio_signature, stored_audio_signature,
                 report_exists, sync, rebuild, reload):
        self._inputs = inputs
        self._canonical_dir = canonical_dir
        self._stored_signature_fn = stored_signature
        self._audio_signature = audio_signature
        self._stored_audio_signature_fn = stored_audio_signature
        self._report_exists = report_exists
        self._sync = sync
        self._rebuild = rebuild
        self._reload = reload
        self._last_inputs = None

    def _stored(self, fn):
        try:
            return fn()
        except Exception:
            return None

    def _stored_signature(self):
        return self._stored(self._stored_signature_fn)

    def _stored_audio_signature(self):
        return self._stored(self._stored_audio_signature_fn)

    def _settled(self):
        """True when inputs, catalog build, report, and the audio
        folder all agree with the stored build."""
        return (self._stored_signature() == manifest_signature(self._canonical_dir())
                and self._report_exists()
                and self._stored_audio_signature() == self._audio_signature())

    def settled(self):
        """The public read of _settled() for decisions that may
        only trust the stored build once it reflects the folders
        as they stand (plan 062's migration stamp is one)."""
        return self._settled()

    def synced(self):
        """Whether a refresh pass has completed in this process:
        the watched sources have been folded into the canonical
        directory at least once, so the signatures _settled()
        compares cover them too. False at boot, however settled
        the stored build looks."""
        return self._last_inputs is not None

    def refresh_if_changed(self, force=False):
        """Sync and rebuild when the sources moved; return True on a rebuild.

        A burst of calls after one change pays for exactly one sync:
        once settled, the stat signature matches the last one seen and
        the call is a pure stat comparison. With force=True the sync
        and rebuild run regardless, for handlers that just rewrote the
        inputs themselves.
        """
        current = stat_signature(self._inputs())
        if not force and current == self._last_inputs and self._settled():
            return False
        self._sync()
        changed = force or not self._settled()
        if changed:
            self._rebuild()
            self._reload()
        self._last_inputs = stat_signature(self._inputs())
        return changed
