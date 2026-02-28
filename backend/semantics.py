"""
backend/semantics.py
====================
Structured intent taxonomy and NLU pipeline for GenomeUI.

Architecture
------------
  Intent      – formal definition of one intent: signals, patterns, extractor
  IntentMatch – result of classify(): op type, domain, payload, confidence
  TAXONOMY    – dict[intent_id → Intent], the single source of truth
  classify()  – generic interpreter: text → IntentMatch | None
  parse_semantic_command() – thin backward-compat shim for main.py

Adding a new intent
-------------------
  1. Write an extractor: ``def _ext_foo(raw: str, lower: str) -> dict | None``
     Return None if the text doesn't actually match this intent.
  2. Register the extractor in ``_EXTRACTORS``.
  3. Add an ``Intent(...)`` entry to ``TAXONOMY``.
  4. Done.  classify() picks it up automatically.

Intent priority
---------------
  TAXONOMY is an ordered dict (Python 3.7+).  classify() iterates it in
  insertion order, so higher-priority intents should be declared first.
  Extractors returning None let lower-priority intents be tried.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable

# ─── Types ────────────────────────────────────────────────────────────────────

ExtractorFn = Callable[[str, str], "dict[str, Any] | None"]


@dataclass  # pylint: disable=too-many-instance-attributes
class Intent:
    """Formal definition of a single user intent.

    Attributes
    ----------
    id          dot-namespaced name, e.g. "weather.forecast"
    op          backend op type string, e.g. "weather_forecast"
    domain      backend domain string, e.g. "weather"
    description human-readable summary
    signals     ANY of these words present → try this intent (word-boundary matched)
    extractor   key into _EXTRACTORS registry
    patterns    optional hard-match regex patterns (also checked by _has_signal)
    blockers    ANY of these present → skip this intent entirely
    examples    sample phrases, for docs and tests
    """
    id: str
    op: str
    domain: str
    description: str
    signals: list[str]
    extractor: str
    patterns: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    slots: dict[str, str] = field(default_factory=dict)  # slot_name → hint for LLM


@dataclass
class IntentMatch:
    """The result of classify()."""
    intent: Intent
    payload: dict[str, Any]
    confidence: float = 0.8   # 0.0–1.0; future: use signal density

    @property
    def op(self) -> str:
        """Shortcut to the matched intent's op type."""
        return self.intent.op

    @property
    def domain(self) -> str:
        """Shortcut to the matched intent's domain."""
        return self.intent.domain

    def to_op_dict(self) -> dict[str, Any]:
        """Convert to the op dict format used by the backend executor."""
        return {"type": self.intent.op, "domain": self.intent.domain, "payload": self.payload}


# ─── Shared helpers ───────────────────────────────────────────────────────────

_FILLER_RE = re.compile(
    r"(?i)^(hey\s+)?"
    r"(?:can\s+you\s+|could\s+you\s+|please\s+|"
    r"i(?:'d|\s+would)\s+like\s+(?:to\s+|you\s+to\s+)?)?",
)


def _normalize(text: str) -> str:
    """Collapse whitespace, strip trailing punctuation, and strip leading filler phrases."""
    s = re.sub(r"\s+", " ", text.strip())
    s = re.sub(r"[?.!]+$", "", s).strip()  # trailing ? ! . don't affect matching
    return _FILLER_RE.sub("", s).strip()


_LOC_PREP_RE = re.compile(
    r"\b(?:in|at|for)\s+(.+?)"
    r"(?:\s+(?:today|tomorrow|tonight|now|right\s+now|this\s+week|next\s+week|this\s+weekend|weekend))?$",
    re.IGNORECASE,
)


def _extract_location(raw: str, lower: str) -> str:
    """Extract explicit location string or return '__current__' / '' if none."""
    # Explicit "in/at/for <place>"
    m = _LOC_PREP_RE.search(raw)
    if m:
        candidate = _normalize(m.group(1))
        if candidate.lower() not in {"today", "tomorrow", "now", "right now", "tonight",
                                     "weekend", "this weekend", "the weekend",
                                     "this week", "next week", "the week"}:
            return candidate

    # "here / where I am / my location"
    if any(p in lower for p in ("where i am", "my location", "where am i", " here", "around me")):
        return "__current__"

    # Leading question starters imply current location
    if re.match(
        r"^(what(?:'s|\s+is|\s+(?:the|will|about)\b)|whats|show|check|get|tell\s+me|"
        r"how(?:'s|\s+is|\s+will\b)|is\s+it\b|will\s+it\b|weather|forecast|temperature|"
        r"what|how|is|will|gonna|going)\b",
        lower,
    ):
        return "__current__"

    # Any temporal marker without an explicit location → current
    if re.search(r"\b(today|tonight|this\s+(?:evening|night|week|weekend|morning|afternoon)|"
                 r"tomorrow|next\s+week)\b", lower):
        return "__current__"

    return ""


_WEEKDAY_FULL = frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
})
_WEEKDAY_ABBREV: dict[str, str] = {
    "mon": "monday", "tue": "tuesday", "tues": "tuesday",
    "wed": "wednesday", "thu": "thursday", "thur": "thursday", "thurs": "thursday",
    "fri": "friday", "sat": "saturday", "sun": "sunday",
}


def _extract_time_window(lower: str) -> str:
    """Extract weather/forecast time window.

    Returns one of: now | tonight | tomorrow | weekend | Nday | <weekday> |
                    morning | afternoon | evening |
                    tomorrow-morning | tomorrow-afternoon | tomorrow-evening
    """
    # N-day forecast: "3 day", "5-day", "10 day forecast"
    m = re.search(r"\b(\d+)[\s\-]?day\b", lower)
    if m:
        n = max(1, min(int(m.group(1)), 14))
        return f"{n}day"

    # Specific weekday (full names first so abbreviation loop won't double-match)
    for full in _WEEKDAY_FULL:
        if re.search(rf"\b{full}\b", lower):
            return full
    for abbrev, full in _WEEKDAY_ABBREV.items():
        if re.search(rf"\b{abbrev}\b", lower):
            return full

    # Time-of-day, optionally combined with "tomorrow"
    has_tomorrow = bool(re.search(r"\b(tomorrow|tom(?:rw)?)\b", lower))
    if re.search(r"\bmorning\b", lower):
        return "tomorrow-morning" if has_tomorrow else "morning"
    if re.search(r"\bafternoon\b", lower):
        return "tomorrow-afternoon" if has_tomorrow else "afternoon"
    if re.search(r"\b(tonight|this\s+(?:evening|night))\b", lower):
        return "tonight"
    if re.search(r"\bevening\b", lower):
        return "tomorrow-evening" if has_tomorrow else "evening"
    if has_tomorrow:
        return "tomorrow"

    if re.search(r"\b(this\s+weekend|the\s+weekend|next\s+weekend|weekend)\b", lower):
        return "weekend"
    if re.search(r"\b(this\s+week|next\s+week|weekly|extended|week(?:ly)?)\b", lower):
        return "7day"
    return "now"


def _parse_relative_delay_ms(text: str) -> int | None:
    """Parse a relative time expression into ms.  Returns None if not found."""
    lower = text.lower()
    m = re.search(r"\bin\s+(\d+(?:\.\d+)?)\s+(second|minute|min|hour|hr|day)s?\b", lower)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        mult = {"second": 1_000, "minute": 60_000, "min": 60_000,
                "hour": 3_600_000, "hr": 3_600_000, "day": 86_400_000}[unit]
        return max(1_000, int(val * mult))
    if re.search(r"\bin\s+half\s+an?\s+hour\b", lower):
        return 30 * 60_000
    m2 = re.search(r"\bin\s+an?\s+(hour|minute|min|second|day)\b", lower)
    if m2:
        unit = m2.group(1)
        return {"hour": 3_600_000, "minute": 60_000, "min": 60_000,
                "second": 1_000, "day": 86_400_000}[unit]
    m3 = re.search(r"\bin\s+(?:a\s+)?(?:few|couple(?:\s+of)?)\s+(minutes?|hours?|days?)\b", lower)
    if m3:
        unit_w = m3.group(1).rstrip("s")
        mult = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}.get(unit_w, 60_000)
        return 3 * mult
    return None


def _split_reminder_body_and_delay(raw: str) -> tuple[str, int] | None:
    """Split 'call dentist in 30 minutes' → ('call dentist', 1800000)."""
    lower = raw.lower()
    tail_pats = [
        r"\s+in\s+(\d+(?:\.\d+)?)\s+(second|minute|min|hour|hr|day)s?\s*$",
        r"\s+in\s+half\s+an?\s+hour\s*$",
        r"\s+in\s+an?\s+(hour|minute|min|second|day)\s*$",
        r"\s+in\s+(?:a\s+)?(?:few|couple(?:\s+of)?)\s+(?:minutes?|hours?|days?)\s*$",
    ]
    for pat in tail_pats:
        tm = re.search(pat, lower)
        if tm:
            body = raw[:tm.start()].strip()
            delay = _parse_relative_delay_ms(tm.group(0))
            if body and delay and delay > 0:
                return (body, delay)
    return None


_EXPENSE_CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("food",          ("lunch", "dinner", "breakfast", "coffee", "groceries", "grocery",
                       "food", "restaurant", "bar", "drink", "drinks", "meal", "snack",
                       "pizza", "burger", "cafe", "café", "takeout", "takeaway",
                       "delivery", "doordash", "grubhub", "ubereats")),
    ("transport",     ("gas", "fuel", "uber", "lyft", "taxi", "transit", "bus", "metro",
                       "subway", "parking", "toll", "train", "commute", "ride")),
    ("travel",        ("hotel", "airbnb", "flight", "airfare", "airline", "vacation",
                       "trip", "travel", "motel", "resort", "hostel", "cruise")),
    ("shopping",      ("amazon", "ebay", "shopping", "clothes", "clothing", "shoes",
                       "shirt", "pants", "jacket", "mall", "store", "market",
                       "outfit", "dress")),
    ("health",        ("doctor", "medical", "pharmacy", "prescription", "dentist",
                       "hospital", "clinic", "medicine", "health", "gym", "fitness",
                       "therapy")),
    ("utilities",     ("electric", "electricity", "water", "internet", "wifi",
                       "phone bill", "utility", "utilities", "cable", "subscription",
                       "rent", "mortgage")),
    ("entertainment", ("movie", "cinema", "concert", "show", "ticket", "game",
                       "netflix", "spotify", "hulu", "streaming", "entertainment",
                       "book", "books")),
]


def _infer_expense_category(note: str) -> str:
    """Infer expense category from free-text note."""
    n = note.lower()
    for category, keywords in _EXPENSE_CATEGORIES:
        if any(w in n for w in keywords):
            return category
    return "other"


def _infer_shopping_category(lower: str) -> str:
    """Classify shopping query into a product category."""
    electronics_kw = ("laptop", "phone", "iphone", "android", "headphones", "earbuds",
                      "airpods", "tablet", "ipad", "monitor", "keyboard", "mouse",
                      "charger", "cable", "speaker", "smartwatch", "watch", "camera",
                      "tv", "television", "gaming", "console", "gpu", "pc")
    home_kw = ("sofa", "couch", "desk", "chair", "bed", "mattress", "pillow", "blanket",
               "curtain", "lamp", "shelf", "bookshelf", "dresser", "rug", "furniture",
               "towel", "cookware", "fridge", "appliance")
    shoe_brands = ("puma", "pumas", "nike", "adidas", "reebok", "new balance", "asics",
                   "converse", "vans", "jordan", "under armour", "fila", "skechers")
    shoe_kw = ("shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "sandal", "sandals")
    apparel_kw = ("outfit", "jacket", "jeans", "hoodie", "sweatshirt", "t-shirt", "tshirt",
                  "shirt", "pants", "shorts", "dress", "skirt", "coat", "blazer", "suit",
                  "sweater", "leggings", "sportswear", "activewear", "athleisure")
    if any(w in lower for w in electronics_kw):
        return "electronics"
    if any(w in lower for w in home_kw):
        return "home"
    if any(w in lower for w in shoe_brands) or any(w in lower for w in shoe_kw) or \
            bool(re.search(r"\bsize\s+\d+\b", lower)):
        return "shoes"
    if any(w in lower for w in apparel_kw):
        return "apparel"
    return "general"


# ─── Intent extractors ────────────────────────────────────────────────────────
# Each function receives (raw: str, lower: str) and returns a payload dict or
# None.  Returning None means "this intent doesn't apply to this text."

def _ext_weather(raw: str, lower: str) -> dict[str, Any] | None:
    location = _extract_location(raw, lower)
    # Default to current location rather than rejecting — any weather signal
    # without an explicit place still implies "where I am now".
    return {"location": location or "__current__", "window": _extract_time_window(lower)}


def _ext_shopping(raw: str, lower: str) -> dict[str, Any] | None:
    return {"query": raw.strip(), "category": _infer_shopping_category(lower)}


def _ext_location_status(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_news(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(?:news|headlines?|stories?)\s+(?:about|on|regarding|for)\s+(.+)$", lower)
    if m:
        query = m.group(1).strip() + " news"
    else:
        # Strip question lead-in
        query = re.sub(
            r"(?i)^(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:latest\s+)?|"
            r"show\s+me\s+(?:the\s+)?(?:latest\s+)?|"
            r"(?:latest|breaking|top|today'?s?)\s+)",
            "",
            lower,
        ).strip()
        if not query or query in ("news", "the news", "headlines"):
            query = "top news today"
    return {"query": query}


def _ext_finance_stock(raw: str, lower: str) -> dict[str, Any] | None:
    # Strip question preamble
    query = re.sub(
        r"(?i)^(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:price\s+(?:of\s+)?)?|"
        r"how\s+(?:is|much\s+is)\s+(?:the\s+)?|show\s+me\s+(?:the\s+)?)",
        "",
        lower,
    ).strip()
    if not query or len(query) < 2:
        query = raw.strip()
    if not query.endswith("today") and "price" not in query:
        query += " stock price today"
    return {"query": query}


def _ext_finance_crypto(raw: str, lower: str) -> dict[str, Any] | None:
    query = re.sub(
        r"(?i)^(?:what(?:'s|\s+is)\s+(?:the\s+)?(?:price\s+(?:of\s+)?)?|"
        r"how\s+much\s+is\s+(?:the\s+)?)",
        "",
        lower,
    ).strip()
    if not query or len(query) < 2:
        query = raw.strip()
    if "price" not in query:
        query += " price today"
    return {"query": query}


def _ext_banking_balance(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_banking_transactions(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_social_feed(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_social_post(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.match(
        r"^(?:post|tweet|share|send\s+a\s+(?:tweet|post|social\s+message))\s*[:\-]?\s*"
        r"(?:that\s+|this\s*[:\-]\s*)?(.+)$",
        raw, re.IGNORECASE,
    )
    if not m:
        return None
    text = m.group(1).strip()
    if not text or len(text) < 2:
        return None
    return {"text": text[:280], "confirmed": False}


_MONEY_RE = re.compile(
    r"\$\s*([0-9]+(?:\.[0-9]{1,2})?)"
    r"|(?<!\w)([0-9]+(?:\.[0-9]{1,2})?)\s*(?:dollars?|bucks?)\b"
)


def _ext_expense_add(_raw: str, lower: str) -> dict[str, Any] | None:
    mm = _MONEY_RE.search(lower)
    if not mm:
        return None
    raw_amt = mm.group(1) or mm.group(2) or "0"
    try:
        amount = float(raw_amt.replace(",", ""))
    except ValueError:
        amount = 0.0
    if amount <= 0:
        return None
    note_m = re.search(r"\b(?:on|for)\s+(?:the\s+|a\s+|an\s+|some\s+)?(.+?)$", lower)
    note = note_m.group(1).strip() if note_m else ""
    return {"amount": amount, "category": _infer_expense_category(note), "note": note}


def _ext_expense_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {"kind": "expense", "limit": 20}


def _ext_note_create(raw: str, _lower: str) -> dict[str, Any] | None:
    pats = [
        r"^jot\s+(?:this\s+)?down\s*[:\-]?\s+(.+)$",
        r"^(?:jot\s+down\s+that|note\s+that|write\s+down\s+that|log\s+that)\s+(.+)$",
        r"^remember\s+that\s+(.+)$",
        r"^(?:add\s+(?:a\s+)?note|make\s+(?:a\s+)?note)\s*[:\-]\s*(.+)$",
        r"^(?:note|write\s+down|save\s+this|log\s+this|save\s+this|keep\s+this)\s*[:\-]\s*(.+)$",
        r"^i\s+want\s+to\s+(?:remember|note)\s+(?:that\s+)?(.+)$",
        r"^(?:note|jot)\s*:\s*(.+)$",
        r"^(?:save|keep)\s+(?:this|that)\s+(?:note\s*[:\-]?\s*)?(.+)$",
    ]
    for pat in pats:
        m = re.match(pat, raw, re.IGNORECASE)
        if m:
            text = m.group(1).strip()
            if text and len(text) > 2:
                return {"text": text}
    return None


def _ext_note_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {"kind": "note", "limit": 20}


def _ext_reminder_set(raw: str, _lower: str) -> dict[str, Any] | None:
    """Only fires when a relative time spec is present; else returns None."""
    pats = [
        r"^(?:please\s+)?(?:set\s+(?:a\s+)?)?remind\s+me\s+(?:to\s+|about\s+|that\s+)?(.+)$",
        r"^(?:set\s+(?:a\s+)?)?reminder\s+(?:to\s+|for\s+|about\s+)?(.+)$",
        r"^(?:alert\s+me\s+(?:to\s+|about\s+)?|ping\s+me\s+(?:to\s+|about\s+)?)(.+)$",
    ]
    for pat in pats:
        m = re.match(pat, raw, re.IGNORECASE)
        if m:
            body = m.group(1).strip()
            parsed = _split_reminder_body_and_delay(body)
            if parsed:
                task_text, delay_ms = parsed
                if task_text and delay_ms > 0:
                    return {"text": task_text, "delayMs": delay_ms}
            break   # matched structure but no time spec → don't handle here
    return None


def _ext_reminder_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_task_create(raw: str, lower: str) -> dict[str, Any] | None:
    # Guard: skip if it's clearly an info query
    if re.search(
        r"\b(know\s+(?:how|what|where|who|why)|understand\s+(?:how|what)|learn\s+(?:how|about|to)|"
        r"find\s+out|look\s+up|research|figure\s+out\s+(?:how|what|why)|"
        r"how\s+to\s+(?:do|build|make|create|fix|set\s+up|install|use|get))\b",
        lower,
    ):
        return None
    pats = [
        r"^(?:please\s+)?remind\s+me\s+to\s+(.+)$",
        r"^remember\s+to\s+(.+)$",
        r"^don'?t\s+(?:let\s+me\s+)?forget\s+to\s+(.+)$",
        r"^(?:i\s+)?(?:need|have|gotta|got(?:ta)?)\s+to\s+(.+)$",
        r"^(?:add|create|make)\s+(?:a\s+)?(?:new\s+)?task\s*[:\-]?\s*(?:to\s+|for\s+|called\s+)?(.+)$",
        r"^(?:can\s+you\s+)?(?:add|create|make)\s+(?:me\s+)?(?:a\s+)?(?:new\s+)?"
        r"task\s*[:\-]?\s*(?:to\s+|for\s+|called\s+)?(.+)$",
        r"^(?:todo|to-do|task)\s*[:\-]\s*(.+)$",
        r"^i\s+(?:should|must|ought\s+to)\s+(?!be\b)(.+)$",
        r"^(?:put|add)\s+(?:on\s+my\s+(?:list|todo)|to\s+my\s+(?:list|todo))\s*[:\-]?\s*(.+)$",
    ]
    for pat in pats:
        m = re.match(pat, raw, re.IGNORECASE)
        if m:
            title = m.group(1).strip()
            if title and len(title) > 2:
                return {"title": title}
    return None


def _ext_task_complete(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.match(
        r"^(?:i(?:'ve|\s+have|\s+just)?\s+)?(?:finished|completed|done\s+(?:with|the))\s+(.+)$"
        r"|^(?:mark|set)\s+(?:the\s+)?(?:task\s+)?(.+?)\s+(?:as\s+)?(?:done|complete|finished)$"
        r"|^(?:check\s+off|cross\s+off)\s+(?:the\s+)?(?:task\s+)?(.+)$",
        raw, re.IGNORECASE,
    )
    if not m:
        return None
    selector = next((g for g in m.groups() if g is not None), "").strip()
    if not selector or len(selector) < 2:
        return None
    return {"selector": selector}


_TASK_DELETE_RE = re.compile(
    r"^(?:delete|remove|drop|cancel)\s+(?:my\s+)?(?:the\s+)?"
    r"(?:task\s+(?:about\s+|to\s+|for\s+)?|todo\s+)?(.+?)(?:\s+task)?$",
    re.IGNORECASE,
)


def _ext_task_delete(raw: str, _lower: str) -> dict[str, Any] | None:
    m = _TASK_DELETE_RE.match(raw)
    if not m:
        return None
    selector = m.group(1).strip()
    # Don't fire for "delete file/note/expense/reminder/all" — wrong intent
    if not selector or len(selector) < 3:
        return None
    if re.search(r"^(file|folder|note|expense|reminder|job|all|everything)\b", selector.lower()):
        return None
    return {"selector": selector}


def _ext_task_clear(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_task_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {"kind": "task", "done": False, "limit": 20}


def _ext_contacts(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.match(
        r"^(?:find|search|look\s+up|get|show|what(?:'s|\s+is)"
        r"|how\s+do\s+i\s+(?:reach|contact|get\s+(?:in\s+touch\s+with|a\s+hold\s+of)))\s+"
        r"(?:(?:the\s+)?(?:contact|phone\s+number|email)\s+(?:for\s+|called\s+|named\s+)?)?(.+?)"
        r"(?:\s+(?:contact|number|email|phone))?$",
        raw, re.IGNORECASE,
    )
    if not m:
        return None
    q = m.group(1).strip()
    if not q or len(q) < 2:
        return None
    return {"query": q}


def _ext_timer_start(_raw: str, lower: str) -> dict[str, Any] | None:
    """Extract timer duration from 'set a timer for 5 minutes' or '30 second timer'."""
    _UNITS = {"second": 1_000, "sec": 1_000, "minute": 60_000, "min": 60_000,
              "hour": 3_600_000, "hr": 3_600_000}
    # "for 5 minutes" / "in 5 minutes"
    m = re.search(r"(?:for|in)\s+(\d+(?:\.\d+)?)\s+(second|sec|minute|min|hour|hr)s?\b", lower)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        return {"durationMs": max(1_000, int(val * _UNITS[unit]))}
    # "30 second timer" / "5 minute timer"
    m2 = re.search(r"(\d+(?:\.\d+)?)\s*(second|sec|minute|min|hour|hr)s?\s+(?:timer|countdown)\b", lower)
    if m2:
        val = float(m2.group(1))
        unit = m2.group(2)
        return {"durationMs": max(1_000, int(val * _UNITS[unit]))}
    return None


def _ext_calc(_raw: str, lower: str) -> dict[str, Any] | None:
    """Extract a calculation expression."""
    m = re.search(
        r"(?:what(?:'s|\s+is)\s+|calculate\s+|compute\s+|eval(?:uate)?\s+)?"
        r"([\d\s\+\-\*\/\(\)\.%]+(?:percent\s+of\s+[\d\.]+)?)",
        lower,
    )
    if not m:
        return None
    expr = m.group(1).strip()
    if len(expr) < 3 or not re.search(r"\d", expr):
        return None
    return {"expression": expr}


def _ext_unit_convert(_raw: str, lower: str) -> dict[str, Any] | None:
    """Extract unit conversion query."""
    _UNITS = (r"miles?|km|kilometers?|meters?|feet|foot|inches?|pounds?|lbs?|"
              r"kilograms?|kg|celsius|fahrenheit|gallons?|liters?|litres?|oz|ounces?|"
              r"cups?|tbsp|tsp")
    # Standard: "10 km to miles" / "convert 72 fahrenheit to celsius"
    m = re.search(
        r"(?:convert\s+|what\s+is\s+)?([\d\.]+)\s*"
        r"(" + _UNITS + r")\s+(?:in|to|into|as)\s+(" + _UNITS + r")",
        lower,
    )
    if m:
        return {"amount": m.group(1), "from_unit": m.group(2), "to_unit": m.group(3),
                "query": lower}
    # Inverted: "how many miles is 10 km"
    m2 = re.search(
        r"how\s+many\s+(" + _UNITS + r")\s+is\s+([\d\.]+)\s*(" + _UNITS + r")",
        lower,
    )
    if m2:
        return {"amount": m2.group(2), "from_unit": m2.group(3), "to_unit": m2.group(1),
                "query": lower}
    return None


def _ext_web_search(raw: str, _lower: str) -> dict[str, Any] | None:
    query = raw.strip()
    strip_pats = [
        r"^(?:search\s+(?:for|the\s+web\s+for|online\s+for)|look\s+up|google|bing)\s+",
        r"^(?:tell\s+me\s+about|explain\s+|define\s+|research\s+)",
        r"^find\s+(?:me\s+)?(?:info(?:rmation)?\s+(?:on|about)\s+|some\s+(?:info\s+on\s+)?)?",
    ]
    for sp in strip_pats:
        sm = re.match(sp, raw, re.IGNORECASE)
        if sm:
            query = raw[sm.end():].strip()
            break
    if not query or len(query) < 2:
        return None
    return {"query": query}


def _ext_web_fetch(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.search(r"(https?://\S+)", raw)
    if not m:
        return None
    return {"url": m.group(1).rstrip(".,;)")}


def _ext_web_summarize(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.search(r"(https?://\S+)", raw)
    if not m:
        return None
    return {"url": m.group(1).rstrip(".,;)")}


# ─── Sports helpers ──────────────────────────────────────────────────────────

# Mapping: lowercase key → (full_name, abbrev, sport_league)
# sport_league matches _SPORT_LEAGUE keys in main.py
_TEAM_LOOKUP: dict[str, tuple[str, str, str]] = {
    # ── NFL ──────────────────────────────────────────────────────────────────
    "bears": ("Chicago Bears", "CHI", "nfl"),
    "chicago bears": ("Chicago Bears", "CHI", "nfl"),
    "chi": ("Chicago Bears", "CHI", "nfl"),
    "packers": ("Green Bay Packers", "GB", "nfl"),
    "green bay packers": ("Green Bay Packers", "GB", "nfl"),
    "gb": ("Green Bay Packers", "GB", "nfl"),
    "vikings": ("Minnesota Vikings", "MIN", "nfl"),
    "minnesota vikings": ("Minnesota Vikings", "MIN", "nfl"),
    "lions": ("Detroit Lions", "DET", "nfl"),
    "detroit lions": ("Detroit Lions", "DET", "nfl"),
    "cowboys": ("Dallas Cowboys", "DAL", "nfl"),
    "dallas cowboys": ("Dallas Cowboys", "DAL", "nfl"),
    "dal": ("Dallas Cowboys", "DAL", "nfl"),
    "eagles": ("Philadelphia Eagles", "PHI", "nfl"),
    "philadelphia eagles": ("Philadelphia Eagles", "PHI", "nfl"),
    "giants": ("New York Giants", "NYG", "nfl"),
    "new york giants": ("New York Giants", "NYG", "nfl"),
    "nyg": ("New York Giants", "NYG", "nfl"),
    "commanders": ("Washington Commanders", "WSH", "nfl"),
    "washington commanders": ("Washington Commanders", "WSH", "nfl"),
    "patriots": ("New England Patriots", "NE", "nfl"),
    "new england patriots": ("New England Patriots", "NE", "nfl"),
    "bills": ("Buffalo Bills", "BUF", "nfl"),
    "buffalo bills": ("Buffalo Bills", "BUF", "nfl"),
    "dolphins": ("Miami Dolphins", "MIA", "nfl"),
    "miami dolphins": ("Miami Dolphins", "MIA", "nfl"),
    "jets": ("New York Jets", "NYJ", "nfl"),
    "new york jets": ("New York Jets", "NYJ", "nfl"),
    "nyj": ("New York Jets", "NYJ", "nfl"),
    "steelers": ("Pittsburgh Steelers", "PIT", "nfl"),
    "pittsburgh steelers": ("Pittsburgh Steelers", "PIT", "nfl"),
    "ravens": ("Baltimore Ravens", "BAL", "nfl"),
    "baltimore ravens": ("Baltimore Ravens", "BAL", "nfl"),
    "browns": ("Cleveland Browns", "CLE", "nfl"),
    "cleveland browns": ("Cleveland Browns", "CLE", "nfl"),
    "bengals": ("Cincinnati Bengals", "CIN", "nfl"),
    "cincinnati bengals": ("Cincinnati Bengals", "CIN", "nfl"),
    "colts": ("Indianapolis Colts", "IND", "nfl"),
    "indianapolis colts": ("Indianapolis Colts", "IND", "nfl"),
    "texans": ("Houston Texans", "HOU", "nfl"),
    "houston texans": ("Houston Texans", "HOU", "nfl"),
    "jaguars": ("Jacksonville Jaguars", "JAX", "nfl"),
    "jacksonville jaguars": ("Jacksonville Jaguars", "JAX", "nfl"),
    "titans": ("Tennessee Titans", "TEN", "nfl"),
    "tennessee titans": ("Tennessee Titans", "TEN", "nfl"),
    "broncos": ("Denver Broncos", "DEN", "nfl"),
    "denver broncos": ("Denver Broncos", "DEN", "nfl"),
    "chiefs": ("Kansas City Chiefs", "KC", "nfl"),
    "kansas city chiefs": ("Kansas City Chiefs", "KC", "nfl"),
    "kc": ("Kansas City Chiefs", "KC", "nfl"),
    "raiders": ("Las Vegas Raiders", "LV", "nfl"),
    "las vegas raiders": ("Las Vegas Raiders", "LV", "nfl"),
    "chargers": ("Los Angeles Chargers", "LAC", "nfl"),
    "los angeles chargers": ("Los Angeles Chargers", "LAC", "nfl"),
    "lac": ("Los Angeles Chargers", "LAC", "nfl"),
    "49ers": ("San Francisco 49ers", "SF", "nfl"),
    "niners": ("San Francisco 49ers", "SF", "nfl"),
    "san francisco 49ers": ("San Francisco 49ers", "SF", "nfl"),
    "seahawks": ("Seattle Seahawks", "SEA", "nfl"),
    "seattle seahawks": ("Seattle Seahawks", "SEA", "nfl"),
    "rams": ("Los Angeles Rams", "LAR", "nfl"),
    "los angeles rams": ("Los Angeles Rams", "LAR", "nfl"),
    "lar": ("Los Angeles Rams", "LAR", "nfl"),
    "cardinals": ("Arizona Cardinals", "ARI", "nfl"),
    "arizona cardinals": ("Arizona Cardinals", "ARI", "nfl"),
    "falcons": ("Atlanta Falcons", "ATL", "nfl"),
    "atlanta falcons": ("Atlanta Falcons", "ATL", "nfl"),
    "panthers": ("Carolina Panthers", "CAR", "nfl"),
    "carolina panthers": ("Carolina Panthers", "CAR", "nfl"),
    "saints": ("New Orleans Saints", "NO", "nfl"),
    "new orleans saints": ("New Orleans Saints", "NO", "nfl"),
    "buccaneers": ("Tampa Bay Buccaneers", "TB", "nfl"),
    "bucs": ("Tampa Bay Buccaneers", "TB", "nfl"),
    "tampa bay buccaneers": ("Tampa Bay Buccaneers", "TB", "nfl"),
    # ── NBA ──────────────────────────────────────────────────────────────────
    "bulls": ("Chicago Bulls", "CHI", "nba"),
    "chicago bulls": ("Chicago Bulls", "CHI", "nba"),
    "bucks": ("Milwaukee Bucks", "MIL", "nba"),
    "milwaukee bucks": ("Milwaukee Bucks", "MIL", "nba"),
    "pacers": ("Indiana Pacers", "IND", "nba"),
    "indiana pacers": ("Indiana Pacers", "IND", "nba"),
    "pistons": ("Detroit Pistons", "DET", "nba"),
    "detroit pistons": ("Detroit Pistons", "DET", "nba"),
    "cavaliers": ("Cleveland Cavaliers", "CLE", "nba"),
    "cavs": ("Cleveland Cavaliers", "CLE", "nba"),
    "cleveland cavaliers": ("Cleveland Cavaliers", "CLE", "nba"),
    "celtics": ("Boston Celtics", "BOS", "nba"),
    "boston celtics": ("Boston Celtics", "BOS", "nba"),
    "knicks": ("New York Knicks", "NYK", "nba"),
    "new york knicks": ("New York Knicks", "NYK", "nba"),
    "76ers": ("Philadelphia 76ers", "PHI", "nba"),
    "sixers": ("Philadelphia 76ers", "PHI", "nba"),
    "philadelphia 76ers": ("Philadelphia 76ers", "PHI", "nba"),
    "nets": ("Brooklyn Nets", "BKN", "nba"),
    "brooklyn nets": ("Brooklyn Nets", "BKN", "nba"),
    "raptors": ("Toronto Raptors", "TOR", "nba"),
    "toronto raptors": ("Toronto Raptors", "TOR", "nba"),
    "heat": ("Miami Heat", "MIA", "nba"),
    "miami heat": ("Miami Heat", "MIA", "nba"),
    "hawks": ("Atlanta Hawks", "ATL", "nba"),
    "atlanta hawks": ("Atlanta Hawks", "ATL", "nba"),
    "hornets": ("Charlotte Hornets", "CHA", "nba"),
    "charlotte hornets": ("Charlotte Hornets", "CHA", "nba"),
    "magic": ("Orlando Magic", "ORL", "nba"),
    "orlando magic": ("Orlando Magic", "ORL", "nba"),
    "wizards": ("Washington Wizards", "WAS", "nba"),
    "washington wizards": ("Washington Wizards", "WAS", "nba"),
    "bucks": ("Milwaukee Bucks", "MIL", "nba"),
    "lakers": ("Los Angeles Lakers", "LAL", "nba"),
    "los angeles lakers": ("Los Angeles Lakers", "LAL", "nba"),
    "lal": ("Los Angeles Lakers", "LAL", "nba"),
    "clippers": ("Los Angeles Clippers", "LAC", "nba"),
    "los angeles clippers": ("Los Angeles Clippers", "LAC", "nba"),
    "warriors": ("Golden State Warriors", "GSW", "nba"),
    "golden state warriors": ("Golden State Warriors", "GSW", "nba"),
    "gsw": ("Golden State Warriors", "GSW", "nba"),
    "suns": ("Phoenix Suns", "PHX", "nba"),
    "phoenix suns": ("Phoenix Suns", "PHX", "nba"),
    "nuggets": ("Denver Nuggets", "DEN", "nba"),
    "denver nuggets": ("Denver Nuggets", "DEN", "nba"),
    "jazz": ("Utah Jazz", "UTA", "nba"),
    "utah jazz": ("Utah Jazz", "UTA", "nba"),
    "trail blazers": ("Portland Trail Blazers", "POR", "nba"),
    "blazers": ("Portland Trail Blazers", "POR", "nba"),
    "portland trail blazers": ("Portland Trail Blazers", "POR", "nba"),
    "thunder": ("Oklahoma City Thunder", "OKC", "nba"),
    "oklahoma city thunder": ("Oklahoma City Thunder", "OKC", "nba"),
    "okc": ("Oklahoma City Thunder", "OKC", "nba"),
    "mavericks": ("Dallas Mavericks", "DAL", "nba"),
    "mavs": ("Dallas Mavericks", "DAL", "nba"),
    "dallas mavericks": ("Dallas Mavericks", "DAL", "nba"),
    "rockets": ("Houston Rockets", "HOU", "nba"),
    "houston rockets": ("Houston Rockets", "HOU", "nba"),
    "grizzlies": ("Memphis Grizzlies", "MEM", "nba"),
    "memphis grizzlies": ("Memphis Grizzlies", "MEM", "nba"),
    "pelicans": ("New Orleans Pelicans", "NOP", "nba"),
    "new orleans pelicans": ("New Orleans Pelicans", "NOP", "nba"),
    "spurs": ("San Antonio Spurs", "SAS", "nba"),
    "san antonio spurs": ("San Antonio Spurs", "SAS", "nba"),
    "timberwolves": ("Minnesota Timberwolves", "MIN", "nba"),
    "wolves": ("Minnesota Timberwolves", "MIN", "nba"),
    "minnesota timberwolves": ("Minnesota Timberwolves", "MIN", "nba"),
    "kings": ("Sacramento Kings", "SAC", "nba"),
    "sacramento kings": ("Sacramento Kings", "SAC", "nba"),
    # ── MLB ──────────────────────────────────────────────────────────────────
    "cubs": ("Chicago Cubs", "CHC", "mlb"),
    "chicago cubs": ("Chicago Cubs", "CHC", "mlb"),
    "chc": ("Chicago Cubs", "CHC", "mlb"),
    "white sox": ("Chicago White Sox", "CWS", "mlb"),
    "chicago white sox": ("Chicago White Sox", "CWS", "mlb"),
    "cws": ("Chicago White Sox", "CWS", "mlb"),
    "yankees": ("New York Yankees", "NYY", "mlb"),
    "new york yankees": ("New York Yankees", "NYY", "mlb"),
    "nyy": ("New York Yankees", "NYY", "mlb"),
    "mets": ("New York Mets", "NYM", "mlb"),
    "new york mets": ("New York Mets", "NYM", "mlb"),
    "nym": ("New York Mets", "NYM", "mlb"),
    "red sox": ("Boston Red Sox", "BOS", "mlb"),
    "boston red sox": ("Boston Red Sox", "BOS", "mlb"),
    "dodgers": ("Los Angeles Dodgers", "LAD", "mlb"),
    "los angeles dodgers": ("Los Angeles Dodgers", "LAD", "mlb"),
    "lad": ("Los Angeles Dodgers", "LAD", "mlb"),
    "giants": ("San Francisco Giants", "SF", "mlb"),
    "san francisco giants": ("San Francisco Giants", "SF", "mlb"),
    "astros": ("Houston Astros", "HOU", "mlb"),
    "houston astros": ("Houston Astros", "HOU", "mlb"),
    "braves": ("Atlanta Braves", "ATL", "mlb"),
    "atlanta braves": ("Atlanta Braves", "ATL", "mlb"),
    "phillies": ("Philadelphia Phillies", "PHI", "mlb"),
    "philadelphia phillies": ("Philadelphia Phillies", "PHI", "mlb"),
    "cardinals": ("St. Louis Cardinals", "STL", "mlb"),
    "st. louis cardinals": ("St. Louis Cardinals", "STL", "mlb"),
    "stl": ("St. Louis Cardinals", "STL", "mlb"),
    "brewers": ("Milwaukee Brewers", "MIL", "mlb"),
    "milwaukee brewers": ("Milwaukee Brewers", "MIL", "mlb"),
    "reds": ("Cincinnati Reds", "CIN", "mlb"),
    "cincinnati reds": ("Cincinnati Reds", "CIN", "mlb"),
    "pirates": ("Pittsburgh Pirates", "PIT", "mlb"),
    "pittsburgh pirates": ("Pittsburgh Pirates", "PIT", "mlb"),
    "tigers": ("Detroit Tigers", "DET", "mlb"),
    "detroit tigers": ("Detroit Tigers", "DET", "mlb"),
    "indians": ("Cleveland Guardians", "CLE", "mlb"),
    "guardians": ("Cleveland Guardians", "CLE", "mlb"),
    "cleveland guardians": ("Cleveland Guardians", "CLE", "mlb"),
    "twins": ("Minnesota Twins", "MIN", "mlb"),
    "minnesota twins": ("Minnesota Twins", "MIN", "mlb"),
    "royals": ("Kansas City Royals", "KC", "mlb"),
    "kansas city royals": ("Kansas City Royals", "KC", "mlb"),
    "athletics": ("Oakland Athletics", "OAK", "mlb"),
    "a's": ("Oakland Athletics", "OAK", "mlb"),
    "angels": ("Los Angeles Angels", "LAA", "mlb"),
    "los angeles angels": ("Los Angeles Angels", "LAA", "mlb"),
    "mariners": ("Seattle Mariners", "SEA", "mlb"),
    "seattle mariners": ("Seattle Mariners", "SEA", "mlb"),
    "rangers": ("Texas Rangers", "TEX", "mlb"),
    "texas rangers": ("Texas Rangers", "TEX", "mlb"),
    "padres": ("San Diego Padres", "SD", "mlb"),
    "san diego padres": ("San Diego Padres", "SD", "mlb"),
    "rockies": ("Colorado Rockies", "COL", "mlb"),
    "colorado rockies": ("Colorado Rockies", "COL", "mlb"),
    "diamondbacks": ("Arizona Diamondbacks", "ARI", "mlb"),
    "dbacks": ("Arizona Diamondbacks", "ARI", "mlb"),
    "arizona diamondbacks": ("Arizona Diamondbacks", "ARI", "mlb"),
    "marlins": ("Miami Marlins", "MIA", "mlb"),
    "miami marlins": ("Miami Marlins", "MIA", "mlb"),
    "nationals": ("Washington Nationals", "WSH", "mlb"),
    "washington nationals": ("Washington Nationals", "WSH", "mlb"),
    "blue jays": ("Toronto Blue Jays", "TOR", "mlb"),
    "toronto blue jays": ("Toronto Blue Jays", "TOR", "mlb"),
    "orioles": ("Baltimore Orioles", "BAL", "mlb"),
    "baltimore orioles": ("Baltimore Orioles", "BAL", "mlb"),
    "rays": ("Tampa Bay Rays", "TB", "mlb"),
    "tampa bay rays": ("Tampa Bay Rays", "TB", "mlb"),
    # ── NHL ──────────────────────────────────────────────────────────────────
    "blackhawks": ("Chicago Blackhawks", "CHI", "nhl"),
    "chicago blackhawks": ("Chicago Blackhawks", "CHI", "nhl"),
    "bruins": ("Boston Bruins", "BOS", "nhl"),
    "boston bruins": ("Boston Bruins", "BOS", "nhl"),
    "rangers": ("New York Rangers", "NYR", "nhl"),
    "new york rangers": ("New York Rangers", "NYR", "nhl"),
    "nyr": ("New York Rangers", "NYR", "nhl"),
    "penguins": ("Pittsburgh Penguins", "PIT", "nhl"),
    "pittsburgh penguins": ("Pittsburgh Penguins", "PIT", "nhl"),
    "capitals": ("Washington Capitals", "WSH", "nhl"),
    "caps": ("Washington Capitals", "WSH", "nhl"),
    "washington capitals": ("Washington Capitals", "WSH", "nhl"),
    "lightning": ("Tampa Bay Lightning", "TBL", "nhl"),
    "tampa bay lightning": ("Tampa Bay Lightning", "TBL", "nhl"),
    "maple leafs": ("Toronto Maple Leafs", "TOR", "nhl"),
    "leafs": ("Toronto Maple Leafs", "TOR", "nhl"),
    "toronto maple leafs": ("Toronto Maple Leafs", "TOR", "nhl"),
    "canadiens": ("Montreal Canadiens", "MTL", "nhl"),
    "habs": ("Montreal Canadiens", "MTL", "nhl"),
    "montreal canadiens": ("Montreal Canadiens", "MTL", "nhl"),
    "red wings": ("Detroit Red Wings", "DET", "nhl"),
    "detroit red wings": ("Detroit Red Wings", "DET", "nhl"),
    "golden knights": ("Vegas Golden Knights", "VGK", "nhl"),
    "vegas golden knights": ("Vegas Golden Knights", "VGK", "nhl"),
    "vgk": ("Vegas Golden Knights", "VGK", "nhl"),
    "oilers": ("Edmonton Oilers", "EDM", "nhl"),
    "edmonton oilers": ("Edmonton Oilers", "EDM", "nhl"),
    "flames": ("Calgary Flames", "CGY", "nhl"),
    "calgary flames": ("Calgary Flames", "CGY", "nhl"),
    "canucks": ("Vancouver Canucks", "VAN", "nhl"),
    "vancouver canucks": ("Vancouver Canucks", "VAN", "nhl"),
    "avalanche": ("Colorado Avalanche", "COL", "nhl"),
    "avs": ("Colorado Avalanche", "COL", "nhl"),
    "colorado avalanche": ("Colorado Avalanche", "COL", "nhl"),
    "stars": ("Dallas Stars", "DAL", "nhl"),
    "dallas stars": ("Dallas Stars", "DAL", "nhl"),
    "wild": ("Minnesota Wild", "MIN", "nhl"),
    "minnesota wild": ("Minnesota Wild", "MIN", "nhl"),
    "blues": ("St. Louis Blues", "STL", "nhl"),
    "st. louis blues": ("St. Louis Blues", "STL", "nhl"),
    "predators": ("Nashville Predators", "NSH", "nhl"),
    "preds": ("Nashville Predators", "NSH", "nhl"),
    "nashville predators": ("Nashville Predators", "NSH", "nhl"),
    "hurricanes": ("Carolina Hurricanes", "CAR", "nhl"),
    "carolina hurricanes": ("Carolina Hurricanes", "CAR", "nhl"),
    "flyers": ("Philadelphia Flyers", "PHI", "nhl"),
    "philadelphia flyers": ("Philadelphia Flyers", "PHI", "nhl"),
    "sabres": ("Buffalo Sabres", "BUF", "nhl"),
    "buffalo sabres": ("Buffalo Sabres", "BUF", "nhl"),
    "senators": ("Ottawa Senators", "OTT", "nhl"),
    "ottawa senators": ("Ottawa Senators", "OTT", "nhl"),
    "islanders": ("New York Islanders", "NYI", "nhl"),
    "new york islanders": ("New York Islanders", "NYI", "nhl"),
    "nyi": ("New York Islanders", "NYI", "nhl"),
    "jets": ("Winnipeg Jets", "WPG", "nhl"),
    "winnipeg jets": ("Winnipeg Jets", "WPG", "nhl"),
    "ducks": ("Anaheim Ducks", "ANA", "nhl"),
    "anaheim ducks": ("Anaheim Ducks", "ANA", "nhl"),
    "sharks": ("San Jose Sharks", "SJS", "nhl"),
    "san jose sharks": ("San Jose Sharks", "SJS", "nhl"),
    "kings": ("Los Angeles Kings", "LAK", "nhl"),
    "los angeles kings": ("Los Angeles Kings", "LAK", "nhl"),
    "lak": ("Los Angeles Kings", "LAK", "nhl"),
    "coyotes": ("Utah Hockey Club", "UTA", "nhl"),
    "utah hockey club": ("Utah Hockey Club", "UTA", "nhl"),
    "devils": ("New Jersey Devils", "NJD", "nhl"),
    "new jersey devils": ("New Jersey Devils", "NJD", "nhl"),
    "njd": ("New Jersey Devils", "NJD", "nhl"),
    "blue jackets": ("Columbus Blue Jackets", "CBJ", "nhl"),
    "columbus blue jackets": ("Columbus Blue Jackets", "CBJ", "nhl"),
    "cbj": ("Columbus Blue Jackets", "CBJ", "nhl"),
    "panthers": ("Florida Panthers", "FLA", "nhl"),
    "florida panthers": ("Florida Panthers", "FLA", "nhl"),
    "fla": ("Florida Panthers", "FLA", "nhl"),
    "kraken": ("Seattle Kraken", "SEA", "nhl"),
    "seattle kraken": ("Seattle Kraken", "SEA", "nhl"),
    # ── NCAAF (College Football) ──────────────────────────────────────────────
    # SEC
    "alabama crimson tide": ("Alabama Crimson Tide", "ALA", "ncaaf"),
    "crimson tide": ("Alabama Crimson Tide", "ALA", "ncaaf"),
    "alabama": ("Alabama Crimson Tide", "ALA", "ncaaf"),
    "georgia bulldogs": ("Georgia Bulldogs", "UGA", "ncaaf"),
    "uga": ("Georgia Bulldogs", "UGA", "ncaaf"),
    "georgia": ("Georgia Bulldogs", "UGA", "ncaaf"),
    "lsu tigers": ("LSU Tigers", "LSU", "ncaaf"),
    "lsu": ("LSU Tigers", "LSU", "ncaaf"),
    "tennessee volunteers": ("Tennessee Volunteers", "TENN", "ncaaf"),
    "volunteers": ("Tennessee Volunteers", "TENN", "ncaaf"),
    "vols": ("Tennessee Volunteers", "TENN", "ncaaf"),
    "tennessee": ("Tennessee Volunteers", "TENN", "ncaaf"),
    "auburn tigers": ("Auburn Tigers", "AUB", "ncaaf"),
    "auburn": ("Auburn Tigers", "AUB", "ncaaf"),
    "aub": ("Auburn Tigers", "AUB", "ncaaf"),
    "texas a&m aggies": ("Texas A&M Aggies", "TAMU", "ncaaf"),
    "texas a&m": ("Texas A&M Aggies", "TAMU", "ncaaf"),
    "tamu": ("Texas A&M Aggies", "TAMU", "ncaaf"),
    "aggies": ("Texas A&M Aggies", "TAMU", "ncaaf"),
    "florida gators": ("Florida Gators", "UF", "ncaaf"),
    "gators": ("Florida Gators", "UF", "ncaaf"),
    "florida": ("Florida Gators", "UF", "ncaaf"),
    "uf": ("Florida Gators", "UF", "ncaaf"),
    "kentucky wildcats": ("Kentucky Wildcats", "UK", "ncaab"),
    "uk": ("Kentucky Wildcats", "UK", "ncaab"),
    "kentucky": ("Kentucky Wildcats", "UK", "ncaab"),
    "ole miss rebels": ("Ole Miss Rebels", "MISS", "ncaaf"),
    "ole miss": ("Ole Miss Rebels", "MISS", "ncaaf"),
    "mississippi state bulldogs": ("Mississippi State Bulldogs", "MSST", "ncaaf"),
    "mississippi state": ("Mississippi State Bulldogs", "MSST", "ncaaf"),
    "msst": ("Mississippi State Bulldogs", "MSST", "ncaaf"),
    "missouri tigers": ("Missouri Tigers", "MIZ", "ncaaf"),
    "missouri": ("Missouri Tigers", "MIZ", "ncaaf"),
    "mizzou": ("Missouri Tigers", "MIZ", "ncaaf"),
    "miz": ("Missouri Tigers", "MIZ", "ncaaf"),
    "arkansas razorbacks": ("Arkansas Razorbacks", "ARK", "ncaaf"),
    "razorbacks": ("Arkansas Razorbacks", "ARK", "ncaaf"),
    "hogs": ("Arkansas Razorbacks", "ARK", "ncaaf"),
    "arkansas": ("Arkansas Razorbacks", "ARK", "ncaaf"),
    "ark": ("Arkansas Razorbacks", "ARK", "ncaaf"),
    "south carolina gamecocks": ("South Carolina Gamecocks", "SC", "ncaaf"),
    "gamecocks": ("South Carolina Gamecocks", "SC", "ncaaf"),
    "south carolina": ("South Carolina Gamecocks", "SC", "ncaaf"),
    "vanderbilt commodores": ("Vanderbilt Commodores", "VAN", "ncaaf"),
    "commodores": ("Vanderbilt Commodores", "VAN", "ncaaf"),
    "vanderbilt": ("Vanderbilt Commodores", "VAN", "ncaaf"),
    "vandy": ("Vanderbilt Commodores", "VAN", "ncaaf"),
    # Big Ten
    "ohio state buckeyes": ("Ohio State Buckeyes", "OSU", "ncaaf"),
    "buckeyes": ("Ohio State Buckeyes", "OSU", "ncaaf"),
    "ohio state": ("Ohio State Buckeyes", "OSU", "ncaaf"),
    "michigan wolverines": ("Michigan Wolverines", "MICH", "ncaaf"),
    "wolverines": ("Michigan Wolverines", "MICH", "ncaaf"),
    "michigan": ("Michigan Wolverines", "MICH", "ncaaf"),
    "penn state nittany lions": ("Penn State Nittany Lions", "PSU", "ncaaf"),
    "nittany lions": ("Penn State Nittany Lions", "PSU", "ncaaf"),
    "penn state": ("Penn State Nittany Lions", "PSU", "ncaaf"),
    "psu": ("Penn State Nittany Lions", "PSU", "ncaaf"),
    "michigan state spartans": ("Michigan State Spartans", "MSU", "ncaaf"),
    "spartans": ("Michigan State Spartans", "MSU", "ncaaf"),
    "michigan state": ("Michigan State Spartans", "MSU", "ncaaf"),
    "msu": ("Michigan State Spartans", "MSU", "ncaaf"),
    "wisconsin badgers": ("Wisconsin Badgers", "WIS", "ncaaf"),
    "badgers": ("Wisconsin Badgers", "WIS", "ncaaf"),
    "wisconsin": ("Wisconsin Badgers", "WIS", "ncaaf"),
    "iowa hawkeyes": ("Iowa Hawkeyes", "IOWA", "ncaaf"),
    "hawkeyes": ("Iowa Hawkeyes", "IOWA", "ncaaf"),
    "iowa": ("Iowa Hawkeyes", "IOWA", "ncaaf"),
    "indiana hoosiers": ("Indiana Hoosiers", "IU", "ncaab"),
    "hoosiers": ("Indiana Hoosiers", "IU", "ncaab"),
    "indiana hoosiers football": ("Indiana Hoosiers", "IU", "ncaaf"),
    "iu": ("Indiana Hoosiers", "IU", "ncaab"),
    "purdue boilermakers": ("Purdue Boilermakers", "PUR", "ncaab"),
    "boilermakers": ("Purdue Boilermakers", "PUR", "ncaab"),
    "boilers": ("Purdue Boilermakers", "PUR", "ncaab"),
    "purdue": ("Purdue Boilermakers", "PUR", "ncaab"),
    "pur": ("Purdue Boilermakers", "PUR", "ncaab"),
    "minnesota golden gophers": ("Minnesota Golden Gophers", "MINN", "ncaaf"),
    "golden gophers": ("Minnesota Golden Gophers", "MINN", "ncaaf"),
    "gophers": ("Minnesota Golden Gophers", "MINN", "ncaaf"),
    "nebraska cornhuskers": ("Nebraska Cornhuskers", "NEB", "ncaaf"),
    "cornhuskers": ("Nebraska Cornhuskers", "NEB", "ncaaf"),
    "huskers": ("Nebraska Cornhuskers", "NEB", "ncaaf"),
    "nebraska": ("Nebraska Cornhuskers", "NEB", "ncaaf"),
    "iowa state cyclones": ("Iowa State Cyclones", "ISU", "ncaaf"),
    "cyclones": ("Iowa State Cyclones", "ISU", "ncaaf"),
    "iowa state": ("Iowa State Cyclones", "ISU", "ncaaf"),
    "isu": ("Iowa State Cyclones", "ISU", "ncaaf"),
    "illinois fighting illini": ("Illinois Fighting Illini", "ILL", "ncaaf"),
    "fighting illini": ("Illinois Fighting Illini", "ILL", "ncaaf"),
    "illinois": ("Illinois Fighting Illini", "ILL", "ncaaf"),
    "northwestern wildcats": ("Northwestern Wildcats", "NW", "ncaaf"),
    "northwestern": ("Northwestern Wildcats", "NW", "ncaaf"),
    "maryland terrapins": ("Maryland Terrapins", "MD", "ncaaf"),
    "terrapins": ("Maryland Terrapins", "MD", "ncaaf"),
    "terps": ("Maryland Terrapins", "MD", "ncaaf"),
    "maryland": ("Maryland Terrapins", "MD", "ncaaf"),
    "rutgers scarlet knights": ("Rutgers Scarlet Knights", "RUTG", "ncaaf"),
    "scarlet knights": ("Rutgers Scarlet Knights", "RUTG", "ncaaf"),
    "rutgers": ("Rutgers Scarlet Knights", "RUTG", "ncaaf"),
    # Big 12
    "texas longhorns": ("Texas Longhorns", "TEX", "ncaaf"),
    "longhorns": ("Texas Longhorns", "TEX", "ncaaf"),
    "texas": ("Texas Longhorns", "TEX", "ncaaf"),
    "oklahoma sooners": ("Oklahoma Sooners", "OU", "ncaaf"),
    "sooners": ("Oklahoma Sooners", "OU", "ncaaf"),
    "oklahoma": ("Oklahoma Sooners", "OU", "ncaaf"),
    "ou": ("Oklahoma Sooners", "OU", "ncaaf"),
    "baylor bears": ("Baylor Bears", "BAY", "ncaaf"),
    "baylor": ("Baylor Bears", "BAY", "ncaaf"),
    "tcu horned frogs": ("TCU Horned Frogs", "TCU", "ncaaf"),
    "horned frogs": ("TCU Horned Frogs", "TCU", "ncaaf"),
    "tcu": ("TCU Horned Frogs", "TCU", "ncaaf"),
    "kansas state wildcats": ("Kansas State Wildcats", "KSU", "ncaaf"),
    "kansas state": ("Kansas State Wildcats", "KSU", "ncaaf"),
    "ksu": ("Kansas State Wildcats", "KSU", "ncaaf"),
    "oklahoma state cowboys": ("Oklahoma State Cowboys", "OKST", "ncaaf"),
    "oklahoma state": ("Oklahoma State Cowboys", "OKST", "ncaaf"),
    "okst": ("Oklahoma State Cowboys", "OKST", "ncaaf"),
    "west virginia mountaineers": ("West Virginia Mountaineers", "WVU", "ncaaf"),
    "mountaineers": ("West Virginia Mountaineers", "WVU", "ncaaf"),
    "west virginia": ("West Virginia Mountaineers", "WVU", "ncaaf"),
    "wvu": ("West Virginia Mountaineers", "WVU", "ncaaf"),
    "cincinnati bearcats": ("Cincinnati Bearcats", "CIN", "ncaaf"),
    "bearcats": ("Cincinnati Bearcats", "CIN", "ncaaf"),
    "cincinnati": ("Cincinnati Bearcats", "CIN", "ncaaf"),
    "houston cougars": ("Houston Cougars", "HOU", "ncaab"),
    "uc": ("Cincinnati Bearcats", "CIN", "ncaaf"),
    # ACC
    "clemson tigers": ("Clemson Tigers", "CLEM", "ncaaf"),
    "clemson": ("Clemson Tigers", "CLEM", "ncaaf"),
    "clem": ("Clemson Tigers", "CLEM", "ncaaf"),
    "florida state seminoles": ("Florida State Seminoles", "FSU", "ncaaf"),
    "seminoles": ("Florida State Seminoles", "FSU", "ncaaf"),
    "noles": ("Florida State Seminoles", "FSU", "ncaaf"),
    "florida state": ("Florida State Seminoles", "FSU", "ncaaf"),
    "fsu": ("Florida State Seminoles", "FSU", "ncaaf"),
    "north carolina tar heels": ("North Carolina Tar Heels", "UNC", "ncaab"),
    "tar heels": ("North Carolina Tar Heels", "UNC", "ncaab"),
    "north carolina": ("North Carolina Tar Heels", "UNC", "ncaab"),
    "unc": ("North Carolina Tar Heels", "UNC", "ncaab"),
    "duke blue devils": ("Duke Blue Devils", "DUKE", "ncaab"),
    "blue devils": ("Duke Blue Devils", "DUKE", "ncaab"),
    "duke": ("Duke Blue Devils", "DUKE", "ncaab"),
    "miami hurricanes": ("Miami Hurricanes", "MIA", "ncaaf"),
    "virginia cavaliers": ("Virginia Cavaliers", "UVA", "ncaab"),
    "cavaliers": ("Virginia Cavaliers", "UVA", "ncaab"),
    "uva": ("Virginia Cavaliers", "UVA", "ncaab"),
    "virginia tech hokies": ("Virginia Tech Hokies", "VT", "ncaaf"),
    "hokies": ("Virginia Tech Hokies", "VT", "ncaaf"),
    "virginia tech": ("Virginia Tech Hokies", "VT", "ncaaf"),
    "vt": ("Virginia Tech Hokies", "VT", "ncaaf"),
    "nc state wolfpack": ("NC State Wolfpack", "NCST", "ncaaf"),
    "wolfpack": ("NC State Wolfpack", "NCST", "ncaaf"),
    "nc state": ("NC State Wolfpack", "NCST", "ncaaf"),
    "georgia tech yellow jackets": ("Georgia Tech Yellow Jackets", "GT", "ncaaf"),
    "yellow jackets": ("Georgia Tech Yellow Jackets", "GT", "ncaaf"),
    "georgia tech": ("Georgia Tech Yellow Jackets", "GT", "ncaaf"),
    "gt": ("Georgia Tech Yellow Jackets", "GT", "ncaaf"),
    "louisville cardinals": ("Louisville Cardinals", "LOU", "ncaab"),
    "louisville": ("Louisville Cardinals", "LOU", "ncaab"),
    "lou": ("Louisville Cardinals", "LOU", "ncaab"),
    "pittsburgh panthers": ("Pittsburgh Panthers", "PITT", "ncaaf"),
    "pitt": ("Pittsburgh Panthers", "PITT", "ncaaf"),
    "pittsburgh": ("Pittsburgh Panthers", "PITT", "ncaaf"),
    "boston college eagles": ("Boston College Eagles", "BC", "ncaaf"),
    "boston college": ("Boston College Eagles", "BC", "ncaaf"),
    "wake forest demon deacons": ("Wake Forest Demon Deacons", "WF", "ncaaf"),
    "demon deacons": ("Wake Forest Demon Deacons", "WF", "ncaaf"),
    "wake forest": ("Wake Forest Demon Deacons", "WF", "ncaaf"),
    "syracuse orange": ("Syracuse Orange", "SYR", "ncaab"),
    "syracuse": ("Syracuse Orange", "SYR", "ncaab"),
    "syr": ("Syracuse Orange", "SYR", "ncaab"),
    # Pac-12 / now Big Ten/Big 12
    "usc trojans": ("USC Trojans", "USC", "ncaaf"),
    "trojans": ("USC Trojans", "USC", "ncaaf"),
    "usc": ("USC Trojans", "USC", "ncaaf"),
    "notre dame fighting irish": ("Notre Dame Fighting Irish", "ND", "ncaaf"),
    "fighting irish": ("Notre Dame Fighting Irish", "ND", "ncaaf"),
    "notre dame": ("Notre Dame Fighting Irish", "ND", "ncaaf"),
    "nd": ("Notre Dame Fighting Irish", "ND", "ncaaf"),
    "oregon ducks": ("Oregon Ducks", "ORE", "ncaaf"),
    "oregon": ("Oregon Ducks", "ORE", "ncaaf"),
    "ore": ("Oregon Ducks", "ORE", "ncaaf"),
    "washington huskies": ("Washington Huskies", "WASH", "ncaaf"),
    "huskies": ("Washington Huskies", "WASH", "ncaaf"),
    "washington": ("Washington Huskies", "WASH", "ncaaf"),
    "utah utes": ("Utah Utes", "UTAH", "ncaaf"),
    "utes": ("Utah Utes", "UTAH", "ncaaf"),
    "utah": ("Utah Utes", "UTAH", "ncaaf"),
    "colorado buffaloes": ("Colorado Buffaloes", "COLO", "ncaaf"),
    "buffaloes": ("Colorado Buffaloes", "COLO", "ncaaf"),
    "buffs": ("Colorado Buffaloes", "COLO", "ncaaf"),
    "colorado": ("Colorado Buffaloes", "COLO", "ncaaf"),
    "arizona state sun devils": ("Arizona State Sun Devils", "ASU", "ncaaf"),
    "sun devils": ("Arizona State Sun Devils", "ASU", "ncaaf"),
    "arizona state": ("Arizona State Sun Devils", "ASU", "ncaaf"),
    "asu": ("Arizona State Sun Devils", "ASU", "ncaaf"),
    "oregon state beavers": ("Oregon State Beavers", "ORST", "ncaaf"),
    "beavers": ("Oregon State Beavers", "ORST", "ncaaf"),
    "oregon state": ("Oregon State Beavers", "ORST", "ncaaf"),
    "orst": ("Oregon State Beavers", "ORST", "ncaaf"),
    "washington state cougars": ("Washington State Cougars", "WSU", "ncaaf"),
    "wsu": ("Washington State Cougars", "WSU", "ncaaf"),
    "washington state": ("Washington State Cougars", "WSU", "ncaaf"),
    "ucla bruins": ("UCLA Bruins", "UCLA", "ncaab"),
    "ucla": ("UCLA Bruins", "UCLA", "ncaab"),
    "arizona wildcats": ("Arizona Wildcats", "ARIZ", "ncaab"),
    "arizona": ("Arizona Wildcats", "ARIZ", "ncaab"),
    "ariz": ("Arizona Wildcats", "ARIZ", "ncaab"),
    "byu cougars": ("BYU Cougars", "BYU", "ncaaf"),
    "byu": ("BYU Cougars", "BYU", "ncaaf"),
    # Independent / other major programs
    "kansas jayhawks": ("Kansas Jayhawks", "KAN", "ncaab"),
    "jayhawks": ("Kansas Jayhawks", "KAN", "ncaab"),
    "kansas": ("Kansas Jayhawks", "KAN", "ncaab"),
    "ku": ("Kansas Jayhawks", "KAN", "ncaab"),
    "kan": ("Kansas Jayhawks", "KAN", "ncaab"),
    "gonzaga bulldogs": ("Gonzaga Bulldogs", "GONZ", "ncaab"),
    "gonzaga": ("Gonzaga Bulldogs", "GONZ", "ncaab"),
    "gonz": ("Gonzaga Bulldogs", "GONZ", "ncaab"),
    "uconn huskies": ("UConn Huskies", "UCONN", "ncaab"),
    "uconn": ("UConn Huskies", "UCONN", "ncaab"),
    "connecticut": ("UConn Huskies", "UCONN", "ncaab"),
    "villanova wildcats": ("Villanova Wildcats", "NOVA", "ncaab"),
    "nova": ("Villanova Wildcats", "NOVA", "ncaab"),
    "villanova": ("Villanova Wildcats", "NOVA", "ncaab"),
    "marquette golden eagles": ("Marquette Golden Eagles", "MARQ", "ncaab"),
    "marquette": ("Marquette Golden Eagles", "MARQ", "ncaab"),
    "marq": ("Marquette Golden Eagles", "MARQ", "ncaab"),
    "xavier musketeers": ("Xavier Musketeers", "XAV", "ncaab"),
    "musketeers": ("Xavier Musketeers", "XAV", "ncaab"),
    "xavier": ("Xavier Musketeers", "XAV", "ncaab"),
    "xav": ("Xavier Musketeers", "XAV", "ncaab"),
    "creighton bluejays": ("Creighton Bluejays", "CREI", "ncaab"),
    "creighton": ("Creighton Bluejays", "CREI", "ncaab"),
    "crei": ("Creighton Bluejays", "CREI", "ncaab"),
    "san diego state aztecs": ("San Diego State Aztecs", "SDSU", "ncaab"),
    "aztecs": ("San Diego State Aztecs", "SDSU", "ncaab"),
    "san diego state": ("San Diego State Aztecs", "SDSU", "ncaab"),
    "sdsu": ("San Diego State Aztecs", "SDSU", "ncaab"),
    "memphis tigers": ("Memphis Tigers", "MEM", "ncaab"),
    "dayton flyers": ("Dayton Flyers", "DAY", "ncaab"),
    "dayton": ("Dayton Flyers", "DAY", "ncaab"),
    "wichita state shockers": ("Wichita State Shockers", "WICH", "ncaab"),
    "shockers": ("Wichita State Shockers", "WICH", "ncaab"),
    "wichita state": ("Wichita State Shockers", "WICH", "ncaab"),
    "st. john's red storm": ("St. John's Red Storm", "SJU", "ncaab"),
    "red storm": ("St. John's Red Storm", "SJU", "ncaab"),
    "st. john's": ("St. John's Red Storm", "SJU", "ncaab"),
    "sju": ("St. John's Red Storm", "SJU", "ncaab"),
    "butler bulldogs": ("Butler Bulldogs", "BUT", "ncaab"),
    "butler": ("Butler Bulldogs", "BUT", "ncaab"),
}

# League keyword → sport_league key (longer keys checked first to avoid "basketball" → nba
# swallowing "college basketball" → ncaab)
_LEAGUE_SIGNALS: dict[str, str] = {
    # College — must come before generic sport names so longer keys win
    "college football": "ncaaf",
    "college basketball": "ncaab",
    "college softball": "ncaas",
    "college baseball": "ncaabase",
    "college hoops": "ncaab",
    "ncaa football": "ncaaf",
    "ncaa basketball": "ncaab",
    "ncaa softball": "ncaas",
    "ncaa baseball": "ncaabase",
    "ncaa tournament": "ncaab",
    "march madness": "ncaab",
    "final four": "ncaab",
    "sweet sixteen": "ncaab",
    "sweet 16": "ncaab",
    "elite eight": "ncaab",
    "bowl game": "ncaaf",
    "big east": "ncaab",
    "cfb": "ncaaf",
    "cbb": "ncaab",
    "ncaaf": "ncaaf",
    "ncaab": "ncaab",
    "ncaas": "ncaas",
    # Pro leagues
    "nfl": "nfl",
    "nba": "nba",
    "mlb": "mlb",
    "nhl": "nhl",
    # Generic — lowest priority (checked last; "softball" always college, no pro equivalent)
    "football": "nfl",
    "basketball": "nba",
    "baseball": "mlb",
    "softball": "ncaas",
    "hockey": "nhl",
}


def _ext_sports(raw: str, lower: str) -> dict[str, Any] | None:
    """Extract team, sport, and abbreviation from a sports query."""
    team_name = ""
    abbrev = ""
    sport = ""

    # Try longest match first (multi-word names before single words)
    for key in sorted(_TEAM_LOOKUP, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", lower):
            team_name, abbrev, sport = _TEAM_LOOKUP[key]
            break

    # Infer sport from league keywords if not yet set.
    # Iterate longest signal first so "college basketball" beats "basketball".
    if not sport:
        for sig, league in sorted(_LEAGUE_SIGNALS.items(), key=lambda x: len(x[0]), reverse=True):
            if re.search(rf"\b{re.escape(sig)}\b", lower):
                sport = league
                break

    # Certain sport keywords always override the team-lookup default.
    # "softball" has no pro equivalent in this system, so it always wins.
    # Checked longest-pattern first to avoid "softball" stomping "college softball".
    _kw_overrides = [
        (r"\bcollege\s+softball\b", "ncaas"),
        (r"\bcollege\s+baseball\b", "ncaabase"),
        (r"\bsoftball\b", "ncaas"),
    ]
    for _pat, _ov in _kw_overrides:
        if re.search(_pat, lower):
            sport = _ov
            break
    else:
        # For college programs, allow an explicit sport keyword to switch
        # the sport (e.g. "Ohio State basketball" → ncaab, "OU baseball" → ncaabase).
        if sport in ("ncaaf", "ncaab", "ncaas", "ncaabase"):
            if re.search(r"\bbasketball\b", lower):
                sport = "ncaab"
            elif re.search(r"\bfootball\b", lower):
                sport = "ncaaf"
            elif re.search(r"\bbaseball\b", lower):
                sport = "ncaabase"

    return {"team": team_name, "abbrev": abbrev, "sport": sport}


# ─── Computer-layer helper data ────────────────────────────────────────────────

_CONTENT_TYPE_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:document|doc|report|letter|memo|essay|paper)\b"), "document"),
    (re.compile(r"\b(?:spreadsheet|sheet|workbook|table)\b"), "spreadsheet"),
    (re.compile(r"\b(?:presentation|slide|slideshow|deck)\b"), "presentation"),
    (re.compile(r"\b(?:code|script|program|function|class|module)\b"), "code"),
    (re.compile(r"\b(?:note|notes)\b"), "note"),
    (re.compile(r"\b(?:image|photo|picture|drawing|diagram)\b"), "image"),
]

_CODE_LANG_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bpython\b"), "python"),
    (re.compile(r"\bjavascript\b|\bjs\b"), "javascript"),
    (re.compile(r"\btypescript\b|\bts\b"), "typescript"),
    (re.compile(r"\brust\b"), "rust"),
    (re.compile(r"\bgolang\b|\bgo\s+(?:code|script|program)\b"), "go"),
    (re.compile(r"\bjava\b"), "java"),
    (re.compile(r"\bc\+\+\b|\bcpp\b"), "cpp"),
    (re.compile(r"\bc#\b|\bcsharp\b"), "csharp"),
    (re.compile(r"\bsql\b"), "sql"),
    (re.compile(r"\bbash\b|\bshell\s+script\b"), "bash"),
    (re.compile(r"\bhtml\b"), "html"),
    (re.compile(r"\bcss\b"), "css"),
    (re.compile(r"\bswift\b"), "swift"),
    (re.compile(r"\bkotlin\b"), "kotlin"),
]


def _detect_content_type(lower: str) -> str | None:
    for pat, ctype in _CONTENT_TYPE_SIGNALS:
        if pat.search(lower):
            return ctype
    return None


def _detect_code_language(lower: str) -> str | None:
    for pat, lang in _CODE_LANG_SIGNALS:
        if pat.search(lower):
            return lang
    return None


def _extract_content_name(lower: str) -> str | None:
    """Extract a named piece of content from quoted strings or 'called/named X'."""
    m = re.search(r'(?:called|named|titled)\s+"?([^"]{2,40}?)"?\s*(?:$|[,.])', lower)
    if m:
        return m.group(1).strip()
    m = re.search(r'"([^"]{2,60})"', lower)
    if m:
        return m.group(1).strip()
    return None


def _extract_topic(lower: str) -> str | None:
    """Extract a topic phrase following 'about', 'for', 'on', 'regarding'."""
    m = re.search(r"\b(?:about|for|on|regarding)\s+(.{3,60}?)(?:\s*$|[,.])", lower)
    if m:
        return m.group(1).strip()
    return None


# ─── Computer-layer extractors ─────────────────────────────────────────────────

def _ext_content(raw: str, lower: str) -> dict | None:
    """Content management: find, list, history, branch, revert, share."""
    action_map = [
        (r"\b(?:history|versions?|revisions?|changes|changelog)\b", "history"),
        (r"\b(?:branch|fork|duplicate|spin\s+off)\b", "branch"),
        (r"\b(?:revert|rollback|restore|go\s+back)\b", "revert"),
        (r"\b(?:share|export|publish|send|download)\b", "share"),
        (r"\b(?:delete|remove|trash)\b", "delete"),
        (r"\b(?:rename|move)\b", "rename"),
        (r"\b(?:recent|all\s+my|show\s+all|list\s+all|what\s+have\s+i)\b", "list"),
        (r"\b(?:find|search|locate|look\s+for|open|pull\s+up|bring\s+up)\b", "find"),
    ]
    action = None
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    if not action:
        return None
    return {
        "action": action,
        "name": _extract_content_name(lower),
        "type": _detect_content_type(lower),
    }


def _ext_document(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:delete|remove|trash)\b", "delete"),
        (r"\b(?:export|download|convert)\b", "export"),
        (r"\b(?:edit|update|revise|rewrite|modify|change|add\s+to)\b", "edit"),
        (r"\b(?:create|new|start|write|draft|make|generate|open)\b", "create"),
    ]
    action = "create"
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    fmt = None
    m = re.search(r"\b(?:as|to)\s+(pdf|docx|word|txt)\b", lower)
    if m:
        fmt = m.group(1)
    return {"action": action, "name": _extract_content_name(lower), "topic": _extract_topic(lower), "format": fmt}


def _ext_spreadsheet(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:delete|remove|trash)\b", "delete"),
        (r"\b(?:export|download|convert)\b", "export"),
        (r"\b(?:edit|update|modify|add\s+(?:a\s+)?(?:row|column))\b", "edit"),
        (r"\b(?:create|new|start|make|build|generate)\b", "create"),
    ]
    action = "create"
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    fmt = None
    m = re.search(r"\b(?:as|to)\s+(csv|xlsx|excel|pdf)\b", lower)
    if m:
        fmt = m.group(1)
    return {"action": action, "name": _extract_content_name(lower), "format": fmt}


def _ext_presentation(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:delete|remove|trash)\b", "delete"),
        (r"\b(?:export|download|convert)\b", "export"),
        (r"\b(?:edit|update|modify|revise|add\s+a\s+slide)\b", "edit"),
        (r"\b(?:create|new|start|make|build|generate|write)\b", "create"),
    ]
    action = "create"
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    slides = None
    m = re.search(r"(\d+)\s+slides?", lower)
    if m:
        slides = int(m.group(1))
    fmt = None
    mf = re.search(r"\b(?:as|to)\s+(pdf|pptx|powerpoint)\b", lower)
    if mf:
        fmt = mf.group(1)
    return {
        "action": action,
        "name": _extract_content_name(lower),
        "slides": slides,
        "topic": _extract_topic(lower),
        "format": fmt,
    }


def _ext_code(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:explain|describe|what\s+does|how\s+does|walk\s+me\s+through)\b", "explain"),
        (r"\b(?:debug|fix\s+the\s+bug|fix\s+(?:this|the)\s+(?:code|error))\b", "debug"),
        (r"\b(?:run|execute|launch)\b", "run"),
        (r"\b(?:review|audit|check|lint)\b", "review"),
        (r"\b(?:test|write\s+tests?|unit\s+test)\b", "test"),
        (r"\b(?:edit|update|refactor|modify|clean\s+up)\b", "edit"),
        (r"\b(?:write|create|generate|implement|scaffold|build)\b", "create"),
    ]
    action = None
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    if not action:
        return None
    return {
        "action": action,
        "language": _detect_code_language(lower),
        "name": _extract_content_name(lower),
        "topic": _extract_topic(lower),
    }


def _ext_terminal(raw: str, lower: str) -> dict | None:
    cmd = None
    m = re.search(r'(?:run|execute)\s+(?:the\s+)?(?:command\s+)?"?(.+?)"?\s*$', lower)
    if m:
        cmd = m.group(1).strip()
    return {"command": cmd}


def _ext_calendar(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:cancel|delete|remove)\b", "cancel"),
        (r"\b(?:edit|update|change|move|reschedule)\b", "edit"),
        (r"\b(?:schedule|book|add|create|set\s+up|new)\b", "create"),
        (r"\b(?:show|list|what(?:'s|\s+is)|upcoming|agenda|what\s+do\s+i\s+have)\b", "list"),
    ]
    action = "list"
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    date_hint = None
    if re.search(r"\btoday\b", lower):
        date_hint = "today"
    elif re.search(r"\btomorrow\b", lower):
        date_hint = "tomorrow"
    elif re.search(r"\bthis\s+week\b", lower):
        date_hint = "this_week"
    elif re.search(r"\bnext\s+week\b", lower):
        date_hint = "next_week"
    else:
        m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lower)
        if m:
            date_hint = m.group(1)
    title = _extract_content_name(lower)
    if not title:
        m = re.search(r"(?:meeting|event|appointment|call|lunch|dinner|interview)\s+(?:with\s+)?([\w\s]+?)(?:\s+(?:at|on|tomorrow|today)|$)", lower)
        if m:
            title = m.group(0).strip()
    return {"action": action, "date": date_hint, "title": title}


def _ext_email(raw: str, lower: str) -> dict | None:
    action_map = [
        (r"\b(?:reply|respond|answer)\s+(?:to\s+)?(?:the\s+)?(?:email|message)\b", "reply"),
        (r"\b(?:forward)\s+(?:the\s+|this\s+)?(?:email|message)\b", "forward"),
        (r"\b(?:search|find|look\s+for)\s+(?:for\s+)?(?:emails?|messages?)\b", "search"),
        (r"\bemails?\s+(?:about|from|with)\b", "search"),
        (r"\b(?:archive|delete|trash)\b", "archive"),
        (r"\b(?:check|read|show|open)\s+(?:my\s+)?(?:email|inbox|mail)\b", "read"),
        (r"\b(?:send|write|compose|draft)\s+(?:an?\s+)?email\b", "compose"),
    ]
    action = None
    for pat, act in action_map:
        if re.search(pat, lower):
            action = act
            break
    if not action:
        return None
    to = None
    m = re.search(r"(?:to|email)\s+([A-Z][a-z]+(?: [A-Z][a-z]+)?)", raw)
    if m:
        to = m.group(1).strip()
    return {"action": action, "to": to, "subject": _extract_content_name(lower)}


# ─── Extended extractors ──────────────────────────────────────────────────────

def _ext_weather_alert(raw: str, lower: str) -> dict[str, Any] | None:
    return {"location": _extract_location(raw, lower) or "__current__", "type": "alert"}


def _ext_location_nearby(raw: str, lower: str) -> dict[str, Any] | None:
    place_m = re.search(
        r"\b([a-z][a-z\s]{2,30}?)"
        r"(?:\s+(?:nearby|near\s+me|around|close\s+by|around\s+here|in\s+the\s+area))\b",
        lower,
    )
    if place_m:
        query = place_m.group(1).strip()
    else:
        q2 = re.sub(r"^(?:find|show\s+me|where(?:'s|\s+is)|are\s+there\s+any)\s+", "", lower)
        q2 = re.sub(
            r"\s+(?:nearby|near\s+me|around\s+(?:here|me)|close\s+by|in\s+the\s+area)\s*$",
            "", q2,
        ).strip()
        query = q2 if q2 else "places"
    return {"query": query, "location": _extract_location(raw, lower) or "__current__"}


def _ext_shopping_track(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:track(?:ing)?|watch(?:ing)?|alert\s+(?:me\s+)?(?:when|if))"
        r"\s+(?:the\s+)?(?:price\s+(?:of\s+)?)?(.+?)"
        r"(?:\s+(?:drops?|goes?\s+(?:down|below)|changes?))?$",
        lower,
    )
    query = m.group(1).strip() if m else raw.strip()
    if not query or len(query) < 2:
        return None
    return {"query": query, "category": _infer_shopping_category(lower)}


def _ext_shopping_wishlist(raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:show|view|see|my|list)\b", lower):
        if not re.search(r"\b(?:add|save|put)\b", lower):
            return {"action": "list"}
    m = re.search(
        r"(?:add|save|put)\s+(.+?)\s+(?:to|on)\s+(?:my\s+)?(?:wishlist|wish\s+list)", lower
    )
    if not m:
        m = re.search(r"(?:wishlist|wish\s+list)\s+(.+)$", lower)
    query = m.group(1).strip() if m else raw.strip()
    return {"action": "add", "query": query}


def _ext_news_saved(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {"kind": "saved"}


def _ext_finance_portfolio(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_finance_watchlist(raw: str, lower: str) -> dict[str, Any] | None:
    action = "list"
    if re.search(r"\b(?:add|track|watch|follow)\b", lower):
        action = "add"
    elif re.search(r"\b(?:remove|unwatch|unfollow|drop)\b", lower):
        action = "remove"
    m = re.search(r"\b([A-Z]{1,5})\b", raw)
    symbol = m.group(1) if m else None
    return {"action": action, "symbol": symbol}


def _ext_banking_transfer(raw: str, lower: str) -> dict[str, Any] | None:
    mm = _MONEY_RE.search(lower)
    if not mm:
        return None
    try:
        amount = float((mm.group(1) or mm.group(2) or "0").replace(",", ""))
    except ValueError:
        amount = 0.0
    m = re.search(r"(?:to|send\s+to|transfer\s+to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    return {"amount": amount, "to": m.group(1).strip() if m else None}


def _ext_banking_pay(raw: str, lower: str) -> dict[str, Any] | None:
    mm = _MONEY_RE.search(lower)
    amount = None
    if mm:
        try:
            amount = float((mm.group(1) or mm.group(2) or "0").replace(",", ""))
        except ValueError:
            pass
    m = re.search(
        r"(?:pay|payment\s+(?:for|to))\s+(?:\$[\d\.]+\s+)?(?:for\s+)?(.+?)(?:\s+\$[\d\.]+)?$",
        lower,
    )
    return {"amount": amount, "payee": m.group(1).strip() if m else None}


def _ext_note_search(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:search|find|look\s+for)\s+(?:(?:in|through|my)\s+)?notes?\s+"
        r"(?:about\s+|for\s+|with\s+)?(.+)$",
        lower,
    )
    if not m:
        m = re.search(r"notes?\s+(?:about|on|with|containing)\s+(.+)$", lower)
    query = m.group(1).strip() if m else ""
    return {"query": query} if query else None


def _ext_note_delete(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:delete|remove|trash)\s+(?:(?:the|my|that)\s+)?(?:note\s+)?"
        r"(?:about\s+|called\s+|titled\s+)?(.+?)(?:\s+note)?$",
        lower,
    )
    if not m:
        return None
    selector = m.group(1).strip()
    return {"selector": selector} if selector and len(selector) >= 2 else None


def _ext_note_pin(_raw: str, lower: str) -> dict[str, Any] | None:
    action = "unpin" if re.search(r"\bunpin\b", lower) else "pin"
    m = re.search(
        r"(?:pin|unpin)\s+(?:(?:the|my)\s+)?(?:note\s+)?(?:about\s+)?(.+?)(?:\s+note)?$", lower
    )
    return {"action": action, "name": m.group(1).strip() if m else ""}


def _ext_reminder_delete(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:delete|remove|cancel|clear)\s+(?:(?:the|my|that)\s+)?(?:reminder\s+)?"
        r"(?:for\s+|about\s+)?(.+?)(?:\s+reminder)?$",
        lower,
    )
    if not m:
        return None
    selector = m.group(1).strip()
    return {"selector": selector} if selector and len(selector) >= 2 else None


def _ext_reminder_snooze(raw: str, lower: str) -> dict[str, Any] | None:
    delay_ms = _parse_relative_delay_ms(lower) or (10 * 60_000)
    m = re.search(
        r"(?:snooze|defer|postpone)\s+(?:(?:the|my)\s+)?(?:reminder\s+)?"
        r"(?:for\s+|about\s+)?(.+?)(?:\s+(?:for|by|until)|\s+\d+|$)",
        lower,
    )
    return {"selector": m.group(1).strip() if m else "", "delayMs": delay_ms}


def _ext_social_dm(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:dm|direct\s+message|message|text)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"
        r"\s*(?:[:\-]\s*(.+))?$",
        raw,
    )
    if not m:
        return None
    recipient = m.group(1).strip()
    return {"to": recipient, "text": (m.group(2) or "").strip()} if recipient else None


def _ext_social_profile(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_terminal_history(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_terminal_kill(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:kill|stop|terminate|end|force\s+quit)\s+(?:the\s+)?(?:process\s+)?(.+?)"
        r"(?:\s+process)?$",
        lower,
    )
    if not m:
        return None
    process = m.group(1).strip()
    return {"process": process} if process else None


def _ext_contacts_create(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:add|save|create)\s+(?:a\s+)?(?:new\s+)?contact\s+(?:for\s+)?(.+)$", lower)
    name = m.group(1).strip() if m else ""
    phone_m = re.search(r"\b(\d[\d\s\-\(\)\.]{6,14}\d)\b", raw)
    email_m = re.search(r"\b([\w.+\-]+@[\w\-]+\.\w+)\b", raw)
    return {
        "name": name,
        "phone": phone_m.group(1) if phone_m else None,
        "email": email_m.group(1) if email_m else None,
    }


def _ext_contacts_call(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.match(r"^(?:call|dial|phone|ring)\s+(.+)$", raw, re.IGNORECASE)
    if not m:
        return None
    return {"name": m.group(1).strip()}


def _ext_contacts_message(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.match(r"^(?:text|sms|message)\s+(.+?)(?:\s+(?:that|saying|:)\s+(.+))?$", raw,
                 re.IGNORECASE)
    if not m:
        return None
    return {"to": m.group(1).strip(), "body": (m.group(2) or "").strip()}


def _ext_calendar_rsvp(raw: str, lower: str) -> dict[str, Any] | None:
    response = None
    if re.search(r"\b(?:accept|yes|going|attending|will\s+attend|i'll\s+be\s+there)\b", lower):
        response = "accept"
    elif re.search(r"\b(?:decline|no|not\s+going|won't\s+attend|can't\s+make\s+it)\b", lower):
        response = "decline"
    elif re.search(r"\b(?:maybe|tentative|might|possibly)\b", lower):
        response = "tentative"
    m = re.search(
        r"(?:rsvp|respond)\s+(?:to\s+)?(?:the\s+)?(.+?)"
        r"(?:\s+(?:invite|invitation|event|meeting))?$",
        lower,
    )
    return {"response": response, "event": m.group(1).strip() if m else ""}


# ─── Extractor registry ───────────────────────────────────────────────────────

_EXTRACTORS: dict[str, ExtractorFn] = {
    "weather":              _ext_weather,
    "shopping":             _ext_shopping,
    "location_status":      _ext_location_status,
    "news":                 _ext_news,
    "finance_stock":        _ext_finance_stock,
    "finance_crypto":       _ext_finance_crypto,
    "banking_balance":      _ext_banking_balance,
    "banking_transactions": _ext_banking_transactions,
    "social_feed":          _ext_social_feed,
    "social_post":          _ext_social_post,
    "expense_add":          _ext_expense_add,
    "expense_list":         _ext_expense_list,
    "note_create":          _ext_note_create,
    "note_list":            _ext_note_list,
    "reminder_set":         _ext_reminder_set,
    "reminder_list":        _ext_reminder_list,
    "task_create":          _ext_task_create,
    "task_complete":        _ext_task_complete,
    "task_delete":          _ext_task_delete,
    "task_clear":           _ext_task_clear,
    "task_list":            _ext_task_list,
    "contacts":             _ext_contacts,
    "web_search":           _ext_web_search,
    "web_fetch":            _ext_web_fetch,
    "web_summarize":        _ext_web_summarize,
    "timer_start":          _ext_timer_start,
    "calc":                 _ext_calc,
    "unit_convert":         _ext_unit_convert,
    "sports":               _ext_sports,
    # Computer layer
    "content":              _ext_content,
    "document":             _ext_document,
    "spreadsheet":          _ext_spreadsheet,
    "presentation":         _ext_presentation,
    "code":                 _ext_code,
    "terminal":             _ext_terminal,
    "calendar":             _ext_calendar,
    "email":                _ext_email,
    # Extended extractors
    "weather_alert":        _ext_weather_alert,
    "location_nearby":      _ext_location_nearby,
    "shopping_track":       _ext_shopping_track,
    "shopping_wishlist":    _ext_shopping_wishlist,
    "news_saved":           _ext_news_saved,
    "finance_portfolio":    _ext_finance_portfolio,
    "finance_watchlist":    _ext_finance_watchlist,
    "banking_transfer":     _ext_banking_transfer,
    "banking_pay":          _ext_banking_pay,
    "note_search":          _ext_note_search,
    "note_delete":          _ext_note_delete,
    "note_pin":             _ext_note_pin,
    "reminder_delete":      _ext_reminder_delete,
    "reminder_snooze":      _ext_reminder_snooze,
    "social_dm":            _ext_social_dm,
    "social_profile":       _ext_social_profile,
    "terminal_history":     _ext_terminal_history,
    "terminal_kill":        _ext_terminal_kill,
    "contacts_create":      _ext_contacts_create,
    "contacts_call":        _ext_contacts_call,
    "contacts_message":     _ext_contacts_message,
    "calendar_rsvp":        _ext_calendar_rsvp,
}


def extract_slots_for_op(op: str, raw: str, lower: str) -> "dict[str, Any]":
    """Run the local slot extractor for *op* and return its result, or {} if none exists.

    Intent.op (e.g. "weather_forecast") and Intent.extractor (e.g. "weather") can differ,
    so we resolve op → extractor key via TAXONOMY before looking up in _EXTRACTORS.
    """
    extractor_key = op  # fallback: try op name directly
    for intent in TAXONOMY.values():
        if intent.op == op:
            extractor_key = intent.extractor
            break
    fn = _EXTRACTORS.get(extractor_key)
    if fn is None:
        return {}
    result = fn(raw, lower)
    return result if isinstance(result, dict) else {}


# ─── Intent taxonomy ──────────────────────────────────────────────────────────
# Ordered by priority.  classify() tries each intent in this order.
# If an extractor returns None the next intent is tried.

TAXONOMY: dict[str, Intent] = {

    # ── Weather ───────────────────────────────────────────────────────────
    "weather.forecast": Intent(
        id="weather.forecast",
        op="weather_forecast",
        domain="weather",
        description="Get current or forecast weather for a location",
        signals=["weather", "forecast", "temperature", "rain", "raining", "snow", "snowing",
                 "wind", "humidity", "uv index", "pollen", "storm", "stormy", "cloudy", "sunny",
                 "overcast", "drizzle", "hail", "tornado", "hurricane", "freeze", "freezing",
                 "hot outside", "cold outside", "warm outside"],
        extractor="weather",
        examples=["what's the weather", "will it rain tomorrow",
                  "weather in Denver this week", "is it cold outside"],
        slots={"window": "now|tonight|tomorrow|morning|afternoon|evening|tomorrow-morning|tomorrow-afternoon|weekend|Nday (e.g. 3day)|weekday name (e.g. wednesday)", "location": "city name, or empty for current location"},
    ),

    # ── Shopping ──────────────────────────────────────────────────────────
    "shopping.search": Intent(
        id="shopping.search",
        op="shop_catalog_search",
        domain="shopping",
        description="Search for products to buy",
        signals=["shoe", "shoes", "sneaker", "sneakers", "boot", "boots", "sandal", "sandals",
                 "outfit", "jacket", "jeans", "hoodie", "sweatshirt", "shirt", "pants",
                 "shorts", "dress", "coat", "blazer", "sweater", "leggings", "activewear",
                 "laptop", "iphone", "headphones", "earbuds", "airpods", "tablet",
                 "monitor", "keyboard", "speaker", "smartwatch", "camera", "gaming",
                 "sofa", "couch", "desk", "mattress", "pillow", "lamp", "furniture",
                 "puma", "nike", "adidas", "reebok", "new balance", "asics", "converse",
                 "vans", "jordan", "under armour", "fila", "skechers",
                 "buy", "order", "purchase", "shop for"],
        # Note/jot verbs, task/reminder verbs, and contact queries must not trigger shopping
        blockers=["jot", "note that", "write down", "remember that", "log this",
                  "save this", "keep this", "jot down",
                  "phone number", "number for", "email for", "contact",
                  "remind me to", "remember to", "don't forget to", "dont forget to",
                  "need to", "have to", "gotta", "i should", "i must",
                  "log expense", "expense", "spent", "paid"],
        extractor="shopping",
        examples=["show me Nike running shoes size 10", "I want to buy a laptop",
                  "find me a black hoodie", "order some AirPods"],
        slots={"query": "full product search query"},
    ),

    # ── Location ──────────────────────────────────────────────────────────
    "location.current": Intent(
        id="location.current",
        op="location_status",
        domain="system",
        description="Show the user's current detected location",
        signals=[],
        patterns=[
            r"^(where am i|where am i right now|what(?:'s| is) my location|"
            r"my location|show my location|what'?s? my current location)$",
        ],
        extractor="location_status",
        examples=["where am I", "what's my location", "show my location"],
    ),

    # ── News ──────────────────────────────────────────────────────────────
    "news.search": Intent(
        id="news.search",
        op="web_search",
        domain="web",
        description="Search for current news and headlines",
        signals=["news", "headlines", "breaking", "top stories", "current events"],
        patterns=[
            r"\b(what(?:'s|\s+is)\s+(?:happening|going\s+on|in\s+the\s+news)|"
            r"latest\s+(?:news|updates?|stories)|what(?:'s|\s+is)\s+new)\b",
        ],
        extractor="news",
        examples=["what's in the news", "latest news about AI",
                  "top stories today", "what's happening in tech"],
        slots={"query": "news topic or empty for general headlines"},
    ),

    # ── Finance ───────────────────────────────────────────────────────────
    "finance.stock": Intent(
        id="finance.stock",
        op="web_search",
        domain="web",
        description="Look up a stock price or market data",
        signals=["stock", "shares", "ticker", "dow", "nasdaq", "nyse", "s&p", "sp500",
                 "aapl", "tsla", "goog", "googl", "msft", "amzn", "meta", "nvda",
                 "amd", "nflx", "market"],
        # Don't match expense phrases like "paid $15 for Uber"
        blockers=["paid", "spent", "pay", "cost", "bought", "charged"],
        extractor="finance_stock",
        examples=["what's AAPL trading at", "Tesla stock price",
                  "how is the market today", "S&P 500 today"],
    ),

    "finance.crypto": Intent(
        id="finance.crypto",
        op="web_search",
        domain="web",
        description="Look up a cryptocurrency price",
        signals=["bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
                 "dogecoin", "doge", "solana", "sol", "litecoin", "ltc",
                 "ripple", "xrp", "coin price"],
        extractor="finance_crypto",
        examples=["bitcoin price", "what's ethereum worth",
                  "crypto prices today", "how much is dogecoin"],
    ),

    # ── Banking ───────────────────────────────────────────────────────────
    "banking.transactions": Intent(
        id="banking.transactions",
        op="banking_transactions_read",
        domain="banking",
        description="Show recent bank transactions",
        signals=["transactions", "spending", "charges", "purchases", "payments",
                 "bank statement", "account history"],
        extractor="banking_transactions",
        blockers=[],
        examples=["show my recent transactions", "show my recent spending",
                  "my recent charges", "bank statement"],
    ),

    "banking.balance": Intent(
        id="banking.balance",
        op="banking_balance_read",
        domain="banking",
        description="Show account balance",
        signals=["balance", "account balance", "how much money", "bank account",
                 "checking", "savings account"],
        extractor="banking_balance",
        examples=["what's my balance", "how much money do I have",
                  "checking account balance"],
    ),

    # ── Social ────────────────────────────────────────────────────────────
    "social.post": Intent(
        id="social.post",
        op="social_message_send",
        domain="social",
        description="Post a message to social media",
        signals=["post", "tweet", "share"],
        patterns=[
            r"^(?:post|tweet|share|send\s+a\s+(?:tweet|post|social\s+message))\s+",
        ],
        extractor="social_post",
        examples=["tweet that I just shipped a new feature",
                  "post: loving this new UI"],
        slots={"text": "message body to post"},
    ),

    "social.feed": Intent(
        id="social.feed",
        op="social_feed_read",
        domain="social",
        description="Show the user's social media feed",
        signals=["social feed", "my feed", "my timeline", "social media feed"],
        extractor="social_feed",
        examples=["show my feed", "what's on my timeline"],
    ),

    # ── Expenses ──────────────────────────────────────────────────────────
    "expense.add": Intent(
        id="expense.add",
        op="add_expense",
        domain="expenses",
        description="Log a new expense",
        signals=["spent", "spend", "paid", "pay", "cost", "bought", "purchased",
                 "charged", "expense", "logged expense"],
        extractor="expense_add",
        examples=["I spent $45 on groceries", "paid $12 for coffee",
                  "log expense $200 for new shoes"],
        slots={"amount": "numeric dollar amount", "category": "category label", "note": "optional description"},
    ),

    "expense.list": Intent(
        id="expense.list",
        op="graph_query",
        domain="graph",
        description="Show logged expenses",
        signals=["my expenses", "show expenses", "list expenses", "what did i spend",
                 "how much did i spend", "what have i spent"],
        extractor="expense_list",
        examples=["show my expenses", "what did I spend this week",
                  "list my expenses"],
    ),

    # ── Notes ─────────────────────────────────────────────────────────────
    "note.create": Intent(
        id="note.create",
        op="add_note",
        domain="notes",
        description="Save a note",
        signals=["jot", "note that", "write down", "remember that", "log this",
                 "save this", "keep this", "add a note", "add note", "make a note",
                 "make note"],
        extractor="note_create",
        examples=["jot this down: the API key expires in March",
                  "note that we need to revisit auth",
                  "remember that the meeting is at 3pm"],
        slots={"text": "full note content"},
    ),

    "note.list": Intent(
        id="note.list",
        op="graph_query",
        domain="graph",
        description="Show saved notes",
        signals=["my notes", "show notes", "list notes", "what notes"],
        extractor="note_list",
        examples=["show my notes", "what notes do I have"],
    ),

    # ── Reminders ─────────────────────────────────────────────────────────
    "reminder.set_timed": Intent(
        id="reminder.set_timed",
        op="schedule_remind_once",
        domain="system",
        description="Set a one-shot timed reminder",
        signals=["remind me", "reminder", "alert me", "ping me"],
        extractor="reminder_set",
        examples=["remind me to call dentist in 30 minutes",
                  "set a reminder for the standup in 2 hours",
                  "alert me to check email in 15 min"],
        slots={"text": "what to remind about", "durationMs": "delay in milliseconds"},
    ),

    "reminder.list": Intent(
        id="reminder.list",
        op="list_reminders",
        domain="system",
        description="Show scheduled reminders",
        signals=["my reminders", "show reminders", "list reminders",
                 "any reminders", "do i have reminders", "what reminders"],
        extractor="reminder_list",
        examples=["show my reminders", "what reminders do I have",
                  "do I have any reminders"],
    ),

    # ── Tasks ─────────────────────────────────────────────────────────────
    "task.complete": Intent(
        id="task.complete",
        op="toggle_task",
        domain="tasks",
        description="Mark a task as done",
        signals=["finished", "completed", "done with", "mark", "check off", "cross off"],
        extractor="task_complete",
        examples=["I finished the grocery run", "mark the report task done",
                  "check off pick up dry cleaning"],
        slots={"selector": "task title or identifying word"},
    ),

    "task.delete": Intent(
        id="task.delete",
        op="delete_task",
        domain="tasks",
        description="Delete a task",
        signals=["delete", "remove", "drop", "cancel"],
        extractor="task_delete",
        blockers=["file", "folder", "expense", "note", "reminder"],
        examples=["delete my grocery task", "remove the dentist appointment task"],
        slots={"selector": "task title or identifying word"},
    ),

    "task.clear_completed": Intent(
        id="task.clear_completed",
        op="clear_completed",
        domain="tasks",
        description="Clear all completed tasks",
        signals=[],
        patterns=[
            r"^(?:clear|clean\s+up|remove)\s+(?:all\s+)?(?:completed|done|finished)\s+"
            r"(?:tasks?|todos?|items?)$",
        ],
        extractor="task_clear",
        examples=["clear completed tasks", "clean up done todos"],
    ),

    "task.list": Intent(
        id="task.list",
        op="graph_query",
        domain="graph",
        description="Show open tasks",
        signals=["my tasks", "my todos", "show tasks", "list tasks", "what are my tasks",
                 "what's on my list", "my to-do", "my to do",
                 "my todo", "todo list", "task list", "do i have any tasks"],
        extractor="task_list",
        examples=["what are my tasks", "show my todo list",
                  "what's on my list", "do I have any tasks"],
    ),

    "task.create": Intent(
        id="task.create",
        op="add_task",
        domain="tasks",
        description="Create a new task",
        signals=["remind me to", "remember to", "don't forget", "need to", "have to",
                 "gotta", "add task", "create task", "make a task", "todo:",
                 "task:", "i should", "i must", "put on my list"],
        extractor="task_create",
        examples=["remind me to buy milk", "I need to call the dentist",
                  "add task: finish the report", "I should renew my passport"],
        slots={"title": "task description"},
    ),

    # ── Contacts ──────────────────────────────────────────────────────────
    "contacts.lookup": Intent(
        id="contacts.lookup",
        op="contacts_lookup",
        domain="contacts",
        description="Look up a contact",
        signals=["contact", "contacts", "phone number", "email for", "number for",
                 "how do i reach", "how to contact"],
        extractor="contacts",
        examples=["find John's phone number", "look up contact for Dr. Kim",
                  "how do I reach Mike"],
        slots={"query": "person name"},
    ),

    # ── Web ───────────────────────────────────────────────────────────────
    "web.summarize": Intent(
        id="web.summarize",
        op="web_summarize",
        domain="web",
        description="Summarize a web page",
        signals=["summarize", "tldr", "summary of"],
        extractor="web_summarize",
        examples=["summarize https://example.com/article",
                  "tldr this: https://news.ycombinator.com"],
        slots={"url": "URL to summarize"},
    ),

    "web.fetch": Intent(
        id="web.fetch",
        op="fetch_url",
        domain="web",
        description="Fetch and display a URL",
        signals=["open", "fetch", "load", "read"],
        patterns=[r"https?://"],
        extractor="web_fetch",
        examples=["fetch https://example.com", "open https://api.github.com/zen"],
        slots={"url": "URL to fetch"},
    ),

    # ── Utility ───────────────────────────────────────────────────────────
    "timer.start": Intent(
        id="timer.start",
        op="timer_start",
        domain="system",
        description="Set a countdown timer",
        signals=["timer", "set a timer", "start a timer", "countdown"],
        extractor="timer_start",
        examples=["set a timer for 5 minutes", "start a 30 second timer",
                  "timer for 1 hour"],
        slots={"durationMs": "duration in milliseconds (e.g. 300000 for 5 minutes)"},
    ),

    "calc.evaluate": Intent(
        id="calc.evaluate",
        op="calc_evaluate",
        domain="system",
        description="Evaluate a math expression or percentage",
        signals=["calculate", "compute", "what is", "how much is",
                 "percent of", "% of"],
        patterns=[r"what(?:'s|\s+is)\s+\d+.*[\+\-\*\/]"],
        extractor="calc",
        examples=["what is 15% of 120", "calculate 42 * 7",
                  "how much is 250 / 5"],
        slots={"expression": "math expression to evaluate"},
    ),

    "unit.convert": Intent(
        id="unit.convert",
        op="unit_convert",
        domain="system",
        description="Convert between units of measurement",
        signals=["convert", "how many", "how much is", "miles to km",
                 "km to miles", "celsius to fahrenheit", "pounds to kg"],
        patterns=[
            r"\d+\s*(?:miles?|km|feet|lbs?|kg|celsius|fahrenheit|gallons?|liters?)\s+"
            r"(?:in|to|into)\s+(?:miles?|km|feet|lbs?|kg|celsius|fahrenheit|gallons?|liters?)",
        ],
        extractor="unit_convert",
        examples=["how many miles is 10 km", "convert 72 fahrenheit to celsius",
                  "5 pounds in kg"],
        slots={"value": "numeric amount", "fromUnit": "source unit", "toUnit": "target unit"},
    ),

    "web.search": Intent(
        id="web.search",
        op="web_search",
        domain="web",
        description="Search the web for information",
        signals=["search for", "look up", "google", "bing",
                 "what is", "what are", "what was", "what were",
                 "who is", "who are", "who was",
                 "where is", "where are",
                 "how do", "how does", "how to", "how did", "how can",
                 "tell me about", "define", "explain", "research",
                 "when did", "when was", "when is",
                 "why did", "why is", "why are", "why was",
                 "what's the difference", "what's the history",
                 "what's the definition", "find me"],
        # Blockers: don't hijack personal data or sports queries
        blockers=["my tasks", "my todos", "my notes", "my expenses",
                  "my reminders", "my balance", "my account", "my feed",
                  # Sports blockers — yield to sports.* intents
                  "score", "scores", "final score", "game score", "scoreboard",
                  "standings", "win-loss", "did they win", "did we win",
                  "who won", "game time", "next game", "play next", "play tonight",
                  "schedule", "upcoming game", "last game", "last night's game",
                  "ncaaf", "ncaab", "cfb", "cbb", "college football", "college basketball",
                  "college hoops", "march madness", "final four", "bowl game",
                  "softball", "college softball", "college baseball",
                  # Calendar blockers — yield to calendar.* intents
                  "my calendar", "on my calendar", "my schedule", "my agenda",
                  "calendar today", "calendar tomorrow",
                  # Code blockers — yield to code.* intents
                  "explain this", "explain the code", "explain the function",
                  "explain the script", "explain this code", "explain this function",
                  "explain this script", "debug this", "fix the bug",
                  # Email blockers — yield to email.* intents
                  "my inbox", "my email", "check my email",
                  "search for emails", "find emails", "look for emails",
                  "emails from", "emails about",
                  # Document/presentation blockers
                  "write a letter", "write a memo", "draft a", "create a document",
                  "create a presentation", "make a presentation", "make a spreadsheet"],
        extractor="web_search",
        examples=["what is quantum computing", "who was Alan Turing",
                  "how does DNS work", "explain neural networks",
                  "when did the Berlin Wall fall"],
        slots={"query": "search query string"},
    ),

    # ── Sports ────────────────────────────────────────────────────────────────
    "sports.scores": Intent(
        id="sports.scores",
        op="sports_scores",
        domain="sports",
        description="Get live or recent scores for a team or league",
        signals=["score", "scores", "scoreboard", "final score", "game score",
                 "did they win", "did we win", "who won", "how did they do",
                 "how did the", "result", "results", "what happened in the game",
                 "last night's game", "last game", "tonight's game",
                 "ncaaf", "ncaab", "cfb", "cbb", "college football", "college basketball",
                 "college hoops", "march madness", "final four", "bowl game",
                 "softball", "college softball", "college baseball"],
        blockers=["my tasks", "my notes", "my expenses",
                  "presentation", "slide deck", "slideshow", "powerpoint"],
        extractor="sports",
        examples=["what's the Bears score", "did the Cubs win", "show me NBA scores",
                  "who won the game last night", "what are tonight's scores",
                  "did Ohio State win", "what's the Alabama score", "college football scores"],
        slots={"team": "team name or empty for all teams", "sport": "nfl|nba|mlb|nhl|ncaaf|ncaab|''"},
    ),

    "sports.schedule": Intent(
        id="sports.schedule",
        op="sports_schedule",
        domain="sports",
        description="Get upcoming game schedule for a team",
        signals=["schedule", "next game", "when do they play", "when do the",
                 "when is the next", "upcoming games", "when does", "game time",
                 "when are the", "play next", "next match"],
        blockers=["my tasks", "my notes", "my expenses",
                  "meeting", "appointment", "dentist", "calendar event",
                  "add to calendar", "block time", "block off"],
        extractor="sports",
        examples=["when do the Bears play next", "show me the Cubs schedule",
                  "when is the next Bulls game", "upcoming Bears games"],
        slots={"team": "team name", "sport": "nfl|nba|mlb|nhl|ncaaf|ncaab"},
    ),

    "sports.standings": Intent(
        id="sports.standings",
        op="sports_standings",
        domain="sports",
        description="Get league or conference standings",
        signals=["standings", "ranking", "rankings", "record", "place in the",
                 "division standings", "conference standings", "in the standings",
                 "league table", "where are the", "where do the", "how are the",
                 "win-loss", "wins and losses"],
        blockers=["my tasks", "my notes", "my expenses"],
        extractor="sports",
        examples=["show me NFL standings", "where are the Bears in the standings",
                  "NBA standings", "what's Chicago's record"],
        slots={"team": "team name or empty for full standings", "sport": "nfl|nba|mlb|nhl|ncaaf|ncaab"},
    ),

    "sports.follow_team": Intent(
        id="sports.follow_team",
        op="sports_follow_team",
        domain="sports",
        description="Add a team to the user's followed teams list",
        signals=["follow", "track", "add to my teams", "save team", "subscribe to",
                 "add the", "follow the", "i like the", "my team"],
        patterns=[r"follow\s+(?:the\s+)?\w+", r"add\s+(?:the\s+)?\w+\s+to\s+my\s+teams?"],
        blockers=["unfollow", "stop following", "remove"],
        extractor="sports",
        examples=["follow the Bears", "add the Cubs to my teams", "track the Bulls",
                  "I like the Bears"],
        slots={"team": "team name", "sport": "nfl|nba|mlb|nhl|ncaaf|ncaab"},
    ),

    "sports.unfollow_team": Intent(
        id="sports.unfollow_team",
        op="sports_unfollow_team",
        domain="sports",
        description="Remove a team from the user's followed teams list",
        signals=["unfollow", "stop following", "remove from my teams", "remove the",
                 "drop the", "stop tracking"],
        patterns=[r"unfollow\s+(?:the\s+)?\w+", r"remove\s+(?:the\s+)?\w+\s+from\s+my\s+teams?"],
        extractor="sports",
        examples=["unfollow the Bears", "remove the Cubs from my teams",
                  "stop following the Bulls"],
        slots={"team": "team name", "sport": "nfl|nba|mlb|nhl|ncaaf|ncaab"},
    ),

    "sports.my_teams": Intent(
        id="sports.my_teams",
        op="sports_my_teams",
        domain="sports",
        description="Show the user's followed sports teams",
        signals=["my teams", "my sports teams", "teams i follow", "teams i track",
                 "show my teams", "what teams", "which teams do i follow"],
        patterns=[r"(?:show|list|what are)\s+my\s+(?:sports\s+)?teams?"],
        extractor="sports",
        examples=["show my teams", "what teams do I follow", "my sports teams"],
        slots={},
    ),

    # ── Content management ────────────────────────────────────────────────
    # Git-style OS content layer: named content, flat namespace, auto-versioned.

    "content.history": Intent(
        id="content.history",
        op="content_history",
        domain="content",
        description="Show version history of a piece of content",
        signals=["history of", "versions of", "revisions of", "what changed", "changelog",
                 "previous versions", "old version", "show changes"],
        patterns=[r"\bhistory\s+(?:of|for)\b", r"\bversions?\s+of\b",
                  r"\bwhat\s+changed\s+(?:in|to)\b"],
        extractor="content",
        examples=["show history of Q4 Report", "what versions of the budget exist",
                  "what changed in the proposal"],
        slots={"action": "history", "name": "content name"},
    ),

    "content.branch": Intent(
        id="content.branch",
        op="content_branch",
        domain="content",
        description="Create a divergent copy (branch) of a piece of content",
        signals=["branch", "fork", "create a copy", "make a copy", "duplicate",
                 "spin off", "draft version of", "new version of"],
        patterns=[r"\b(?:branch|fork|duplicate)\s+(?:the\s+)?\w",
                  r"\b(?:create|make)\s+(?:a\s+)?(?:copy|draft|version)\s+of\b"],
        extractor="content",
        examples=["branch the Q4 Report", "create a draft version of the proposal",
                  "fork the budget into a 2027 version"],
        slots={"action": "branch", "name": "source content name", "branch_name": "new name"},
    ),

    "content.revert": Intent(
        id="content.revert",
        op="content_revert",
        domain="content",
        description="Revert content to a previous version",
        signals=["revert", "rollback", "restore", "undo changes", "go back to",
                 "previous version", "older version"],
        patterns=[r"\brevert\s+(?:the\s+)?\w", r"\bgo\s+back\s+to\s+(?:the\s+)?(?:previous|last|old)\b"],
        extractor="content",
        examples=["revert the proposal to yesterday's version", "undo changes to the report",
                  "restore the budget from last week"],
        slots={"action": "revert", "name": "content name", "version_hint": "version description"},
    ),

    "content.share": Intent(
        id="content.share",
        op="content_share",
        domain="content",
        description="Share or export a piece of content",
        signals=["share the", "export the", "publish the", "download the"],
        patterns=[r"\b(?:share|export|publish)\s+(?:the\s+)?\w"],
        blockers=["share screen", "share my location", "share my location"],
        extractor="content",
        examples=["share the Q4 Report", "export the budget as PDF",
                  "send the proposal to Mike"],
        slots={"action": "share", "name": "content name", "format": "pdf|docx|xlsx|pptx"},
    ),

    "content.list": Intent(
        id="content.list",
        op="content_list",
        domain="content",
        description="List recent or all content",
        signals=["recent files", "recent documents", "my documents", "my files",
                 "everything i've been working on", "show all my", "what have i been",
                 "what did i work on"],
        patterns=[r"\bshow\s+(?:all\s+)?(?:my\s+)?(?:recent\s+)?(?:files|documents|content|work)\b",
                  r"\blist\s+(?:my\s+)?(?:files|documents|content)\b"],
        extractor="content",
        examples=["show my recent documents", "list all my files",
                  "what have I been working on"],
        slots={"action": "list", "type": "document|spreadsheet|presentation|code|note"},
    ),

    "content.find": Intent(
        id="content.find",
        op="content_find",
        domain="content",
        description="Find and open a named piece of content",
        signals=["open my", "find my", "pull up", "bring up", "where is my",
                 "look for my", "locate my"],
        patterns=[r"\b(?:open|find|pull\s+up|bring\s+up|locate)\s+(?:my\s+)?[\"']?[\w\s]{2,}[\"']?"],
        blockers=["search the web", "google", "search online", "find a store",
                  "terminal", "command line", "shell", "a terminal"],
        extractor="content",
        examples=["open Q4 Report", "find my budget spreadsheet",
                  "pull up the project proposal"],
        slots={"action": "find", "name": "content name", "type": "content type"},
    ),

    # ── Documents ─────────────────────────────────────────────────────────

    "document.create": Intent(
        id="document.create",
        op="document_create",
        domain="document",
        description="Create a new document",
        signals=["write a letter", "write a memo", "write an essay", "write a report",
                 "write a proposal", "write an article", "write a contract",
                 "draft a letter", "draft a memo", "draft a proposal", "draft an essay",
                 "create a document", "new document", "cover letter", "resume",
                 "write me a", "start a document"],
        patterns=[r"\b(?:write|draft|create|start)\s+(?:a|an)\s+(?:letter|memo|essay|report|proposal|article|contract|agreement|cover\s+letter|resume|document)\b"],
        blockers=["edit the", "update the", "change the", "modify the"],
        extractor="document",
        examples=["write a cover letter for a software engineer role",
                  "draft a proposal for the client",
                  "create a memo about the policy change"],
        slots={"action": "create", "name": "document name", "topic": "topic or purpose"},
    ),

    "document.edit": Intent(
        id="document.edit",
        op="document_edit",
        domain="document",
        description="Edit an existing document",
        signals=["edit the document", "edit the report", "edit the letter",
                 "update the document", "revise the", "rewrite the", "modify the document"],
        patterns=[r"\b(?:edit|update|revise|rewrite|modify)\s+(?:the\s+)?[\"']?[\w\s]+[\"']?\s+(?:document|doc|report|letter|memo|essay|proposal)\b"],
        extractor="document",
        examples=["edit the Q4 report", "update the proposal", "revise my cover letter"],
        slots={"action": "edit", "name": "document name"},
    ),

    # ── Spreadsheets ──────────────────────────────────────────────────────

    "spreadsheet.create": Intent(
        id="spreadsheet.create",
        op="spreadsheet_create",
        domain="spreadsheet",
        description="Create a new spreadsheet",
        signals=["create a spreadsheet", "new spreadsheet", "make a spreadsheet",
                 "build a spreadsheet", "budget spreadsheet", "expense tracker",
                 "tracking spreadsheet", "data table", "make a table"],
        patterns=[r"\b(?:create|make|build|start|new)\s+(?:a\s+)?spreadsheet\b",
                  r"\bspreadsheet\s+(?:for|to\s+track)\b"],
        blockers=["edit the spreadsheet", "open the spreadsheet", "update the spreadsheet"],
        extractor="spreadsheet",
        examples=["create a budget spreadsheet", "make a spreadsheet to track expenses",
                  "build a project data table"],
        slots={"action": "create", "name": "spreadsheet name"},
    ),

    "spreadsheet.edit": Intent(
        id="spreadsheet.edit",
        op="spreadsheet_edit",
        domain="spreadsheet",
        description="Edit an existing spreadsheet",
        signals=["edit the spreadsheet", "update the spreadsheet", "add a row",
                 "add a column", "modify the spreadsheet", "change the spreadsheet"],
        patterns=[r"\b(?:edit|update|modify|change)\s+(?:the\s+)?spreadsheet\b",
                  r"\badd\s+(?:a\s+)?(?:row|column)\s+to\b"],
        extractor="spreadsheet",
        examples=["edit the budget spreadsheet", "add a row to the expense tracker",
                  "update the project table"],
        slots={"action": "edit", "name": "spreadsheet name"},
    ),

    # ── Presentations ─────────────────────────────────────────────────────

    "presentation.create": Intent(
        id="presentation.create",
        op="presentation_create",
        domain="presentation",
        description="Create a new presentation or slide deck",
        signals=["create a presentation", "make a presentation", "build a deck",
                 "slide deck", "slideshow", "build slides", "make slides",
                 "powerpoint", "keynote", "make a deck"],
        patterns=[r"\b(?:create|make|build|write|generate)\s+(?:a\s+)?(?:presentation|slideshow|deck|slides)\b",
                  r"\bpresentation\s+(?:for|on|about)\b"],
        blockers=["edit the presentation", "update the slides", "add a slide to"],
        extractor="presentation",
        examples=["create a 10-slide presentation on the Q4 results",
                  "make a sales pitch deck", "build a presentation for the board meeting"],
        slots={"action": "create", "name": "presentation name", "topic": "topic", "slides": "slide count"},
    ),

    "presentation.edit": Intent(
        id="presentation.edit",
        op="presentation_edit",
        domain="presentation",
        description="Edit an existing presentation",
        signals=["edit the presentation", "update the slides", "add a slide",
                 "modify the deck", "revise the presentation", "change the slides"],
        patterns=[r"\b(?:edit|update|modify|revise)\s+(?:the\s+)?(?:presentation|deck|slides)\b",
                  r"\badd\s+a\s+slide\s+to\b"],
        extractor="presentation",
        examples=["edit the Q4 presentation", "add a slide to the deck",
                  "update the sales pitch"],
        slots={"action": "edit", "name": "presentation name"},
    ),

    # ── Code / IDE ────────────────────────────────────────────────────────

    "code.create": Intent(
        id="code.create",
        op="code_create",
        domain="code",
        description="Write new code — a function, script, class, or program",
        signals=["write a script", "write a function", "write code", "create a script",
                 "generate code", "implement", "scaffold", "write a class",
                 "write a program", "write a component", "write an api"],
        patterns=[r"\b(?:write|create|generate|implement|build|scaffold)\s+(?:a\s+)?(?:\w+\s+)?(?:script|function|class|module|program|api|endpoint|component|app)\b"],
        blockers=["edit", "fix", "debug", "explain", "review", "run", "test"],
        extractor="code",
        examples=["write a Python script to parse a CSV",
                  "create a function that validates email addresses",
                  "generate a REST API endpoint"],
        slots={"action": "create", "language": "language", "topic": "what it should do"},
    ),

    "code.explain": Intent(
        id="code.explain",
        op="code_explain",
        domain="code",
        description="Explain what a piece of code does",
        signals=["explain this", "explain the code", "what does this do",
                 "how does this work", "walk me through", "what is this code",
                 "describe the code", "understand this"],
        patterns=[r"\b(?:explain|describe|walk\s+me\s+through)\s+(?:this\s+)?(?:code|script|function|class)\b",
                  r"\bwhat\s+does\s+(?:this\s+)?(?:code|function|script)\s+do\b"],
        extractor="code",
        examples=["explain this function", "what does this script do",
                  "walk me through the auth module"],
        slots={"action": "explain", "language": "language"},
    ),

    "code.debug": Intent(
        id="code.debug",
        op="code_debug",
        domain="code",
        description="Debug or fix broken code",
        signals=["debug", "fix the bug", "there's an error", "error in the code",
                 "why is it failing", "broken", "not working", "fix the code",
                 "fix this error", "fix this bug"],
        patterns=[r"\b(?:debug|fix)\s+(?:this\s+)?(?:code|script|function|error|bug)\b",
                  r"\bwhy\s+(?:is|isn't)\s+(?:this\s+)?(?:code|function|script)\s+working\b"],
        extractor="code",
        examples=["debug this Python script", "fix the bug in my function",
                  "why is this code not working"],
        slots={"action": "debug", "language": "language"},
    ),

    "code.run": Intent(
        id="code.run",
        op="code_run",
        domain="code",
        description="Run or execute a script or program",
        signals=["run the script", "execute the code", "run this code",
                 "execute this", "run it", "run the program"],
        patterns=[r"\b(?:run|execute|launch)\s+(?:the\s+)?(?:script|code|program|file)\b"],
        extractor="code",
        examples=["run the Python script", "execute this code", "run it"],
        slots={"action": "run", "name": "script or file name"},
    ),

    # ── Terminal ──────────────────────────────────────────────────────────

    "terminal.run": Intent(
        id="terminal.run",
        op="terminal_run",
        domain="terminal",
        description="Open a terminal or run a shell command",
        signals=["open terminal", "open the terminal", "command line", "shell",
                 "run command", "open a terminal", "command prompt", "open cli"],
        patterns=[r"\b(?:open|launch|start)\s+(?:a\s+)?(?:terminal|shell|command\s+line|cli)\b",
                  r"\brun\s+(?:the\s+)?command\b"],
        extractor="terminal",
        examples=["open a terminal", "run git status", "open the command line"],
        slots={"command": "shell command to run"},
    ),

    # ── Calendar ──────────────────────────────────────────────────────────

    "calendar.create": Intent(
        id="calendar.create",
        op="calendar_create",
        domain="calendar",
        description="Create a calendar event",
        signals=["schedule a meeting", "book a meeting", "add to calendar",
                 "set up a meeting", "calendar event", "add an event",
                 "create an event", "block time", "block off", "appointment at",
                 "meeting at", "lunch with", "dinner with", "call with"],
        patterns=[r"\b(?:schedule|book|add|create|set\s+up)\s+(?:a\s+)?(?:meeting|event|appointment|call|lunch|dinner|interview)\b",
                  r"\bblock\s+(?:off\s+)?(?:time|my\s+calendar)\b"],
        blockers=["cancel", "delete the meeting", "remove from calendar", "what's on"],
        extractor="calendar",
        examples=["schedule a meeting with Sarah tomorrow at 2pm",
                  "add a dentist appointment on Friday at 10am",
                  "block off Tuesday afternoon for deep work"],
        slots={"action": "create", "title": "event title", "date": "date/time", "with": "attendees"},
    ),

    "calendar.list": Intent(
        id="calendar.list",
        op="calendar_list",
        domain="calendar",
        description="Show upcoming calendar events or agenda",
        signals=["what's on my calendar", "show my calendar", "upcoming events",
                 "what do i have", "my schedule", "agenda", "what meetings do i have",
                 "any meetings", "what's scheduled", "calendar today", "calendar tomorrow",
                 "show my schedule"],
        patterns=[r"\bwhat(?:'s|\s+is)\s+on\s+(?:my\s+)?(?:calendar|schedule)\b",
                  r"\bshow\s+(?:my\s+)?(?:calendar|schedule|agenda)\b",
                  r"\bwhat\s+(?:meetings?|events?|appointments?)\s+do\s+i\s+have\b"],
        extractor="calendar",
        examples=["what's on my calendar today", "show my schedule for next week",
                  "what meetings do I have tomorrow", "my agenda for Friday"],
        slots={"action": "list", "date": "date/time range"},
    ),

    "calendar.cancel": Intent(
        id="calendar.cancel",
        op="calendar_cancel",
        domain="calendar",
        description="Cancel or delete a calendar event",
        signals=["cancel the meeting", "cancel my appointment", "delete the event",
                 "remove from calendar", "cancel the call", "cancel lunch",
                 "cancel the interview"],
        patterns=[r"\b(?:cancel|delete|remove)\s+(?:the\s+)?(?:meeting|event|appointment|call|lunch|dinner|interview)\b"],
        extractor="calendar",
        examples=["cancel the meeting with Sarah", "delete the dentist appointment",
                  "remove the Friday call from my calendar"],
        slots={"action": "cancel", "title": "event title", "date": "date/time"},
    ),

    # ── Email ─────────────────────────────────────────────────────────────

    "email.compose": Intent(
        id="email.compose",
        op="email_compose",
        domain="email",
        description="Compose and send an email",
        signals=["send an email", "write an email", "compose an email",
                 "draft an email", "email to", "send a message to"],
        patterns=[r"\b(?:send|write|compose|draft)\s+(?:an?\s+)?email\b",
                  r"\bemail\s+(?:to\s+)?\w"],
        blockers=["check email", "read email", "show email", "my inbox",
                  "emails from", "search email", "reply to"],
        extractor="email",
        examples=["send an email to John about the meeting",
                  "compose an email to the team about the update"],
        slots={"action": "compose", "to": "recipient", "subject": "subject"},
    ),

    "email.read": Intent(
        id="email.read",
        op="email_read",
        domain="email",
        description="Read or check email inbox",
        signals=["check email", "read email", "check my email", "my inbox",
                 "my email", "new emails", "unread emails", "show my email",
                 "any new email", "emails from"],
        patterns=[r"\b(?:check|read|show|open)\s+(?:my\s+)?(?:email|inbox|mail)\b",
                  r"\bany\s+(?:new|unread)\s+(?:email|messages?)\b"],
        blockers=["send", "write", "compose", "draft", "reply to"],
        extractor="email",
        examples=["check my email", "show my inbox", "any new emails", "emails from Mike"],
        slots={"action": "read", "from": "sender filter"},
    ),

    "email.reply": Intent(
        id="email.reply",
        op="email_reply",
        domain="email",
        description="Reply to an email",
        signals=["reply to", "respond to the email", "answer the email",
                 "reply back", "write a reply", "send a reply"],
        patterns=[r"\b(?:reply|respond|answer)\s+(?:to\s+)?(?:the\s+)?(?:email|message)\b"],
        extractor="email",
        examples=["reply to Mike's email", "respond to the meeting request",
                  "write a reply to Sarah"],
        slots={"action": "reply", "to": "recipient"},
    ),

    "email.search": Intent(
        id="email.search",
        op="email_search",
        domain="email",
        description="Search emails",
        signals=["search email", "find email", "look for email", "email about",
                 "find the email", "search my inbox", "emails about"],
        patterns=[r"\b(?:search|find|look\s+for)\s+(?:emails?|messages?)\b",
                  r"\bemails?\s+(?:about|from|with)\b"],
        extractor="email",
        examples=["find emails from Sarah about the project",
                  "search my inbox for the invoice"],
        slots={"action": "search", "query": "search terms", "from": "sender"},
    ),

    "email.forward": Intent(
        id="email.forward",
        op="email_forward",
        domain="email",
        description="Forward an email to someone",
        signals=["forward the email", "forward this email", "forward to",
                 "pass along the email"],
        patterns=[r"\b(?:forward)\s+(?:the\s+|this\s+)?(?:email|message)\b"],
        extractor="email",
        examples=["forward the meeting invite to Sarah",
                  "forward John's email to the team"],
        slots={"action": "forward", "to": "recipient"},
    ),

    "email.archive": Intent(
        id="email.archive",
        op="email_archive",
        domain="email",
        description="Archive or delete an email",
        signals=["archive the email", "delete the email", "trash the email",
                 "archive this email", "mark as read and archive"],
        patterns=[r"\b(?:archive|delete|trash)\s+(?:the\s+|this\s+)?(?:email|message)\b"],
        extractor="email",
        examples=["archive the newsletter", "delete the promotional emails",
                  "trash yesterday's spam"],
        slots={"action": "archive", "query": "which emails"},
    ),

    # ── Weather extended ───────────────────────────────────────────────────

    "weather.alert": Intent(
        id="weather.alert",
        op="weather_alert",
        domain="weather",
        description="Get active severe weather alerts and warnings for a location",
        signals=["weather alert", "severe weather", "weather warning", "tornado warning",
                 "flood warning", "winter storm warning", "heat advisory",
                 "weather advisory", "any alerts", "storm warning"],
        extractor="weather_alert",
        examples=["any weather alerts today", "severe weather warning in Chicago",
                  "are there tornado warnings near me", "weather advisories tonight"],
        slots={"location": "city name or empty for current location"},
    ),

    # ── Shopping extended ──────────────────────────────────────────────────

    "shopping.track": Intent(
        id="shopping.track",
        op="shopping_track",
        domain="shopping",
        description="Track the price of a product and get alerted when it drops",
        signals=["track the price", "price alert", "notify me when", "alert me when",
                 "watch the price", "price drop alert", "let me know when it drops"],
        extractor="shopping_track",
        examples=["track the price of the MacBook Pro",
                  "alert me when Nike shoes drop below $80",
                  "watch the AirPods Pro price"],
        slots={"query": "product to track"},
    ),

    "shopping.wishlist": Intent(
        id="shopping.wishlist",
        op="shopping_wishlist",
        domain="shopping",
        description="Add a product to the wishlist or view the wishlist",
        signals=["add to wishlist", "add to my wishlist", "save for later",
                 "wish list", "wishlist", "my wishlist", "show my wishlist"],
        extractor="shopping_wishlist",
        examples=["add these Nikes to my wishlist", "show my wishlist",
                  "save the MacBook for later"],
        slots={"action": "add|list", "query": "product name"},
    ),

    # ── News extended ──────────────────────────────────────────────────────

    "news.by_topic": Intent(
        id="news.by_topic",
        op="web_search",
        domain="web",
        description="Get news filtered to a specific topic or category",
        signals=["tech news", "business news", "sports news", "politics news",
                 "science news", "health news", "entertainment news", "finance news",
                 "world news", "local news", "news about", "latest in"],
        extractor="news",
        examples=["show me tech news", "latest business news",
                  "news about AI", "what's happening in politics"],
        slots={"query": "news topic"},
    ),

    "news.saved": Intent(
        id="news.saved",
        op="news_saved",
        domain="news",
        description="Show saved or bookmarked articles",
        signals=["saved articles", "bookmarked articles", "saved news",
                 "my articles", "articles i saved", "news i saved", "my saved articles"],
        extractor="news_saved",
        examples=["show my saved articles", "bookmarked news", "articles I saved"],
    ),

    # ── Location extended ──────────────────────────────────────────────────

    "location.nearby": Intent(
        id="location.nearby",
        op="location_nearby",
        domain="system",
        description="Find places or businesses near the user's current location",
        signals=["nearby", "near me", "around me", "close by", "in the area",
                 "around here", "close to me", "restaurants near", "coffee near"],
        blockers=["search the web", "google", "explain"],
        extractor="location_nearby",
        examples=["restaurants near me", "coffee shops nearby",
                  "find a gas station near me", "what's around me"],
        slots={"query": "type of place", "location": "location or current"},
    ),

    # ── Contacts extended ──────────────────────────────────────────────────

    "contacts.create": Intent(
        id="contacts.create",
        op="contacts_create",
        domain="contacts",
        description="Create or save a new contact",
        signals=["add a contact", "save contact", "new contact", "create a contact",
                 "add to contacts", "save as contact", "add someone to my contacts"],
        extractor="contacts_create",
        examples=["add John Smith 555-1234 to my contacts",
                  "save Sarah Lee sarah@email.com as a contact",
                  "create a contact for Mike"],
        slots={"name": "person name", "phone": "phone number", "email": "email"},
    ),

    "contacts.call": Intent(
        id="contacts.call",
        op="contacts_call",
        domain="contacts",
        description="Call a contact by name",
        signals=["call", "dial", "give a call", "place a call"],
        patterns=[r"^(?:call|dial|phone|ring)\s+\w"],
        blockers=["conference call", "on a call", "schedule a call",
                  "video call", "cancel the call", "cancel call"],
        extractor="contacts_call",
        examples=["call John", "dial Sarah", "phone my mom", "ring the office"],
        slots={"name": "person or number to call"},
    ),

    "contacts.message": Intent(
        id="contacts.message",
        op="contacts_message",
        domain="contacts",
        description="Send a text message to a contact",
        signals=["send a text", "send a message", "text message to", "sms to"],
        patterns=[r"^(?:text|sms)\s+\w"],
        blockers=["email", "tweet", "post", "dm", "direct message", "slack"],
        extractor="contacts_message",
        examples=["text Sarah I'm on my way", "send a text to John: running late",
                  "message my mom that dinner is at 7"],
        slots={"to": "recipient name", "body": "message text"},
    ),

    # ── Finance extended ───────────────────────────────────────────────────

    "finance.portfolio": Intent(
        id="finance.portfolio",
        op="finance_portfolio",
        domain="finance",
        description="View the user's investment portfolio and holdings",
        signals=["my portfolio", "my investments", "my holdings", "my stocks",
                 "portfolio value", "my positions", "what am i invested in",
                 "investment portfolio", "what stocks do i own"],
        extractor="finance_portfolio",
        examples=["show my portfolio", "what are my holdings",
                  "my investment value", "what stocks do I own"],
    ),

    "finance.watchlist": Intent(
        id="finance.watchlist",
        op="finance_watchlist",
        domain="finance",
        description="Manage or view a stock/crypto watchlist",
        signals=["my watchlist", "add to watchlist", "watch this stock",
                 "remove from watchlist", "stock watchlist", "crypto watchlist"],
        extractor="finance_watchlist",
        examples=["show my watchlist", "add AAPL to my watchlist",
                  "remove Tesla from watchlist"],
        slots={"action": "add|remove|list", "symbol": "ticker symbol"},
    ),

    # ── Banking extended ───────────────────────────────────────────────────

    "banking.transfer": Intent(
        id="banking.transfer",
        op="banking_transfer",
        domain="banking",
        description="Transfer money between accounts or to a person",
        signals=["transfer", "send money", "move money", "wire", "zelle",
                 "venmo", "pay someone", "send to"],
        blockers=["pay bill", "pay my bill", "pay the bill", "subscription"],
        extractor="banking_transfer",
        examples=["transfer $200 to savings", "send $50 to Sarah via Zelle",
                  "move $500 from checking to savings"],
        slots={"amount": "dollar amount", "to": "destination or recipient"},
    ),

    "banking.pay": Intent(
        id="banking.pay",
        op="banking_pay",
        domain="banking",
        description="Pay a bill or make a scheduled payment",
        signals=["pay my bill", "pay the bill", "pay my rent", "pay my mortgage",
                 "make a payment", "schedule a payment", "bill payment",
                 "pay utilities", "pay my credit card"],
        extractor="banking_pay",
        examples=["pay my electricity bill", "make a $200 credit card payment",
                  "schedule my rent payment for Friday"],
        slots={"amount": "dollar amount", "payee": "who to pay"},
    ),

    # ── Notes extended ─────────────────────────────────────────────────────

    "note.search": Intent(
        id="note.search",
        op="note_search",
        domain="notes",
        description="Search through saved notes",
        signals=["search notes", "find notes", "look for notes", "search my notes",
                 "notes about", "find a note", "look through my notes"],
        extractor="note_search",
        examples=["search my notes for meeting agenda",
                  "find notes about Q4", "look for the password note"],
        slots={"query": "search terms"},
    ),

    "note.delete": Intent(
        id="note.delete",
        op="note_delete",
        domain="notes",
        description="Delete a note",
        signals=["delete the note", "remove the note", "trash the note",
                 "delete my note", "remove my note"],
        patterns=[
            r"\b(?:delete|remove|trash)\s+(?:the\s+|my\s+)?note\s+(?:about|called|titled)?\s*\w"
        ],
        extractor="note_delete",
        examples=["delete the note about the dentist",
                  "remove the grocery list note", "trash my shopping note"],
        slots={"selector": "note identifier"},
    ),

    "note.pin": Intent(
        id="note.pin",
        op="note_pin",
        domain="notes",
        description="Pin or unpin a note to keep it at the top",
        signals=["pin the note", "pin my note", "unpin the note",
                 "pin this note", "keep this note at the top"],
        extractor="note_pin",
        examples=["pin the API key note", "unpin my grocery list",
                  "pin this note to the top"],
        slots={"action": "pin|unpin", "name": "note name"},
    ),

    # ── Reminders extended ─────────────────────────────────────────────────

    "reminder.delete": Intent(
        id="reminder.delete",
        op="reminder_delete",
        domain="system",
        description="Delete a reminder",
        signals=["delete the reminder", "remove the reminder", "cancel the reminder",
                 "clear the reminder", "delete my reminder"],
        patterns=[r"\b(?:delete|remove|cancel|clear)\s+(?:the\s+|my\s+)?reminder\b"],
        extractor="reminder_delete",
        examples=["delete the dentist reminder", "remove my 3pm reminder",
                  "cancel the standup reminder"],
        slots={"selector": "reminder text"},
    ),

    "reminder.snooze": Intent(
        id="reminder.snooze",
        op="reminder_snooze",
        domain="system",
        description="Snooze or postpone a reminder",
        signals=["snooze", "snooze the reminder", "remind me later", "postpone the reminder",
                 "defer the reminder", "push back the reminder"],
        extractor="reminder_snooze",
        examples=["snooze the dentist reminder for 20 minutes",
                  "remind me later about the standup"],
        slots={"selector": "reminder text", "delayMs": "snooze duration in ms"},
    ),

    # ── Social extended ────────────────────────────────────────────────────

    "social.dm": Intent(
        id="social.dm",
        op="social_dm_send",
        domain="social",
        description="Send a direct message to someone on social media",
        signals=["dm", "direct message", "send a dm", "message on twitter",
                 "instagram dm"],
        patterns=[r"^(?:dm|direct\s+message)\s+[A-Z]"],
        extractor="social_dm",
        examples=["DM John: hey are you free tomorrow",
                  "send a direct message to Sarah", "dm @user this"],
        slots={"to": "recipient handle or name", "text": "message body"},
    ),

    "social.profile": Intent(
        id="social.profile",
        op="social_profile_read",
        domain="social",
        description="View a social media profile",
        signals=["my profile", "social profile", "twitter profile", "instagram profile",
                 "view profile", "my twitter", "my instagram", "my social"],
        extractor="social_profile",
        examples=["show my Twitter profile", "view my Instagram", "my social profile"],
    ),

    # ── Terminal extended ──────────────────────────────────────────────────

    "terminal.history": Intent(
        id="terminal.history",
        op="terminal_history",
        domain="terminal",
        description="Show recent terminal command history",
        signals=["command history", "terminal history", "shell history",
                 "recent commands", "last commands"],
        patterns=[r"\b(?:show|view|see)\s+(?:(?:my|the|command|shell|terminal)\s+)?history\b"],
        extractor="terminal_history",
        examples=["show my command history", "terminal history", "recent commands"],
    ),

    "terminal.kill": Intent(
        id="terminal.kill",
        op="terminal_kill",
        domain="terminal",
        description="Kill or terminate a running process",
        signals=["kill the process", "stop the process", "terminate the process",
                 "end the process", "force quit", "kill the program"],
        patterns=[r"\b(?:kill|terminate|force\s+quit)\s+(?:the\s+)?\w"],
        blockers=["don't kill", "cancel kill"],
        extractor="terminal_kill",
        examples=["kill the Python process", "terminate node",
                  "force quit Chrome", "stop the webpack process"],
        slots={"process": "process name or PID"},
    ),

    # ── Document extended ──────────────────────────────────────────────────

    "document.delete": Intent(
        id="document.delete",
        op="document_delete",
        domain="document",
        description="Delete a document",
        signals=["delete the document", "remove the document", "trash the document",
                 "delete the report", "delete the letter", "delete the memo"],
        patterns=[
            r"\b(?:delete|remove|trash)\s+(?:the\s+)?[\w\s]+\s+(?:document|doc|report|letter|memo)\b"
        ],
        extractor="document",
        examples=["delete the Q4 report", "remove my cover letter",
                  "trash the old proposal"],
        slots={"action": "delete", "name": "document name"},
    ),

    "document.export": Intent(
        id="document.export",
        op="document_export",
        domain="document",
        description="Export a document to PDF or another format",
        signals=["export the document", "export the report", "export as pdf",
                 "download the document", "save as pdf", "convert to pdf"],
        extractor="document",
        examples=["export the Q4 report as PDF", "download the proposal as Word",
                  "convert my resume to PDF"],
        slots={"action": "export", "name": "document name", "format": "pdf|docx|txt"},
    ),

    # ── Spreadsheet extended ───────────────────────────────────────────────

    "spreadsheet.delete": Intent(
        id="spreadsheet.delete",
        op="spreadsheet_delete",
        domain="spreadsheet",
        description="Delete a spreadsheet",
        signals=["delete the spreadsheet", "remove the spreadsheet",
                 "trash the spreadsheet", "delete my spreadsheet"],
        patterns=[r"\b(?:delete|remove|trash)\s+(?:the\s+)?[\w\s]+\s+spreadsheet\b"],
        extractor="spreadsheet",
        examples=["delete the budget spreadsheet", "remove the expense tracker"],
        slots={"action": "delete", "name": "spreadsheet name"},
    ),

    "spreadsheet.export": Intent(
        id="spreadsheet.export",
        op="spreadsheet_export",
        domain="spreadsheet",
        description="Export a spreadsheet to CSV, Excel, or PDF",
        signals=["export the spreadsheet", "download the spreadsheet",
                 "export as csv", "export as excel", "save spreadsheet as"],
        extractor="spreadsheet",
        examples=["export the budget spreadsheet as CSV",
                  "download the tracker as Excel"],
        slots={"action": "export", "name": "spreadsheet name", "format": "csv|xlsx|pdf"},
    ),

    # ── Presentation extended ──────────────────────────────────────────────

    "presentation.delete": Intent(
        id="presentation.delete",
        op="presentation_delete",
        domain="presentation",
        description="Delete a presentation",
        signals=["delete the presentation", "remove the presentation",
                 "trash the deck", "delete the deck", "delete the slides"],
        patterns=[
            r"\b(?:delete|remove|trash)\s+(?:the\s+)?[\w\s]+\s+(?:presentation|deck|slides)\b"
        ],
        extractor="presentation",
        examples=["delete the Q4 presentation", "remove the sales deck"],
        slots={"action": "delete", "name": "presentation name"},
    ),

    "presentation.export": Intent(
        id="presentation.export",
        op="presentation_export",
        domain="presentation",
        description="Export a presentation to PDF or PowerPoint",
        signals=["export the presentation", "export the deck", "download the deck",
                 "export as powerpoint", "save the deck as"],
        extractor="presentation",
        examples=["export the Q4 deck as PDF",
                  "download the sales presentation as PowerPoint"],
        slots={"action": "export", "name": "presentation name", "format": "pdf|pptx"},
    ),

    # ── Code extended ──────────────────────────────────────────────────────

    "code.review": Intent(
        id="code.review",
        op="code_review",
        domain="code",
        description="Review code for quality, bugs, or best practices",
        signals=["review the code", "audit the code", "code review", "check this code",
                 "lint this", "critique the code", "review this function"],
        patterns=[
            r"\b(?:review|audit|lint|critique)\s+(?:this\s+)?(?:code|script|function|class|module)\b"
        ],
        blockers=["explain", "fix", "debug", "run"],
        extractor="code",
        examples=["review this Python function", "audit my auth code",
                  "do a code review of the module"],
        slots={"action": "review", "language": "language"},
    ),

    "code.test": Intent(
        id="code.test",
        op="code_test",
        domain="code",
        description="Write unit tests or run the test suite for a piece of code",
        signals=["write tests", "write unit tests", "add tests", "test this",
                 "run the tests", "generate tests", "unit test this"],
        patterns=[
            r"\b(?:write|add|generate|create)\s+(?:unit\s+)?tests?\s+(?:for|to)\b",
            r"\b(?:run|execute)\s+(?:the\s+)?tests?\b",
        ],
        extractor="code",
        examples=["write unit tests for this function",
                  "add tests to the auth module", "run the tests"],
        slots={"action": "test", "language": "language", "name": "what to test"},
    ),

    # ── Calendar extended ──────────────────────────────────────────────────

    "calendar.reschedule": Intent(
        id="calendar.reschedule",
        op="calendar_reschedule",
        domain="calendar",
        description="Reschedule or move an existing calendar event",
        signals=["reschedule", "move the meeting", "push the meeting",
                 "change the time", "move the appointment", "postpone the meeting",
                 "shift the call", "change the meeting to"],
        patterns=[
            r"\b(?:reschedule|move|push|postpone)\s+(?:the\s+)?(?:meeting|event|appointment|call)\b"
        ],
        blockers=["cancel"],
        extractor="calendar",
        examples=["reschedule the Monday meeting to Wednesday",
                  "move the dentist appointment to next week",
                  "push the call to 3pm"],
        slots={"action": "reschedule", "title": "event title", "date": "new date/time"},
    ),

    "calendar.rsvp": Intent(
        id="calendar.rsvp",
        op="calendar_rsvp",
        domain="calendar",
        description="Accept, decline, or mark tentative for an event invitation",
        signals=["rsvp", "accept the invite", "decline the invite", "accept the meeting",
                 "decline the meeting", "not going", "can't make it", "will attend",
                 "i'll be there", "accept the invitation"],
        extractor="calendar_rsvp",
        examples=["RSVP yes to the team dinner", "decline the Monday meeting invite",
                  "accept Sarah's calendar invite", "can't make it to the standup"],
        slots={"response": "accept|decline|tentative", "event": "event name"},
    ),
}


# ─── Classifier ───────────────────────────────────────────────────────────────

def _has_signal(intent: Intent, lower: str) -> bool:
    """Return True if any signal word/phrase is present, or if a hard pattern matches.

    Uses word-boundary matching for all signals to avoid false substring hits
    (e.g. "dow" inside "download", "jot" inside "jotter").
    Multi-word phrases are also word-boundary anchored on each edge.
    """
    for signal in intent.signals:
        # Build a pattern that respects word boundaries on both sides
        if re.search(r"\b" + re.escape(signal) + r"\b", lower):
            return True
    if intent.patterns:
        for pat in intent.patterns:
            if re.search(pat, lower, re.IGNORECASE):
                return True
    return False


def _has_blocker(intent: Intent, lower: str) -> bool:
    """Return True if any blocker phrase is present (intent should be skipped)."""
    return any(b in lower for b in intent.blockers)


def classify(text: str) -> IntentMatch | None:
    """
    Classify a natural-language input string and return the best IntentMatch,
    or None if no intent matches.

    The classifier iterates TAXONOMY in insertion order (priority order).
    For each intent it:
      1. Checks blocker phrases — skip if any present.
      2. Checks signal words / hard patterns — skip if none match.
      3. Runs the extractor function — skip if it returns None.
      4. Returns the first successful IntentMatch.
    """
    if not text or not text.strip():
        return None

    raw = _normalize(text)
    lower = raw.lower()
    # Strip URLs before signal matching so words inside URLs (e.g. "news" in
    # "news.ycombinator.com") don't accidentally trigger unrelated intents.
    lower_no_url = re.sub(r"https?://\S+", "", lower).strip()

    for intent in TAXONOMY.values():
        # Skip if a blocker phrase is present
        if _has_blocker(intent, lower_no_url):
            continue
        # Skip if no signal / pattern matched (use URL-stripped lower for signals
        # so words inside URLs don't accidentally trigger unrelated intents)
        if not _has_signal(intent, lower_no_url):
            # Intent with no signals and no patterns can't be activated
            if not intent.signals and not intent.patterns:
                continue
            # Intents that rely purely on patterns have been checked above
            continue
        # Run the extractor
        extractor_fn = _EXTRACTORS.get(intent.extractor)
        if extractor_fn is None:
            continue
        payload = extractor_fn(raw, lower)
        if payload is None:
            continue
        return IntentMatch(intent=intent, payload=payload)

    return None


# ─── Backward-compat shim ─────────────────────────────────────────────────────

def parse_semantic_command(text: str) -> "dict[str, Any] | None":
    """Drop-in replacement for the old monolithic parse_semantic_command in main.py."""
    match = classify(text)
    return match.to_op_dict() if match else None


# ─── Nous integration ─────────────────────────────────────────────────────────
#
# Set NOUS_URL=http://localhost:7700 to enable LLM-backed classification.
# When set, classify_async() tries the Nous HTTP server first and falls back
# to the rule-based classify() if Nous is unavailable or returns no match.
#
# Build and start the Nous server with:
#   cd Nous/rust
#   cargo run --bin nous-server --features http-api -- --model phi4-mini

NOUS_URL: str = os.getenv("NOUS_URL", "")


def _taxonomy_for_nous() -> list[dict[str, Any]]:
    """Serialize TAXONOMY into the compact form nous-server expects."""
    return [
        {
            "id": intent.id,
            "op": intent.op,
            "description": intent.description,
            "examples": intent.examples[:3],  # keep payload small
            "slots": intent.slots,
        }
        for intent in TAXONOMY.values()
    ]


async def classify_async(text: str) -> IntentMatch | None:
    """
    Async variant of classify() that routes through Nous when available.

    Resolution order:
      1. Nous LLM (if NOUS_URL is set and server responds within 2 s)
      2. Rule-based classify() as fallback

    Returns an IntentMatch or None.
    """
    if NOUS_URL:
        try:
            import httpx  # already a project dependency (main.py uses it)

            payload = {"text": text, "intents": _taxonomy_for_nous()}
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(f"{NOUS_URL}/api/classify", json=payload)
            if resp.status_code == 200:
                data = resp.json()
                op = data.get("op")
                slots = data.get("slots") or {}
                confidence = float(data.get("confidence", 0.0))
                # Only accept if Nous is confident and the op maps to a known intent
                if op and confidence >= 0.6:
                    intent = next(
                        (i for i in TAXONOMY.values() if i.op == op), None
                    )
                    if intent:
                        return IntentMatch(intent=intent, payload=slots)
        except Exception:
            pass  # Nous down or timed out — fall through to rules

    # Rule-based fallback (always works, zero latency)
    return classify(text)


async def parse_semantic_command_async(text: str) -> "dict[str, Any] | None":
    """Async variant of parse_semantic_command; uses Nous when NOUS_URL is set."""
    match = await classify_async(text)
    return match.to_op_dict() if match else None
