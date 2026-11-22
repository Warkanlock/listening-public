"""Bounded tail reads for log files (plan 050 Step 3, F2).

The process view and the activity history used to read whole log
files to keep their last few dozen (or few hundred) lines. Batch
and runner logs grow without bound, so the cost of every poll grew
with them. `tail_lines` reads backwards from the end of the file in
growing blocks and stops as soon as it holds enough lines: the
bytes touched are proportional to the lines kept, not to the file.
"""

from pathlib import Path

BLOCK = 65536


def tail_lines(path, count, encoding="utf-8"):
    """The last `count` lines of a text file.

    The result matches `Path.read_text(encoding, errors="replace")
    .splitlines()[-count:]` without reading the whole file. Raises
    OSError for a missing or unreadable file, like read_text.
    """
    if count <= 0:
        return []
    with Path(path).open("rb") as handle:
        handle.seek(0, 2)
        end = handle.tell()
        if end == 0:
            return []
        chunks = []
        held_newlines = 0
        start = end
        size = BLOCK
        while start > 0:
            step = min(size, start)
            start -= step
            handle.seek(start)
            chunk = handle.read(step)
            chunks.append(chunk)
            held_newlines += chunk.count(b"\n")
            # Enough when the held bytes hold more lines than asked
            # for: the first line in the window may be a fragment of
            # a longer line, so one spare line must be in hand
            # before the fragment can be dropped safely.
            if held_newlines > count:
                break
            size *= 2
    lines = b"".join(reversed(chunks)).decode(encoding, errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines[-count:]
