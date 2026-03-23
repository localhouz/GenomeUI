from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import backend.main as main


class DeferredNousReconcileUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sessions_before = dict(main.SESSIONS)

    def tearDown(self) -> None:
        main.SESSIONS.clear()
        main.SESSIONS.update(self._sessions_before)

    def test_reconcile_updates_last_turn_for_read_only_surface_change(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("reconcilebatch")
            session.revision = 5
            baseline_envelope = main.compile_intent_envelope("show overview")
            baseline_execution = {"ok": True, "message": "No state changes requested.", "toolResults": [], "journalTail": []}
            session.last_turn = {
                "intent": "show overview",
                "envelope": baseline_envelope,
                "execution": baseline_execution,
                "kernelTrace": {},
                "plan": {},
                "planner": "local",
                "route": {"target": "deterministic", "reason": "default", "model": None, "intentClass": "inspect", "confidence": 0.5},
                "merge": {"rebased": False, "fromRevision": None, "toRevision": None},
                "timestamp": main.now_ms(),
            }
            delayed_result = {"ops": [{"type": "weather_forecast", "slots": {"location": "Chicago"}}], "response": "weather"}

            with patch.object(main._nous, "is_loaded", return_value=True), \
                 patch.object(main._nous, "classify", AsyncMock(return_value=delayed_result)), \
                 patch.object(main, "broadcast_session", AsyncMock()), \
                 patch.object(main, "persist_sessions_to_disk_safe", AsyncMock(return_value=True)):
                await main._reconcile_delayed_nous_turn(
                    "reconcilebatch",
                    5,
                    "show overview",
                    baseline_envelope,
                    baseline_execution,
                    {"rebased": False, "fromRevision": None, "toRevision": None},
                )

            self.assertEqual(session.last_turn.get("planner"), "local-deferred-nous")
            envelope = session.last_turn.get("envelope") or {}
            domains = ((envelope.get("stateIntent") or {}).get("readDomains") or [])
            self.assertIn("weather", domains)

        asyncio.run(run_case())

    def test_reconcile_does_not_mutate_when_delayed_result_is_write(self) -> None:
        async def run_case() -> None:
            session = main.ensure_session("reconcilewrite")
            session.revision = 9
            baseline_envelope = main.compile_intent_envelope("show overview")
            baseline_execution = {"ok": True, "message": "No state changes requested.", "toolResults": [], "journalTail": []}
            original_last_turn = {
                "intent": "show overview",
                "envelope": baseline_envelope,
                "execution": baseline_execution,
                "kernelTrace": {},
                "plan": {"title": "Baseline"},
                "planner": "local",
                "route": {"target": "deterministic", "reason": "default", "model": None, "intentClass": "inspect", "confidence": 0.5},
                "merge": {"rebased": False, "fromRevision": None, "toRevision": None},
                "timestamp": main.now_ms(),
            }
            session.last_turn = dict(original_last_turn)
            delayed_result = {"ops": [{"type": "add_task", "slots": {"title": "Write drift"}}], "response": "write"}

            with patch.object(main._nous, "is_loaded", return_value=True), \
                 patch.object(main._nous, "classify", AsyncMock(return_value=delayed_result)), \
                 patch.object(main, "broadcast_session", AsyncMock()), \
                 patch.object(main, "persist_sessions_to_disk_safe", AsyncMock(return_value=True)):
                await main._reconcile_delayed_nous_turn(
                    "reconcilewrite",
                    9,
                    "show overview",
                    baseline_envelope,
                    baseline_execution,
                    {"rebased": False, "fromRevision": None, "toRevision": None},
                )

            self.assertEqual(session.last_turn.get("planner"), "local")
            self.assertTrue(any(str(item.get("type", "")) == "nous_reconcile" for item in session.notifications))

        asyncio.run(run_case())


if __name__ == "__main__":
    unittest.main()
