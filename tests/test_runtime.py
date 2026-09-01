import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from captain_bridge.domain import ConflictError, OperationError
from captain_bridge.runtime import (
    launch_assignment,
    message_crewmate,
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
        self.assignment = {
            "id": ASSIGNMENT_ID,
            "repoDir": str(self.repo.resolve()),
            "repository": "read",
            "model": "test-model",
            "effort": "low",
        }

    @patch("captain_bridge.runtime.subprocess.run")
    def test_read_launch_uses_repo_dir_and_submits_prompt_without_waiting(self, run):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer"}}}),
            completed({"result": {"pane": {"pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]

        binding = launch_assignment(self.ship, self.assignment)

        self.assertEqual(binding["agentName"], ASSIGNMENT_ID)
        self.assertEqual(binding["paneId"], "w1:p2")
        self.assertEqual(binding["worktreeDir"], str(self.repo.resolve()))
        split = run.call_args_list[2].args[0]
        self.assertIn(f"CAPTAIN_BRIDGE_SHIP={self.ship.resolve()}", split)
        self.assertIn(f"CAPTAIN_BRIDGE_ASSIGNMENT={ASSIGNMENT_ID}", split)
        self.assertIn("CAPTAIN_BRIDGE_OFFICER=officer", split)
        self.assertIn(str(self.repo.resolve()), split)
        prompt = run.call_args_list[-1].args[0]
        self.assertEqual(prompt, ["herdr", "agent", "prompt", ASSIGNMENT_ID, "Do the assigned work."])
        self.assertNotIn("--wait", prompt)
        self.assertEqual(
            run.call_args_list[3].args[0][-5:],
            ["--", "--model", "test-model", "--thinking", "low"],
        )

    @patch("captain_bridge.runtime.subprocess.run")
    def test_worktree_launch_creates_named_branch_at_canonical_path(self, run):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed(returncode=1),
            completed(),
            completed({"result": {"pane": {"pane_id": "w1:p1"}}}),
            completed({"result": {"pane": {"pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]
        assignment = {**self.assignment, "repository": "worktree"}
        expected = (self.repo.parent / ".captain-bridge-worktrees" / ASSIGNMENT_ID).resolve()

        binding = launch_assignment(self.ship, assignment)

        self.assertEqual(binding["worktreeDir"], str(expected))
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
    def test_repeated_launch_returns_complete_live_binding_without_duplication(self, run):
        binding = {
            "agentName": ASSIGNMENT_ID,
            "paneId": "w1:p2",
            "worktreeDir": str(self.repo),
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        assignment = {**self.assignment, "runtime": binding}
        run.return_value = completed(
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}
        )

        self.assertEqual(launch_assignment(self.ship, assignment), binding)
        run.assert_called_once()


    @patch("captain_bridge.runtime.subprocess.run")
    def test_repeated_launch_rejects_partial_binding_before_subprocess(self, run):
        assignment = {**self.assignment, "runtime": {"agentName": ASSIGNMENT_ID}}

        with self.assertRaises(ConflictError):
            launch_assignment(self.ship, assignment)
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
    def test_failed_wake_only_treats_explicit_not_found_as_absent(self, run):
        run.return_value = completed(
            returncode=1, stderr='{"error":{"code":"agent_not_found"}}'
        )
        event = {"kind": "result-ready", "assignmentId": ASSIGNMENT_ID}

        self.assertFalse(wake_officer(self.ship, event))
        self.assertEqual([item.args[0][3] for item in run.call_args_list], ["officer"])

    @patch("captain_bridge.runtime.subprocess.run")
    def test_herdr_outage_is_not_treated_as_missing_agent(self, run):
        run.return_value = completed(
            returncode=1, stderr='{"error":{"code":"daemon_unavailable"}}'
        )

        with self.assertRaises(OperationError):
            observe_assignment(self.ship, self.assignment)

    @patch("captain_bridge.runtime.subprocess.run")
    def test_metadata_repo_mismatch_is_rejected_before_herdr(self, run):
        assignment = {**self.assignment, "repoDir": str(self.root / "other")}

        with self.assertRaisesRegex(ConflictError, "repoDir does not match"):
            launch_assignment(self.ship, assignment)
        run.assert_not_called()

    @patch("captain_bridge.runtime.subprocess.run")
    def test_existing_agent_without_binding_is_ambiguous_partial_launch(self, run):
        run.return_value = completed(
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p9"}}}
        )

        with self.assertRaisesRegex(ConflictError, "without persisted launch facts"):
            launch_assignment(self.ship, self.assignment)
        run.assert_called_once()
    @patch("captain_bridge.runtime.subprocess.run")
    def test_orphaned_worktree_is_a_partial_launch_conflict(self, run):
        expected = self.repo.parent / ".captain-bridge-worktrees" / ASSIGNMENT_ID
        expected.mkdir(parents=True)
        run.return_value = completed(
            returncode=1, stderr='{"error":{"code":"agent_not_found"}}'
        )
        assignment = {**self.assignment, "repository": "worktree"}

        with self.assertRaisesRegex(ConflictError, "worktree without matching agent"):
            launch_assignment(self.ship, assignment)
        run.assert_called_once()

    @patch("captain_bridge.runtime.subprocess.run")
    def test_observation_marks_pane_reuse_as_stale_and_unavailable(self, run):
        assignment = {
            **self.assignment,
            "runtime": {
                "agentName": ASSIGNMENT_ID,
                "paneId": "w1:old",
                "worktreeDir": str(self.repo),
                "launchedAt": "2026-01-01T00:00:00Z",
            },
        }
        run.return_value = completed(
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:new"}}}
        )

        self.assertEqual(
            observe_assignment(self.ship, assignment),
            {"available": False, "status": "stale", "agentName": ASSIGNMENT_ID, "paneId": "w1:old"},
        )

    @patch("captain_bridge.runtime.subprocess.run")
    def test_observation_rejects_noncanonical_persisted_identity_before_lookup(self, run):
        assignment = {
            **self.assignment,
            "runtime": {
                "agentName": "other-agent",
                "paneId": "w1:p2",
                "worktreeDir": str(self.repo),
                "launchedAt": "2026-01-01T00:00:00Z",
            },
        }

        self.assertEqual(
            observe_assignment(self.ship, assignment),
            {
                "available": False,
                "status": "stale",
                "agentName": "other-agent",
                "paneId": "w1:p2",
            },
        )
        run.assert_not_called()

    @patch("captain_bridge.runtime.subprocess.run")
    def test_prompt_failure_tears_down_created_pane_and_worktree(self, run):
        assignment = {**self.assignment, "repository": "worktree"}
        expected = self.repo.parent / ".captain-bridge-worktrees" / ASSIGNMENT_ID

        def create_worktree(*_):
            expected.mkdir(parents=True)
            return expected

        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1"}}}),
            completed({"result": {"pane": {"pane_id": "w1:p2"}}}),
            completed(),
            completed(returncode=1, stderr='{"error":{"code":"agent_prompt_stalled"}}'),
            completed(),
            completed(),
        ]
        with patch("captain_bridge.runtime._create_worktree", side_effect=create_worktree):
            with self.assertRaises(OperationError):
                launch_assignment(self.ship, assignment)
        self.assertEqual(run.call_args_list[-2].args[0], ["herdr", "pane", "close", "w1:p2"])
        self.assertEqual(run.call_args_list[-1].args[0][:3], ["git", "worktree", "remove"])
        self.assertEqual(run.call_args_list[-1].kwargs["cwd"], self.repo.resolve())

    @patch("captain_bridge.runtime.subprocess.run")
    def test_officer_wake_uses_recorded_pane_id_without_agent_name(self, run):
        (self.ship / "officer.json").write_text(json.dumps({"paneId": "w1:p9"}))
        run.return_value = completed({"result": {"ok": True}})

        self.assertTrue(wake_officer(self.ship, {"kind": "result-ready"}))
        self.assertEqual(
            run.call_args_list[0].args[0][:4],
            ["herdr", "agent", "prompt", "w1:p9"],
        )

    @patch("captain_bridge.runtime.subprocess.run")
    def test_message_requires_live_matching_agent_and_pane(self, run):
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
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}
        )

        self.assertTrue(message_crewmate(self.ship, assignment, "Continue."))
        self.assertEqual(
            [item.args[0] for item in run.call_args_list],
            [
                ["herdr", "agent", "get", ASSIGNMENT_ID],
                ["herdr", "agent", "prompt", ASSIGNMENT_ID, "Continue."],
            ],
        )

if __name__ == "__main__":
    unittest.main()
