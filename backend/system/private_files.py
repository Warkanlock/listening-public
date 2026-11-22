"""The one home for private file writes.

Every private file in the room (settings, credentials, tokens, playback
state, imported lists) is written through here: mode 0600, written to a
unique temporary file in the target directory, then atomically moved
over the target, so a concurrent reader or writer can never observe or
produce a torn file.
"""

import os
import tempfile
from pathlib import Path


def private_write(path, contents, *, exclusive=False):
    """Write text `contents` to `path` at mode 0600, atomically.

    With `exclusive=True` the file must not already exist: it is created
    directly with O_EXCL and FileExistsError propagates, for
    create-once files such as the Sockseek credentials config.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(contents)
        return
    descriptor, temporary = tempfile.mkstemp(dir=path.parent,
                                             prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(contents)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
