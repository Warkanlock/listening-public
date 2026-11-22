import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import sqlite3
import tempfile
import unittest

from system import state_database


class StateDatabaseTests(unittest.TestCase):
    """Plan 054: the shared connect ritual (WAL, busy timeout,
    schema in both shapes), the user_version adoption, and the
    stamp helper every store now opens and reads through.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "app" / "requests.sqlite3"

    def test_fresh_connect_creates_the_file_with_the_house_pragmas(self):
        connection = state_database.connect(self.database)
        try:
            self.assertTrue(self.database.is_file())
            self.assertEqual(
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                "wal")
            self.assertEqual(
                connection.execute("PRAGMA busy_timeout").fetchone()[0],
                30000)
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                state_database.SCHEMA_VERSION)
        finally:
            connection.close()

    def test_both_schema_shapes_leave_their_tables_in_place(self):
        script = ("CREATE TABLE IF NOT EXISTS from_script "
                  "(stamp REAL NOT NULL);")
        statements = (
            "CREATE TABLE IF NOT EXISTS from_statements "
            "(stamp REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS from_statements_stamp "
            "ON from_statements (stamp)",
        )
        connection = state_database.connect(self.database, script)
        connection.close()
        connection = state_database.connect(self.database, statements)
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            self.assertIn("from_script", tables)
            self.assertIn("from_statements", tables)
        finally:
            connection.close()
        # Re-connecting re-applies create-if-missing schema cleanly.
        connection = state_database.connect(self.database, script)
        connection.close()
        connection = state_database.connect(self.database, statements)
        connection.close()

    def test_row_factory_is_honored_when_passed_and_absent_by_default(self):
        schema = ("CREATE TABLE IF NOT EXISTS t (name TEXT);"
                  " INSERT OR IGNORE INTO t (rowid, name) VALUES (1, 'x');")
        plain = state_database.connect(self.database, schema)
        try:
            row = plain.execute("SELECT name FROM t").fetchone()
            self.assertIsInstance(row, tuple)
        finally:
            plain.close()
        factory = state_database.connect(
            self.database, schema, row_factory=sqlite3.Row)
        try:
            row = factory.execute("SELECT name FROM t").fetchone()
            self.assertEqual(row["name"], "x")
        finally:
            factory.close()

    def test_user_version_adoption_keeps_existing_tables_and_version(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(self.database))
        raw.execute("CREATE TABLE keepsake (stamp REAL)")
        raw.execute("INSERT INTO keepsake VALUES (1.25)")
        raw.commit()
        raw.close()
        connection = state_database.connect(self.database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT stamp FROM keepsake").fetchall(),
                [(1.25,)])
        finally:
            connection.close()
        raw = sqlite3.connect(str(self.database))
        raw.execute("PRAGMA user_version=1")
        raw.commit()
        raw.close()
        connection = state_database.connect(self.database)
        try:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()

    def test_table_stamp_reports_count_and_newest_write(self):
        schema = "CREATE TABLE IF NOT EXISTS stamped (stamp REAL NOT NULL);"
        connection = state_database.connect(self.database, schema)
        try:
            self.assertEqual(
                state_database.table_stamp(connection, "stamped"),
                (0, 0.0))
            connection.execute("INSERT INTO stamped VALUES (10.5)")
            connection.execute("INSERT INTO stamped VALUES (42.25)")
            connection.execute("INSERT INTO stamped VALUES (7.0)")
            connection.commit()
            self.assertEqual(
                state_database.table_stamp(connection, "stamped"),
                (3, 42.25))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
