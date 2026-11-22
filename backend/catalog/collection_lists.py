"""Atomic upload and adapter writes to the portable collection's list documents."""
from contextlib import closing
from pathlib import Path
import sqlite3
import unicodedata


PREVIOUS_SCHEMA = "CREATE TABLE IF NOT EXISTS collection_list_previous (name TEXT PRIMARY KEY, content BLOB NOT NULL)"


def document_key(name):
    # Keep uploaded collections usable on case- and normalization-insensitive volumes.
    return unicodedata.normalize("NFC", name).casefold()


def same_text(original, incoming):
    mark = b"\xef\xbb\xbf"
    return original is not None and original.removeprefix(mark) == incoming.removeprefix(mark)


def place_documents(database, incoming, mode, expected, working):
    """Commit a batch or report all name conflicts without applying any member.

    An unpublished local edit still participates in conflict resolution. SQLite
    holds the original uploaded bytes and one previous generation on replace.
    """
    incoming = [(name, text.encode("utf-8")) for name, text in incoming]
    names = {document_key(name): name for name in working}
    conflicts = []
    visible = dict(working)
    final_names = []
    previous = {}
    for name, data in incoming:
        name = names.get(document_key(name), name)
        old = visible.get(name)
        if old is not None and not same_text(old, data):
            if mode == "rename":
                path = Path(name)
                number = 2
                while document_key(f"{path.stem} ({number}){path.suffix}") in names:
                    number += 1
                name = f"{path.stem} ({number}){path.suffix}"
            elif mode == "replace":
                previous.setdefault(name, old)
            else:
                conflicts.append(name)
                continue
        final_names.append(name)
        names[document_key(name)] = name
        if not same_text(old, data):
            visible[name] = data
    if conflicts:
        return None, list(dict.fromkeys(conflicts))
    updates = {name: visible[name] for name in final_names}
    commit_documents(database, expected, updates, previous)
    return final_names, []


def commit_documents(database, expected, updates, previous=None):
    """Publish writes only if their source collection is still current."""
    with closing(sqlite3.connect(database, timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        canonical = dict(db.execute("SELECT name,content FROM collection_lists"))
        if canonical != expected:
            raise RuntimeError("The collection changed during playlist import. Retry the import.")
        if previous:
            db.execute(PREVIOUS_SCHEMA)
            db.executemany("INSERT OR REPLACE INTO collection_list_previous VALUES (?,?)", previous.items())
        db.executemany("INSERT OR REPLACE INTO collection_lists VALUES (?,?)",
                       ((name, data) for name, data in updates.items() if canonical.get(name) != data))
