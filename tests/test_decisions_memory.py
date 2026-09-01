import json
import tempfile
import tomllib
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import captain_bridge.decisions as decisions_module
import captain_bridge.memory as memory_module
from captain_bridge.decisions import pending_decisions, request_decision, resolve_decision, review_decision
from captain_bridge.domain import ConflictError
from captain_bridge.memory import inspect_memory, record_memory, search_memory


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ship = Path(self.temporary.name) / "ship"
        (self.ship / "decisions").mkdir(parents=True)
        (self.ship / "metadata.json").write_text("{}\n", encoding="utf-8")
        (self.ship / "index.md").write_text("# Test ship\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_mode_is_explicit_and_approval_context_is_preserved(self):
        assignment_id = "assignment_23456789"
        decision = request_decision(
            self.ship,
            question="May dependent integration proceed?",
            mode="approval-required",
            confidence="high",
            assignment_id=assignment_id,
            affected_assignments=[assignment_id],
            blocks_further_dependent_work=True,
            impact="May publish external changes.",
        )

        self.assertEqual(decision["status"], "pending")
        self.assertEqual(decision["mode"], "approval-required")
        self.assertEqual(decision["confidence"], "high")
        self.assertEqual(decision["affectedAssignments"], [assignment_id])
        self.assertTrue(decision["blocksFurtherDependentWork"])
        persisted = json.loads((self.ship / "decisions" / f"{decision['id']}.json").read_text())
        self.assertEqual(persisted, decision)

    def test_resolution_and_review_are_idempotent_and_preserve_audit_fields(self):
        decision = request_decision(
            self.ship,
            question="Use the repository convention?",
            mode="reviewable",
            confidence="low",
        )
        resolved = resolve_decision(
            self.ship,
            decision["id"],
            answer="Yes",
            resolved_by="officer:test",
            rationale="The existing convention is reversible and sufficient.",
        )
        repeated = resolve_decision(
            self.ship,
            decision["id"],
            answer="Yes",
            resolved_by="officer:test",
            rationale="The existing convention is reversible and sufficient.",
        )

        self.assertEqual(repeated, resolved)
        self.assertEqual(resolved["createdAt"], decision["createdAt"])
        self.assertEqual(resolved["resolvedBy"], "officer:test")
        self.assertIsNotNone(resolved["resolvedAt"])
        with self.assertRaises(ConflictError):
            resolve_decision(
                self.ship,
                decision["id"],
                answer="No",
                resolved_by="officer:test",
                rationale="Changed answer.",
            )

        reviewed = review_decision(self.ship, decision["id"], note="Checked by the Officer.")
        self.assertEqual(review_decision(self.ship, decision["id"], note="Checked by the Officer."), reviewed)
        self.assertEqual(reviewed["status"], "resolved")
        self.assertIsNotNone(reviewed["reviewedAt"])
        with self.assertRaises(ConflictError):
            review_decision(self.ship, decision["id"], note="A different review.")
    def test_resolution_rechecks_and_writes_under_decision_lock(self):
        decision = request_decision(
            self.ship,
            question="Lock this decision?",
            mode="approval-required",
            confidence="high",
            assignment_id=None,
        )
        with patch("captain_bridge.decisions.Storage.file_lock", return_value=nullcontext()) as lock:
            resolve_decision(
                self.ship,
                decision["id"],
                answer="Yes",
                resolved_by="captain",
                rationale="confirmed",
            )
        self.assertEqual(lock.call_args.args[0], (self.ship / "decisions.lock").resolve())


    def test_override_is_a_new_record_and_keeps_original_history(self):
        original = request_decision(
            self.ship,
            question="Choose A?",
            mode="autonomous",
            confidence="medium",
        )
        original_path = self.ship / "decisions" / f"{original['id']}.json"
        original_text = original_path.read_text(encoding="utf-8")
        override = request_decision(
            self.ship,
            question="Choose B instead?",
            mode="reviewable",
            confidence="high",
            supersedes=original["id"],
        )

        self.assertNotEqual(override["id"], original["id"])
        self.assertEqual(override["supersedes"], original["id"])
        self.assertEqual(original_path.read_text(encoding="utf-8"), original_text)

    def test_supersession_rejects_fan_out(self):
        original = request_decision(
            self.ship,
            question="Choose A?",
            mode="autonomous",
            confidence="medium",
        )
        request_decision(
            self.ship,
            question="Choose B instead?",
            mode="reviewable",
            confidence="high",
            supersedes=original["id"],
        )

        with self.assertRaises(ConflictError):
            request_decision(
                self.ship,
                question="Choose C instead?",
                mode="approval-required",
                confidence="low",
                supersedes=original["id"],
            )

    def test_locked_recheck_rejects_successor_appearing_after_precheck(self):
        original = request_decision(
            self.ship, question="Choose A?", mode="autonomous", confidence="medium"
        )
        successor = {
            **original,
            "id": "decision_successor",
            "supersedes": original["id"],
            "createdAt": "2026-01-02T00:00:00Z",
        }
        with patch.object(
            decisions_module,
            "_all_decisions",
            side_effect=[[], [original, successor]],
        ):
            with self.assertRaises(ConflictError):
                request_decision(
                    self.ship,
                    question="Choose B instead?",
                    mode="reviewable",
                    confidence="high",
                    supersedes=original["id"],
                )

    def test_pending_decisions_are_chronological(self):
        assignment_id = "assignment_23456789"
        with patch.object(
            decisions_module,
            "now",
            side_effect=["2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z"],
        ):
            first = request_decision(
                self.ship,
                question="First?",
                mode="autonomous",
                confidence="low",
                assignment_id=assignment_id,
            )
            second = request_decision(
                self.ship,
                question="Second?",
                mode="reviewable",
                confidence="high",
                assignment_id=assignment_id,
            )

        self.assertEqual(pending_decisions(self.ship, assignment_id), [first, second])


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _record(self, **overrides):
        values = {
            "title": "Missing module after worktree launch",
            "area": "Build Systems",
            "symptom": "The worker reports a missing module.",
            "context": "A fresh worktree starts the Python command.",
            "cause": "The source directory is absent from the import path.",
            "workaround": "Run with the package source directory on the import path.",
            "evidence": "The same command succeeds after setting the import path.",
            "follow_up": "Package the command when distribution is required.",
            "home": self.home,
        }
        values.update(overrides)
        return record_memory(**values)

    def test_markdown_frontmatter_sections_search_and_inspect(self):
        memory = self._record()
        path = self.home / "memory" / f"{memory['id']}.md"
        text = path.read_text(encoding="utf-8")
        metadata = tomllib.loads(text.split("+++", 2)[1])

        self.assertEqual(metadata["status"], "active")
        self.assertEqual(metadata["area"], "build-systems")
        for heading in ("Symptom", "Context", "Cause", "Workaround", "Evidence", "Follow-up"):
            self.assertIn(f"## {heading}\n", text)
        self.assertEqual(search_memory("MISSING MODULE", home=self.home), [memory])
        self.assertEqual(search_memory(area="BUILD SYSTEMS", home=self.home), [memory])
        self.assertEqual(inspect_memory(memory["id"], home=self.home), memory)

    def test_supersession_is_derived_without_rewriting_history(self):
        original = self._record()
        original_path = self.home / "memory" / f"{original['id']}.md"
        original_text = original_path.read_text(encoding="utf-8")
        replacement = self._record(
            title="Portable worktree import setup",
            symptom="A worktree command cannot import the package.",
            supersedes=original["id"],
        )

        self.assertEqual(original_path.read_text(encoding="utf-8"), original_text)
        self.assertEqual(search_memory(home=self.home), [replacement])
        historical = inspect_memory(original["id"], home=self.home)
        self.assertEqual(historical["supersededBy"], replacement["id"])
        self.assertEqual(historical["status"], "active")

    def test_supersession_rejects_fan_out(self):
        original = self._record()
        self._record(
            title="Portable worktree import setup",
            symptom="A worktree command cannot import the package.",
            supersedes=original["id"],
        )

        with self.assertRaises(ConflictError):
            self._record(
                title="Another portable import setup",
                symptom="The package remains unavailable in a worktree.",
                supersedes=original["id"],
            )

    def test_locked_recheck_rejects_successor_appearing_after_precheck(self):
        original = self._record()
        successor = {
            **original,
            "id": "memory_successor",
            "supersedes": original["id"],
            "createdAt": "2026-01-02T00:00:00Z",
        }
        with patch.object(
            memory_module,
            "_all_memories",
            side_effect=[[], [original, successor]],
        ):
            with self.assertRaises(ConflictError):
                self._record(
                    title="Another portable import setup",
                    supersedes=original["id"],
                )


if __name__ == "__main__":
    unittest.main()
