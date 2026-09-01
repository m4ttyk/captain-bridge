import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import unittest
from unittest.mock import patch

from captain_bridge import assignments
from captain_bridge.domain import ConflictError, OperationError
from captain_bridge.storage import Storage


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = __import__("tempfile").TemporaryDirectory()
        tmp_path = __import__("pathlib").Path(self._tmp.name)
        home = tmp_path / "home"
        roles = home / "roles"
        roles.mkdir(parents=True)
        (roles / "builder.md").write_text(
            '+++\nmodel = "test"\neffort = "low"\nrepository = "worktree"\n+++\n\n# Builder\n\nBuild carefully.\n'
        )
        (roles / "researcher.md").write_text(
            '+++\nmodel = "test"\neffort = "low"\nrepository = "read"\n+++\n\n# Researcher\n\nRead only.\n'
        )
        repo = tmp_path / "repo"
        repo.mkdir()
        ship = tmp_path / "ship"
        (ship / "assignments").mkdir(parents=True)
        (ship / "decisions").mkdir()
        (ship / "metadata.json").write_text(json.dumps({"repoDir": str(repo), "shipId": "ship_23456789", "createdAt": "2026-01-01T00:00:00Z"}))
        (ship / "index.md").write_text("# test\n")
        storage = Storage(home)
        self.storage_patch = patch.object(assignments, "Storage", lambda: storage); self.storage_patch.start()
        self.event_n = 0
        def ids(kind):
            if kind == "assignment": return "assignment_23456789"
            self.event_n += 1
            return f"event_{self.event_n:08d}"
        self.id_patch = patch.object(assignments, "new_id", ids); self.id_patch.start()
        self.assignment_env = (ship, repo)

    def tearDown(self):
        self.id_patch.stop(); self.storage_patch.stop(); self._tmp.cleanup()

    def create(self, role="builder"):
        ship, _ = self.assignment_env
        return assignments.create_assignment(ship, role_name=role, prompt="Do the work.")


    def test_create_writes_durable_inputs_and_artifact_directory(self):
        ship, repo = self.assignment_env

        record = self.create()

        assert record == {
            "id": "assignment_23456789",
            "createdAt": record["createdAt"],
            "role": "builder",
            "repository": "worktree",
            "repoDir": str(repo.resolve()),
        }
        directory = ship / "assignments" / record["id"]
        assert json.loads((directory / "assignment.json").read_text()) == record
        assert (directory / "prompt.md").read_text() == "# Builder\n\nBuild carefully.\n\n## Assignment\n\nDo the work.\n"
        assert (directory / "artifacts").is_dir()


    def test_launch_persists_opaque_runtime_binding_and_is_idempotent(self):
        ship, _ = self.assignment_env
        record = self.create()
        facts = {
            "agentName": "builder-1",
            "paneId": "w1:p2",
            "worktreeDir": "/tmp/worktree",
            "launchedAt": "2026-01-01T00:00:00Z",
            "adapterFact": 7,
        }
        launch = lambda ship_arg, assignment, role: facts
        mocked = SimpleNamespace(launch_assignment=Mock(side_effect=launch))
        patch.object(assignments, "runtime", mocked).start()

        assert assignments.launch_assignment(ship, record["id"]) == facts
        assert assignments.launch_assignment(ship, record["id"]) == facts
        assert mocked.launch_assignment.call_count == 1
        saved = json.loads((ship / "assignments" / record["id"] / "assignment.json").read_text())
        assert saved["runtime"] == facts
        assert "status" not in saved


    def test_launch_failure_leaves_precise_partial_conflict(self):
        ship, _ = self.assignment_env
        record = self.create()
        mocked = SimpleNamespace(launch_assignment=Mock(side_effect=RuntimeError("adapter failed")))
        patch.object(assignments, "runtime", mocked).start()

        with self.assertRaisesRegex(OperationError, "adapter failed"):
            assignments.launch_assignment(ship, record["id"])
        with self.assertRaisesRegex(ConflictError, "launch request but no runtime binding"):
            assignments.launch_assignment(ship, record["id"])
        assert mocked.launch_assignment.call_count == 1


    def test_inspection_derives_result_events_pending_decisions_and_runtime(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        worktree = directory / "worktree"
        worktree.mkdir()
        record["runtime"] = {"agentName": "a", "paneId": "p", "worktreeDir": str(worktree)}
        (directory / "assignment.json").write_text(json.dumps(record))
        (directory / "result.md").write_text(
            "## Outcome\nDone\n## Commits\nNone\n## Verification\nChecked\n"
            "## Findings\nNone\n## Open questions\nNone\n"
        )
        pending = {
            "id": "decision_23456789",
            "assignmentId": record["id"],
            "mode": "approval-required",
            "answer": None,
            "requestedAt": "2026-01-01T00:00:00Z",
        }
        (ship / "decisions" / "decision_23456789.json").write_text(json.dumps(pending))
        observed = {"status": "settled", "available": True}
        patch.object(assignments, "runtime", SimpleNamespace(observe_assignment=lambda *_: observed)).start()

        view = assignments.inspect_assignment(ship, record["id"])

        assert view["status"] == "result-ready"
        assert view["result"]["Commits"] == "None"
        assert view["pendingDecisions"] == [pending]
        assert view["runtime"] == observed
        assert view["worktreeExists"] is True
        assert "status" not in json.loads((directory / "assignment.json").read_text())


    def test_message_uses_assignment_runtime_facts(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        record["runtime"] = {"agentName": "crew", "paneId": "p", "worktreeDir": "/tmp/w"}
        (directory / "assignment.json").write_text(json.dumps(record))
        send = Mock(return_value={"agentName": "crew", "delivered": True})
        patch.object(assignments, "runtime", SimpleNamespace(message_crewmate=send)).start()

        result = assignments.message_assignment(ship, record["id"], "Continue.")

        assert result == {"agentName": "crew", "delivered": True}
        assert send.call_args.args[1]["runtime"]["agentName"] == "crew"


    @staticmethod
    def git_result(args, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


    def test_integration_cherry_picks_then_records_receipt(self):
        ship, _ = self.assignment_env
        record = self.create()
        calls = []

        def fake_git(repo, *args, check=True):
            calls.append(args)
            if args[:2] == ("status", "--porcelain"):
                return self.git_result(args)
            if args[0] == "rev-parse" and args[-1] != "HEAD":
                return self.git_result(args, stdout="a" * 40 + "\n")
            if args[:2] == ("merge-base", "--is-ancestor"):
                return self.git_result(args, returncode=1)
            if args[0] == "cherry-pick":
                return self.git_result(args)
            if args == ("rev-parse", "HEAD"):
                return self.git_result(args, stdout="b" * 40 + "\n")
            raise AssertionError(args)

        patch.object(assignments, "_git", fake_git).start()
        receipt = assignments.integrate_assignment(ship, record["id"], "topic")

        assert ("cherry-pick", "a" * 40) in calls
        assert receipt["commit"] == "a" * 40
        assert receipt["targetCommit"] == "b" * 40
        saved = json.loads((ship / "assignments" / record["id"] / "integration.json").read_text())
        assert saved == receipt
        assert assignments.integrate_assignment(ship, record["id"], "topic") == receipt


    def test_integration_aborts_conflict_without_recording_success(self):
        ship, _ = self.assignment_env
        record = self.create()
        calls = []

        def fake_git(repo, *args, check=True):
            calls.append(args)
            if args[:2] == ("status", "--porcelain"):
                return self.git_result(args)
            if args[0] == "rev-parse":
                return self.git_result(args, stdout="a" * 40 + "\n")
            if args[:2] == ("merge-base", "--is-ancestor"):
                return self.git_result(args, returncode=1)
            if args == ("cherry-pick", "a" * 40):
                return self.git_result(args, returncode=1, stderr="conflict")
            if args == ("cherry-pick", "--abort"):
                return self.git_result(args)
            raise AssertionError(args)

        patch.object(assignments, "_git", fake_git).start()

        with self.assertRaisesRegex(ConflictError, "did not apply cleanly"):
            assignments.integrate_assignment(ship, record["id"], "topic")
        assert ("cherry-pick", "--abort") in calls
        assert not (ship / "assignments" / record["id"] / "integration.json").exists()


    def test_integration_refuses_dirty_target_before_cherry_pick(self):
        ship, _ = self.assignment_env
        record = self.create()
        patch.object(assignments, "_git", lambda repo, *args, **kwargs: self.git_result(args, stdout=" M file.py\n")).start()

        with self.assertRaisesRegex(ConflictError, "target repository is dirty"):
            assignments.integrate_assignment(ship, record["id"], "topic")
        assert not (ship / "assignments" / record["id"] / "integration.json").exists()


    def test_cleanup_allows_read_only_and_requires_integration_for_worktree(self):
        ship, _ = self.assignment_env
        read_record = self.create("researcher")
        assert assignments.cleanup_assignment(ship, read_record["id"])["cleaned"] is True

        patch.object(assignments, "new_id", lambda kind: {"assignment": "assignment_3456789a", "event": "event_bcdefghj"}[kind]).start()
        write_record = assignments.create_assignment(ship, role_name="builder", prompt="Write.")
        with self.assertRaisesRegex(ConflictError, "must be integrated"):
            assignments.cleanup_assignment(ship, write_record["id"])


    def test_cleanup_removes_integrated_writable_worktree_with_git(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        worktree = directory / "worktree"
        worktree.mkdir()
        record["runtime"] = {"agentName": "crew", "paneId": "p", "worktreeDir": str(worktree)}
        (directory / "assignment.json").write_text(json.dumps(record))
        (directory / "integration.json").write_text(json.dumps({"commit": "a" * 40}))
        git = Mock(return_value=self.git_result(("worktree", "remove", str(worktree.resolve()))))
        patch.object(assignments, "_git", git).start()

        result = assignments.cleanup_assignment(ship, record["id"])

        assert result["worktreeRemoved"] is True
        assert git.call_args.args[1:] == ("worktree", "remove", str(worktree.resolve()))

if __name__ == "__main__":
    unittest.main()
