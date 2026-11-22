"""Reconcile derived catalog references with independently owned audio.

Explicit migration copies source audio. Coverage and playlist membership
may change without removing any bytes from the collection.
"""

import json
from pathlib import Path

from catalog.audio_resolution import locate_stored
from catalog.audio_store import (blob_key, blob_ref, is_blob_ref,
                                 request_is_stale, source_stamp)
from downloads.verification import judge, judge_claim


class BlobIndex:
    """The blob-backed catalog records, by index: (reference, probe).

    Candidacy is exactly report()'s (plan 060). A verified coverage
    row naming a blob reference contributes first, re-judged later
    against the probe stored at landing. A downloaded catalog
    request then bridges a fresh landing into its first report, but
    only into a gap: a record whose coverage already verifies a
    different path keeps that path, so a stale request can never
    reinstate an old key (request_is_stale). Either way the entry
    counts only while its stored probe exists. Custom requests
    (no track index) are never catalog candidacy.
    """

    def __init__(self, store, coverage_rows, downloaded=None, records=()):
        self._rows = {}
        by_index = {row["index"]: row for row in coverage_rows}

        def add(index, reference):
            facts = store.probe(blob_key(reference))
            if facts is not None:
                self._rows[index] = (reference, facts)

        for index, row in by_index.items():
            if row["status"] == "verified" and is_blob_ref(row["path"]):
                add(index, row["path"])
        if downloaded is not None:
            for entry in downloaded:
                index = entry["track_index"]
                if not index or not is_blob_ref(entry["audio_path"]) \
                        or index in self._rows:
                    continue
                if request_is_stale(by_index.get(index),
                                    entry["audio_path"]):
                    continue
                add(index, entry["audio_path"])

        if records:
            owners = store.owned_catalog_audio()
            for record in records:
                if record["index"] not in self._rows and record["key"] in owners:
                    self._rows[record["index"]] = owners[record["key"]]

    def get(self, index):
        """The (reference, probe) for an index, or None."""
        return self._rows.get(index)

    def references(self):
        """The whole map, index to (reference, probe)."""
        return self._rows


def ingest_verified_file(store, path, probe, owner=None):
    """Copy verified bytes into the collection. Owner sources remain in place."""
    return blob_ref(store.put_file(path, probe, owners=(owner,) if owner else ()))


# The migration's recorded progress (plan 062): one small JSON
# file in the application folder, migration-status.json. The
# migration worker writes it as it runs; the server reads it for
# the migrate job's card. The shape is owned here so the two
# processes cannot drift apart on what an idle or partial record
# means (the export codec's discipline, plan 061 step 1).

MIGRATION_STATUS_IDLE = {"state": "idle", "done": 0, "total": 0,
                         "migrated": 0, "skipped": 0, "already": 0,
                         "skipped_files": [], "stamp": 0}


def migration_status_record(state, done, total, counts, stamp,
                            skipped_files=()):
    """One migration run's progress record, in the shape
    read_migration_status reads back. The codec owns the shape in
    both directions: writers build records here, readers parse
    them here. skipped_files names the existing files the run
    could not probe as audio (as strings): they stay files, and
    a completed run's list is what lets the server treat them
    as settled instead of owing another run."""
    return {"state": state, "done": done, "total": total,
            "migrated": counts.get("migrated", 0),
            "skipped": counts.get("skipped", 0),
            "already": counts.get("already", 0),
            "skipped_files": list(skipped_files),
            "stamp": stamp}


def read_migration_status(path):
    """The recorded migration progress at path, or the idle
    default. A missing, unreadable, malformed, or non-dict file
    reads as idle, exactly as the export status does."""
    try:
        status = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(MIGRATION_STATUS_IDLE)
    return status if isinstance(status, dict) \
        else dict(MIGRATION_STATUS_IDLE)


def dumps_migration_status(status):
    """The serialized form of a migration status record. The
    write itself stays with each process's own writer, because
    the runner's private_write is atomic at mode 0600 and a
    status file must never be observed torn."""
    return json.dumps(status)


def unmigrated_files(coverage_rows, downloaded, library, settled=()):
    """The library's existing files the migration still owes a
    move for (plan 062): the same participation migrate_library
    applies (verified coverage rows and downloaded entries,
    resolved with locate_stored), minus the files a completed
    run already settled as skipped. The server's completion
    check reads through this, so the two cannot drift: a file
    the routine would move is owed, a file it settled is not,
    and a file it has never seen is owed."""
    settled = {str(path) for path in settled}
    owed = []
    values = [row.get("path") or "" for row in coverage_rows
              if row.get("status") == "verified"]
    values += [entry.get("audio_path") or "" for entry in downloaded]
    for value in values:
        if not value or is_blob_ref(value):
            continue
        located = locate_stored(value, library)
        if located is None or not located.is_file():
            continue
        resolved = located.resolve()
        if str(resolved) not in settled and resolved not in owed:
            owed.append(resolved)
    return owed


def migrate_library(store, coverage_rows, downloaded, library,
                    rewrite_coverage, rewrite_downloaded, probe_file,
                    on_progress=None, on_skip=None, records=()):
    """Move the library's file-backed entries into the store
    (plan 062): the one-time migration, as a pure routine over
    injected seams so it can be tested against a fabricated
    library.

    The participants are the verified coverage rows and every
    downloaded request entry. Each names either a blob reference
    (already home; counted as already) or a file under the
    library. A file's move follows the per-file discipline:
    probe it, copy its bytes into the store and rewrite every reference.
    Preserve the original, including an external symlink target.
    A file that is missing, or that probe_file cannot confirm as
    audio, stays a file and counts as skipped; it blocks nothing.
    Two entries naming one file share its move; two files with
    identical bytes become one blob with two references, which
    is the store's hash keying, not a special case.

    rewrite_coverage(index, reference) and
    rewrite_downloaded(request_id, reference) are the two stores'
    write paths, probe_file(path) returns the probe facts or
    None, and on_progress(done, total, counts), when given, is
    called as entries settle. on_skip(resolved), when given, is
    called once per existing file that cannot be probed as
    audio, so the run's caller can record it as settled.
    Returns the counts dict:
    {"migrated": n, "skipped": n, "already": n}, counted per
    entry, not per file.
    """
    counts = {"migrated": 0, "skipped": 0, "already": 0}
    library = Path(library)
    work = []
    by_index = {record["index"]: record for record in records}
    for row in coverage_rows:
        if row.get("status") == "verified":
            index = row["index"]
            record = by_index.get(index, row)
            owner = ("catalog", record["key"]) if "key" in record else ("catalog-index", str(index))
            work.append((row.get("path") or "", owner, record.get("title"), record.get("length"), None,
                         lambda reference, index=row["index"]:
                             rewrite_coverage(index, reference)))
    for entry in downloaded:
        work.append((entry.get("audio_path") or "", ("request", str(entry["id"])),
                     entry.get("title"), entry.get("length"), entry,
                     lambda reference, request_id=entry["id"]:
                         rewrite_downloaded(request_id, reference)))
    total = len(work)
    done = 0

    def settled():
        if on_progress is not None:
            on_progress(done, total, counts)

    # Group the file-naming entries by the file they resolve to,
    # in first-seen order (coverage before requests: the catalog
    # is the primary reference). Blob references and values that
    # name no existing file settle where they stand.
    groups = {}
    order = []
    for value, owner, title, length, request, rewrite in work:
        if is_blob_ref(value):
            counts["already"] += 1
            done += 1
            continue
        located = locate_stored(value, library) if value else None
        if located is None or not located.is_file():
            counts["skipped"] += 1
            done += 1
            continue
        resolved = located.resolve()
        if resolved not in groups:
            groups[resolved] = []
            order.append(resolved)
        groups[resolved].append((owner, title, length, request, rewrite))
    settled()
    for resolved in order:
        claims = groups[resolved]
        try:
            before = source_stamp(resolved.stat())
            facts = probe_file(resolved)
            if source_stamp(resolved.stat()) != before:
                facts = None
        except OSError:
            facts = None
        accepted = [(owner, rewrite) for owner, title, length, request, rewrite in claims
                    if title and judge_claim(title, length, facts, resolved.suffix, request).ok]
        if accepted:
            try:
                reference = blob_ref(store.put_file(resolved, facts, expected_stamp=before,
                                                    owners=[owner for owner, _ in accepted]))
            except OSError:
                if resolved.is_file() and source_stamp(resolved.stat()) == before:
                    raise
                accepted = []
            else:
                for _, rewrite in accepted:
                    rewrite(reference)
        if len(accepted) < len(claims) and on_skip is not None:
            on_skip(resolved)
        counts["migrated"] += len(accepted)
        counts["skipped"] += len(claims) - len(accepted)
        done += len(claims)
        settled()
    return counts
