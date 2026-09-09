import unittest

from captain_bridge.domain import (
    ValidationError,
    decision_mode,
    derive_assignment_status,
    new_id,
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

    def test_status_distinguishes_terminal_turn_from_integration(self):
        self.assertEqual(
            derive_assignment_status(
                event_kinds=("assignment-launched", "result-ready"),
                has_result=True,
                has_integration=False,
            ),
            "result-ready",
        )
        self.assertEqual(
            derive_assignment_status(
                event_kinds=("result-ready", "assignment-integrated"),
                has_result=True,
                has_integration=True,
            ),
            "integrated",
        )

    def test_status_derives_from_durable_records(self):
        self.assertEqual(
            derive_assignment_status(
                event_kinds=("assignment-launched",),
                has_result=False,
                has_integration=False,
            ),
            "launched",
        )


if __name__ == "__main__":
    unittest.main()
