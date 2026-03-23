from __future__ import annotations

import asyncio
import unittest

import backend.main as main


class ActionPlanUnitTests(unittest.TestCase):
    def test_build_nous_hint_accepts_plan_schema(self) -> None:
        hint = main._build_nous_hint_from_result(
            {
                "response": "On it.",
                "plan": [
                    {"op": "add_task", "slots": {"title": "Book flight"}, "depends_on": []},
                    {"op": "schedule_remind_once", "slots": {"text": "Pack bag", "durationMs": "600000"}, "depends_on": ["step_1"]},
                ],
            },
            123,
            "unit",
        )
        self.assertIsNotNone(hint)
        self.assertEqual(str(hint.get("op", "")), "add_task")
        self.assertEqual(len(hint.get("_plan_steps", [])), 2)
        self.assertEqual(str(hint["_plan_steps"][1]["depends_on"][0]), "step_1")

    def test_run_plan_skips_steps_with_failed_dependencies(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("unit_plan_skip")
            result = await main.run_plan(
                session,
                "unit_plan_skip",
                [
                    {"id": "step_1", "op": "gdrive.share", "slots": {"name": "Budget.xlsx"}, "depends_on": []},
                    {"id": "step_2", "op": "add_note", "slots": {"text": "Should not run"}, "depends_on": ["step_1"]},
                ],
            )
            self.assertFalse(bool(result.get("ok", True)))
            tool_results = result.get("toolResults", [])
            self.assertEqual(len(tool_results), 2)
            self.assertFalse(bool(tool_results[0].get("ok", True)))
            self.assertTrue(bool(tool_results[1].get("skipped", False)))
            self.assertIn("dependency", str(tool_results[1].get("message", "")).lower())

        asyncio.run(run_case())

    def test_travel_calendar_message_flow_executes(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("unit_plan_travel")
            result = await main.run_plan(
                session,
                "unit_plan_travel",
                [
                    {"id": "step_1", "op": "travel_flight_search", "slots": {"origin": "Austin", "destination": "New York", "date": "next Tuesday"}, "depends_on": []},
                    {"id": "step_2", "op": "calendar_create", "slots": {"title": "NYC trip", "date": "next Tuesday 3pm"}, "depends_on": ["step_1"]},
                    {"id": "step_3", "op": "messaging_send", "slots": {"to": "Sarah", "text": "I am coming to New York next Tuesday."}, "depends_on": ["step_2"]},
                ],
            )
            self.assertTrue(bool(result.get("ok", False)))
            self.assertEqual(int(result.get("planSteps", 0) or 0), 3)
            tool_results = result.get("toolResults", [])
            self.assertEqual([step.get("op") for step in tool_results], ["travel_flight_search", "calendar_create", "messaging_send"])
            self.assertTrue(all(bool(step.get("ok", False)) for step in tool_results))

        asyncio.run(run_case())

    def test_shopping_payment_flow_executes_with_confirmed_payment(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("unit_plan_shop")
            result = await main.run_plan(
                session,
                "unit_plan_shop",
                [
                    {"id": "step_1", "op": "shop_catalog_search", "slots": {"query": "running shoes"}, "depends_on": []},
                    {"id": "step_2", "op": "payments_send", "slots": {"to": "Nike", "amount": 120, "note": "Running shoes", "confirmed": True}, "depends_on": ["step_1"]},
                ],
            )
            self.assertTrue(bool(result.get("ok", False)))
            tool_results = result.get("toolResults", [])
            self.assertEqual(len(tool_results), 2)
            self.assertEqual(str((tool_results[1].get("data") or {}).get("op", "")), "payments_send")
            self.assertTrue(bool(tool_results[1].get("ok", False)))

        asyncio.run(run_case())

    def test_message_reminder_flow_executes(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("unit_plan_message")
            result = await main.run_plan(
                session,
                "unit_plan_message",
                [
                    {"id": "step_1", "op": "messaging_send", "slots": {"to": "Alex", "text": "Meeting moved to 4pm."}, "depends_on": []},
                    {"id": "step_2", "op": "schedule_remind_once", "slots": {"text": "Follow up with Alex", "durationMs": "900000"}, "depends_on": ["step_1"]},
                ],
            )
            self.assertTrue(bool(result.get("ok", False)))
            tool_results = result.get("toolResults", [])
            self.assertEqual(len(tool_results), 2)
            self.assertEqual([step.get("op") for step in tool_results], ["messaging_send", "schedule_remind_once"])
            self.assertTrue(all(bool(step.get("ok", False)) for step in tool_results))

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
