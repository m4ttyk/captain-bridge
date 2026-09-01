import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_bridge.domain import ConflictError
from captain_bridge.runtime import (
    launch_assignment,
    observe_assignment,
    wake_officer,
)


ASSIGNMENT_ID = "assignment_23456789"


def completed(payload=None, *, returncode=0, stderr=""):
    stdout = json.dumps(payload) if payload is not None else ""
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.ship = self.root / "ship"
        prompt_dir = self.ship / "assignments" / ASSIGNMENT_ID
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "prompt.md").write_text("Do the assigned work.", encoding="utf-8")
        (self.ship / "metadata.json").write_text(
            json.dumps({"repoDir": str(self.repo)}), encoding="utf-8"
        )
        (self.ship / "officer.json").write_text(
            json.dumps({"agentName": "officer", "paneId": "w1:p1"}), encoding="utf-8"
        )
        self.assignment = {"id": ASSIGNMENT_ID}
        self.role = {"repository": "read", "model": "test-model", "effort": "low"}

    @patch("captain_bridge.runtime.subprocess.run")
    def test_read_launch_uses_repo_dir_and_submits_prompt_without_waiting(self, run):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer"}}}),
            completed({"result": {"pane": {"pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]

        facts = launch_assignment(self.ship, self.assignment, self.role)

        self.assertEqual(facts["agentName"], ASSIGNMENT_ID)
        self.assertEqual(facts["paneId"], "w1:p2")
        self.assertEqual(facts["worktreeDir"], str(self.repo.resolve()))
        split = run.call_args_list[2].args[0]
        self.assertIn(f"CAPTAIN_BRIDGE_SHIP={self.ship.resolve()}", split)
        self.assertIn(f"CAPTAIN_BRIDGE_ASSIGNMENT={ASSIGNMENT_ID}", split)
        self.assertIn("CAPTAIN_BRIDGE_OFFICER=officer", split)
        self.assertIn(str(self.repo.resolve()), split)
        prompt = run.call_args_list[-1].args[0]
        self.assertEqual(prompt, ["herdr", "agent", "prompt", ASSIGNMENT_ID, "Do the assigned work."])
        self.assertNotIn("--wait", prompt)

    @patch("captain_bridge.runtime.subprocess.run")
    def test_worktree_launch_creates_named_branch_at_canonical_path(self, run):
        run.side_effect = [
            completed(returncode=1),
            completed(returncode=1),
            completed(),
            completed({"result": {"pane": {"pane_id": "w1:p1"}}}),
            completed({"result": {"pane": {"pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]
        role = {**self.role, "repository": "worktree"}
        expected = (self.repo.parent / ".captain-bridge-worktrees" / ASSIGNMENT_ID).resolve()

        facts = launch_assignment(self.ship, self.assignment, role)

        self.assertEqual(facts["worktreeDir"], str(expected))
        self.assertEqual(
            run.call_args_list[2].args[0],
            [
                "git",
                "worktree",
                "add",
                "-b",
                f"captain/{ASSIGNMENT_ID}",
                str(expected),
            ],
        )
        self.assertEqual(run.call_args_list[2].kwargs["cwd"], self.repo.resolve())

    @patch("captain_bridge.runtime.subprocess.run")
    def test_repeated_launch_returns_complete_live_facts_without_duplication(self, run):
        facts = {
            "agentName": ASSIGNMENT_ID,
            "paneId": "w1:p2",
            "worktreeDir": str(self.repo),
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        assignment = {**self.assignment, "runtime": facts}
        run.return_value = completed(
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}
        )

        self.assertEqual(launch_assignment(self.ship, assignment, self.role), facts)
        run.assert_called_once()

    @patch("captain_bridge.runtime.subprocess.run")
    def test_repeated_launch_rejects_partial_facts_before_subprocess(self, run):
        assignment = {**self.assignment, "runtime": {"agentName": ASSIGNMENT_ID}}

        with self.assertRaises(ConflictError):
            launch_assignment(self.ship, assignment, self.role)
        run.assert_not_called()

    @patch("captain_bridge.runtime.subprocess.run")
    def test_observation_returns_best_effort_live_evidence(self, run):
        assignment = {
            **self.assignment,
            "runtime": {
                "agentName": ASSIGNMENT_ID,
                "paneId": "w1:p2",
                "worktreeDir": str(self.repo),
                "launchedAt": "2026-01-01T00:00:00Z",
            },
        }
        run.return_value = completed(
            {
                "result": {
                    "agent": {
                        "name": ASSIGNMENT_ID,
                        "pane_id": "w1:p2",
                        "agent_status": "working",
                        "revision": 7,
                    }
                }
            }
        )

        self.assertEqual(
            observe_assignment(self.ship, assignment),
            {
                "available": True,
                "status": "working",
                "agentName": ASSIGNMENT_ID,
                "paneId": "w1:p2",
                "revision": 7,
            },
        )

    @patch("captain_bridge.runtime.subprocess.run")
    def test_failed_wake_tries_officer_name_then_pane_without_mutating_event(self, run):
        run.side_effect = [completed(returncode=1), completed(returncode=1)]
        event = {"kind": "result-ready", "assignmentId": ASSIGNMENT_ID}
        original = dict(event)

        self.assertFalse(wake_officer(self.ship, event))
        self.assertEqual(event, original)
        self.assertEqual(
            [call.args[0][3] for call in run.call_args_list],
            ["officer", "w1:p1"],
        )


if __name__ == "__main__":
    unittest.main()
