import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import json
import os
import stat
import tempfile
import unittest

from devices.device_store import DeviceStore


class DeviceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "devices.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_persistence_round_trip_across_instances(self):
        store = DeviceStore(self.path)
        credential = store.issue("device-0001", "Kitchen tablet")
        reopened = DeviceStore(self.path)
        record = reopened.verify(credential)
        self.assertIsNotNone(record)
        self.assertEqual(record["id"], "device-0001")
        self.assertEqual(record["name"], "Kitchen tablet")
        self.assertEqual(reopened.get("device-0001")["credential_hash"],
                         record["credential_hash"])
        self.assertEqual([item["id"] for item in reopened.records()], ["device-0001"])

    def test_store_file_is_mode_0600(self):
        DeviceStore(self.path).issue("device-0001", "Kitchen tablet")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_file_bytes_never_contain_an_issued_credential(self):
        store = DeviceStore(self.path)
        credentials = [store.issue(f"device-000{i}", f"Device {i}") for i in range(3)]
        contents = self.path.read_text(encoding="utf-8")
        for credential in credentials:
            self.assertNotIn(credential, contents)
        # What is stored instead is the SHA-256 hash of each credential.
        import hashlib
        for credential in credentials:
            self.assertIn(hashlib.sha256(credential.encode("utf-8")).hexdigest(), contents)

    def test_verify_accepts_the_issued_credential_and_rejects_others(self):
        store = DeviceStore(self.path)
        credential = store.issue("device-0001", "Kitchen tablet")
        self.assertIsNotNone(store.verify(credential))
        self.assertIsNone(store.verify("some-other-credential"))
        self.assertIsNone(store.verify(""))
        self.assertIsNone(store.verify(None))

    def test_issue_replaces_the_prior_credential_for_the_device(self):
        store = DeviceStore(self.path)
        first = store.issue("device-0001", "Kitchen tablet")
        second = store.issue("device-0001", "Kitchen tablet")
        self.assertNotEqual(first, second)
        self.assertIsNone(store.verify(first))
        self.assertIsNotNone(store.verify(second))
        self.assertEqual(len(store.records()), 1)

    def test_delete_ends_verification(self):
        store = DeviceStore(self.path)
        credential = store.issue("device-0001", "Kitchen tablet")
        self.assertTrue(store.delete("device-0001"))
        self.assertIsNone(store.verify(credential))
        self.assertIsNone(store.get("device-0001"))
        self.assertFalse(store.delete("device-0001"))
        self.assertIsNone(DeviceStore(self.path).verify(credential))

    def test_corrupt_file_reads_as_an_empty_store(self):
        self.path.write_text("{not json", encoding="utf-8")
        store = DeviceStore(self.path)
        self.assertEqual(store.records(), [])
        credential = store.issue("device-0001", "Kitchen tablet")
        self.assertIsNotNone(store.verify(credential))

    def test_non_record_file_reads_as_an_empty_store(self):
        self.path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        self.assertEqual(DeviceStore(self.path).records(), [])

    def test_missing_file_reads_as_an_empty_store(self):
        self.assertEqual(DeviceStore(self.path).records(), [])

    def test_touch_updates_name_and_last_seen(self):
        clock = [1000.0]
        store = DeviceStore(self.path, now=lambda: clock[0])
        store.issue("device-0001", "Old name")
        clock[0] = 2000.0
        store.touch("device-0001", "New name")
        record = DeviceStore(self.path).get("device-0001")
        self.assertEqual(record["name"], "New name")
        self.assertEqual(record["last_seen_at"], 2000.0)
        store.touch("no-such-device", "Name")  # no record, no error


if __name__ == "__main__":
    unittest.main()
