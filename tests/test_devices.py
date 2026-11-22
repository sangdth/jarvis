import json
import tempfile
import unittest
from pathlib import Path

from jarvis_core.devices import DeviceStore, hash_secret


class DeviceStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.dir / "devices.json"
        self.store = DeviceStore(self.path)

    def test_a_new_device_gets_a_secret_kept_only_as_a_hash(self) -> None:
        device, secret = self.store.add("Sang's MacBook")
        self.assertTrue(device.id.startswith("dev-"))
        self.assertEqual(device.name, "Sang's MacBook")
        self.assertGreater(len(secret), 30)
        on_disk = self.path.read_text()
        self.assertNotIn(secret, on_disk)  # the file is not a credential store
        self.assertIn(hash_secret(secret), on_disk)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_the_secret_identifies_its_device_and_nothing_else(self) -> None:
        device, secret = self.store.add("laptop")
        other, other_secret = self.store.add("phone")
        self.assertEqual(self.store.verify(secret).id, device.id)
        self.assertEqual(self.store.verify(other_secret).id, other.id)
        self.assertIsNone(self.store.verify("not-a-secret"))
        self.assertIsNone(self.store.verify(""))

    def test_a_revoked_device_stops_working_and_the_others_do_not(self) -> None:
        first, first_secret = self.store.add("old laptop")
        second, second_secret = self.store.add("current laptop")
        revoked = self.store.revoke(first.id)
        self.assertTrue(revoked.revoked)
        self.assertIsNone(self.store.verify(first_secret))
        self.assertEqual(self.store.verify(second_secret).id, second.id)
        self.assertIsNone(self.store.revoke("dev-nope"))

    def test_last_seen_is_recorded_without_rewriting_every_call(self) -> None:
        _, secret = self.store.add("laptop")
        device = self.store.verify(secret)
        self.assertIsNotNone(device.last_seen)
        stamp = self.path.stat().st_mtime_ns
        self.store.verify(secret)  # inside the throttle window: no write
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)

    def test_the_file_round_trips_across_instances(self) -> None:
        device, secret = self.store.add("laptop")
        reopened = DeviceStore(self.path)
        self.assertEqual([d.id for d in reopened.devices()], [device.id])
        self.assertEqual(reopened.verify(secret).id, device.id)

    def test_a_broken_file_is_treated_as_no_devices(self) -> None:
        self.path.write_text("{not json")
        store = DeviceStore(self.path)
        self.assertEqual(store.devices(), [])
        _, secret = store.add("laptop")
        self.assertEqual(json.loads(self.path.read_text())["devices"][0]["name"], "laptop")
        self.assertEqual(store.verify(secret).name, "laptop")

    def test_names_are_trimmed_and_bounded(self) -> None:
        device, _ = self.store.add("  Sang's   MacBook  ")
        self.assertEqual(device.name, "Sang's MacBook")
        long_name, _ = self.store.add("x" * 200)
        self.assertEqual(len(long_name.name), 60)
        blank, _ = self.store.add("   ")
        self.assertEqual(blank.name, "A device")

    def test_a_device_never_exposes_its_hash(self) -> None:
        device, _ = self.store.add("laptop")
        public = device.public()
        self.assertNotIn("secret_sha256", public)
        self.assertEqual(public["id"], device.id)


if __name__ == "__main__":
    unittest.main()
