"""The one way anything opens the state database.

Every store in the room shares one SQLite file in the
application folder, and the two-process design (server +
runner) depends on the same connection discipline in every
store: WAL so readers and writers overlap, a 30 second busy
timeout so a locked moment is a wait and not an error. That
ritual lives here, once. Stores keep their tables, their
schemas, and their queries; this module owns how a
connection is opened and the schema version marker.

user_version: 1 is the additive-era baseline, every table
create-if-missing plus the coverage.updated_at ALTER from
plan 045. Nothing reads the version yet; the first
non-additive migration gates on it instead of sniffing
columns."""
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_SECONDS = 30


def enable_wal(connection):
    # Simultaneous first openers can both acquire read locks. SQLite can
    # return BUSY immediately when upgrading those locks to switch modes.
    deadline = time.monotonic() + BUSY_SECONDS
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL").close()
            return
        except sqlite3.OperationalError as error:
            # Python 3.9 and 3.10 do not expose sqlite_errorcode.
            code = getattr(error, "sqlite_errorcode", None)
            busy = (code & 0xff) == sqlite3.SQLITE_BUSY if code is not None else str(error) == "database is locked"
            if not busy or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


def connect(database, schema=None, *, row_factory=None):
    """Open the state database with the shared discipline.

    schema may be a script string (executescript) or an
    iterable of statements (executed in order, then a
    commit), matching the two shapes the stores carry
    today."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(database), timeout=0)
    try:
        enable_wal(connection)
        connection.execute(f"PRAGMA busy_timeout={BUSY_SECONDS * 1000}")
        if row_factory is not None:
            connection.row_factory = row_factory
        if schema is not None:
            if isinstance(schema, str):
                connection.executescript(schema)
            else:
                for statement in schema:
                    connection.execute(statement)
                connection.commit()
        if connection.execute("PRAGMA user_version").fetchone()[0] == 0:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def table_stamp(connection, table, column="stamp"):
    """(count, newest write) for a stamp-column table: how a
    reader spots another process's writes. 0.0 when empty.
    The table and column names are the caller's own
    constants, never user input."""
    count, newest = connection.execute(
        f"SELECT COUNT(*), MAX({column}) FROM {table}").fetchone()
    return (count, newest or 0.0)
