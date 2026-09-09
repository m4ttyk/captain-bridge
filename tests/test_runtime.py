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
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2", "workspace_id": "w1"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]

        binding = launch_assignment(self.ship, self.assignment)

        self.assertEqual(binding["agentName"], ASSIGNMENT_ID)
        self.assertEqual(binding["paneId"], "w1:p2")
        self.assertEqual(binding["worktreeDir"], str(self.repo.resolve()))
        create = run.call_args_list[2].args[0]
        self.assertEqual(create[:4], ["herdr", "tab", "create", "--workspace"])
        self.assertIn("w1", create)
        self.assertIn("--cwd", create)
        self.assertIn(str(self.repo.resolve()), create)
        self.assertIn("--label", create)
        self.assertIn(ASSIGNMENT_ID, create)
        self.assertIn(f"CAPTAIN_BRIDGE_SHIP={self.ship.resolve()}", create)
        self.assertIn(f"CAPTAIN_BRIDGE_ASSIGNMENT={ASSIGNMENT_ID}", create)
        self.assertIn("CAPTAIN_BRIDGE_OFFICER=officer", create)
        self.assertIn("--no-focus", create)
        prompt = run.call_args_list[-1].args[0]
        self.assertEqual(prompt, ["herdr", "agent", "prompt", ASSIGNMENT_ID, "Do the assigned work."])
        self.assertNotIn("--wait", prompt)

    @patch("captain_bridge.runtime.time.sleep")
    @patch("captain_bridge.runtime.subprocess.run")
    def test_read_launch_retries_transient_pane_busy_before_prompt(self, run, sleep):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed(
                returncode=1,
                stderr='{"error":{"code":"agent_pane_busy","message":"pane is still starting"}}',
            ),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed({"result": {"ok": True}}),
        ]

        binding = launch_assignment(self.ship, self.assignment)

        self.assertEqual(binding["paneId"], "w1:p2")
        sleep.assert_called_once_with(0.1)
        start_calls = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["herdr", "agent", "start"]
        ]
        self.assertEqual(len(start_calls), 2)
        prompt_calls = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["herdr", "agent", "prompt"]
        ]
        self.assertEqual(len(prompt_calls), 1)


    @patch("captain_bridge.runtime.time.sleep")
    @patch("captain_bridge.runtime.subprocess.run")
    def test_read_launch_non_busy_start_failure_is_immediate(self, run, sleep):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed(
                returncode=1,
                stderr='{"error":{"code":"agent_start_failed","message":"start failed"}}',
            ),
            completed({"result": {"ok": True}}),
        ]

        with self.assertRaisesRegex(OperationError, "start failed"):
            launch_assignment(self.ship, self.assignment)

        sleep.assert_not_called()
        start_calls = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:3] == ["herdr", "agent", "start"]
        ]
        self.assertEqual(len(start_calls), 1)
        self.assertEqual(run.call_args_list[-1].args[0], ["herdr", "tab", "close", "w1:t2"])

    @patch("captain_bridge.runtime.subprocess.run")
    def test_root_pane_parse_failure_closes_created_tab(self, run):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "workspace_id": "w1"}}}),
            completed({"result": {"tab": {"tab_id": "w1:t2"}}}),
            completed({"result": {"ok": True}}),
        ]

        with self.assertRaisesRegex(OperationError, "root pane ID"):
            launch_assignment(self.ship, self.assignment)

        self.assertEqual(run.call_args_list[-1].args[0], ["herdr", "tab", "close", "w1:t2"])

    @patch("captain_bridge.runtime.time.sleep")
    @patch("captain_bridge.runtime.subprocess.run")
    def test_read_launch_persistent_pane_busy_is_bounded_and_rolls_back(self, run, sleep):
        start_calls = 0

        def herdr(args, **_):
            nonlocal start_calls
            if args[:4] == ["herdr", "agent", "get", ASSIGNMENT_ID]:
                return completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}')
            if args[:3] == ["herdr", "pane", "current"]:
                return completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer", "workspace_id": "w1"}}})
            if args[:3] == ["herdr", "tab", "create"]:
                return completed(
                    {
                        "result": {
                            "tab": {"tab_id": "w1:t2"},
                            "root_pane": {"pane_id": "w1:p2"},
                        }
                    }
                )
            if args[:3] == ["herdr", "agent", "start"]:
                start_calls += 1
                return completed(
                    returncode=1,
                    stderr='{"error":{"code":"agent_pane_busy","message":"pane is still starting"}}',
                )
            if args[:3] == ["herdr", "tab", "close"]:
                return completed({"result": {"ok": True}})
            raise AssertionError(args)

        run.side_effect = herdr
        with self.assertRaisesRegex(OperationError, "pane is still starting"):
            launch_assignment(self.ship, self.assignment)

        self.assertEqual(start_calls, 6)
        self.assertEqual(sleep.call_count, 5)
        self.assertEqual(run.call_args_list[-1].args[0][:3], ["herdr", "tab", "close"])

    @patch("captain_bridge.runtime.subprocess.run")
    def test_worktree_launch_creates_named_branch_at_canonical_path(self, run):
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            subprocess.CompletedProcess(
                [],
                0,
                f"worktree {self.repo.resolve()}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n",
                "",
            ),
            completed(returncode=1),
            completed(),
            completed({"result": {"pane": {"pane_id": "w1:p1", "name": "officer", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]
        assignment = {**self.assignment, "repository": "worktree"}
        expected = (self.repo / ".worktrees" / ASSIGNMENT_ID).resolve()

        binding = launch_assignment(self.ship, assignment)

        self.assertEqual(binding["worktreeDir"], str(expected))
        self.assertEqual(
            run.call_args_list[3].args[0],
            [
                "git",
                "worktree",
                "add",
                "-b",
                f"captain/{ASSIGNMENT_ID}",
                str(expected),
            ],
        )
        self.assertEqual(run.call_args_list[3].kwargs["cwd"], self.repo.resolve())
        self.assertEqual(
            (self.repo / ".git" / "info" / "exclude").read_text(),
            "/.worktrees/\n",
        )

    @patch("captain_bridge.runtime.subprocess.run")
    def test_linked_checkout_uses_primary_for_worktree_and_preserves_excludes(self, run):
        primary = self.repo
        linked = self.root / "linked"
        linked.mkdir()
        (linked / ".git").write_text(f"gitdir: {primary / '.git' / 'worktrees' / 'linked'}\n")
        (primary / ".git" / "info").mkdir()
        (primary / ".git" / "info" / "exclude").write_text("# local rules\n")
        self.ship.joinpath("metadata.json").write_text(
            json.dumps({"repoDir": str(linked)}), encoding="utf-8"
        )
        assignment = {**self.assignment, "repoDir": str(linked), "repository": "worktree"}
        expected = (primary / ".worktrees" / ASSIGNMENT_ID).resolve()
        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            subprocess.CompletedProcess(
                [],
                0,
                (
                    f"worktree {primary.resolve()}\nHEAD {'a' * 40}\nbranch refs/heads/main\n\n"
                    f"worktree {linked.resolve()}\nHEAD {'b' * 40}\nbranch refs/heads/topic\n\n"
                ),
                "",
            ),
            completed(returncode=1),
            completed(),
            completed({"result": {"pane": {"pane_id": "w1:p1", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
        ]

        binding = launch_assignment(self.ship, assignment)

        self.assertEqual(binding["worktreeDir"], str(expected))
        self.assertEqual(run.call_args_list[3].kwargs["cwd"], linked.resolve())
        self.assertEqual(
            (primary / ".git" / "info" / "exclude").read_text(),
            "# local rules\n/.worktrees/\n",
        )
    @patch("captain_bridge.runtime.subprocess.run")
    def test_existing_worktree_binding_is_not_migrated(self, run):
        old_path = self.repo.parent / ".captain-bridge-worktrees" / ASSIGNMENT_ID
        binding = {
            "agentName": ASSIGNMENT_ID,
            "paneId": "w1:p2",
            "worktreeDir": str(old_path),
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        assignment = {**self.assignment, "repository": "worktree", "runtime": binding}
        run.return_value = completed(
            {"result": {"agent": {"name": ASSIGNMENT_ID, "pane_id": "w1:p2"}}}
        )

        with patch(
            "captain_bridge.runtime._primary_checkout",
            side_effect=AssertionError("existing assignments must not migrate"),
        ):
            self.assertEqual(launch_assignment(self.ship, assignment), binding)
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
        expected = self.repo / ".worktrees" / ASSIGNMENT_ID
        expected.mkdir(parents=True)
        run.return_value = completed(
            returncode=1, stderr='{"error":{"code":"agent_not_found"}}'
        )
        assignment = {**self.assignment, "repository": "worktree"}

        with patch("captain_bridge.runtime._primary_checkout", return_value=self.repo):
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
    def test_prompt_failure_tears_down_created_tab_and_worktree(self, run):
        assignment = {**self.assignment, "repository": "worktree"}
        expected = self.repo / ".worktrees" / ASSIGNMENT_ID

        def create_worktree(*_):
            expected.mkdir(parents=True)
            return expected

        run.side_effect = [
            completed(returncode=1, stderr='{"error":{"code":"agent_not_found"}}'),
            completed({"result": {"pane": {"pane_id": "w1:p1", "workspace_id": "w1"}}}),
            completed(
                {
                    "result": {
                        "tab": {"tab_id": "w1:t2"},
                        "root_pane": {"pane_id": "w1:p2"},
                    }
                }
            ),
            completed({"result": {"agent": {"name": ASSIGNMENT_ID}}}),
            completed(returncode=1, stderr='{"error":{"code":"agent_prompt_stalled"}}'),
            completed(),
            completed(),
        ]
        with patch("captain_bridge.runtime._primary_checkout", return_value=self.repo):
            with patch("captain_bridge.runtime._create_worktree", side_effect=create_worktree):
                with self.assertRaises(OperationError):
                    launch_assignment(self.ship, assignment)
        self.assertEqual(run.call_args_list[-2].args[0], ["herdr", "tab", "close", "w1:t2"])
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
