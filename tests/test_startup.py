import json
import tempfile
import unittest
from pathlib import Path

from captain_bridge.domain import ConflictError
from captain_bridge.ships import create_ship, open_ship
from captain_bridge.startup import _merge_prompt
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
