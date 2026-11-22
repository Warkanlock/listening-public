import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from fixture_environment import isolate_room_environment as setUpModule
from system import owner_auth
from system.owner_auth import AccountRole, Household, OwnerAuth, SESSION_AGE


OWNER_PASSWORD = "Fixture owner password"
MEMBER_PASSWORD = "Fixture member password"
NEW_PASSWORD = "Changed member password"


class HouseholdTests(unittest.TestCase):
    def setUp(self):
        room = tempfile.TemporaryDirectory()
        self.addCleanup(room.cleanup)
        self.folder = Path(room.name)
        self.owner = OwnerAuth(self.folder)
        self.household = Household(self.folder)

    def install_owner(self):
        self.owner.set_password("RoomOwner", OWNER_PASSWORD)

    def test_member_identity_persists_and_does_not_grant_owner_access(self):
        with self.assertRaisesRegex(ValueError, "Set the room owner"):
            self.household.add_member("alice", MEMBER_PASSWORD)
        self.install_owner()
        alice = self.household.add_member("Alice", MEMBER_PASSWORD)
        reopened = Household(self.folder)
        owner, listener = reopened.accounts()
        self.assertEqual((owner.id, owner.role), ("owner", AccountRole.OWNER))
        self.assertEqual((listener, listener.role), (alice, AccountRole.MEMBER))

        account, member_token = reopened.login("ALICE", MEMBER_PASSWORD)
        self.assertEqual(account, alice)
        self.assertEqual(self.household.session(member_token), alice)
        self.assertIsNone(self.owner.session_username(member_token))
        self.assertFalse(self.owner.verify_cli(member_token))
        self.assertIsNone(self.household.session(self.owner.cli_token()))
        self.assertIsNone(self.household.login("RoomOwner", OWNER_PASSWORD))
        self.assertIsNone(self.owner.login("Alice", MEMBER_PASSWORD))
        database = self.owner.path.read_bytes()
        self.assertNotIn(MEMBER_PASSWORD.encode(), database)
        self.assertNotIn(member_token.encode(), database)

    def test_casefold_name_collision_does_not_change_either_account(self):
        self.install_owner()
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.household.add_member("ROOMOWNER", MEMBER_PASSWORD)
        alice = self.household.add_member("Alice", MEMBER_PASSWORD)
        self.household.set_enabled(alice.id, False)
        with self.assertRaisesRegex(ValueError, "already in use"):
            self.household.add_member("alice", MEMBER_PASSWORD)
        _, owner_token = self.owner.login("roomowner", OWNER_PASSWORD)
        with self.assertRaisesRegex(ValueError, "already belongs"):
            self.owner.set_password("aLiCe", NEW_PASSWORD)
        self.assertEqual(self.owner.session_username(owner_token), "RoomOwner")
        self.assertTrue(any(member.id == alice.id and not member.enabled for member in self.household.accounts()))

    def test_concurrent_owner_rename_and_member_setup_cannot_share_a_username(self):
        self.install_owner()
        start = Barrier(2)

        def try_create(operation, username, password):
            start.wait(timeout=5)
            try:
                operation(username, password)
            except ValueError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as threads:
            rename = threads.submit(try_create, self.owner.set_password, "SharedName", OWNER_PASSWORD)
            add = threads.submit(try_create, self.household.add_member, "SHAREDNAME", MEMBER_PASSWORD)
            self.assertNotEqual(rename.result(timeout=10), add.result(timeout=10))
        shared = [account for account in self.household.accounts() if account.username.lower() == "sharedname"]
        self.assertEqual(len(shared), 1)

    def test_reset_disable_and_revoke_are_isolated_to_one_member(self):
        self.install_owner()
        alice = self.household.add_member("Alice", MEMBER_PASSWORD)
        bob = self.household.add_member("Bob", MEMBER_PASSWORD)
        _, first = self.household.login("Alice", MEMBER_PASSWORD)
        _, second = self.household.login("Alice", MEMBER_PASSWORD)
        _, other = self.household.login("Bob", MEMBER_PASSWORD)

        self.household.logout(first)
        self.assertIsNone(self.household.session(first))
        self.assertEqual(self.household.session(second), alice)

        self.household.reset_password(alice.id, NEW_PASSWORD)
        self.assertIsNone(self.household.session(second))
        self.assertIsNone(self.household.login("Alice", MEMBER_PASSWORD))
        account, third = self.household.login("Alice", NEW_PASSWORD)
        self.assertEqual(account.id, alice.id)

        self.household.set_enabled(alice.id, False)
        self.assertIsNone(self.household.session(third))
        self.assertIsNone(self.household.login("Alice", NEW_PASSWORD))
        self.household.reset_password(alice.id, MEMBER_PASSWORD)
        self.assertIsNone(self.household.login("Alice", MEMBER_PASSWORD))
        self.household.set_enabled(alice.id, True)
        _, fourth = self.household.login("Alice", MEMBER_PASSWORD)
        self.assertEqual(self.household.revoke_sessions(alice.id), 1)
        self.assertIsNone(self.household.session(fourth))
        self.assertEqual(self.household.session(other), bob)

    def test_upgrade_of_an_existing_owner_database_preserves_owner_session(self):
        self.install_owner()
        _, existing_session = self.owner.login("RoomOwner", OWNER_PASSWORD)
        with closing(sqlite3.connect(self.owner.path)) as database, database:
            database.executescript("DROP TABLE household_sessions; DROP TABLE household_members;")
            tables = {name for name, in database.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
        self.assertEqual(tables, {"owner_credentials", "owner_sessions"})

        alice = self.household.add_member("Alice", MEMBER_PASSWORD)
        self.assertEqual(self.household.login("Alice", MEMBER_PASSWORD)[0], alice)
        self.assertEqual(self.owner.session_username(existing_session), "RoomOwner")

    def test_member_session_expiration(self):
        self.install_owner()
        alice = self.household.add_member("Alice", MEMBER_PASSWORD)
        _, token = self.household.login("Alice", MEMBER_PASSWORD)
        self.assertEqual(self.household.session(token), alice)
        expired = SimpleNamespace(time=lambda: time.time() + SESSION_AGE + 1)
        with mock.patch.object(owner_auth, "time", expired):
            self.assertIsNone(self.household.session(token))
