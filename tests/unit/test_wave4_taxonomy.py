import unittest

from backend import main


class Wave4TaxonomyTests(unittest.TestCase):
    def _first_op(self, text: str) -> dict:
        env = main.compile_intent_envelope(text)
        writes = ((env.get("stateIntent") or {}).get("writeOperations") or [])
        self.assertGreaterEqual(len(writes), 1, msg=text)
        first = writes[0]
        self.assertIsInstance(first, dict)
        return first

    def test_show_slack_channels_routes_to_channel_listing(self) -> None:
        op = self._first_op("show slack channels")
        self.assertEqual(str(op.get("type", "")), "slack.list_channels")

    def test_show_notifications_routes_to_notifications_view(self) -> None:
        op = self._first_op("show my notifications")
        self.assertEqual(str(op.get("type", "")), "notifications_view")

    def test_create_jira_ticket_routes_to_jira_create(self) -> None:
        op = self._first_op("create a jira ticket for mobile crash")
        self.assertEqual(str(op.get("type", "")), "jira_create")

    def test_show_vpn_status_routes_to_vpn_status(self) -> None:
        op = self._first_op("show vpn status")
        self.assertEqual(str(op.get("type", "")), "vpn_status")


if __name__ == "__main__":
    unittest.main()
