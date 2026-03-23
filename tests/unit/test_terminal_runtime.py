import unittest

from backend import main


class TerminalRuntimeTests(unittest.TestCase):
    def test_run_terminal_command_captures_stdout(self) -> None:
        result = main._run_terminal_command("Write-Output 'GenomeUI Terminal Test'")
        self.assertTrue(bool(result.get("ok", False)))
        self.assertEqual(str(result.get("shell", "")), "powershell")
        self.assertIn("GenomeUI Terminal Test", str(result.get("output", "")))

    def test_terminal_run_operation_returns_output_payload(self) -> None:
        session = main.ensure_session("unit_terminal_runtime")
        result = main.run_operation(
            session,
            {"type": "terminal_run", "payload": {"command": "Write-Output 'Terminal Surface OK'"}} ,
        )
        self.assertTrue(bool(result.get("ok", False)))
        data = result.get("data", {})
        self.assertIsInstance(data, dict)
        self.assertEqual(str(data.get("command", "")), "Write-Output 'Terminal Surface OK'")
        self.assertIn("Terminal Surface OK", str(data.get("output", "")))
        self.assertEqual(int(data.get("exitCode", -1)), 0)


if __name__ == "__main__":
    unittest.main()
