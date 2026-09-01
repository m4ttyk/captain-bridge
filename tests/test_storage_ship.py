import json
import tempfile
import unittest
from pathlib import Path

from captain_bridge.storage import Storage
from captain_bridge.ships import create_ship, open_ship


class StorageShipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_and_exclusive(self):
        s = Storage(self.path / "home")
        p = self.path / "x.json"
        s.atomic_write_json(p, {"a": 1})
        self.assertEqual(json.loads(p.read_text()), {"a": 1})
        with self.assertRaises(Exception):
            s.exclusive_write_json(p, {"a": 2})

    def test_ship_reopen(self):
        repo = self.path / "repo"
        (repo / ".git").mkdir(parents=True)
        ship = create_ship(repo, "demo", storage=Storage(self.path / "home"))
        self.assertEqual(open_ship(ship["path"], storage=Storage(self.path / "home"))["repoDir"], str(repo.resolve()))


if __name__ == "__main__":
    unittest.main()
