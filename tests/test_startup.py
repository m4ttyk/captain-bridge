import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_bridge.domain import ConflictError
from captain_bridge.ships import create_ship, open_ship
from captain_bridge.startup import _current_officer, _merge_prompt, _select_ship
from captain_bridge.storage import Storage


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.home = self.root / "home"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_ship(self, storage, name, repo, officer):
        storage.ships_dir.mkdir(parents=True, exist_ok=True)
        ship = storage.ships_dir / name
        ship.mkdir()
        (ship / "index.md").write_text(f"# {name}\n", encoding="utf-8")
        (ship / "metadata.json").write_text(
            json.dumps({"shipId": f"{name}-id", "name": name, "repoDir": str(repo)}),
            encoding="utf-8",
        )
        (ship / "officer.json").write_text(json.dumps(officer), encoding="utf-8")
        return ship

    def test_current_pane_overrides_inherited_officer_identity(self):
        (self.repo / ".git").mkdir()
        storage = Storage(self.home)
        created = create_ship(
            self.repo,
            "demo",
            storage=storage,
            officer={"agentName": "old-parent", "paneId": "old-pane"},
        )
        persisted = {"agentName": "old-parent", "paneId": "old-pane"}

        with patch.dict(
            os.environ,
            {"CAPTAIN_BRIDGE_OFFICER_NAME": "inherited-parent", "CAPTAIN_BRIDGE_OFFICER_ID": "parent-pane"},
            clear=True,
        ):
            identity = _current_officer(
                {"agentName": "current-officer", "pane_id": "current-pane", "status": "working"},
                persisted,
            )
            opened = open_ship(created["path"], storage=storage, officer=identity)

        self.assertEqual(identity, {"agentName": "current-officer", "paneId": "current-pane"})
        self.assertEqual(opened["officer"], identity)
        self.assertNotEqual(opened["officer"].get("agentName"), "inherited-parent")
        self.assertNotEqual(opened["officer"].get("agentName"), "old-parent")

    def test_no_current_pane_ignores_inherited_officer_identity(self):
        persisted = {"agentName": "current-officer", "paneId": "current-pane"}

        with patch.dict(
            os.environ,
            {"CAPTAIN_BRIDGE_OFFICER_NAME": "inherited-parent", "CAPTAIN_BRIDGE_OFFICER_ID": "parent-pane"},
            clear=True,
        ):
            identity = _current_officer(None, persisted)

        self.assertEqual(identity, persisted)



    def test_foreign_inherited_ship_falls_back_to_existing_current_ship(self):
        storage = Storage(self.home)
        foreign_repo = self.root / "parent-repo"
        foreign_repo.mkdir()
        foreign = self._write_ship(storage, "parent-default", foreign_repo, {"agentName": "parent"})
        local = self._write_ship(storage, "repo-default", self.repo, {"agentName": "local"})
        foreign_metadata = (foreign / "metadata.json").read_text(encoding="utf-8")
        foreign_officer = (foreign / "officer.json").read_text(encoding="utf-8")

        with patch.dict(os.environ, {"CAPTAIN_BRIDGE_SHIP": str(foreign)}):
            selected = _select_ship(self.repo, storage)

        self.assertEqual(selected, local.resolve())
        self.assertEqual((foreign / "metadata.json").read_text(encoding="utf-8"), foreign_metadata)
        self.assertEqual((foreign / "officer.json").read_text(encoding="utf-8"), foreign_officer)

    def test_foreign_inherited_ship_creates_current_checkout_ship(self):
        (self.repo / ".git").mkdir()
        storage = Storage(self.home)
        foreign_repo = self.root / "parent-repo"
        foreign_repo.mkdir()
        foreign = self._write_ship(storage, "parent-default", foreign_repo, {"paneId": "parent-pane"})
        foreign_metadata = (foreign / "metadata.json").read_text(encoding="utf-8")
        foreign_officer = (foreign / "officer.json").read_text(encoding="utf-8")

        with patch.dict(
            os.environ,
            {"CAPTAIN_BRIDGE_SHIP": str(foreign), "CAPTAIN_BRIDGE_SHIP_SLUG": "isolated"},
        ):
            selected = _select_ship(self.repo, storage)

        self.assertEqual(selected, (self.home / "ships" / "repo-isolated").resolve())
        metadata = json.loads((selected / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(Path(metadata["repoDir"]).resolve(), self.repo.resolve())
        self.assertEqual((foreign / "metadata.json").read_text(encoding="utf-8"), foreign_metadata)
        self.assertEqual((foreign / "officer.json").read_text(encoding="utf-8"), foreign_officer)

    def test_matching_env_ship_disambiguates_current_checkout(self):
        storage = Storage(self.home)
        first = self._write_ship(storage, "repo-one", self.repo, {"agentName": "one"})
        second = self._write_ship(storage, "repo-two", self.repo, {"agentName": "two"})

        with patch.dict(os.environ, {"CAPTAIN_BRIDGE_SHIP": str(second)}):
            selected = _select_ship(self.repo, storage)

        self.assertEqual(selected, second.resolve())
        self.assertNotEqual(selected, first.resolve())


    def test_merge_prompt_preserves_omp_arguments_and_custom_append(self):
        merged = _merge_prompt(
            ["--model", "gpt", "--append-system-prompt", "custom", "message"],
            "officer",
        )
        self.assertEqual(
            merged,
            ["--model", "gpt", "message", "--append-system-prompt", "officer\n\ncustom"],
        )

    def test_merge_prompt_resolves_relative_append_file_from_launch_directory(self):
        append_file = self.root / "APPEND_SYSTEM.md"
        append_file.write_text("custom file prompt\n", encoding="utf-8")

        merged = _merge_prompt(
            ["--append-system-prompt", append_file.name],
            "officer",
            cwd=self.root,
        )
        self.assertEqual(merged[-1], "officer\n\ncustom file prompt\n")

    def test_explicit_empty_officer_clears_stale_binding(self):
        (self.repo / ".git").mkdir()
        storage = Storage(self.home)
        created = create_ship(
            self.repo,
            "demo",
            storage=storage,
            officer={"agentName": "old", "paneId": "old-pane"},
        )

        opened = open_ship(created["path"], storage=storage, officer={})

        self.assertEqual(opened["officer"], {})

    def test_repository_lookup_rejects_ambiguous_registration(self):
        storage = Storage(self.home)
        storage.ships_dir.mkdir(parents=True)
        for name in ("repo-one", "repo-two"):
            ship = storage.ships_dir / name
            ship.mkdir()
            (ship / "index.md").write_text("# ship\n", encoding="utf-8")
            (ship / "metadata.json").write_text(
                json.dumps({"repoDir": str(self.repo)}),
                encoding="utf-8",
            )

        with self.assertRaisesRegex(ConflictError, "multiple ships registered"):
            storage.resolve_ship_for_repository(self.repo)


if __name__ == "__main__":
    unittest.main()
