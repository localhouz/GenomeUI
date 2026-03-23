import unittest

from backend import main


class Wave4AliasTests(unittest.TestCase):
    def test_calc_and_weather_alert_ops_execute(self) -> None:
        session = main.ensure_session("unit_wave4_calc_weather")
        calc = main.run_operation(session, {"type": "calc_evaluate", "payload": {"expression": "12 * 4"}})
        weather = main.run_operation(session, {"type": "weather_alert", "payload": {"location": "Chicago"}})
        self.assertTrue(bool(calc.get("ok", False)))
        self.assertTrue(bool(weather.get("ok", False)))
        self.assertEqual(str((calc.get("data") or {}).get("display", "")), "48")
        self.assertIn(str((weather.get("data") or {}).get("severity", "")), {"none", "medium", "high"})

    def test_weather_variant_alias_executes(self) -> None:
        session = main.ensure_session("unit_wave4_weather")
        result = main.run_operation(session, {"type": "weather_radar", "payload": {"location": "Chicago"}})
        self.assertIn("weather", str(result.get("message", "")).lower())
        self.assertIn(str((result.get("data") or {}).get("op", "")), {"weather_forecast", ""})

    def test_task_metadata_ops_execute(self) -> None:
        session = main.ensure_session("unit_wave4_tasks")
        priority = main.run_operation(session, {"type": "task_priority", "payload": {"selector": "budget", "priority": "high"}})
        due = main.run_operation(session, {"type": "task_due", "payload": {"selector": "budget", "date": "next week"}})
        self.assertTrue(bool(priority.get("ok", False)))
        self.assertTrue(bool(due.get("ok", False)))
        self.assertEqual(str((priority.get("data") or {}).get("priority", "")), "high")
        self.assertEqual(str((due.get("data") or {}).get("date", "")), "next week")

    def test_maps_and_news_ops_execute(self) -> None:
        session = main.ensure_session("unit_wave4_maps_news")
        maps_result = main.run_operation(session, {"type": "maps_search", "payload": {"query": "coffee near me"}})
        news_result = main.run_operation(session, {"type": "news_trending", "payload": {}})
        self.assertTrue(bool(maps_result.get("ok", False)))
        self.assertTrue(bool(news_result.get("ok", False)))
        self.assertEqual(str((maps_result.get("data") or {}).get("op", "")), "maps_search")
        self.assertEqual(str((news_result.get("data") or {}).get("op", "")), "news_trending")

    def test_code_and_calendar_aliases_execute(self) -> None:
        session = main.ensure_session("unit_wave4_code_calendar")
        code = main.run_operation(session, {"type": "code_review", "payload": {"language": "python"}})
        cal = main.run_operation(session, {"type": "calendar_availability", "payload": {"days": 3}})
        self.assertTrue(bool(code.get("ok", False)))
        self.assertTrue(bool(cal.get("ok", False)))
        self.assertEqual(str((code.get("data") or {}).get("op", "")), "code_review")
        self.assertEqual(str((cal.get("data") or {}).get("op", "")), "calendar_availability")


if __name__ == "__main__":
    unittest.main()
