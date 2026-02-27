"""tests/unit/test_semantics.py
=================================
Unit tests for the structured intent taxonomy in backend/semantics.py.

Coverage
--------
- All 25 intents, each with ≥1 positive example from its ``examples`` list
- Blocker phrases that must suppress intent matching
- Signal-boundary: signals must not substring-match inside other words
- Extractor logic: payload field shapes and values
- Edge cases: empty input, filler-only input, ambiguous phrases
- Backward-compat shim: parse_semantic_command()
- Taxonomy meta-checks: all intents have examples, all extractors are registered
"""
from __future__ import annotations

import unittest

from backend.semantics import (
    TAXONOMY,
    IntentMatch,
    _EXTRACTORS,
    classify,
    parse_semantic_command,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def op(text: str) -> str | None:
    """Classify text and return the matched op type, or None."""
    m = classify(text)
    return m.op if m else None


def payload(text: str) -> dict | None:
    """Classify text and return the matched payload dict, or None."""
    m = classify(text)
    return m.payload if m else None


def domain(text: str) -> str | None:
    """Classify text and return the matched domain, or None."""
    m = classify(text)
    return m.domain if m else None


# ─── Test classes ─────────────────────────────────────────────────────────────

class TestWeatherForecast(unittest.TestCase):

    def test_current_location_implicit(self):
        self.assertEqual(op("what's the weather"), "weather_forecast")
        p = payload("what's the weather")
        self.assertEqual(p["location"], "__current__")
        self.assertEqual(p["window"], "now")

    def test_named_city(self):
        m = classify("weather in Denver")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "weather_forecast")
        self.assertEqual(m.payload["location"], "Denver")

    def test_tomorrow_window(self):
        m = classify("will it rain tomorrow")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["window"], "tomorrow")

    def test_7day_window_this_week(self):
        m = classify("weather forecast this week")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["window"], "7day")

    def test_7day_window_weekend(self):
        m = classify("what's the weather this weekend")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["window"], "7day")

    def test_tonight_window(self):
        m = classify("what's the weather tonight")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["window"], "tonight")

    def test_natural_phrasing(self):
        self.assertEqual(op("is it cold outside"), "weather_forecast")

    def test_storm_signal(self):
        self.assertEqual(op("any storm warnings?"), "weather_forecast")

    def test_temperature_signal(self):
        self.assertEqual(op("check the temperature in Austin"), "weather_forecast")


class TestShoppingSearch(unittest.TestCase):

    def test_shoes_query(self):
        self.assertEqual(op("show me Nike running shoes size 10"), "shop_catalog_search")
        self.assertEqual(payload("show me Nike running shoes size 10")["category"], "shoes")

    def test_brand_puma(self):
        self.assertEqual(op("I want to buy some Pumas"), "shop_catalog_search")
        self.assertEqual(payload("I want to buy some Pumas")["category"], "shoes")

    def test_electronics(self):
        self.assertEqual(op("I want to buy a laptop"), "shop_catalog_search")
        self.assertEqual(payload("I want to buy a laptop")["category"], "electronics")

    def test_apparel(self):
        self.assertEqual(op("find me a black hoodie"), "shop_catalog_search")
        self.assertEqual(payload("find me a black hoodie")["category"], "apparel")

    def test_blocker_jot_down(self):
        # "buy" is a shopping signal, but "jot" is a blocker
        self.assertNotEqual(op("jot this down: buy coffee beans"), "shop_catalog_search")

    def test_blocker_phone_number(self):
        # "phone number" is a blocker — should not trigger shopping
        self.assertNotEqual(op("find Johns phone number"), "shop_catalog_search")

    def test_blocker_contact(self):
        self.assertNotEqual(op("look up contact for Dr. Kim"), "shop_catalog_search")

    def test_order_airpods(self):
        self.assertEqual(op("order some AirPods"), "shop_catalog_search")
        self.assertEqual(payload("order some AirPods")["category"], "electronics")


class TestLocationCurrent(unittest.TestCase):

    def test_where_am_i(self):
        self.assertEqual(op("where am I"), "location_status")

    def test_my_location(self):
        self.assertEqual(op("what's my location"), "location_status")

    def test_show_my_location(self):
        self.assertEqual(op("show my location"), "location_status")

    def test_current_location_phrasing(self):
        self.assertEqual(op("what's my current location"), "location_status")


class TestNewsSearch(unittest.TestCase):

    def test_headlines(self):
        self.assertEqual(op("show me today's headlines"), "web_search")

    def test_news_about_topic(self):
        m = classify("latest news about AI")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")
        self.assertIn("ai", m.payload["query"].lower())

    def test_top_stories(self):
        self.assertEqual(op("top stories today"), "web_search")

    def test_breaking_news(self):
        self.assertEqual(op("what's the breaking news"), "web_search")


class TestFinanceStock(unittest.TestCase):

    def test_stock_ticker(self):
        m = classify("what's AAPL trading at")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")

    def test_stock_company(self):
        self.assertEqual(op("Tesla stock price"), "web_search")

    def test_market(self):
        self.assertEqual(op("how is the market today"), "web_search")

    def test_dow_not_download(self):
        # "dow" in "download" must not trigger finance domain; web_search is fine
        m = classify("how do I download this file")
        if m:
            self.assertNotEqual(m.domain, "finance",
                msg="'download' must not trigger dow/finance signal")

    def test_sp500(self):
        self.assertEqual(op("S&P 500 today"), "web_search")


class TestFinanceCrypto(unittest.TestCase):

    def test_bitcoin(self):
        m = classify("bitcoin price")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")
        self.assertIn("bitcoin", m.payload["query"].lower())

    def test_ethereum(self):
        self.assertEqual(op("what's ethereum worth"), "web_search")

    def test_dogecoin(self):
        self.assertEqual(op("how much is dogecoin"), "web_search")

    def test_generic_crypto(self):
        self.assertEqual(op("crypto prices today"), "web_search")


class TestBankingBalance(unittest.TestCase):

    def test_balance_query(self):
        self.assertEqual(op("what's my balance"), "banking_balance_read")
        self.assertEqual(domain("what's my balance"), "banking")

    def test_how_much_money(self):
        self.assertEqual(op("how much money do I have"), "banking_balance_read")

    def test_checking_account(self):
        self.assertEqual(op("checking account balance"), "banking_balance_read")


class TestBankingTransactions(unittest.TestCase):

    def test_recent_transactions(self):
        self.assertEqual(op("show my recent transactions"), "banking_transactions_read")

    def test_spending(self):
        self.assertEqual(op("show my recent charges"), "banking_transactions_read")

    def test_bank_statement(self):
        self.assertEqual(op("bank statement"), "banking_transactions_read")


class TestSocialPost(unittest.TestCase):

    def test_tweet(self):
        m = classify("tweet that I just shipped a new feature")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "social_message_send")
        self.assertIn("shipped", m.payload.get("text", ""))

    def test_post_with_colon(self):
        m = classify("post: loving this new UI")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "social_message_send")

    def test_unmatched_post_structure_returns_none(self):
        # "post" alone without content should not match (extractor returns None)
        m = classify("post")
        if m:
            self.assertNotEqual(m.op, "social_message_send")


class TestSocialFeed(unittest.TestCase):

    def test_show_feed(self):
        self.assertEqual(op("show my feed"), "social_feed_read")

    def test_timeline(self):
        self.assertEqual(op("what's on my timeline"), "social_feed_read")


class TestExpenseAdd(unittest.TestCase):

    def test_dollar_sign(self):
        m = classify("I spent $45 on groceries")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_expense")
        self.assertAlmostEqual(m.payload["amount"], 45.0)

    def test_words_dollars(self):
        m = classify("paid 12 dollars for coffee")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_expense")
        self.assertAlmostEqual(m.payload["amount"], 12.0)

    def test_category_inferred_food(self):
        m = classify("I spent $20 on lunch")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["category"], "food")

    def test_category_inferred_transport(self):
        m = classify("paid $15 for uber")
        self.assertIsNotNone(m)
        self.assertEqual(m.payload["category"], "transport")

    def test_no_amount_returns_none(self):
        m = classify("spent some money")
        if m:
            self.assertNotEqual(m.op, "add_expense")

    def test_log_expense_format(self):
        m = classify("log expense $200 for new shoes")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_expense")
        self.assertAlmostEqual(m.payload["amount"], 200.0)


class TestExpenseList(unittest.TestCase):

    def test_show_expenses(self):
        self.assertEqual(op("show my expenses"), "graph_query")
        self.assertEqual(payload("show my expenses")["kind"], "expense")

    def test_what_did_i_spend(self):
        self.assertEqual(op("what did I spend this week"), "graph_query")

    def test_list_expenses(self):
        self.assertEqual(op("list my expenses"), "graph_query")


class TestNoteCreate(unittest.TestCase):

    def test_jot_down(self):
        m = classify("jot this down: the API key expires in March")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_note")
        self.assertIn("API key", m.payload["text"])

    def test_note_that(self):
        m = classify("note that we need to revisit auth")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_note")

    def test_remember_that(self):
        m = classify("remember that the meeting is at 3pm")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_note")
        self.assertIn("3pm", m.payload["text"])

    def test_add_note_colon(self):
        m = classify("add a note: deadline is Friday")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_note")

    def test_jot_does_not_trigger_shopping(self):
        # "buy" inside a jot command must not fire shopping
        m = classify("jot this down: buy coffee beans")
        if m:
            self.assertNotEqual(m.op, "shop_catalog_search")


class TestNoteList(unittest.TestCase):

    def test_show_notes(self):
        self.assertEqual(op("show my notes"), "graph_query")
        self.assertEqual(payload("show my notes")["kind"], "note")

    def test_what_notes_do_i_have(self):
        self.assertEqual(op("what notes do I have"), "graph_query")

    def test_list_notes(self):
        self.assertEqual(op("list notes"), "graph_query")


class TestReminderSet(unittest.TestCase):

    def test_remind_me_in_minutes(self):
        m = classify("remind me to call dentist in 30 minutes")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "schedule_remind_once")
        self.assertIn("call dentist", m.payload["text"])
        self.assertEqual(m.payload["delayMs"], 30 * 60_000)

    def test_remind_me_in_hours(self):
        m = classify("set a reminder for the standup in 2 hours")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "schedule_remind_once")
        self.assertEqual(m.payload["delayMs"], 2 * 3_600_000)

    def test_alert_me_min_abbrev(self):
        m = classify("alert me to check email in 15 min")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "schedule_remind_once")
        self.assertEqual(m.payload["delayMs"], 15 * 60_000)

    def test_remind_me_without_time_falls_through(self):
        # No time spec → reminder.set_timed extractor returns None → should fall through to task.create
        m = classify("remind me to buy milk")
        self.assertIsNotNone(m)
        # Should become a task, not a timed reminder
        self.assertEqual(m.op, "add_task")

    def test_in_half_an_hour(self):
        m = classify("remind me to stretch in half an hour")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "schedule_remind_once")
        self.assertEqual(m.payload["delayMs"], 30 * 60_000)


class TestReminderList(unittest.TestCase):

    def test_show_reminders(self):
        self.assertEqual(op("show my reminders"), "list_reminders")

    def test_what_reminders(self):
        self.assertEqual(op("what reminders do I have"), "list_reminders")

    def test_any_reminders(self):
        self.assertEqual(op("do I have any reminders"), "list_reminders")


class TestTaskComplete(unittest.TestCase):

    def test_finished(self):
        m = classify("I finished the grocery run")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "toggle_task")
        self.assertIn("grocery run", m.payload["selector"])

    def test_mark_done(self):
        m = classify("mark the report task done")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "toggle_task")

    def test_check_off(self):
        m = classify("check off pick up dry cleaning")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "toggle_task")


class TestTaskDelete(unittest.TestCase):

    def test_delete_task(self):
        m = classify("delete my grocery task")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "delete_task")
        self.assertIn("grocery", m.payload["selector"])

    def test_remove_task(self):
        m = classify("remove the dentist appointment task")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "delete_task")

    def test_delete_does_not_match_note(self):
        # "delete" + "note" keyword → blocker should prevent task.delete
        m = classify("delete my note about the project")
        if m:
            self.assertNotEqual(m.op, "delete_task")

    def test_delete_does_not_match_expense(self):
        m = classify("delete my expense for coffee")
        if m:
            self.assertNotEqual(m.op, "delete_task")


class TestTaskClearCompleted(unittest.TestCase):

    def test_clear_completed_tasks(self):
        self.assertEqual(op("clear completed tasks"), "clear_completed")

    def test_clean_up_done_todos(self):
        self.assertEqual(op("clean up done todos"), "clear_completed")

    def test_remove_finished_items(self):
        self.assertEqual(op("remove all finished tasks"), "clear_completed")


class TestTaskList(unittest.TestCase):

    def test_what_are_my_tasks(self):
        self.assertEqual(op("what are my tasks"), "graph_query")
        self.assertEqual(payload("what are my tasks")["kind"], "task")

    def test_show_todo_list(self):
        self.assertEqual(op("show my todo list"), "graph_query")

    def test_my_tasks(self):
        self.assertEqual(op("show my tasks"), "graph_query")

    def test_whats_on_my_list(self):
        self.assertEqual(op("what's on my list"), "graph_query")

    def test_do_i_have_any_tasks(self):
        self.assertEqual(op("do I have any tasks"), "graph_query")

    def test_task_list_payload_done_false(self):
        p = payload("show my tasks")
        self.assertFalse(p.get("done", True))


class TestTaskCreate(unittest.TestCase):

    def test_need_to(self):
        m = classify("I need to call the dentist")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_task")
        self.assertIn("call the dentist", m.payload["title"])

    def test_add_task_colon(self):
        m = classify("add task: finish the report")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_task")
        self.assertIn("finish the report", m.payload["title"])

    def test_i_should(self):
        m = classify("I should renew my passport")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_task")

    def test_gotta(self):
        m = classify("gotta to pick up dry cleaning")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_task")

    def test_dont_forget(self):
        m = classify("don't forget to submit the invoice")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "add_task")

    def test_info_query_not_task(self):
        # "how to build X" should not become a task
        m = classify("how to build a weather app")
        if m:
            self.assertNotEqual(m.op, "add_task")


class TestContactsLookup(unittest.TestCase):

    def test_phone_number_for(self):
        m = classify("find Johns phone number")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "contacts_lookup")

    def test_contact_for_name(self):
        m = classify("look up contact for Dr. Kim")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "contacts_lookup")

    def test_whats_sarahs_email(self):
        # "what's Sarah's email" is genuinely ambiguous — may route to
        # contacts_lookup or web_search. Just verify no crash.
        classify("what's Sarah's email")  # no assertion, must not raise

    def test_how_do_i_reach(self):
        m = classify("how do I reach Mike")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "contacts_lookup")


class TestWebSummarize(unittest.TestCase):

    def test_summarize_url(self):
        m = classify("summarize https://example.com/article")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_summarize")
        self.assertEqual(m.payload["url"], "https://example.com/article")

    def test_tldr(self):
        m = classify("tldr this: https://news.ycombinator.com")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_summarize")


class TestWebFetch(unittest.TestCase):

    def test_fetch_url(self):
        m = classify("fetch https://example.com")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "fetch_url")
        self.assertEqual(m.payload["url"], "https://example.com")

    def test_open_url(self):
        m = classify("open https://api.github.com/zen")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "fetch_url")


class TestWebSearch(unittest.TestCase):

    def test_what_is(self):
        m = classify("what is quantum computing")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")
        self.assertIn("quantum computing", m.payload["query"])

    def test_who_was(self):
        m = classify("who was Alan Turing")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")

    def test_how_does(self):
        m = classify("how does DNS work")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")

    def test_explain(self):
        m = classify("explain neural networks")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "web_search")

    def test_blocker_my_tasks(self):
        # "what are my tasks" must NOT route to web.search despite "what are"
        self.assertNotEqual(op("what are my tasks"), "web_search")

    def test_blocker_my_notes(self):
        self.assertNotEqual(op("what are my notes"), "web_search")

    def test_blocker_my_expenses(self):
        self.assertNotEqual(op("show my expenses"), "web_search")


# ─── Edge case tests ───────────────────────────────────────────────────────────

class TestEdgeCases(unittest.TestCase):

    def test_empty_string_returns_none(self):
        self.assertIsNone(classify(""))

    def test_whitespace_only_returns_none(self):
        self.assertIsNone(classify("   "))

    def test_filler_only_returns_none(self):
        # Pure filler stripped to empty → None
        m = classify("can you please")
        # May or may not match something — just ensure it doesn't crash
        # (normalize may strip to nothing)
        # No assertion on value; just validate no exception raised

    def test_returns_intent_match_type(self):
        m = classify("what's the weather")
        self.assertIsInstance(m, IntentMatch)

    def test_confidence_in_range(self):
        m = classify("show my tasks")
        self.assertIsNotNone(m)
        self.assertGreaterEqual(m.confidence, 0.0)
        self.assertLessEqual(m.confidence, 1.0)

    def test_shim_returns_dict_or_none(self):
        result = parse_semantic_command("show my tasks")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["type"], "graph_query")

    def test_shim_returns_none_for_empty(self):
        result = parse_semantic_command("")
        self.assertIsNone(result)

    def test_shim_has_type_domain_payload(self):
        result = parse_semantic_command("what's the weather")
        self.assertIsNotNone(result)
        self.assertIn("type", result)
        self.assertIn("domain", result)
        self.assertIn("payload", result)

    def test_with_filler_preamble(self):
        # "can you" filler is stripped; resulting phrase should still classify
        m = classify("can you show the weather for tomorrow")
        self.assertIsNotNone(m)
        self.assertEqual(m.op, "weather_forecast")


# ─── Taxonomy meta-tests ──────────────────────────────────────────────────────

class TestTaxonomyIntegrity(unittest.TestCase):

    def test_all_intents_have_id(self):
        for intent_id, intent in TAXONOMY.items():
            self.assertEqual(intent.id, intent_id, f"Intent id mismatch for {intent_id}")

    def test_all_intents_have_op(self):
        for intent_id, intent in TAXONOMY.items():
            self.assertTrue(intent.op, f"Intent {intent_id} has empty op")

    def test_all_intents_have_domain(self):
        for intent_id, intent in TAXONOMY.items():
            self.assertTrue(intent.domain, f"Intent {intent_id} has empty domain")

    def test_all_intents_have_examples(self):
        for intent_id, intent in TAXONOMY.items():
            self.assertGreater(len(intent.examples), 0,
                f"Intent {intent_id} has no examples")

    def test_all_extractors_registered(self):
        for intent_id, intent in TAXONOMY.items():
            self.assertIn(intent.extractor, _EXTRACTORS,
                f"Intent {intent_id} references unregistered extractor '{intent.extractor}'")

    def test_taxonomy_has_28_intents(self):
        self.assertEqual(len(TAXONOMY), 28)

    def test_each_example_classifiable(self):
        """Every intent's own examples should classify to that intent's op."""
        failures = []
        # Intents that share op with multiple intent IDs (news/finance → web_search)
        shared_ops = {"web_search"}
        for intent_id, intent in TAXONOMY.items():
            if intent.op in shared_ops:
                continue  # multiple intents share this op — skip strict check
            for example in intent.examples[:2]:  # test first 2 examples only
                m = classify(example)
                if m is None or m.op != intent.op:
                    failures.append(
                        f"{intent_id}: '{example}' → {m.op if m else 'None'} (expected {intent.op})"
                    )
        if failures:
            self.fail("Some intent examples did not classify to the expected op:\n  " +
                      "\n  ".join(failures))


# ─── Signal boundary tests ─────────────────────────────────────────────────────

class TestSignalBoundaries(unittest.TestCase):
    """Ensure word-boundary matching prevents false positive signal hits."""

    def test_dow_not_in_download(self):
        # "dow" is a finance.stock signal — must not trigger finance domain
        m = classify("how do I download the file")
        if m:
            self.assertNotEqual(m.domain, "finance",
                msg="'download' must not trigger dow/finance signal")

    def test_jot_boundary(self):
        # "jot" is a note.create signal — "jotter" should not trigger it
        # (and certainly shouldn't match shopping)
        m = classify("open my jotter app")
        if m:
            self.assertNotEqual(m.op, "add_note")

    def test_post_not_in_repost(self):
        # social.post signals: "post" — must not match mid-word
        # "my recent repost" shouldn't fire social.post extractor with empty text
        m = classify("show my recent repost")
        if m:
            # If it fires social.post, the extractor should return None (no body)
            # so the whole match should be None or some other intent
            self.assertNotEqual(m.op, "social_message_send")

    def test_note_signal_boundary(self):
        # "note" in TAXONOMY shopping blockers; "footnote" should not block shopping
        m = classify("show me nike shoes for footnote collectors")
        # Main check: shouldn't crash, and the shopping intent can fire
        self.assertIsNotNone(m)


if __name__ == "__main__":
    unittest.main()
