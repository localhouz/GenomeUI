import unittest

from backend import main


class Wave4RuntimeTests(unittest.TestCase):
    def test_reading_list_alias_executes_view_handler(self) -> None:
        session = main.ensure_session("unit_wave4_reading")
        result = main.run_operation(session, {"type": "reading_list", "payload": {}})
        self.assertTrue(bool(result.get("ok", False)))
        data = result.get("data", {})
        self.assertIsInstance(data, dict)
        self.assertEqual(str(data.get("op", "")), "reading_list_view")
        self.assertIsInstance(data.get("articles", []), list)

    def test_password_2fa_returns_scaffold_code(self) -> None:
        session = main.ensure_session("unit_wave4_password")
        result = main.run_operation(session, {"type": "password_2fa", "payload": {"service": "github"}})
        self.assertTrue(bool(result.get("ok", False)))
        data = result.get("data", {})
        self.assertEqual(str(data.get("service", "")), "github")
        self.assertTrue(str(data.get("code", "")).isdigit())

    def test_camera_and_print_ops_return_structured_payloads(self) -> None:
        session = main.ensure_session("unit_wave4_device")
        photo = main.run_operation(session, {"type": "camera_photo", "payload": {}})
        self.assertTrue(bool(photo.get("ok", False)))
        self.assertEqual(str((photo.get("data") or {}).get("mediaType", "")), "photo")

        print_job = main.run_operation(session, {"type": "print_document", "payload": {"document": "Agenda.pdf"}})
        self.assertTrue(bool(print_job.get("ok", False)))
        self.assertEqual(str((print_job.get("data") or {}).get("target", "")), "Agenda.pdf")

    def test_settings_wave4_ops_execute(self) -> None:
        session = main.ensure_session("unit_wave4_settings")
        for op_type in ("settings_battery", "settings_storage", "settings_notification"):
            result = main.run_operation(session, {"type": op_type, "payload": {}})
            self.assertTrue(bool(result.get("ok", False)), msg=op_type)
            data = result.get("data", {})
            self.assertIsInstance(data, dict)
            self.assertEqual(str(data.get("op", "")), op_type)


if __name__ == "__main__":
    unittest.main()
