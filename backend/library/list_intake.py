"""List intake: writing portable lists into the Library.

The portable list files are the Library's source of truth, and
this module owns the discipline for writing them: an identical
re-import is a no-op, a replace first snapshots the outgoing
file into a .previous folder (one generation), a rename derives
the next free name, and only a differing same-name file is a
conflict. Handlers decide HTTP; this module decides files.

A Source-owned list is never written back to. Enforcing that
rule is the caller's business (the merge handler refuses
source-owned targets); this module writes what it is given.
"""

import hashlib
import shutil
from pathlib import Path

from system.private_files import private_write

PREVIOUS_DIRNAME = ".previous"


def preserve_legacy_copy(path, expected):
    """Back up an exact legacy generator output before adopting its stable name."""
    if path.is_symlink() or not path.is_file() or path.read_bytes() != expected:
        return False
    digest = hashlib.sha256(expected).hexdigest()
    directory = path.parent
    for name in (PREVIOUS_DIRNAME, "legacy-mirrors", digest):
        directory = directory / name
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            return False
        directory.mkdir(exist_ok=True)
    backup = directory / path.name
    try:
        private_write(backup, expected.decode("utf-8"), exclusive=True)
    except FileExistsError:
        if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != expected:
            return False
    return not path.is_symlink() and path.is_file() and path.read_bytes() == expected


def _read_stored_list(path):
    """The stored list's text, or None when unreadable or not UTF-8."""
    try:
        return Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None


def _stored_list_matches(path, content):
    """True when the file at path already holds this exact list.

    The stored copy is read as UTF-8 with an optional byte-order mark
    and the incoming copy loses one leading mark too, so re-importing
    the same file is a no-op whether or not it carries a BOM.
    """
    stored = _read_stored_list(path)
    if stored is None:
        return False
    if content.startswith("\ufeff"):
        content = content[1:]
    return stored == content


def _free_import_name(folder, filename):
    stem, suffix = Path(filename).stem, Path(filename).suffix
    number = 2
    while (folder / f"{stem} ({number}){suffix}").exists():
        number += 1
    return f"{stem} ({number}){suffix}"


def list_conflicts(folder, incoming):
    """Names from (filename, content) pairs whose on-disk file exists
    with different content. A missing file proceeds and an identical
    file is a no-op; only a differing same-name file conflicts."""
    return [filename for filename, content in incoming
            if (folder / filename).is_file()
            and not _stored_list_matches(folder / filename, content)]


def write_lists(folder, incoming, on_conflict):
    """Write validated (filename, content) pairs into the list folder.

    Identical re-imports are no-ops in every mode (the file, and its
    mtime, stay untouched). Under "replace", the outgoing file is
    first snapshotted into the .previous folder inside the source
    directory, same filename, one generation only. Under "rename", a
    differing list lands under a derived free name (Name (2).xspf,
    then (3), ...). Any other mode reaches here only after the caller
    found no conflicts, so a differing file found now can only be an
    intra-bundle duplicate and keeps the old last-wins behavior.
    Returns the final names used, in incoming order.
    """
    final_names = []
    for filename, content in incoming:
        target = folder / filename
        if target.is_file():
            if _stored_list_matches(target, content):
                final_names.append(filename)
                continue
            if on_conflict == "replace":
                snapshot = folder / PREVIOUS_DIRNAME / filename
                stored = _read_stored_list(target)
                if stored is None:
                    snapshot.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(target, snapshot)
                else:
                    private_write(snapshot, stored)
            elif on_conflict == "rename":
                filename = _free_import_name(folder, filename)
                target = folder / filename
        private_write(target, content)
        final_names.append(filename)
    return final_names


def place_lists(folder, incoming, on_conflict):
    """Write incoming lists, or report why not.

    With on_conflict "replace" or "rename", writes and
    returns (final_names, []). With any other value (the
    caller has not chosen a mode), a differing same-name
    file is a conflict: nothing is written and the result
    is (None, [conflicting names]). With no conflicts,
    writes with mode None (identical files are no-ops;
    a differing file can then only be an intra-bundle
    duplicate and keeps last-wins) and returns
    (final_names, [])."""
    if on_conflict not in {"replace", "rename"}:
        conflicts = list_conflicts(folder, incoming)
        if conflicts:
            return None, conflicts
        on_conflict = None
    return write_lists(folder, incoming, on_conflict), []
