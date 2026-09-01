import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_bridge.domain import ConflictError, NotFoundError
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


    def test_explicit_invalid_ship_does_not_fall_back(self):
        repo = self.path / "repo"
        (repo / ".git").mkdir(parents=True)
        storage = Storage(self.path / "home")
        created = create_ship(repo, "demo", storage=storage)

        with patch.dict(os.environ, {"CAPTAIN_BRIDGE_SHIP": created["path"]}, clear=True):
            with self.assertRaises(NotFoundError):
                open_ship(self.path / "not-a-ship", storage=storage)

    def test_staged_directory_refuses_collision_and_cleans_staging(self):
        storage = Storage(self.path / "home")
        target = storage.ships_dir / "repo-demo"
        target.mkdir(parents=True)
        (target / "existing").write_text("keep")

        with self.assertRaises(ConflictError):
            with storage.staged_directory(target) as staging:
                (staging / "new").write_text("do not install")

        self.assertEqual((target / "existing").read_text(), "keep")
        self.assertFalse((target / "new").exists())
        self.assertEqual(list(storage.ships_dir.glob(".repo-demo.*")), [])

    def test_open_ship_restores_policy_defaults(self):
        repo = self.path / "repo"
        (repo / ".git").mkdir(parents=True)
        home = self.path / "relocated-home"
        with patch.dict(os.environ, {"CAPTAIN_BRIDGE_HOME": str(home)}, clear=True):
            storage = Storage()
            created = create_ship(repo, "demo", storage=storage)
            (home / "authority.md").unlink()
            for role in (home / "roles").glob("*.md"):
                role.unlink()

            open_ship(created["path"], storage=Storage())

        self.assertTrue((home / "authority.md").exists())
        self.assertEqual({p.name for p in (home / "roles").glob("*.md")}, {"builder.md", "researcher.md", "scout.md"})

    def test_failed_creation_cleans_staging_and_can_retry(self):
        repo = self.path / "repo"
        (repo / ".git").mkdir(parents=True)
        storage = Storage(self.path / "home")
        real_write = storage.exclusive_write_json
        calls = 0

        def fail_once(path, obj):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("write failed")
            return real_write(path, obj)

        with patch.object(storage, "exclusive_write_json", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "write failed"):
                create_ship(repo, "demo", storage=storage)
            ship_path = storage.ships_dir / "repo-demo"
            self.assertFalse(ship_path.exists())
            self.assertEqual(list(storage.ships_dir.glob(".repo-demo.*")), [])
            created = create_ship(repo, "demo", storage=storage)

        self.assertEqual(Path(created["path"]), ship_path)

    def test_partial_officer_override_replaces_previous_identity(self):
        repo = self.path / "repo"
        (repo / ".git").mkdir(parents=True)
        storage = Storage(self.path / "home")
        created = create_ship(
            repo,
            "demo",
            storage=storage,
            officer={"agentName": "old", "paneId": "old-pane", "legacy": "stale"},
        )

        with patch.dict(
            os.environ,
            {"CAPTAIN_BRIDGE_OFFICER_NAME": "new"},
            clear=True,
        ):
            opened = open_ship(created["path"], storage=storage)

        expected = {"agentName": "new"}
        self.assertEqual(opened["officer"], expected)
        self.assertEqual(json.loads((Path(created["path"]) / "officer.json").read_text()), expected)


if __name__ == "__main__":
    unittest.main()
