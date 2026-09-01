import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import nullcontext

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
        self.storage_patch = patch.object(assignments, "Storage", lambda: storage)
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.decisions_storage_patch = patch.object(assignments.decisions, "Storage", lambda: storage)
        self.decisions_storage_patch.start()
        self.addCleanup(self.decisions_storage_patch.stop)
        self.event_n = 0
        def ids(kind):
            if kind == "assignment": return "assignment_23456789"
            self.event_n += 1
            return f"event_{self.event_n:08d}"
        self.id_patch = patch.object(assignments, "new_id", ids)
        self.id_patch.start()
        self.addCleanup(self.id_patch.stop)
        self.assignment_env = (ship, repo)

    def patch_runtime(self, mocked):
        runtime_patch = patch.object(assignments, "runtime", mocked)
        runtime_patch.start()
        self.addCleanup(runtime_patch.stop)
        return mocked

    def patch_git(self, mocked):
        git_patch = patch.object(assignments, "_git", mocked)
        git_patch.start()
        self.addCleanup(git_patch.stop)
        return mocked

    def tearDown(self):
        self._tmp.cleanup()

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
            "model": "test",
            "effort": "low",
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
        launch = lambda ship_arg, assignment: facts
        mocked = SimpleNamespace(launch_assignment=Mock(side_effect=launch))
        self.patch_runtime(mocked)

        assert assignments.launch_assignment(ship, record["id"]) == facts
        assert assignments.launch_assignment(ship, record["id"]) == facts
        assert mocked.launch_assignment.call_count == 2
        assert mocked.launch_assignment.call_args.args[1]["model"] == "test"
        saved = json.loads((ship / "assignments" / record["id"] / "assignment.json").read_text())
        assert saved["runtime"] == facts
        assert "status" not in saved
    def test_launch_serializes_per_assignment_with_file_lock(self):
        ship, _ = self.assignment_env
        record = self.create()
        facts = {
            "agentName": "builder-1",
            "paneId": "w1:p2",
            "worktreeDir": "/tmp/worktree",
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        self.patch_runtime(SimpleNamespace(launch_assignment=Mock(return_value=facts)))

        with patch.object(Storage, "file_lock", return_value=nullcontext()) as lock:
            assignments.launch_assignment(ship, record["id"])

        lock.assert_called_once_with(
            ship.resolve() / "assignments" / record["id"] / "launch.lock"
        )


    def test_launch_recovers_after_stale_launch_intent(self):
        ship, _ = self.assignment_env
        record = self.create()
        facts = {
            "agentName": "builder-1",
            "paneId": "w1:p2",
            "worktreeDir": "/tmp/worktree",
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        mocked = SimpleNamespace(
            launch_assignment=Mock(side_effect=[RuntimeError("adapter failed"), facts])
        )
        self.patch_runtime(mocked)

        with self.assertRaisesRegex(OperationError, "adapter failed"):
            assignments.launch_assignment(ship, record["id"])
        assert assignments.launch_assignment(ship, record["id"]) == facts
        assert mocked.launch_assignment.call_count == 2
        assert json.loads(
            (ship / "assignments" / record["id"] / "launch.json").read_text()
        )["assignmentId"] == record["id"]
    def test_launch_replaces_stale_binding_via_runtime(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        old_facts = {
            "agentName": "old",
            "paneId": "old-pane",
            "worktreeDir": "/tmp/old",
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        new_facts = {
            "agentName": "new",
            "paneId": "new-pane",
            "worktreeDir": "/tmp/new",
            "launchedAt": "2026-01-02T00:00:00Z",
        }
        record["runtime"] = old_facts
        (directory / "assignment.json").write_text(json.dumps(record))
        mocked = SimpleNamespace(launch_assignment=Mock(return_value=new_facts))
        self.patch_runtime(mocked)

        assert assignments.launch_assignment(ship, record["id"]) == new_facts
        assert mocked.launch_assignment.call_count == 1
        assert json.loads((directory / "assignment.json").read_text())["runtime"] == new_facts

    def test_launch_uses_snapshotted_role_after_role_changes(self):
        ship, _ = self.assignment_env
        record = self.create()
        role_path = Path(self._tmp.name) / "home" / "roles" / "builder.md"
        role_path.write_text('+++\nmodel = "changed"\neffort = "high"\nrepository = "read"\n+++\nChanged.\n')
        facts = {
            "agentName": "builder-1",
            "paneId": "w1:p2",
            "worktreeDir": "/tmp/worktree",
            "launchedAt": "2026-01-01T00:00:00Z",
        }
        mocked = SimpleNamespace(launch_assignment=Mock(return_value=facts))
        self.patch_runtime(mocked)

        assignments.launch_assignment(ship, record["id"])

        launched_assignment = mocked.launch_assignment.call_args.args[1]
        assert launched_assignment["repository"] == "worktree"
        assert launched_assignment["model"] == "test"
        assert launched_assignment["effort"] == "low"


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
        self.patch_runtime(SimpleNamespace(observe_assignment=lambda *_: observed))

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
        self.patch_runtime(SimpleNamespace(message_crewmate=send))
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

        self.patch_git(fake_git)
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

        self.patch_git(fake_git)

        with self.assertRaisesRegex(ConflictError, "did not apply cleanly"):
            assignments.integrate_assignment(ship, record["id"], "topic")
        assert ("cherry-pick", "--abort") in calls
        assert not (ship / "assignments" / record["id"] / "integration.json").exists()


    def test_cleanup_rejects_assignment_repo_mismatch_before_runtime(self):
        ship, repo = self.assignment_env
        record = self.create()
        (ship / "metadata.json").write_text(
            json.dumps({"repoDir": str(repo.parent / "other")})
        )
        observe = Mock(return_value={"available": False, "status": "missing"})
        self.patch_runtime(SimpleNamespace(observe_assignment=observe))

        with self.assertRaisesRegex(ConflictError, "repoDir does not match"):
            assignments.cleanup_assignment(ship, record["id"])
        observe.assert_not_called()

    def test_integration_refuses_dirty_target_before_cherry_pick(self):
        ship, _ = self.assignment_env
        record = self.create()
        self.patch_git(lambda repo, *args, **kwargs: self.git_result(args, stdout=" M file.py\n"))

        with self.assertRaisesRegex(ConflictError, "target repository is dirty"):
            assignments.integrate_assignment(ship, record["id"], "topic")
        assert not (ship / "assignments" / record["id"] / "integration.json").exists()
    def test_cleanup_refuses_live_agent_before_removing_worktree(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        (directory / "integration.json").write_text(json.dumps({"commit": "a" * 40}))
        self.patch_runtime(
            SimpleNamespace(
                observe_assignment=lambda *_: {"available": True, "status": "working"}
            )
        )
        git = self.patch_git(Mock())

        with self.assertRaisesRegex(ConflictError, "live runtime resources"):
            assignments.cleanup_assignment(ship, record["id"])
        git.assert_not_called()


    def test_cleanup_allows_read_only_and_requires_integration_for_worktree(self):
        ship, _ = self.assignment_env
        self.patch_runtime(SimpleNamespace(observe_assignment=lambda *_: {"available": False, "status": "missing"}))
        read_record = self.create("researcher")
        assert assignments.cleanup_assignment(ship, read_record["id"])["cleaned"] is True

        id_patch = patch.object(assignments, "new_id", lambda kind: {"assignment": "assignment_3456789a", "event": "event_bcdefghj"}[kind])
        id_patch.start()
        self.addCleanup(id_patch.stop)
        write_record = assignments.create_assignment(ship, role_name="builder", prompt="Write.")
        with self.assertRaisesRegex(ConflictError, "must be integrated"):
            assignments.cleanup_assignment(ship, write_record["id"])


    def test_cleanup_removes_integrated_writable_worktree_with_git(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        worktree = (Path(record["repoDir"]).parent / ".captain-bridge-worktrees" / record["id"]).resolve()
        worktree.mkdir(parents=True)
        record["runtime"] = {"agentName": "crew", "paneId": "p", "worktreeDir": str(worktree)}
        (directory / "assignment.json").write_text(json.dumps(record))
        (directory / "integration.json").write_text(json.dumps({"commit": "a" * 40}))
        def remove_worktree(repo, *args, **kwargs):
            worktree.rmdir()
            return self.git_result(args)
        git = Mock(side_effect=remove_worktree)
        self.patch_git(git)
        self.patch_runtime(SimpleNamespace(observe_assignment=lambda *_: {"available": False, "status": "missing"}))

        result = assignments.cleanup_assignment(ship, record["id"])

        assert result["worktreeRemoved"] is True
        assert git.call_args.args[1:] == ("worktree", "remove", str(worktree.resolve()))
    def test_cleanup_is_idempotent_under_cleanup_lock(self):
        ship, _ = self.assignment_env
        record = self.create()
        directory = ship / "assignments" / record["id"]
        worktree = (Path(record["repoDir"]).parent / ".captain-bridge-worktrees" / record["id"]).resolve()
        worktree.mkdir(parents=True)
        record["runtime"] = {"agentName": "crew", "paneId": "p", "worktreeDir": str(worktree)}
        directory.joinpath("assignment.json").write_text(json.dumps(record))
        directory.joinpath("integration.json").write_text(json.dumps({"commit": "a" * 40}))
        git = self.patch_git(Mock(side_effect=lambda repo, *args, **kwargs: (worktree.rmdir(), self.git_result(args))[1]))
        self.patch_runtime(SimpleNamespace(observe_assignment=lambda *_: {"available": False, "status": "missing"}))

        assert assignments.cleanup_assignment(ship, record["id"])["worktreeRemoved"] is True
        assert assignments.cleanup_assignment(ship, record["id"]) == {
            "assignmentId": record["id"], "cleaned": True, "worktreeRemoved": False
        }
        assert git.call_count == 1
        assert len([path for path in (ship / "events").glob("*.json")
                    if json.loads(path.read_text()).get("kind") == "assignment-cleaned"]) == 1

if __name__ == "__main__":
    unittest.main()
