"""Reconcile portable list documents with one installation's editable copy."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from catalog.collection_lists import commit_documents, place_documents
from library.library_protocol import SUPPORTED, owns_derived_bytes, retire_derived
from system.private_files import private_write


def room_cache(room, app):
    identity = hashlib.sha256(os.fsencode(Path(room).resolve())).hexdigest()
    return Path(app) / "collection-cache" / identity


def workspace_paths(room, app):
    """Keep existing portable-room folders usable and new projections private."""
    root = Path(room)
    work = room_cache(root, app) / "workspace"
    return {setting: str(root / name if (root / name).is_dir() else work / name)
            for setting, name in (("lists_folder", "lists"), ("music_folder", "audio"),
                                  ("playlist_output", "playlists"), ("reports_folder", "reports"))}


def documents(directory):
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir())
            if p.is_file() and p.suffix.lower() in SUPPORTED}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_document(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temp = Path(out.name)
        try:
            out.write(content)
            out.flush()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def checked_names(mapping):
    for name in mapping:
        if not isinstance(name, str) or Path(name).name != name or Path(name).suffix.lower() not in SUPPORTED:
            raise ValueError("Invalid portable playlist name")
    return mapping


def read_marker(path, *, check_names=True):
    try:
        marker = json.loads(path.read_text())
        if not isinstance(marker, dict):
            raise ValueError("Invalid playlist cache marker")
        return checked_names(marker) if check_names else marker
    except FileNotFoundError:
        return {}


class ListProjection:
    def __init__(self, database, directory, app):
        self.database = Path(database)
        self.app = Path(app).resolve()
        self.cache = room_cache(self.database.parent, self.app)
        self.directory = self.cache / "workspace" / "lists"
        declared = Path(directory).expanduser().resolve()
        self.source = declared if declared != self.directory.resolve() else None
        self.marker = self.cache / "canonical-lists.json"
        source_key = hashlib.sha256(os.fsencode(declared)).hexdigest()
        self.source_marker = self.cache / ("source-" + source_key + ".json")
        self.observed_inputs = None

    def place(self, incoming, mode):
        expected = self.reconcile()
        names, conflicts = place_documents(self.database, incoming, mode, expected, documents(self.directory))
        if names is not None:
            self.reconcile()
        return names, conflicts

    def edit_document(self, name, edit):
        """Build a replacement from the reconciled edition and atomically check it on commit."""
        expected = self.reconcile()
        working = documents(self.directory)
        if name not in working:
            raise ValueError("This playlist has moved. Reopen it before adding a song.")
        content = edit(self.directory / name)
        if content is None:
            return False
        place_documents(self.database, [(name, content)], "replace", expected, working)
        self.reconcile()
        return True

    def publish_adapter_edits(self):
        """Flush generated mirror documents even when a catalog build is deferred."""
        expected = self.reconcile()
        working = documents(self.directory)
        commit_documents(self.database, expected, working)
        self.published({**expected, **working})

    def playlist_names(self):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT playlist_names FROM collection_info WHERE version=1").fetchone()
            return json.loads(row[0]) if row else {}

    def remember(self, docs):
        private_write(self.marker, json.dumps({name: digest(content) for name, content in docs.items()}))

    def published(self, docs):
        self.remember(docs)
        if self.observed_inputs is not None:
            self.remember_source(self.observed_inputs, {name: digest(data) for name, data in docs.items()})

    def remember_source(self, seen, canonical):
        private_write(self.source_marker, json.dumps({"inputs": seen, "canonical": canonical}))

    def update_cache(self, name, before, after, *, retain):
        """Publish only inside the private cache. An input directory is never written."""
        path = self.directory / name
        if (path.read_bytes() if path.is_file() else None) != before:
            raise RuntimeError("A playlist changed during cache reconciliation. Retry the refresh.")
        if before is not None and retain:
            backup = self.cache / "retained-lists" / digest(before) / name
            if not backup.exists():
                write_document(backup, before)
        if after is None:
            path.unlink(missing_ok=True)
        else:
            write_document(path, after)

    def remove_source(self, name, expected):
        """Delete only an adapter's identified DB version and its unedited derivatives."""
        checked_names({name: expected})
        raw = self.directory / name
        removed = {}
        with closing(sqlite3.connect(self.database, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT content FROM collection_lists WHERE name=?", (name,)).fetchone()
            if not row or digest(row[0]) != expected:
                return removed
            removed[name] = row[0]
            if raw.suffix.lower() != ".xspf":
                prefix = name + "."
                candidates = db.execute("SELECT name,content FROM collection_lists WHERE name=? OR substr(name,1,?)=?",
                                        (raw.stem + ".xspf", len(prefix), prefix))
                for sibling, content in candidates:
                    checked_names({sibling: content})
                    target = self.directory / sibling
                    if not owns_derived_bytes(raw, target, content):
                        continue
                    if not target.is_symlink() and (not target.exists() or target.read_bytes() == content):
                        removed[sibling] = content
            db.executemany("DELETE FROM collection_lists WHERE name=?", ((key,) for key in removed))
        return removed

    def import_source(self, remote):
        if not self.source or not self.source.is_dir():
            return
        inputs = documents(self.source)
        observation = read_marker(self.source_marker, check_names=False)
        last = checked_names(observation.get("inputs", {}))
        observed_canonical = checked_names(observation.get("canonical", {}))
        current_canonical = {name: digest(data) for name, data in remote.items()}
        seen = {name: digest(data) for name, data in inputs.items()}
        for name in set(last) | set(seen):
            if last.get(name) == seen.get(name):
                continue
            canonical = remote.get(name)
            canonical_hash = digest(canonical) if canonical is not None else None
            if canonical_hash != observed_canonical.get(name):
                # The collection changed since this source was last observed.
                continue
            path = self.directory / name
            before = path.read_bytes() if path.is_file() else None
            after = inputs.get(name)
            if after is None and canonical is not None:
                # A stale input can disappear after this host has observed a newer DB copy.
                # Retirement authority belongs to the input's own last bytes.
                removed = self.remove_source(name, last.get(name))
                if not removed:
                    continue
                for key in removed:
                    remote.pop(key, None)
                    current_canonical.pop(key, None)
                if path.suffix.lower() != ".xspf":
                    retire_derived(path)
            if before != after:
                self.update_cache(name, before, after, retain=before != canonical)
        # Only a committed catalog build gives newly imported bytes a database version.
        self.observed_inputs = seen
        self.remember_source(seen, current_canonical)

    def reconcile(self):
        """Apply canonical changes, keeping offline edits when their base is current."""
        if self.directory.resolve() != self.directory:
            raise ValueError("The private list workspace contains a symbolic link")
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as db:
            remote = checked_names(dict(db.execute("SELECT name,content FROM collection_lists")))
        base = read_marker(self.marker)
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        local = documents(self.directory)
        for name in set(remote) | set(base):
            incoming = remote.get(name)
            current = local.get(name)
            incoming_hash = digest(incoming) if incoming is not None else None
            if incoming == current or (current is not None and base.get(name) == incoming_hash):
                continue
            retain = current is not None and (digest(current) != base.get(name) or incoming is None)
            self.update_cache(name, current, incoming, retain=retain)
        self.import_source(remote)
        for name in base:
            path = self.directory / name
            if path.suffix.lower() != ".xspf" and not path.exists():
                retire_derived(path)
        self.remember(remote)
        return remote
