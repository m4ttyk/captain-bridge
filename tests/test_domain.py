import unittest

from captain_bridge.domain import (
    ValidationError,
    decision_mode,
    derive_assignment_status,
    new_id,
    parse_result_sections,
    validate_id,
)


class DomainTests(unittest.TestCase):
    def test_decision_mode_is_explicit_and_independent_of_confidence(self):
        self.assertEqual(decision_mode("low", "reviewable"), "reviewable")
        self.assertEqual(decision_mode("high", "approval-required"), "approval-required")
        with self.assertRaises(ValidationError):
            decision_mode("low")

    def test_ids_have_entity_prefix_and_eight_readable_characters(self):
        value = new_id("assignment")
        self.assertEqual(validate_id(value, "assignment"), value)
        prefix, suffix = value.split("_")
        self.assertEqual(prefix, "assignment")
        self.assertEqual(len(suffix), 8)
        self.assertNotIn("0", suffix)
        self.assertNotIn("1", suffix)
        self.assertNotIn("l", suffix)

    def test_result_headings_must_be_unique_complete_and_ordered(self):
        valid = "\n".join(f"## {heading}\ntext" for heading in (
            "Outcome", "Commits", "Verification", "Findings", "Open questions"
        ))
        self.assertEqual(list(parse_result_sections(valid)), [
            "Outcome", "Commits", "Verification", "Findings", "Open questions"
        ])
        for headings in (
            ("Outcome", "Commits", "Commits", "Findings", "Open questions"),
            ("Outcome", "Verification", "Findings", "Open questions"),
            ("Outcome", "Verification", "Commits", "Findings", "Open questions"),
        ):
            with self.assertRaises(ValidationError):
                parse_result_sections("\n".join(f"# {h}" for h in headings))

    def test_status_ignores_runtime_snapshot(self):
        self.assertEqual(
            derive_assignment_status(
                event_kinds=("assignment-launched",),
                has_result=False,
                has_integration=False,
                runtime={"status": "settled"},
            ),
            "launched",
        )


if __name__ == "__main__":
    unittest.main()
