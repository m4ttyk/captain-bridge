import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CliContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "captain-home"
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.env = os.environ.copy()
        self.env["CAPTAIN_BRIDGE_HOME"] = str(self.home)
        self.env.pop("CAPTAIN_BRIDGE_SHIP", None)
        self.env["PYTHONPATH"] = str(ROOT / "src")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, cwd=None, env=None):
        return subprocess.run(
            [sys.executable, "-m", "captain_bridge", *args],
            cwd=cwd or self.outside,
            env=env or self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def json_ok(self, *args, cwd=None, env=None):
        result = self.run_cli(*args, cwd=cwd, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertTrue(result.stdout.endswith("\n"))
        return json.loads(result.stdout)

    def test_documented_ship_assignment_decision_memory_and_event_flows(self):
        created = self.json_ok("ship", "create", str(self.repo), "demo")
        ship = Path(created["path"])
        self.assertTrue(ship.is_dir())
        self.assertEqual(created["repoDir"], str(self.repo.resolve()))

        selected_env = self.env | {"CAPTAIN_BRIDGE_SHIP": str(ship)}
        reconciled = self.json_ok("ship", "reconcile", cwd=self.outside, env=selected_env)
        self.assertEqual(reconciled["path"], str(ship))

        assignment = self.json_ok(
            "assignment",
            "create",
            "--role",
            "builder",
            "--prompt",
            "Add input validation",
            cwd=self.outside,
            env=selected_env,
        )
        assignment_id = assignment["id"]
        inspected = self.json_ok(
            "assignment",
            "inspect",
            assignment_id,
            cwd=self.outside,
            env=selected_env,
        )
        self.assertEqual(inspected["assignment"]["id"], assignment_id)
        self.assertEqual(inspected["status"], "created")

        requested = self.json_ok(
            "decision",
            "request",
            "--mode",
            "reviewable",
            "--confidence",
            "medium",
            "--question",
            "Should this change ship?",
            "--assignment-id",
            assignment_id,
            cwd=self.outside,
            env=selected_env,
        )
        decision_id = requested["id"]
        self.assertEqual(requested["status"], "pending")
        pending = self.json_ok(
            "assignment",
            "inspect",
            assignment_id,
            cwd=self.outside,
            env=selected_env,
        )
        self.assertEqual([item["id"] for item in pending["pendingDecisions"]], [decision_id])

        resolved = self.json_ok(
            "decision",
            "resolve",
            decision_id,
            "--answer",
            "Yes",
            "--resolved-by",
            "officer",
            "--rationale",
            "The change is scoped and reversible.",
            cwd=self.outside,
            env=selected_env,
        )
        self.assertEqual(resolved["status"], "resolved")
        reviewed = self.json_ok(
            "decision",
            "review",
            decision_id,
            "--note",
            "Recorded after review.",
            cwd=self.outside,
            env=selected_env,
        )
        self.assertEqual(reviewed["reviewNote"], "Recorded after review.")

        memory = self.json_ok(
            "memory",
            "record",
            "--title",
            "Validation policy",
            "--area",
            "runtime",
            "--symptom",
            "Invalid input reached the adapter.",
            "--context",
            "The CLI accepted an incomplete request.",
            "--cause",
            "The boundary lacked validation.",
            "--workaround",
            "Reject malformed requests before launch.",
            "--evidence",
            "The adapter log showed the malformed payload.",
            "--follow-up",
            "Keep the boundary test in the contract suite.",
            cwd=self.outside,
            env=selected_env,
        )
        memory_id = memory["id"]
        self.assertEqual(self.json_ok("memory", "inspect", memory_id, env=selected_env)["id"], memory_id)
        search = self.json_ok("memory", "search", "malformed payload", env=selected_env)
        self.assertEqual([item["id"] for item in search], [memory_id])

        event = self.json_ok(
            "_event",
            "emit",
            "--kind",
            "session-started",
            "--assignment",
            assignment_id,
            "--session-id",
            "session-test",
            cwd=self.outside,
            env=selected_env,
        )
        self.assertEqual(event["event"]["kind"], "session-started")
        self.assertEqual(event["event"]["assignmentId"], assignment_id)
        self.assertEqual(len(list((ship / "events").glob("*.json"))), 2)

    def test_result_ready_requires_valid_assignment_result(self):
        created = self.json_ok("ship", "create", str(self.repo), "demo")
        ship = Path(created["path"])
        selected_env = self.env | {"CAPTAIN_BRIDGE_SHIP": str(ship)}
        assignment = self.json_ok(
            "assignment",
            "create",
            "--role",
            "builder",
            "--prompt",
            "Produce a result",
            cwd=self.outside,
            env=selected_env,
        )
        assignment_id = assignment["id"]
        baseline_events = list((ship / "events").glob("*.json"))
        self.assertEqual(len(baseline_events), 1)

        event_args = ("_event", "emit", "--kind", "result-ready", "--assignment", assignment_id)

        missing = self.run_cli(*event_args, cwd=self.outside, env=selected_env)
        self.assertEqual(missing.returncode, 3)
        self.assertIn("assignment result not found", missing.stderr)
        self.assertEqual(list((ship / "events").glob("*.json")), baseline_events)

        result_path = ship / "assignments" / assignment_id / "result.md"
        result_path.write_text("## Outcome\nincomplete\n", encoding="utf-8")
        malformed = self.run_cli(*event_args, cwd=self.outside, env=selected_env)
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("result.md must contain these headings", malformed.stderr)
        self.assertEqual(list((ship / "events").glob("*.json")), baseline_events)

        result_path.write_text(
            "## Outcome\nDone\n## Commits\nNone\n## Verification\nPassed\n"
            "## Findings\nNone\n## Open questions\nNone\n",
            encoding="utf-8",
        )
        valid = self.json_ok(*event_args, cwd=self.outside, env=selected_env)
        self.assertEqual(valid["event"]["kind"], "result-ready")
        self.assertEqual(len(list((ship / "events").glob("*.json"))), len(baseline_events) + 1)

        other = self.json_ok(
            "_event", "emit", "--kind", "session-started", "--assignment", assignment_id,
            cwd=self.outside, env=selected_env,
        )
        self.assertEqual(other["event"]["kind"], "session-started")
        self.assertEqual(len(list((ship / "events").glob("*.json"))), len(baseline_events) + 2)

    def test_json_errors_are_stderr_only_with_exit_code(self):
        result = self.run_cli(
            "ship",
            "reconcile",
            env=self.env | {"CAPTAIN_BRIDGE_SHIP": str(self.root / "missing-ship")},
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        error = json.loads(result.stderr)
        self.assertEqual(error["error"]["code"], 3)
        self.assertIn("ship not found", error["error"]["message"])


if __name__ == "__main__":
    unittest.main()
