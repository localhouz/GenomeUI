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


def _ext_location_status(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:in|at|near|around)\s+(.+?)(?:\s+right now|\s+currently)?$", lower)
    return {"city": m.group(1).strip() if m else ""}


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


def _ext_banking_balance(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(checking|savings|credit|investment|retirement)\b", lower)
    return {"account": m.group(1) if m else ""}


def _ext_banking_transactions(_raw: str, lower: str) -> dict[str, Any] | None:
    m_days = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", lower)
    m_acct = re.search(r"\b(checking|savings|credit)\b", lower)
    return {"days": int(m_days.group(1)) if m_days else 30,
            "account": m_acct.group(1) if m_acct else ""}


def _ext_social_feed(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(twitter|x|instagram|facebook|linkedin|tiktok|reddit)\b", lower)
    return {"platform": m.group(1) if m else ""}


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
        (r"\b(?:share|send\s+to|collaborate)\b", "share"),
        (r"\b(?:rename|move)\b", "rename"),
        (r"\b(?:template|from\s+(?:a\s+)?template)\b", "template"),
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
        (r"\b(?:share|send\s+to|collaborate)\b", "share"),
        (r"\b(?:rename)\b", "rename"),
        (r"\b(?:formula|function|sum|average|vlookup|countif)\b", "formula"),
        (r"\b(?:chart|graph|plot|visualize|bar\s+chart|pie\s+chart|line\s+chart)\b", "chart"),
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
        (r"\b(?:share|send\s+to|collaborate)\b", "share"),
        (r"\b(?:rename)\b", "rename"),
        (r"\b(?:template|from\s+(?:a\s+)?template)\b", "template"),
        (r"\b(?:speaker\s+notes?|presenter\s+notes?|add\s+notes?\s+to\s+slide)\b", "speaker_notes"),
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
        (r"\b(?:optimize|improve\s+performance|speed\s+up|make\s+(?:it\s+)?faster)\b", "optimize"),
        (r"\b(?:refactor|restructure|reorganize|clean\s+up)\b", "refactor"),
        (r"\b(?:edit|update|modify|change)\b", "edit"),
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
        (r"\b(?:unsubscribe)\b", "unsubscribe"),
        (r"\b(?:label|tag|categorize|move\s+to\s+folder|file\s+(?:this|the)\s+email)\b", "label"),
        (r"\b(?:snooze\s+(?:this\s+|the\s+)?(?:email|message)|remind\s+me\s+about\s+(?:this\s+)?email)\b", "snooze"),
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

# ── Weather ───────────────────────────────────────────────────────────────────

def _ext_weather_hourly(raw: str, lower: str) -> dict[str, Any] | None:
    return {"location": _extract_location(raw, lower) or "__current__", "window": "hourly"}


def _ext_weather_radar(raw: str, lower: str) -> dict[str, Any] | None:
    return {"location": _extract_location(raw, lower) or "__current__", "view": "radar"}


def _ext_weather_air_quality(raw: str, lower: str) -> dict[str, Any] | None:
    return {"location": _extract_location(raw, lower) or "__current__"}


def _ext_weather_astronomy(raw: str, lower: str) -> dict[str, Any] | None:
    if "sunrise" in lower:
        query = "sunrise"
    elif "sunset" in lower:
        query = "sunset"
    elif "moon" in lower:
        query = "moon"
    else:
        query = "astronomy"
    return {"location": _extract_location(raw, lower) or "__current__", "query": query}


# ── Shopping ──────────────────────────────────────────────────────────────────

def _ext_shopping_cart(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_shopping_orders(raw: str, lower: str) -> dict[str, Any] | None:
    order_m = re.search(r"order\s+(?:#|number\s+)?(\w+)", lower)
    return {"orderId": order_m.group(1) if order_m else None}


def _ext_shopping_compare(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"compare\s+(.+?)\s+(?:and|vs\.?|versus|with)\s+(.+?)$", lower)
    if m:
        return {"item1": m.group(1).strip(), "item2": m.group(2).strip()}
    m2 = re.search(r"(.+?)\s+(?:vs\.?|versus)\s+(.+?)$", lower)
    if m2:
        return {"item1": m2.group(1).strip(), "item2": m2.group(2).strip()}
    return {"query": raw.strip()}


def _ext_shopping_recommendations(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"category": _infer_shopping_category(lower)}


# ── News ──────────────────────────────────────────────────────────────────────

def _ext_news_trending(_raw: str, lower: str) -> dict[str, Any] | None:
    platform_m = re.search(r"\b(twitter|x|instagram|tiktok|reddit|facebook)\b", lower)
    return {"platform": platform_m.group(1) if platform_m else None}


def _ext_news_by_source(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:from|on|at|via)\s+(.+?)(?:\s+news)?$", lower
    )
    source = m.group(1).strip() if m else ""
    return {"source": source, "query": (source + " news").strip() if source else "news"}


# ── Contacts ──────────────────────────────────────────────────────────────────

def _ext_contacts_list(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:search|find|show)\s+(?:contacts?\s+)?(?:named|called|with)?\s+(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}


def _ext_contacts_edit(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:update|edit|change|modify)\s+(.+?)(?:'s)?\s+(?:contact|number|email|address)?",
        lower,
    )
    return {"name": m.group(1).strip() if m else ""}


def _ext_contacts_delete(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:delete|remove)\s+(.+?)\s+(?:from\s+)?(?:my\s+)?(?:contacts?)?$", lower
    )
    if not m:
        return None
    name = m.group(1).strip()
    return {"name": name} if name and len(name) >= 2 else None


def _ext_contacts_favorite(raw: str, lower: str) -> dict[str, Any] | None:
    action = "unfavorite" if re.search(
        r"\b(?:unfavorite|unstar|remove\s+from\s+favorites)\b", lower
    ) else "favorite"
    m = re.search(r"(?:favorite|star|unfavorite|unstar)\s+(.+?)(?:'s)?$", lower)
    return {"action": action, "name": m.group(1).strip() if m else ""}


# ── Social ────────────────────────────────────────────────────────────────────

def _ext_social_react(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:love|heart)\b", lower):
        reaction = "love"
    elif re.search(r"\b(?:laugh|haha|lol)\b", lower):
        reaction = "haha"
    elif re.search(r"\b(?:wow|whoa|amazing)\b", lower):
        reaction = "wow"
    elif re.search(r"\b(?:sad|cry)\b", lower):
        reaction = "sad"
    else:
        reaction = "like"
    return {"reaction": reaction}


def _ext_social_comment(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:comment|reply)\s+(?:on\s+.+?\s+)?(?:that|this|saying|:)\s*(.+)$",
        raw, re.IGNORECASE,
    )
    return {"text": m.group(1).strip() if m else ""}


def _ext_social_follow_person(raw: str, lower: str) -> dict[str, Any] | None:
    action = "unfollow" if re.search(r"\bunfollow\b", lower) else "follow"
    m = re.search(r"(?:follow|unfollow)\s+(?:@?)(.+?)(?:\s+on\s+\w+)?$", lower)
    name = m.group(1).strip() if m else ""
    return {"action": action, "handle": name} if name else None


def _ext_social_notifications(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(twitter|x|instagram|facebook|linkedin|tiktok|reddit)\b", lower)
    return {"platform": m.group(1) if m else ""}


def _ext_social_trending(_raw: str, lower: str) -> dict[str, Any] | None:
    platform_m = re.search(r"\b(twitter|x|instagram|tiktok|reddit)\b", lower)
    return {"platform": platform_m.group(1) if platform_m else None}


# ── Terminal ──────────────────────────────────────────────────────────────────

def _ext_terminal_ssh(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:ssh\s+(?:into\s+|to\s+)?|connect\s+to\s+(?:server\s+)?)"
        r"(?:([a-z_][a-z0-9_-]*)@)?([a-z0-9][a-z0-9.\-]+[a-z0-9])",
        lower,
    )
    if not m:
        return None
    return {"user": m.group(1) or None, "host": m.group(2)}


def _ext_terminal_env(raw: str, lower: str) -> dict[str, Any] | None:
    action = "set" if re.search(r"\b(?:set|add|export|update)\b", lower) else "list"
    m = re.search(r"(?:set|export|add)\s+([A-Z_][A-Z0-9_]*)\s*=?\s*(.+)?$", raw)
    var = m.group(1) if m else None
    value = m.group(2).strip() if m and m.group(2) else None
    return {"action": action, "var": var, "value": value}


def _ext_terminal_output(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:output|result|log)\s+(?:of|from|for)\s+(.+)", lower)
    return {"command": m.group(1).strip() if m else ""}


# ── Banking ───────────────────────────────────────────────────────────────────

def _ext_banking_history(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:today|this\s+day)\b", lower):
        window = "today"
    elif re.search(r"\b(?:this\s+week|7\s+days?|past\s+week)\b", lower):
        window = "week"
    elif re.search(r"\b(?:this\s+year|ytd|year\s+to\s+date)\b", lower):
        window = "year"
    else:
        window = "month"
    cat_m = re.search(
        r"\b(food|transport|shopping|travel|entertainment|health|utilities)\b", lower
    )
    return {"window": window, "category": cat_m.group(0) if cat_m else None}


def _ext_banking_statement(_raw: str, lower: str) -> dict[str, Any] | None:
    month_m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\b",
        lower,
    )
    year_m = re.search(r"\b(20\d\d)\b", lower)
    return {
        "month": month_m.group(1) if month_m else None,
        "year": year_m.group(1) if year_m else None,
    }


# ── Finance ───────────────────────────────────────────────────────────────────

def _ext_finance_alert(raw: str, lower: str) -> dict[str, Any] | None:
    mm = _MONEY_RE.search(lower)
    amount = None
    if mm:
        try:
            amount = float((mm.group(1) or mm.group(2) or "0").replace(",", ""))
        except ValueError:
            pass
    symbol_m = re.search(r"\b([A-Z]{1,5})\b", raw)
    direction = "below" if re.search(
        r"\b(?:below|under|drops?|falls?|dips?)\b", lower
    ) else "above"
    return {
        "symbol": symbol_m.group(1) if symbol_m else None,
        "threshold": amount,
        "direction": direction,
    }


def _ext_finance_news(raw: str, _lower: str) -> dict[str, Any] | None:
    symbol_m = re.search(r"\b([A-Z]{1,5})\b", raw)
    return {
        "symbol": symbol_m.group(1) if symbol_m else None,
        "query": raw.strip() + " news",
    }


# ── Reminders ─────────────────────────────────────────────────────────────────

_RECURRENCE_PATS: list[tuple[str, str | None]] = [
    (r"\bevery\s+day\b|daily\b", "daily"),
    (r"\bevery\s+(?:other\s+)?week\b|weekly\b", "weekly"),
    (r"\bevery\s+month\b|monthly\b", "monthly"),
    (r"\bevery\s+year\b|annually\b|yearly\b", "yearly"),
    (r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", None),
    (r"\bevery\s+weekday\b|on\s+weekdays\b", "weekdays"),
    (r"\bevery\s+weekend\b", "weekends"),
]


def _extract_recurrence(lower: str) -> str | None:
    for pat, rec in _RECURRENCE_PATS:
        m = re.search(pat, lower)
        if m:
            return rec if rec else m.group(1)
    return None


def _ext_reminder_recurring(raw: str, lower: str) -> dict[str, Any] | None:
    recurrence = _extract_recurrence(lower)
    if not recurrence:
        return None
    m_body = re.search(r"^(?:remind\s+me\s+(?:to\s+|about\s+)?)?(.+?)\s+every\b", lower)
    body = m_body.group(1).strip() if m_body else raw.strip()
    return {"text": body, "recurrence": recurrence}


# ── Tasks ─────────────────────────────────────────────────────────────────────

def _ext_task_priority(raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:low|minor|not\s+urgent|whenever|backlog)\b", lower):
        level = "low"
    elif re.search(r"\b(?:medium|normal|moderate)\b", lower):
        level = "medium"
    else:
        level = "high"
    m = re.search(
        r"(?:set|mark|make|change)\s+(?:the\s+)?(.+?)\s+(?:task\s+)?(?:as\s+)?(?:high|low|medium|urgent|priority)",
        lower,
    )
    if not m:
        m = re.search(
            r"(?:priority|prioritize)\s+(?:the\s+)?(.+?)(?:\s+task)?$", lower
        )
    selector = m.group(1).strip() if m else ""
    return {"selector": selector, "priority": level}


def _ext_task_due(raw: str, lower: str) -> dict[str, Any] | None:
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
        wday_m = re.search(
            r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lower
        )
        if wday_m:
            date_hint = wday_m.group(1)
    m = re.search(
        r"(?:set|add|update|change)\s+(?:the\s+)?(.+?)\s+(?:task\s+)?(?:due|deadline)", lower
    )
    if not m:
        m = re.search(r"(?:due|deadline)\s+(?:for\s+)?(?:the\s+)?(.+?)(?:\s+(?:is|to))", lower)
    return {"selector": m.group(1).strip() if m else "", "date": date_hint}


# ── Notes ─────────────────────────────────────────────────────────────────────

def _ext_note_edit(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:edit|update|change|modify|rewrite|append\s+to)\s+"
        r"(?:(?:the|my)\s+)?(?:note\s+)?(?:about\s+|called\s+|titled\s+)?(.+?)(?:\s+note)?$",
        lower,
    )
    name = m.group(1).strip() if m else ""
    return {"name": name} if name else None


def _ext_note_tag(raw: str, lower: str) -> dict[str, Any] | None:
    action = "remove_tag" if re.search(r"\b(?:remove|untag)\b", lower) else "tag"
    tag_m = re.search(r"(?:tag|label)\s+(?:it\s+)?(?:as\s+|with\s+)(.+?)(?:\s+tag)?$", lower)
    note_m = re.search(r"(?:note|the)\s+(?:called\s+|about\s+|titled\s+)?(.+?)\s+(?:with|as|tag)", lower)
    return {
        "action": action,
        "tag": tag_m.group(1).strip() if tag_m else "",
        "name": note_m.group(1).strip() if note_m else "",
    }


# ── Calendar ──────────────────────────────────────────────────────────────────

def _ext_calendar_invite(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"invite\s+(.+?)\s+(?:to|for)\b", lower)
    invitees = m.group(1).strip() if m else ""
    event_m = re.search(r"(?:to|for)\s+(.+?)$", lower)
    event = event_m.group(1).strip() if event_m else ""
    return {"invitees": invitees, "event": event}


def _ext_calendar_availability(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:availability|available|free\s+(?:time|slots?))\s+(?:of\s+|for\s+)?(.+?)"
        r"(?:\s+(?:on|this|next|for|tomorrow|today))?$",
        lower,
    )
    person = m.group(1).strip() if m else ""
    date_hint = None
    if re.search(r"\btoday\b", lower):
        date_hint = "today"
    elif re.search(r"\btomorrow\b", lower):
        date_hint = "tomorrow"
    elif re.search(r"\bthis\s+week\b", lower):
        date_hint = "this_week"
    elif re.search(r"\bnext\s+week\b", lower):
        date_hint = "next_week"
    return {"person": person, "date": date_hint}


def _ext_calendar_recurring(raw: str, lower: str) -> dict[str, Any] | None:
    recurrence = _extract_recurrence(lower)
    if not recurrence:
        return None
    base = _ext_calendar(raw, lower) or {}
    base["recurrence"] = recurrence
    base["action"] = "recurring"
    return base


def _ext_weather_alert(raw: str, lower: str) -> dict[str, Any] | None:
    return {"location": _extract_location(raw, lower) or "__current__", "type": "alert"}


# ── Google Calendar ────────────────────────────────────────────────────────────

def _ext_gcal_list(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:next|upcoming|this week)?\s*(\d+)?\s*(?:days?)?", lower)
    days = int(m.group(1)) if m and m.group(1) else 7
    return {"days": days}

def _ext_gcal_create(raw: str, lower: str) -> dict | None:
    base = _ext_calendar(raw, lower) or {}
    return {"summary": base.get("title", ""), "start": base.get("date", ""), "end": base.get("endDate", ""), "location": base.get("location", "")}

def _ext_gcal_update(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:update|reschedule|change)\s+(?:event\s+)?(.+?)(?:\s+to\s+(.+))?$", lower)
    return {"summary": m.group(1).strip() if m else "", "eventId": ""}

def _ext_gcal_delete(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:delete|cancel|remove)\s+(?:event\s+)?(.+)", lower)
    return {"summary": m.group(1).strip() if m else "", "eventId": ""}


# ── Google Drive ───────────────────────────────────────────────────────────────

def _ext_gdrive_search(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:find|search|open|show)\s+(?:my\s+)?(?:google\s+drive\s+)?(?:file|doc|sheet|folder)?\s+(?:called|named|about)?\s*(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_gdrive_create(raw: str, lower: str) -> dict | None:
    type_map = {"doc": "application/vnd.google-apps.document",
                "document": "application/vnd.google-apps.document",
                "sheet": "application/vnd.google-apps.spreadsheet",
                "spreadsheet": "application/vnd.google-apps.spreadsheet",
                "slide": "application/vnd.google-apps.presentation",
                "presentation": "application/vnd.google-apps.presentation",
                "folder": "application/vnd.google-apps.folder"}
    detected = next((v for k, v in type_map.items() if k in lower), "")
    m = re.search(r"(?:create|new|make)\s+(?:a\s+)?(?:google\s+)?(?:drive\s+)?(?:\w+\s+)?(?:called|named)?\s*(.+)?", lower)
    return {"name": m.group(1).strip() if m and m.group(1) else "Untitled", "mimeType": detected}

def _ext_gdrive_share(raw: str, lower: str) -> dict | None:
    m = re.search(r"share\s+(.+?)\s+with\s+(.+)", lower)
    return {"query": m.group(1).strip() if m else "", "email": m.group(2).strip() if m else ""}


def _ext_location_directions(raw: str, lower: str) -> dict[str, Any] | None:
    # "directions to X", "navigate to X", "how do I get to X", "take me to X"
    m = re.search(
        r"(?:directions?\s+to|navigate\s+to|take\s+me\s+to|get\s+to|"
        r"how\s+(?:do\s+i|to)\s+get\s+to|way\s+to|route\s+to)\s+(.+?)$",
        lower,
    )
    destination = m.group(1).strip() if m else ""
    origin_m = re.search(r"\bfrom\s+(.+?)\s+to\b", lower)
    origin = origin_m.group(1).strip() if origin_m else "__current__"
    mode_m = re.search(r"\b(walking|walk|driving|drive|cycling|bike|transit|bus|train)\b", lower)
    mode_map = {"walk": "walking", "drive": "driving", "bike": "cycling",
                "bus": "transit", "train": "transit"}
    raw_mode = mode_m.group(1) if mode_m else None
    mode = mode_map.get(raw_mode, raw_mode) if raw_mode else "driving"
    return {"destination": destination, "origin": origin, "mode": mode} if destination else None


def _ext_location_distance(raw: str, lower: str) -> dict[str, Any] | None:
    # "how far is X from Y", "distance from X to Y", "how far to X"
    m = re.search(r"how\s+far\s+(?:is\s+)?(.+?)\s+from\s+(.+?)$", lower)
    if m:
        return {"from": m.group(2).strip(), "to": m.group(1).strip()}
    m2 = re.search(r"distance\s+(?:from\s+)?(.+?)\s+to\s+(.+?)$", lower)
    if m2:
        return {"from": m2.group(1).strip(), "to": m2.group(2).strip()}
    m3 = re.search(r"how\s+far\s+(?:is\s+it\s+)?to\s+(.+?)$", lower)
    if m3:
        return {"from": "__current__", "to": m3.group(1).strip()}
    return None


def _ext_location_traffic(raw: str, lower: str) -> dict[str, Any] | None:
    route_m = re.search(
        r"(?:traffic\s+(?:on|to|toward|for|heading\s+to)|"
        r"how(?:'s|\s+is)\s+traffic\s+(?:on|to|for))\s+(.+?)$",
        lower,
    )
    route = route_m.group(1).strip() if route_m else ""
    return {"route": route, "location": _extract_location(raw, lower) or "__current__"}


def _ext_location_share(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_location_saved(raw: str, lower: str) -> dict[str, Any] | None:
    action = "list"
    if re.search(r"\b(?:add|save|set|update)\b", lower):
        action = "add"
    elif re.search(r"\b(?:remove|delete|clear)\b", lower):
        action = "remove"
    label_m = re.search(r"\b(?:set|save|add|update)\s+(?:my\s+)?(\w+)\s+(?:as|to|address)", lower)
    label = label_m.group(1) if label_m else ""
    addr_m = re.search(r"(?:as|to)\s+(.+?)$", lower)
    address = addr_m.group(1).strip() if addr_m else ""
    return {"action": action, "label": label, "address": address}


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


def _ext_finance_portfolio(_raw: str, lower: str) -> dict[str, Any] | None:
    m_ticker = re.search(r"\b([A-Z]{1,5})\b", _raw)
    m_days = re.search(r"\b(?:last|past)\s+(\d+)\s+days?\b", lower)
    return {"ticker": m_ticker.group(1) if m_ticker else "",
            "days": int(m_days.group(1)) if m_days else 30}


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


def _ext_social_profile(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"@(\w+)", lower) or re.search(r"(?:profile|account|page)\s+(?:of|for|about)\s+@?(\S+)", lower)
    return {"handle": m.group(1).strip("@") if m else ""}


def _ext_terminal_history(_raw: str, lower: str) -> dict[str, Any] | None:
    m_limit = re.search(r"\b(?:last|recent|past)\s+(\d+)\b", lower)
    m_filter = re.search(r"(?:with|containing|matching|for)\s+(.+)$", lower)
    return {"limit": int(m_limit.group(1)) if m_limit else 20,
            "filter": m_filter.group(1).strip() if m_filter else ""}


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


# ─── Wave-3 extractors ────────────────────────────────────────────────────────

# ── Shared helpers ─────────────────────────────────────────────────────────────

_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b", re.IGNORECASE
)


def _extract_time_str(lower: str) -> str | None:
    m = _TIME_RE.search(lower)
    if not m:
        return None
    h, mn, ampm = m.group(1), m.group(2) or "00", (m.group(3) or "").lower().replace(".", "")
    return f"{h}:{mn} {ampm}".strip()


def _extract_music_platform(lower: str) -> str | None:
    for p in ("spotify", "apple music", "youtube music", "tidal", "amazon music",
              "deezer", "pandora", "soundcloud"):
        if p in lower:
            return p.replace(" ", "_")
    return None


def _extract_msg_platform(lower: str) -> str | None:
    for p in ("imessage", "whatsapp", "signal", "telegram", "messenger",
              "instagram", "snapchat", "discord", "slack"):
        if p in lower:
            return p
    return None


def _extract_payment_platform(lower: str) -> str | None:
    for p in ("venmo", "zelle", "paypal", "cash app", "cashapp", "apple pay", "google pay"):
        if p in lower:
            return p.replace(" ", "_")
    return None


def _extract_streaming_platform(lower: str) -> str | None:
    for p in ("netflix", "hulu", "disney", "hbo", "amazon prime", "youtube",
              "peacock", "paramount", "apple tv"):
        if p in lower:
            return p.replace(" ", "_")
    return None


def _extract_person(lower: str) -> str:
    m = re.search(
        r"(?:to|from|with|for)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)", lower
    )
    return m.group(1).strip() if m else ""


def _extract_amount(lower: str) -> float | None:
    mm = _MONEY_RE.search(lower)
    if not mm:
        return None
    try:
        return float((mm.group(1) or mm.group(2) or "0").replace(",", ""))
    except ValueError:
        return None


# ── Messaging ──────────────────────────────────────────────────────────────────

def _ext_messaging_send(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:text|message|send\s+(?:a\s+)?(?:text|message|msg))\s+"
        r"(?:to\s+)?([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)",
        lower,
    )
    recipient = m.group(1).strip() if m else _extract_person(lower)
    body_m = re.search(r"(?:saying|that|:)\s*(.+)$", raw, re.IGNORECASE)
    return {
        "recipient": recipient,
        "body": body_m.group(1).strip() if body_m else "",
        "platform": _extract_msg_platform(lower),
    }


def _ext_messaging_read(_raw: str, lower: str) -> dict[str, Any] | None:
    person_m = re.search(
        r"(?:from|with|messages?\s+from)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)", lower
    )
    return {
        "contact": person_m.group(1).strip() if person_m else "",
        "platform": _extract_msg_platform(lower),
    }


def _ext_messaging_reply(raw: str, lower: str) -> dict[str, Any] | None:
    body_m = re.search(r"(?:reply|respond)\s+(?:to\s+.+?\s+)?(?:saying|with|:)\s*(.+)$",
                       raw, re.IGNORECASE)
    return {
        "body": body_m.group(1).strip() if body_m else "",
        "platform": _extract_msg_platform(lower),
    }


def _ext_messaging_delete(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:delete|remove|clear)\s+(?:the\s+)?(?:conversation|messages?|chat)\s+"
        r"(?:with|from\s+)?(.+?)(?:\s+messages?)?$",
        lower,
    )
    return {"contact": m.group(1).strip() if m else ""}


def _ext_messaging_search(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:search|find)\s+(?:messages?\s+)?(?:for\s+|about\s+)?(.+?)$", lower)
    return {"query": m.group(1).strip() if m else raw.strip()}


def _ext_messaging_group_create(raw: str, lower: str) -> dict[str, Any] | None:
    name_m = re.search(r"(?:called|named)\s+[\"']?(.+?)[\"']?$", lower)
    return {
        "name": name_m.group(1).strip() if name_m else "",
        "platform": _extract_msg_platform(lower),
    }


def _ext_messaging_group_add(raw: str, lower: str) -> dict[str, Any] | None:
    person_m = re.search(r"add\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+to", lower)
    group_m = re.search(r"to\s+(?:the\s+)?(.+?)(?:\s+group|chat)?$", lower)
    return {
        "person": person_m.group(1).strip() if person_m else "",
        "group": group_m.group(1).strip() if group_m else "",
    }


def _ext_messaging_react_msg(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:love|heart)\b", lower):
        reaction = "love"
    elif re.search(r"\b(?:laugh|haha|lol)\b", lower):
        reaction = "haha"
    elif re.search(r"\b(?:thumbs\s+up|like)\b", lower):
        reaction = "like"
    elif re.search(r"\b(?:thumbs\s+down|dislike)\b", lower):
        reaction = "dislike"
    else:
        reaction = "like"
    return {"reaction": reaction}


def _ext_messaging_forward(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"forward\s+(?:this\s+)?(?:message\s+)?to\s+(.+?)$", lower)
    return {"recipient": m.group(1).strip() if m else ""}


def _ext_messaging_block(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:block|mute)\s+(?:messages?\s+from\s+)?(.+?)$", lower)
    return {"contact": m.group(1).strip() if m else ""}


def _ext_messaging_schedule(raw: str, lower: str) -> dict[str, Any] | None:
    person_m = re.search(
        r"(?:message|text)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+(?:saying|at|tomorrow)",
        lower,
    )
    body_m = re.search(r"(?:saying|:)\s*(.+?)(?:\s+at\b|$)", raw, re.IGNORECASE)
    return {
        "recipient": person_m.group(1).strip() if person_m else "",
        "body": body_m.group(1).strip() if body_m else "",
        "time": _extract_time_str(lower),
    }


# ── Music ──────────────────────────────────────────────────────────────────────

def _ext_music_play(raw: str, lower: str) -> dict[str, Any] | None:
    platform = _extract_music_platform(lower)
    # artist
    artist_m = re.search(r"(?:by|from|artist)\s+([A-Za-z][^\s]+(?:\s+[A-Za-z][^\s]+){0,3})",
                         lower)
    # album
    album_m = re.search(r"(?:album|the\s+album)\s+[\"']?(.+?)[\"']?(?:\s+by|\s+on|$)", lower)
    # playlist
    playlist_m = re.search(r"(?:playlist|mix)\s+[\"']?(.+?)[\"']?(?:\s+on|$)", lower)
    # song title — text before "by" keyword or after play/listen keywords
    title_m = re.search(
        r"(?:play|listen\s+to|put\s+on|queue\s+up)\s+[\"']?(.+?)[\"']?"
        r"(?:\s+by|\s+on\s+spotify|\s+playlist|$)",
        lower,
    )
    return {
        "query": title_m.group(1).strip() if title_m else raw.strip(),
        "artist": artist_m.group(1).strip() if artist_m else None,
        "album": album_m.group(1).strip() if album_m else None,
        "playlist": playlist_m.group(1).strip() if playlist_m else None,
        "platform": platform,
    }


def _ext_music_pause(_raw: str, lower: str) -> dict[str, Any] | None:
    action = "resume" if re.search(r"\b(?:resume|unpause|continue)\b", lower) else "pause"
    return {"action": action}


def _ext_music_skip(_raw: str, lower: str) -> dict[str, Any] | None:
    direction = "previous" if re.search(r"\b(?:previous|back|last\s+song|go\s+back)\b",
                                        lower) else "next"
    return {"direction": direction}


def _ext_music_volume(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:mute|silence)\b", lower):
        return {"action": "mute", "level": None}
    if re.search(r"\b(?:louder|turn\s+up|volume\s+up|increase\s+volume)\b", lower):
        return {"action": "up", "level": None}
    if re.search(r"\b(?:quieter|turn\s+down|volume\s+down|lower\s+the\s+volume)\b", lower):
        return {"action": "down", "level": None}
    pct_m = re.search(r"\b(\d{1,3})\s*(?:percent|%)\b", lower)
    return {"action": "set", "level": int(pct_m.group(1)) if pct_m else None}


def _ext_music_like(_raw: str, lower: str) -> dict[str, Any] | None:
    action = "unlike" if re.search(r"\b(?:unlike|dislike|remove\s+from\s+liked)\b",
                                   lower) else "like"
    return {"action": action}


def _ext_music_playlist_add(raw: str, lower: str) -> dict[str, Any] | None:
    playlist_m = re.search(r"(?:to|into)\s+(?:my\s+)?(?:playlist\s+)?[\"']?(.+?)[\"']?\s*$",
                           lower)
    song_m = re.search(r"add\s+[\"']?(.+?)[\"']?\s+(?:to|into)\b", lower)
    return {
        "song": song_m.group(1).strip() if song_m else "",
        "playlist": playlist_m.group(1).strip() if playlist_m else "",
    }


def _ext_music_playlist_create(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:called|named|titled)\s+[\"']?(.+?)[\"']?$", lower)
    if not m:
        m = re.search(r"playlist\s+(?:for|of|about)\s+(.+?)$", lower)
    return {"name": m.group(1).strip() if m else ""}


def _ext_music_queue(raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:show|view|open|what(?:'s|\s+is)\s+(?:in|on))\b", lower):
        return {"action": "view", "song": None}
    song_m = re.search(r"(?:add|queue|play\s+next)\s+[\"']?(.+?)[\"']?\s*$", lower)
    return {"action": "add", "song": song_m.group(1).strip() if song_m else ""}


def _ext_music_lyrics(_raw: str, lower: str) -> dict[str, Any] | None:
    song_m = re.search(r"lyrics\s+(?:for|to|of)\s+[\"']?(.+?)[\"']?$", lower)
    return {"song": song_m.group(1).strip() if song_m else ""}


def _ext_music_radio(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:radio|station)\s+(?:for|based\s+on|like)\s+[\"']?(.+?)[\"']?$", lower)
    if not m:
        m = re.search(r"(.+?)\s+radio$", lower)
    return {"seed": m.group(1).strip() if m else raw.strip(), "platform": _extract_music_platform(lower)}


def _ext_music_discover(_raw: str, lower: str) -> dict[str, Any] | None:
    genre_m = re.search(
        r"\b(jazz|rock|pop|hip.?hop|r&b|classical|country|electronic|indie|metal|"
        r"folk|blues|reggae|latin|k.?pop|edm|soul|punk|alternative)\b",
        lower,
    )
    mood_m = re.search(
        r"\b(chill|happy|sad|energetic|focus|workout|sleep|study|party|relax)\b", lower
    )
    return {
        "genre": genre_m.group(1) if genre_m else None,
        "mood": mood_m.group(1) if mood_m else None,
    }


def _ext_music_cast(raw: str, lower: str) -> dict[str, Any] | None:
    device_m = re.search(
        r"(?:to|on)\s+(?:the\s+)?(?:my\s+)?(.+?)\s*(?:speaker|sonos|echo|homepod|chromecast|tv)?$",
        lower,
    )
    return {"device": device_m.group(1).strip() if device_m else ""}


def _ext_music_sleep_timer(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(\d+)\s*(minute|min|hour|hr)s?", lower)
    if not m:
        return None
    unit = "hour" if "h" in m.group(2) else "minute"
    return {"duration": int(m.group(1)), "unit": unit}


# ── Phone / Calls ──────────────────────────────────────────────────────────────

def _ext_phone_call(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:call|dial|phone|ring)\s+(?:up\s+)?([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?|"
        r"\+?[\d\s\-().]{7,})",
        lower,
    )
    return {"contact": m.group(1).strip() if m else ""} if m else None


def _ext_phone_voicemail(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_phone_recent(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_phone_block(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:block|blacklist)\s+(?:calls?\s+from\s+)?(.+?)$", lower)
    return {"contact": m.group(1).strip() if m else ""} if m else None


def _ext_phone_conference(raw: str, lower: str) -> dict[str, Any] | None:
    members_m = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?", raw)
    return {"participants": members_m}


def _ext_phone_record(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


# ── Camera / Photos ────────────────────────────────────────────────────────────

def _ext_camera_photo(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_camera_video(_raw: str, lower: str) -> dict[str, Any] | None:
    duration_m = re.search(r"(\d+)\s*(?:second|minute|min|sec)s?\s*video", lower)
    return {"duration": int(duration_m.group(1)) if duration_m else None}


def _ext_camera_scan_qr(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_camera_scan_doc(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_camera_ocr(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_photos_search(raw: str, lower: str) -> dict[str, Any] | None:
    person_m = re.search(r"(?:of|with)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    date_m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|"
        r"october|november|december|20\d\d|last\s+\w+|this\s+\w+)\b",
        lower,
    )
    loc_m = re.search(r"(?:in|at|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    return {
        "person": person_m.group(1) if person_m else None,
        "date": date_m.group(1) if date_m else None,
        "location": loc_m.group(1) if loc_m else None,
        "query": raw.strip(),
    }


def _ext_photos_album(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:album|folder)\s+(?:called|named)?\s*[\"']?(.+?)[\"']?$", lower)
    return {"name": m.group(1).strip() if m else ""}


def _ext_photos_share(raw: str, lower: str) -> dict[str, Any] | None:
    recipient_m = re.search(r"(?:share|send)\s+(?:it|this|these|.+?)\s+(?:to|with)\s+(.+?)$",
                            lower)
    return {"recipient": recipient_m.group(1).strip() if recipient_m else ""}


def _ext_photos_edit(_raw: str, lower: str) -> dict[str, Any] | None:
    for edit_type in ("crop", "rotate", "filter", "brightness", "contrast", "saturation",
                      "portrait", "black and white", "background"):
        if edit_type in lower:
            return {"edit": edit_type}
    return {"edit": "general"}


# ── Smart Home ─────────────────────────────────────────────────────────────────

_ROOM_RE = re.compile(
    r"\b(bedroom|living\s+room|kitchen|bathroom|garage|office|basement|"
    r"hallway|dining\s+room|porch|backyard|front\s+yard|all\s+lights?|everywhere)\b"
)


def _ext_smarthome_lights(_raw: str, lower: str) -> dict[str, Any] | None:
    room_m = _ROOM_RE.search(lower)
    room = room_m.group(1) if room_m else None
    pct_m = re.search(r"\b(\d{1,3})\s*(?:percent|%)\b", lower)
    color_m = re.search(
        r"\b(red|blue|green|yellow|purple|pink|orange|white|warm|cool|daylight)\b", lower
    )
    if re.search(r"\b(?:turn\s+off|switch\s+off|lights?\s+off|off)\b", lower):
        action = "off"
    elif re.search(r"\b(?:turn\s+on|switch\s+on|lights?\s+on|on)\b", lower):
        action = "on"
    elif re.search(r"\b(?:dim|dimmer|lower|softer)\b", lower):
        action = "dim"
    elif color_m or re.search(r"\b(?:color|colour|hue)\b", lower):
        action = "color"
    else:
        action = "on"
    return {
        "action": action,
        "room": room,
        "level": int(pct_m.group(1)) if pct_m else None,
        "color": color_m.group(1) if color_m else None,
    }


def _ext_smarthome_thermostat(_raw: str, lower: str) -> dict[str, Any] | None:
    temp_m = re.search(r"\b(\d{2,3})\s*(?:degrees?|°)?\s*(?:fahrenheit|celsius|f|c)?\b", lower)
    mode_m = re.search(r"\b(heat|cool|auto|off|fan)\b", lower)
    action = "set" if temp_m or mode_m else "check"
    return {
        "action": action,
        "temperature": int(temp_m.group(1)) if temp_m else None,
        "mode": mode_m.group(1) if mode_m else None,
    }


def _ext_smarthome_lock(_raw: str, lower: str) -> dict[str, Any] | None:
    action = "unlock" if re.search(r"\b(?:unlock|open\s+the\s+door)\b", lower) else "lock"
    door_m = re.search(r"\b(front|back|garage|side|main)\s+(?:door|gate|lock)\b", lower)
    return {"action": action, "door": door_m.group(1) if door_m else "front"}


def _ext_smarthome_camera(raw: str, lower: str) -> dict[str, Any] | None:
    loc_m = re.search(r"\b(front\s+door|backyard|garage|porch|driveway|living\s+room|baby)\b",
                      lower)
    return {"camera": loc_m.group(1) if loc_m else ""}


def _ext_smarthome_appliance(raw: str, lower: str) -> dict[str, Any] | None:
    appliance_m = re.search(
        r"\b(dishwasher|washing\s+machine|washer|dryer|oven|microwave|"
        r"coffee\s+maker|robot\s+vacuum|roomba|tv|fan|air\s+purifier)\b",
        lower,
    )
    action = "off" if re.search(r"\b(?:off|stop|pause|cancel)\b", lower) else "on"
    return {
        "appliance": appliance_m.group(1) if appliance_m else "",
        "action": action,
    }


def _ext_smarthome_scene(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:activate|run|start|set|turn\s+on)\s+(?:the\s+)?[\"']?(.+?)[\"']?"
        r"\s+(?:scene|mode|routine)?$",
        lower,
    )
    if not m:
        m = re.search(r"[\"']?(.+?)[\"']?\s+(?:scene|mode|routine)", lower)
    return {"scene": m.group(1).strip() if m else ""} if m else {}


def _ext_smarthome_energy(_raw: str, lower: str) -> dict[str, Any] | None:
    window_m = re.search(r"\b(today|this\s+week|this\s+month|this\s+year)\b", lower)
    return {"window": window_m.group(1) if window_m else "today"}


# ── Payments ───────────────────────────────────────────────────────────────────

def _ext_payments_send(raw: str, lower: str) -> dict[str, Any] | None:
    amount = _extract_amount(lower)
    person_m = re.search(
        r"(?:send|pay|venmo|zelle)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+"
        r"(?:\$|£|€|\d)",
        lower,
    )
    if not person_m:
        person_m = re.search(r"\$[\d.]+\s+(?:to|for)\s+(.+?)$", lower)
    note_m = re.search(r"(?:for|note:?)\s+(.+?)(?:\s+on\s+|\s+via\s+|$)", lower)
    return {
        "recipient": person_m.group(1).strip() if person_m else _extract_person(lower),
        "amount": amount,
        "note": note_m.group(1).strip() if note_m else "",
        "platform": _extract_payment_platform(lower),
    }


def _ext_payments_request(raw: str, lower: str) -> dict[str, Any] | None:
    amount = _extract_amount(lower)
    person_m = re.search(
        r"(?:request|charge|ask)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+"
        r"(?:for\s+)?\$?[\d]",
        lower,
    )
    note_m = re.search(r"(?:for|because)\s+(.+?)$", lower)
    return {
        "recipient": person_m.group(1).strip() if person_m else _extract_person(lower),
        "amount": amount,
        "note": note_m.group(1).strip() if note_m else "",
    }


def _ext_payments_split(raw: str, lower: str) -> dict[str, Any] | None:
    amount = _extract_amount(lower)
    people_m = re.findall(r"[A-Z][a-z]+", raw)
    ways_m = re.search(r"(\d+)\s+ways?", lower)
    return {
        "amount": amount,
        "people": people_m,
        "ways": int(ways_m.group(1)) if ways_m else len(people_m) or 2,
    }


def _ext_payments_history(_raw: str, lower: str) -> dict[str, Any] | None:
    person_m = re.search(r"(?:with|from|to)\s+([A-Za-z][a-z]+(?:\s+[A-Z][a-z]+)?)", lower)
    window_m = re.search(r"\b(today|this\s+week|this\s+month|last\s+month)\b", lower)
    return {
        "contact": person_m.group(1).strip() if person_m else None,
        "window": window_m.group(1) if window_m else "recent",
        "platform": _extract_payment_platform(lower),
    }


def _ext_payments_balance(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_payment_platform(lower)}


# ── Food Delivery ──────────────────────────────────────────────────────────────

def _extract_delivery_platform(lower: str) -> str | None:
    for p in ("doordash", "uber eats", "ubereats", "grubhub", "seamless", "instacart",
              "postmates", "caviar", "gopuff"):
        if p in lower:
            return p.replace(" ", "_")
    return None


def _ext_food_delivery_order(raw: str, lower: str) -> dict[str, Any] | None:
    rest_m = re.search(
        r"(?:from|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?(?:\s+[A-Z][a-z]+)?)", raw
    )
    return {
        "restaurant": rest_m.group(1).strip() if rest_m else "",
        "platform": _extract_delivery_platform(lower),
    }


def _ext_food_delivery_track(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_delivery_platform(lower)}


def _ext_food_delivery_reorder(_raw: str, lower: str) -> dict[str, Any] | None:
    rest_m = re.search(r"(?:from|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", lower)
    return {
        "restaurant": rest_m.group(1).strip() if rest_m else "",
        "platform": _extract_delivery_platform(lower),
    }


def _ext_food_delivery_browse(raw: str, lower: str) -> dict[str, Any] | None:
    cuisine_m = re.search(
        r"\b(pizza|sushi|chinese|mexican|indian|thai|italian|burger|"
        r"mediterranean|japanese|korean|vietnamese|greek|american)\b",
        lower,
    )
    return {
        "cuisine": cuisine_m.group(1) if cuisine_m else None,
        "platform": _extract_delivery_platform(lower),
    }


# ── Rideshare ──────────────────────────────────────────────────────────────────

def _extract_rideshare_platform(lower: str) -> str | None:
    if "lyft" in lower:
        return "lyft"
    if "uber" in lower:
        return "uber"
    return None


def _ext_rideshare_book(raw: str, lower: str) -> dict[str, Any] | None:
    dest_m = re.search(r"(?:to|for)\s+(.+?)(?:\s+on\s+uber|\s+on\s+lyft|$)", lower)
    ride_type_m = re.search(r"\b(xl|pool|share|comfort|black|lux|plus|x)\b", lower)
    return {
        "destination": dest_m.group(1).strip() if dest_m else "",
        "type": ride_type_m.group(1) if ride_type_m else "standard",
        "platform": _extract_rideshare_platform(lower),
    }


def _ext_rideshare_track(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_rideshare_platform(lower)}


def _ext_rideshare_schedule(raw: str, lower: str) -> dict[str, Any] | None:
    dest_m = re.search(r"(?:to|for)\s+(.+?)(?:\s+at\b|$)", lower)
    return {
        "destination": dest_m.group(1).strip() if dest_m else "",
        "time": _extract_time_str(lower),
        "platform": _extract_rideshare_platform(lower),
    }


def _ext_rideshare_cancel(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_rideshare_platform(lower)}


# ── Maps / Navigation extras ───────────────────────────────────────────────────

def _ext_maps_search(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:find|search|show|look\s+up)\s+(?:a\s+|the\s+)?(.+?)$", lower)
    return {"query": m.group(1).strip() if m else raw.strip()}


def _ext_maps_save_place(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:save|bookmark|add)\s+(.+?)\s+(?:to\s+(?:my\s+)?(?:places?|maps?|saved))?$",
                  lower)
    return {"place": m.group(1).strip() if m else ""}


def _ext_maps_explore(_raw: str, lower: str) -> dict[str, Any] | None:
    category_m = re.search(
        r"\b(restaurant|bar|cafe|museum|park|hotel|gym|pharmacy|hospital|"
        r"atm|gas\s+station|grocery|coffee\s+shop|shopping)\b",
        lower,
    )
    return {
        "category": category_m.group(1) if category_m else "",
        "location": _extract_location(_raw, lower) or "__current__",
    }


def _ext_maps_review(raw: str, lower: str) -> dict[str, Any] | None:
    place_m = re.search(
        r"(?:review|rate)\s+(?:a\s+|the\s+)?(.+?)(?:\s+\d\s+star|\s+five\s+|\s+one\s+|$)",
        lower,
    )
    rating_m = re.search(r"\b(\d)\s*(?:star|out\s+of\s+5)?\b", lower)
    return {
        "place": place_m.group(1).strip() if place_m else "",
        "rating": int(rating_m.group(1)) if rating_m else None,
    }


def _ext_maps_share_eta(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — shares current location ETA


# ── Travel ─────────────────────────────────────────────────────────────────────

_AIRPORT_RE = re.compile(r"\b([A-Z]{3})\b")
_MONTH_NAMES = (
    "january|february|march|april|may|june|july|august|september|"
    "october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
)


def _ext_travel_flight_search(raw: str, lower: str) -> dict[str, Any] | None:
    airports = _AIRPORT_RE.findall(raw)
    origin = airports[0] if airports else None
    dest = airports[1] if len(airports) > 1 else None
    if not dest:
        dest_m = re.search(r"(?:to|for)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
        dest = dest_m.group(1).strip() if dest_m else None
    date_m = re.search(
        rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})\b", lower
    )
    cabin_m = re.search(r"\b(economy|business|first\s+class|premium)\b", lower)
    return {
        "origin": origin,
        "destination": dest,
        "date": f"{date_m.group(1)} {date_m.group(2)}" if date_m else None,
        "cabin": cabin_m.group(1) if cabin_m else "economy",
    }


def _ext_travel_flight_status(raw: str, lower: str) -> dict[str, Any] | None:
    flight_m = re.search(r"\b([A-Z]{2,3})\s*(\d{1,4})\b", raw)
    return {
        "flight": f"{flight_m.group(1)}{flight_m.group(2)}" if flight_m else None,
        "airline": flight_m.group(1) if flight_m else None,
    }


def _ext_travel_boarding_pass(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}


def _ext_travel_hotel_search(raw: str, lower: str) -> dict[str, Any] | None:
    city_m = re.search(r"(?:in|at|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    date_m = re.search(rf"\b({_MONTH_NAMES})\s+(\d{{1,2}})\b", lower)
    guests_m = re.search(r"(\d+)\s+(?:guest|person|people|adult)", lower)
    stars_m = re.search(r"(\d)\s*star", lower)
    return {
        "city": city_m.group(1).strip() if city_m else None,
        "checkin": f"{date_m.group(1)} {date_m.group(2)}" if date_m else None,
        "guests": int(guests_m.group(1)) if guests_m else 1,
        "stars": int(stars_m.group(1)) if stars_m else None,
    }


def _ext_travel_hotel_book(raw: str, lower: str) -> dict[str, Any] | None:
    hotel_m = re.search(r"(?:book|reserve)\s+(?:a\s+(?:room\s+at|night\s+at)\s+)?(.+?)(?:\s+for|\s+in|$)",
                        lower)
    return {"hotel": hotel_m.group(1).strip() if hotel_m else ""}


def _ext_travel_checkin(raw: str, lower: str) -> dict[str, Any] | None:
    airline_m = re.search(
        r"\b(united|delta|american|southwest|jetblue|alaska|spirit|frontier|"
        r"lufthansa|ba|british airways|air france|emirates)\b",
        lower,
    )
    flight_m = re.search(r"\b([A-Z]{2,3})\s*(\d{1,4})\b", raw)
    return {
        "airline": airline_m.group(1) if airline_m else None,
        "flight": f"{flight_m.group(1)}{flight_m.group(2)}" if flight_m else None,
    }


def _ext_travel_itinerary(raw: str, lower: str) -> dict[str, Any] | None:
    trip_m = re.search(r"(?:for|to|trip\s+to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    return {"trip": trip_m.group(1).strip() if trip_m else ""}


def _ext_travel_car_rental(raw: str, lower: str) -> dict[str, Any] | None:
    city_m = re.search(r"(?:in|at|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    type_m = re.search(r"\b(economy|compact|midsize|suv|luxury|truck|van|convertible)\b", lower)
    return {
        "location": city_m.group(1).strip() if city_m else None,
        "type": type_m.group(1) if type_m else None,
    }


def _ext_travel_alert(raw: str, lower: str) -> dict[str, Any] | None:
    dest_m = re.search(r"(?:for|to|in)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    return {"destination": dest_m.group(1).strip() if dest_m else ""}


# ── Video Streaming ────────────────────────────────────────────────────────────

def _ext_video_play(raw: str, lower: str) -> dict[str, Any] | None:
    platform = _extract_streaming_platform(lower)
    type_m = re.search(r"\b(movie|film|episode|show|series|documentary|season)\b", lower)
    title_m = re.search(
        r"(?:watch|play|put\s+on|stream)\s+[\"']?(.+?)[\"']?"
        r"(?:\s+on\s+\w+|\s+episode|\s+season|$)",
        lower,
    )
    return {
        "title": title_m.group(1).strip() if title_m else raw.strip(),
        "type": type_m.group(1) if type_m else None,
        "platform": platform,
    }


def _ext_video_search(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:find|search|look\s+for|browse)\s+[\"']?(.+?)[\"']?(?:\s+on\s+\w+)?$",
                  lower)
    return {
        "query": m.group(1).strip() if m else raw.strip(),
        "platform": _extract_streaming_platform(lower),
    }


def _ext_video_watchlist(raw: str, lower: str) -> dict[str, Any] | None:
    action = "view" if re.search(r"\b(?:show|view|open|my)\b", lower) else "add"
    title_m = re.search(r"(?:add|save)\s+[\"']?(.+?)[\"']?\s+to\b", lower)
    return {
        "action": action,
        "title": title_m.group(1).strip() if title_m else "",
        "platform": _extract_streaming_platform(lower),
    }


def _ext_video_browse(_raw: str, lower: str) -> dict[str, Any] | None:
    genre_m = re.search(
        r"\b(action|comedy|drama|horror|thriller|sci.?fi|romance|animation|"
        r"documentary|crime|fantasy|mystery|adventure)\b",
        lower,
    )
    return {
        "genre": genre_m.group(1) if genre_m else None,
        "platform": _extract_streaming_platform(lower),
    }


def _ext_video_recommend(_raw: str, lower: str) -> dict[str, Any] | None:
    mood_m = re.search(
        r"\b(funny|scary|sad|inspiring|relaxing|thrilling|romantic|educational)\b", lower
    )
    return {
        "mood": mood_m.group(1) if mood_m else None,
        "platform": _extract_streaming_platform(lower),
    }


def _ext_video_cast(_raw: str, lower: str) -> dict[str, Any] | None:
    device_m = re.search(
        r"(?:to|on)\s+(?:the\s+)?(?:my\s+)?(.+?)\s*(?:tv|chromecast|fire\s+stick|apple\s+tv|roku)?$",
        lower,
    )
    return {"device": device_m.group(1).strip() if device_m else "tv"}


def _ext_video_continue(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_streaming_platform(lower)}


def _ext_video_rate(raw: str, lower: str) -> dict[str, Any] | None:
    rating_m = re.search(r"\b(\d)\s*(?:star|out\s+of\s+5)?\b", lower)
    thumbs = "up" if re.search(r"\bthumb\s*s?\s*up|good|great|loved?\b", lower) else None
    if re.search(r"\bthumb\s*s?\s*down|bad|hated?\b", lower):
        thumbs = "down"
    return {"rating": int(rating_m.group(1)) if rating_m else None, "thumbs": thumbs}


# ── Health / Fitness ───────────────────────────────────────────────────────────

_WORKOUT_TYPES = (
    r"running|run|jog|walk|hike|cycling|bike|swim|yoga|pilates|crossfit|"
    r"weightlifting|weights|gym|basketball|tennis|soccer|football|golf|"
    r"rowing|jump\s+rope|hiit|cardio|strength|stretching|meditation"
)


def _ext_health_workout_log(raw: str, lower: str) -> dict[str, Any] | None:
    type_m = re.search(_WORKOUT_TYPES, lower)
    duration_m = re.search(r"(\d+)\s*(?:minute|min|hour|hr)s?", lower)
    distance_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:mile|km|meter)s?", lower)
    cal_m = re.search(r"(\d+)\s*(?:calorie|cal)s?", lower)
    return {
        "type": type_m.group(0) if type_m else "",
        "duration_min": int(duration_m.group(1)) if duration_m else None,
        "distance": float(distance_m.group(1)) if distance_m else None,
        "calories": int(cal_m.group(1)) if cal_m else None,
    }


def _ext_health_workout_start(_raw: str, lower: str) -> dict[str, Any] | None:
    type_m = re.search(_WORKOUT_TYPES, lower)
    return {"type": type_m.group(0) if type_m else "general"}


def _ext_health_steps(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(?:today|yesterday|last\s+(\d+)\s+days?)\b", lower)
    days = int(m.group(1)) if m and m.group(1) else 1
    return {"days": days}


def _ext_health_heart_rate(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(?:last\s+(\d+)\s+(?:hours?|days?))\b", lower)
    return {"period": m.group(0) if m else "today"}


def _ext_health_sleep(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"\b(?:last\s+(\d+)\s+nights?|tonight|last\s+night)\b", lower)
    nights = int(m.group(1)) if m and m.group(1) else 1
    return {"nights": nights}


def _ext_health_food_log(raw: str, lower: str) -> dict[str, Any] | None:
    cal_m = re.search(r"(\d+)\s*(?:calorie|cal)s?", lower)
    meal_m = re.search(r"\b(breakfast|lunch|dinner|snack)\b", lower)
    food_m = re.search(
        r"(?:ate|had|log(?:ged)?|record(?:ed)?)\s+(?:a\s+|some\s+|my\s+)?(.+?)(?:\s+for\s+|\s*$)",
        lower,
    )
    return {
        "food": food_m.group(1).strip() if food_m else raw.strip(),
        "calories": int(cal_m.group(1)) if cal_m else None,
        "meal": meal_m.group(1) if meal_m else None,
    }


def _ext_health_water(_raw: str, lower: str) -> dict[str, Any] | None:
    oz_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:oz|ounce)s?", lower)
    ml_m = re.search(r"(\d+(?:\.\d+)?)\s*(?:ml|milliliter)s?", lower)
    cup_m = re.search(r"(\d+(?:\.\d+)?)\s*cup?s?", lower)
    if oz_m:
        return {"amount": float(oz_m.group(1)), "unit": "oz"}
    if ml_m:
        return {"amount": float(ml_m.group(1)), "unit": "ml"}
    if cup_m:
        return {"amount": float(cup_m.group(1)), "unit": "cup"}
    return {"amount": None, "unit": None}


def _ext_health_weight(_raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:pounds?|lbs?|kg|kilograms?)", lower)
    unit = "lbs" if m and re.search(r"pound|lb", m.group(0)) else "kg"
    return {"weight": float(m.group(1)) if m else None, "unit": unit}


def _ext_health_medication(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:take|took|log|remind\s+me\s+to\s+take|medication|medicine|pill|supplement)\s+"
        r"(?:my\s+)?(.+?)(?:\s+at\b|$)",
        lower,
    )
    return {
        "medication": m.group(1).strip() if m else "",
        "time": _extract_time_str(lower),
    }


def _ext_health_mood(_raw: str, lower: str) -> dict[str, Any] | None:
    for mood in ("great", "good", "okay", "ok", "bad", "terrible", "anxious",
                 "happy", "sad", "stressed", "calm", "tired", "energetic"):
        if mood in lower:
            return {"mood": mood}
    score_m = re.search(r"\b([1-9]|10)\s*(?:out\s+of\s+10|/10)?\b", lower)
    return {"mood": None, "score": int(score_m.group(1)) if score_m else None}


# ── Files / Storage ────────────────────────────────────────────────────────────

def _ext_files_upload(raw: str, lower: str) -> dict[str, Any] | None:
    file_m = re.search(r"upload\s+(.+?)(?:\s+to\b|$)", lower)
    dest_m = re.search(r"(?:to|into)\s+(.+?)(?:\s+folder)?$", lower)
    return {
        "file": file_m.group(1).strip() if file_m else "",
        "destination": dest_m.group(1).strip() if dest_m else "",
    }


def _ext_files_download(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"download\s+(.+?)(?:\s+from\b|$)", lower)
    return {"file": m.group(1).strip() if m else ""}


def _ext_files_share_file(raw: str, lower: str) -> dict[str, Any] | None:
    file_m = re.search(r"(?:share|send)\s+(.+?)\s+(?:with|to)\b", lower)
    person_m = re.search(r"(?:with|to)\s+(.+?)$", lower)
    return {
        "file": file_m.group(1).strip() if file_m else "",
        "recipient": person_m.group(1).strip() if person_m else "",
    }


def _ext_files_search(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:find|search|look\s+for)\s+(?:a\s+)?(?:file\s+)?(.+?)(?:\s+file)?$",
                  lower)
    return {"query": m.group(1).strip() if m else raw.strip()}


def _ext_files_recent(_raw: str, lower: str) -> dict[str, Any] | None:
    m_limit = re.search(r"\b(?:last|recent|past)\s+(\d+)\b", lower)
    m_type = re.search(r"\b(pdf|doc|docx|image|photo|video|spreadsheet|csv)\b", lower)
    return {"limit": int(m_limit.group(1)) if m_limit else 10,
            "type": m_type.group(1) if m_type else ""}


# ── Alarm / Clock ──────────────────────────────────────────────────────────────

def _ext_alarm_set(raw: str, lower: str) -> dict[str, Any] | None:
    time_str = _extract_time_str(lower)
    if not time_str:
        return None
    label_m = re.search(r"(?:called|labeled?|named?|for)\s+(.+?)(?:\s+at\b|$)", lower)
    days_m = re.findall(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"weekday|weekend|daily|every\s+day)\b",
        lower,
    )
    return {
        "time": time_str,
        "label": label_m.group(1).strip() if label_m else "",
        "days": days_m,
    }


def _ext_alarm_delete(raw: str, lower: str) -> dict[str, Any] | None:
    time_str = _extract_time_str(lower)
    label_m = re.search(r"(?:the\s+|my\s+)?(.+?)\s+alarm$", lower)
    return {
        "time": time_str,
        "label": label_m.group(1).strip() if label_m else "",
    }


def _ext_alarm_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — lists all alarms


def _ext_clock_world(_raw: str, lower: str) -> dict[str, Any] | None:
    city_m = re.search(
        r"(?:in|at|for)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)", lower
    )
    tz_m = re.search(r"\b(est|cst|mst|pst|gmt|utc|et|ct|mt|pt)\b", lower)
    return {
        "city": city_m.group(1).strip() if city_m else None,
        "timezone": tz_m.group(1) if tz_m else None,
    }


def _ext_clock_stopwatch(_raw: str, lower: str) -> dict[str, Any] | None:
    action = "stop" if re.search(r"\b(?:stop|pause|end)\b", lower) else "start"
    lap = bool(re.search(r"\blap\b", lower))
    return {"action": action, "lap": lap}


def _ext_clock_bedtime(_raw: str, lower: str) -> dict[str, Any] | None:
    time_str = _extract_time_str(lower)
    return {"bedtime": time_str}


# ── Podcasts ───────────────────────────────────────────────────────────────────

def _ext_podcast_find(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:find|search|discover)\s+(?:a\s+|the\s+)?(?:podcast\s+)?(.+?)$", lower)
    topic_m = re.search(r"(?:about|on)\s+(.+?)$", lower)
    return {
        "query": m.group(1).strip() if m else raw.strip(),
        "topic": topic_m.group(1).strip() if topic_m else None,
    }


def _ext_podcast_play(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(
        r"(?:play|listen\s+to|put\s+on)\s+(?:the\s+)?(?:latest\s+)?(?:episode\s+of\s+)?(.+?)$",
        lower,
    )
    latest = bool(re.search(r"\b(?:latest|newest|recent)\b", lower))
    return {
        "podcast": m.group(1).strip() if m else "",
        "latest": latest,
    }


def _ext_podcast_subscribe(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:subscribe|follow)\s+(?:to\s+)?(.+?)(?:\s+podcast)?$", lower)
    return {"podcast": m.group(1).strip() if m else ""}


def _ext_podcast_queue(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:add|queue)\s+(.+?)\s+to\b", lower)
    return {"episode": m.group(1).strip() if m else ""}


# ── Recipes / Food ─────────────────────────────────────────────────────────────

def _ext_recipe_find(raw: str, lower: str) -> dict[str, Any] | None:
    dish_m = re.search(
        r"(?:recipe\s+for|how\s+(?:to\s+)?(?:make|cook)|cook(?:ing)?)\s+(.+?)$", lower
    )
    cuisine_m = re.search(
        r"\b(italian|mexican|thai|indian|chinese|japanese|french|greek|"
        r"mediterranean|american|vegan|vegetarian|keto|paleo)\b",
        lower,
    )
    ingredient_m = re.search(r"(?:with|using|made\s+with)\s+(.+?)(?:\s+recipe)?$", lower)
    return {
        "dish": dish_m.group(1).strip() if dish_m else raw.strip(),
        "cuisine": cuisine_m.group(1) if cuisine_m else None,
        "ingredients": ingredient_m.group(1).strip() if ingredient_m else None,
    }


def _ext_recipe_save(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"save\s+(?:this\s+)?(?:recipe\s+for\s+|the\s+)?(.+?)(?:\s+recipe)?$", lower)
    return {"recipe": m.group(1).strip() if m else ""}


def _ext_recipe_nutrition(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:nutrition|calories|macros?)\s+(?:in|for|of)\s+(.+?)$", lower)
    if not m:
        m = re.search(r"(?:how\s+(?:many|much)\s+calories?\s+(?:in|does)\s+)(.+?)(?:\s+have)?$",
                      lower)
    return {"food": m.group(1).strip() if m else raw.strip()}


def _ext_recipe_scale(raw: str, lower: str) -> dict[str, Any] | None:
    servings_m = re.search(r"(\d+)\s+(?:serving|portion|people|person)s?", lower)
    return {"servings": int(servings_m.group(1)) if servings_m else None}


def _ext_grocery_add(raw: str, lower: str) -> dict[str, Any] | None:
    m = re.search(r"add\s+(.+?)\s+(?:to\s+(?:(?:my|the)\s+)?(?:grocery|shopping)\s+list)?$",
                  lower)
    items_raw = m.group(1).strip() if m else raw.strip()
    # try to split comma/and separated items
    items = [i.strip() for i in re.split(r",\s*|\s+and\s+", items_raw) if i.strip()]
    return {"items": items}


def _ext_grocery_list(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — returns full grocery list


def _ext_grocery_order(_raw: str, lower: str) -> dict[str, Any] | None:
    return {"platform": _extract_delivery_platform(lower)}


# ── Translation ────────────────────────────────────────────────────────────────

_LANG_RE = re.compile(
    r"\b(spanish|french|german|italian|portuguese|russian|chinese|mandarin|"
    r"japanese|korean|arabic|hindi|dutch|polish|turkish|swedish|greek|hebrew|"
    r"vietnamese|thai|indonesian|english)\b",
    re.IGNORECASE,
)


def _ext_translate_text(raw: str, lower: str) -> dict[str, Any] | None:
    langs = _LANG_RE.findall(lower)
    source = langs[0].lower() if langs else None
    target = langs[1].lower() if len(langs) > 1 else (langs[0].lower() if langs else None)
    text_m = re.search(r"(?:translate|say)\s+[\"'](.+?)[\"']", raw)
    if not text_m:
        text_m = re.search(
            r"(?:translate|convert|say)\s+(.+?)\s+(?:to\s+\w+|in\s+\w+)?$", lower
        )
    return {
        "text": text_m.group(1).strip() if text_m else "",
        "source": source,
        "target": target,
    }


def _ext_translate_detect(raw: str, _lower: str) -> dict[str, Any] | None:
    text_m = re.search(r"(?:what\s+language\s+is|detect\s+the\s+language\s+of)\s+[\"']?(.+?)[\"']?$",
                       raw, re.IGNORECASE)
    return {"text": text_m.group(1).strip() if text_m else ""}


def _ext_translate_conversation(_raw: str, lower: str) -> dict[str, Any] | None:
    langs = _LANG_RE.findall(lower)
    return {
        "lang1": langs[0].lower() if langs else "english",
        "lang2": langs[1].lower() if len(langs) > 1 else None,
    }


# ── Books / Reading ────────────────────────────────────────────────────────────

def _ext_book_find(raw: str, lower: str) -> dict[str, Any] | None:
    author_m = re.search(r"(?:by|from\s+author)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", raw)
    genre_m = re.search(
        r"\b(mystery|thriller|romance|fantasy|sci.?fi|biography|history|"
        r"self.?help|non.?fiction|fiction|horror|business|memoir)\b",
        lower,
    )
    title_m = re.search(r"(?:find|read|get|buy)\s+[\"']?(.+?)[\"']?(?:\s+by|\s+book|$)", lower)
    return {
        "title": title_m.group(1).strip() if title_m else "",
        "author": author_m.group(1) if author_m else None,
        "genre": genre_m.group(1) if genre_m else None,
    }


def _ext_book_read(raw: str, lower: str) -> dict[str, Any] | None:
    title_m = re.search(
        r"(?:read|open|continue|start)\s+[\"']?(.+?)[\"']?(?:\s+book)?$", lower
    )
    audio = bool(re.search(r"\b(?:audiobook|audio|listen)\b", lower))
    return {
        "title": title_m.group(1).strip() if title_m else "",
        "audio": audio,
    }


def _ext_book_library(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — returns full book library


def _ext_book_highlight(raw: str, _lower: str) -> dict[str, Any] | None:
    m = re.search(r"(?:highlight|mark)\s+[\"']?(.+?)[\"']?$", raw, re.IGNORECASE)
    return {"text": m.group(1).strip() if m else ""}


# ── Device / Settings ──────────────────────────────────────────────────────────

def _ext_settings_wifi(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:connect|join)\b", lower):
        net_m = re.search(r"(?:connect\s+to|join)\s+[\"']?(.+?)[\"']?(?:\s+wifi|network)?$",
                          lower)
        return {"action": "connect", "network": net_m.group(1).strip() if net_m else ""}
    if re.search(r"\b(?:turn\s+off|disable|off)\b", lower):
        return {"action": "off"}
    if re.search(r"\b(?:turn\s+on|enable|on)\b", lower):
        return {"action": "on"}
    return {"action": "settings"}


def _ext_settings_bluetooth(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:pair|connect)\b", lower):
        device_m = re.search(r"(?:pair|connect)\s+(?:to\s+|with\s+)?(.+?)$", lower)
        return {"action": "pair", "device": device_m.group(1).strip() if device_m else ""}
    if re.search(r"\b(?:turn\s+off|disable|off)\b", lower):
        return {"action": "off"}
    if re.search(r"\b(?:turn\s+on|enable|on)\b", lower):
        return {"action": "on"}
    return {"action": "settings"}


def _ext_settings_brightness(_raw: str, lower: str) -> dict[str, Any] | None:
    pct_m = re.search(r"\b(\d{1,3})\s*(?:percent|%)?\b", lower)
    if re.search(r"\b(?:up|increase|brighter|max|full)\b", lower):
        return {"action": "up", "level": None}
    if re.search(r"\b(?:down|decrease|dimmer|lower|min)\b", lower):
        return {"action": "down", "level": None}
    return {"action": "set", "level": int(pct_m.group(1)) if pct_m else None}


def _ext_settings_dnd(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:off|disable|turn\s+off)\b", lower):
        action = "off"
    elif re.search(r"\b(?:on|enable|turn\s+on)\b", lower):
        action = "on"
    else:
        action = "toggle"
    duration_m = re.search(r"(?:for\s+)?(\d+)\s*(?:hour|minute|min|hr)s?", lower)
    return {
        "action": action,
        "duration": int(duration_m.group(1)) if duration_m else None,
    }


def _ext_settings_airplane(_raw: str, lower: str) -> dict[str, Any] | None:
    if re.search(r"\b(?:off|disable|turn\s+off)\b", lower):
        return {"action": "off"}
    return {"action": "on"}


def _ext_settings_battery(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — reads battery state


def _ext_settings_storage(_raw: str, _lower: str) -> dict[str, Any] | None:
    return {}  # no params needed — reads storage state


def _ext_settings_notification(raw: str, lower: str) -> dict[str, Any] | None:
    app_m = re.search(
        r"(?:notifications?\s+for|from)\s+(.+?)(?:\s+notifications?|$)", lower
    )
    action = "off" if re.search(r"\b(?:off|disable|mute|silence|stop)\b", lower) else "on"
    return {
        "app": app_m.group(1).strip() if app_m else "",
        "action": action,
    }


# ── Wave-4 extractors ─────────────────────────────────────────────────────────

# ── Notifications ──────────────────────────────────────────────────────────────
def _ext_notif_view(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

def _ext_notif_clear(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

def _ext_notif_clear_app(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:clear|dismiss|remove)\s+(?:all\s+)?notifications?\s+(?:from|for)\s+(.+)", lower)
    return {"app": m.group(1).strip() if m else ""}

def _ext_notif_mark_read(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

def _ext_notif_settings(raw: str, lower: str) -> dict | None:
    m = re.search(r"notification\s+settings?\s+(?:for\s+)?(.+)", lower)
    return {"app": m.group(1).strip() if m else ""}

# ── Handoff / Continuity ───────────────────────────────────────────────────────
def _ext_handoff_airdrop(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:airdrop|air\s+drop)\s+(?:this|it|\w+)?\s*(?:to\s+(.+))?", lower)
    return {"target": m.group(1).strip() if m and m.group(1) else ""}

def _ext_handoff_clipboard(raw: str, lower: str) -> dict | None:
    return {}  # no params needed — syncs current clipboard

def _ext_handoff_continue(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:continue|hand\s*off|pick\s+up)\s+(?:on|from|to)\s+(.+)", lower)
    return {"device": m.group(1).strip() if m else ""}

def _ext_handoff_screen_share(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

# ── Enterprise — Jira ──────────────────────────────────────────────────────────
def _ext_jira_create(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:create|new|file|open)\s+(?:a\s+)?(?:jira\s+)?(?:ticket|issue|bug|story)\s+(?:for|about)?\s*(.+)?", lower)
    return {"title": m.group(1).strip() if m and m.group(1) else ""}

def _ext_jira_view(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:view|show|open)\s+(?:jira\s+)?(?:ticket|issue)?\s*([A-Za-z]+-\d+)", raw)
    return {"ticket": m.group(1).upper() if m else ""}

def _ext_jira_update(raw: str, lower: str) -> dict | None:
    m = re.search(r"([A-Za-z]+-\d+)", raw)
    return {"ticket": m.group(1).upper() if m else ""}

def _ext_jira_my_issues(raw: str, lower: str) -> dict | None:
    return {}

def _ext_jira_sprint(raw: str, lower: str) -> dict | None:
    return {}

# ── Enterprise — GitHub ────────────────────────────────────────────────────────
def _ext_github_pr_view(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:pr|pull\s+request)\s+#?(\d+)", lower)
    return {"pr": m.group(1) if m else ""}

def _ext_github_issue_create(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:open|create|file)\s+(?:a\s+)?(?:github\s+)?issue\s+(?:for|about)?\s*(.+)?", lower)
    return {"title": m.group(1).strip() if m and m.group(1) else ""}

def _ext_github_my_prs(raw: str, lower: str) -> dict | None:
    return {}

def _ext_github_repo_search(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:find|search)\s+(?:a\s+)?(?:github\s+)?repo(?:sitory)?\s+(?:for|named|about)?\s*(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_github_commit(raw: str, lower: str) -> dict | None:
    return {}

# ── Enterprise — Slack ─────────────────────────────────────────────────────────
def _ext_slack_send(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:send|message|dm)\s+(?:in\s+|to\s+)?#?(\S+)\s+(?:on\s+slack\s+)?(?:saying\s+|that\s+)?(.*)", lower)
    ch = m.group(1).strip() if m else ""
    msg = m.group(2).strip() if m and m.group(2) else ""
    return {"channel": ch, "message": msg}

def _ext_slack_read(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:check|read|show)\s+(?:my\s+)?(?:slack\s+)?#?(\S+)?(?:\s+channel)?", lower)
    return {"channel": m.group(1).strip() if m and m.group(1) else ""}

def _ext_slack_search(raw: str, lower: str) -> dict | None:
    m = re.search(r"search\s+(?:for\s+)?(.+?)(?:\s+in\s+slack)?$", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_slack_status(raw: str, lower: str) -> dict | None:
    m = re.search(r"set\s+(?:my\s+)?(?:slack\s+)?status\s+(?:to\s+)?(.+)", lower)
    return {"status": m.group(1).strip() if m else ""}

def _ext_slack_reaction(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:react|reaction|emoji)\s+(.+)", lower)
    return {"emoji": m.group(1).strip() if m else ""}

# ── Enterprise — Notion ────────────────────────────────────────────────────────
def _ext_notion_create(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:create|new|add)\s+(?:a\s+)?(?:notion\s+)?(?:page|note|doc)\s+(?:about|for|titled)?\s*(.+)?", lower)
    return {"title": m.group(1).strip() if m and m.group(1) else ""}

def _ext_notion_find(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:find|search|open)\s+(?:my\s+)?(?:notion\s+)?(?:page|note|doc)\s+(?:about|on|for)?\s*(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_notion_update(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:update|edit)\s+(?:the\s+)?(?:notion\s+)?(?:page|note)\s+(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_notion_database(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:notion|my)\s+database\s+(?:for|about)?\s*(.+)?", lower)
    return {"name": m.group(1).strip() if m and m.group(1) else ""}

# ── Enterprise — Asana ────────────────────────────────────────────────────────
def _ext_asana_create(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:create|add|new)\s+(?:an?\s+)?(?:asana\s+)?task\s+(?:for|about)?\s*(.+)?", lower)
    return {"title": m.group(1).strip() if m and m.group(1) else ""}

def _ext_asana_my_tasks(raw: str, lower: str) -> dict | None:
    return {}

def _ext_asana_update(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:update|complete|mark|close)\s+(?:asana\s+)?task\s+(.+)", lower)
    return {"task": m.group(1).strip() if m else ""}

def _ext_asana_project(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:show|open|view)\s+(?:asana\s+)?project\s+(.+)", lower)
    return {"project": m.group(1).strip() if m else ""}

# ── Wallet / Passes ────────────────────────────────────────────────────────────
def _ext_wallet_passes(raw: str, lower: str) -> dict | None:
    return {}

def _ext_wallet_loyalty(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:loyalty|rewards?|points|membership)\s+(?:card\s+)?(?:for|at|from)?\s*(.+)?", lower)
    return {"brand": m.group(1).strip() if m and m.group(1) else ""}

def _ext_wallet_gift_card(raw: str, lower: str) -> dict | None:
    m = re.search(r"gift\s+card\s+(?:for|at|from)?\s*(.+)?", lower)
    return {"brand": m.group(1).strip() if m and m.group(1) else ""}

def _ext_wallet_coupon(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:coupon|promo|discount|deal|offer)\s+(?:for|at|from)?\s*(.+)?", lower)
    return {"brand": m.group(1).strip() if m and m.group(1) else ""}

# ── VPN ────────────────────────────────────────────────────────────────────────
def _ext_vpn_connect(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:connect|enable|turn\s+on|start)\s+vpn\s*(?:to\s+)?(.+)?", lower)
    return {"server": m.group(1).strip() if m and m.group(1) else ""}

def _ext_vpn_disconnect(raw: str, lower: str) -> dict | None:
    return {"action": "disconnect"}

def _ext_vpn_status(raw: str, lower: str) -> dict | None:
    return {}

# ── Focus / Productivity ───────────────────────────────────────────────────────
def _ext_focus_pomodoro(raw: str, lower: str) -> dict | None:
    m = re.search(r"(\d+)\s*(?:minute|min)", lower)
    return {"duration": int(m.group(1)) if m else 25, "type": "pomodoro"}

def _ext_focus_session(raw: str, lower: str) -> dict | None:
    dur_m = re.search(r"(\d+)\s*(?:hours?|hr|minutes?|min)", lower)
    return {"duration": dur_m.group(1) if dur_m else ""}

def _ext_focus_block(raw: str, lower: str) -> dict | None:
    m = re.search(r"block\s+(.+?)(?:\s+(?:for\s+\d+|during|until))?$", lower)
    return {"app": m.group(1).strip() if m else ""}

def _ext_focus_stats(raw: str, lower: str) -> dict | None:
    return {}

# ── Dictionary / Reference ─────────────────────────────────────────────────────
def _ext_dict_define(raw: str, lower: str) -> dict | None:
    m = re.search(r"\bdefine\s+(\w+)", lower)
    if not m:
        m = re.search(r"(?:meaning|definition)\s+of\s+(\w+)", lower)
    if not m:
        m = re.search(r"what\s+does\s+(\w+)\s+mean", lower)
    word = m.group(1).strip() if m else ""
    return {"word": word} if word else None

def _ext_dict_thesaurus(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:synonyms?\s+(?:for|of)|another\s+word\s+for|thesaurus\s+(?:for|of)?)\s+(\w+)", lower)
    word = m.group(1).strip() if m else ""
    return {"word": word} if word else None

def _ext_dict_wikipedia(raw: str, lower: str) -> dict | None:
    m = re.search(r"wikipedia\s+(?:article\s+)?(?:about|for|on)?\s*(.+)", lower)
    if not m:
        m = re.search(r"(?:look\s+up|tell\s+me\s+about)\s+(.+)\s+on\s+wikipedia", lower)
    query = m.group(1).strip() if m else ""
    return {"query": query} if query else None

def _ext_dict_etymology(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:etymology|origin|history\s+of\s+the\s+word)\s+(\w+)", lower)
    word = m.group(1).strip() if m else ""
    return {"word": word} if word else None

# ── Password Manager ───────────────────────────────────────────────────────────
def _ext_password_find(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:password|login|credentials?)\s+(?:for|to)\s+(.+)", lower)
    return {"service": m.group(1).strip() if m else ""}

def _ext_password_generate(raw: str, lower: str) -> dict | None:
    length_m = re.search(r"(\d+)\s*(?:character|char|digit)", lower)
    svc_m = re.search(r"(?:for|to)\s+(.+)", lower)
    return {"service": svc_m.group(1).strip() if svc_m else "", "length": int(length_m.group(1)) if length_m else 16}

def _ext_password_2fa(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:2fa|two[\-\s]factor|totp|authenticator|verification\s+code)\s+(?:code\s+)?(?:for\s+)?(.+)?", lower)
    return {"service": m.group(1).strip() if m and m.group(1) else ""}

def _ext_password_update(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:update|change|reset|rotate)\s+(?:my\s+)?password\s+(?:for|to)\s+(.+)", lower)
    return {"service": m.group(1).strip() if m else ""}

# ── App Store ──────────────────────────────────────────────────────────────────
def _ext_app_find(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:find|search|look\s+for)\s+(?:an?\s+)?app\s+(?:for|that|to)?\s*(.+)", lower)
    return {"query": m.group(1).strip() if m else ""}

def _ext_app_install(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:install|download|get)\s+(?:the\s+)?(.+?)(?:\s+app)?$", lower)
    name = m.group(1).strip() if m else ""
    return {"name": name} if name else None

def _ext_app_update_apps(raw: str, lower: str) -> dict | None:
    m = re.search(r"update\s+(?:my\s+)?(?:all\s+)?apps?|(?:app\s+updates?)", lower)
    return {} if m else None

# ── Reading List ───────────────────────────────────────────────────────────────
def _ext_reading_save(raw: str, lower: str) -> dict | None:
    return {}

def _ext_reading_list_view(raw: str, lower: str) -> dict | None:
    return {}

def _ext_reading_mark_read(raw: str, lower: str) -> dict | None:
    return {}

# ── Date Calculator ────────────────────────────────────────────────────────────
def _ext_date_days_until(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:how\s+many\s+days?\s+(?:until|till|to)|days?\s+(?:until|till|to)|countdown\s+to)\s+(.+)", lower)
    return {"event": m.group(1).strip() if m else ""}

def _ext_date_countdown(raw: str, lower: str) -> dict | None:
    m = re.search(r"countdown\s+(?:to|until|till)\s+(.+)", lower)
    return {"event": m.group(1).strip() if m else ""}

def _ext_date_day_of(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:what\s+day\s+(?:is|was|will)|day\s+of\s+(?:the\s+)?week)\s+(?:is\s+)?(.+)", lower)
    return {"date": m.group(1).strip() if m else ""}

def _ext_date_age(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:how\s+old\s+(?:am\s+i|is|was|will)|born\s+(?:in|on))\s*(.+)?", lower)
    return {"dob": m.group(1).strip() if m and m.group(1) else ""}

# ── Screen ─────────────────────────────────────────────────────────────────────
def _ext_screen_screenshot(raw: str, lower: str) -> dict | None:
    return {}

def _ext_screen_record(raw: str, lower: str) -> dict | None:
    action = "stop" if re.search(r"\bstop\b", lower) else "start"
    return {"action": action}

def _ext_screen_mirror(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:mirror|cast|project|airplay)\s+(?:my\s+)?(?:screen|display)\s+(?:to\s+)?(.+)?", lower)
    return {"target": m.group(1).strip() if m and m.group(1) else ""}

def _ext_screen_split(raw: str, lower: str) -> dict | None:
    return {}

# ── Print / Scan ───────────────────────────────────────────────────────────────
def _ext_print_document(raw: str, lower: str) -> dict | None:
    m = re.search(r"print\s+(?:this\s+)?(?:document|file|page|report)?\s*(.+)?", lower)
    return {"name": m.group(1).strip() if m and m.group(1) else ""}

def _ext_print_photo(raw: str, lower: str) -> dict | None:
    m = re.search(r"print\s+(?:this\s+)?(?:photo|picture|image)\s*(.+)?", lower)
    return {"name": m.group(1).strip() if m and m.group(1) else ""}

def _ext_print_scan(raw: str, lower: str) -> dict | None:
    return {}

# ── Backup ─────────────────────────────────────────────────────────────────────
def _ext_backup_now(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

def _ext_backup_status(raw: str, lower: str) -> dict | None:
    return {}  # no params needed

# ── Accessibility ──────────────────────────────────────────────────────────────
def _ext_access_font(raw: str, lower: str) -> dict | None:
    action = "increase" if re.search(r"\b(?:larger?|bigger?|increase|up)\b", lower) else "decrease"
    return {"action": action}

def _ext_access_voice(raw: str, lower: str) -> dict | None:
    action = "off" if re.search(r"\b(?:off|disable|turn\s+off)\b", lower) else "on"
    return {"action": action}

def _ext_access_zoom(raw: str, lower: str) -> dict | None:
    action = "out" if "out" in lower else "in"
    return {"action": action}

def _ext_access_display(raw: str, lower: str) -> dict | None:
    m = re.search(r"(bold\s+text|invert\s+colors?|reduce\s+motion|color\s+filter|high\s+contrast)", lower)
    return {"feature": m.group(1) if m else ""}

# ── Shortcuts / Automations ────────────────────────────────────────────────────
def _ext_shortcut_run(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:run|execute|activate)\s+(?:the\s+)?(.+?)(?:\s+shortcut)?$", lower)
    name = m.group(1).strip() if m else ""
    return {"name": name} if name else None

def _ext_shortcut_create(raw: str, lower: str) -> dict | None:
    m = re.search(r"(?:create|make|new)\s+(?:a\s+)?shortcut\s+(?:to|for|that)?\s*(.+)?", lower)
    return {"description": m.group(1).strip() if m and m.group(1) else ""}

def _ext_shortcut_list(raw: str, lower: str) -> dict | None:
    return {}

# ── Currency ───────────────────────────────────────────────────────────────────
_CURRENCY_NAMES = {
    "dollar": "USD", "dollars": "USD", "usd": "USD",
    "euro": "EUR", "euros": "EUR", "eur": "EUR",
    "pound": "GBP", "pounds": "GBP", "gbp": "GBP",
    "yen": "JPY", "jpy": "JPY",
    "yuan": "CNY", "cny": "CNY",
    "rupee": "INR", "rupees": "INR", "inr": "INR",
    "peso": "MXN", "pesos": "MXN", "mxn": "MXN",
    "cad": "CAD", "canadian": "CAD",
    "aud": "AUD", "australian": "AUD",
    "chf": "CHF", "franc": "CHF", "francs": "CHF",
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
}

def _ext_currency_convert(raw: str, lower: str) -> dict | None:
    amount_m = re.search(r"(\d+(?:\.\d+)?)", lower)
    amount = float(amount_m.group(1)) if amount_m else None
    found = []
    for name, code in _CURRENCY_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", lower) and code not in found:
            found.append(code)
    return {"amount": amount, "from": found[0] if found else "", "to": found[1] if len(found) > 1 else ""}

def _ext_currency_rates(raw: str, lower: str) -> dict | None:
    return {}  # no params needed — returns all rates

# ── Health extensions ──────────────────────────────────────────────────────────
def _ext_health_cycle(raw: str, lower: str) -> dict | None:
    # No params needed — returns current cycle data
    return {}

def _ext_health_streak(raw: str, lower: str) -> dict | None:
    m = re.search(r"\b(steps?|workout|water|sleep|meditation)\b", lower)
    return {"metric": m.group(1) if m else ""}

def _ext_health_goals(raw: str, lower: str) -> dict | None:
    m = re.search(r"\b(steps?|calories?|weight|sleep|water|workout)\b", lower)
    return {"metric": m.group(1) if m else ""}

def _ext_health_hrv(raw: str, lower: str) -> dict | None:
    m = re.search(r"\b(?:last\s+(\d+)\s+days?|today|this\s+week)\b", lower)
    days = int(m.group(1)) if m and m.group(1) else 7
    return {"days": days}


def _ext_connections_manage(_raw: str, _lower: str) -> dict | None:
    return {}


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
    "location_directions":  _ext_location_directions,
    "location_distance":    _ext_location_distance,
    "location_traffic":     _ext_location_traffic,
    "location_share":       _ext_location_share,
    "location_saved":       _ext_location_saved,
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
    # Wave-2 extractors
    "weather_hourly":           _ext_weather_hourly,
    "weather_radar":            _ext_weather_radar,
    "weather_air_quality":      _ext_weather_air_quality,
    "weather_astronomy":        _ext_weather_astronomy,
    "shopping_cart":            _ext_shopping_cart,
    "shopping_orders":          _ext_shopping_orders,
    "shopping_compare":         _ext_shopping_compare,
    "shopping_recommendations": _ext_shopping_recommendations,
    "news_trending":            _ext_news_trending,
    "news_by_source":           _ext_news_by_source,
    "contacts_list":            _ext_contacts_list,
    "contacts_edit":            _ext_contacts_edit,
    "contacts_delete":          _ext_contacts_delete,
    "contacts_favorite":        _ext_contacts_favorite,
    "social_react":             _ext_social_react,
    "social_comment":           _ext_social_comment,
    "social_follow_person":     _ext_social_follow_person,
    "social_notifications":     _ext_social_notifications,
    "social_trending":          _ext_social_trending,
    "terminal_ssh":             _ext_terminal_ssh,
    "terminal_env":             _ext_terminal_env,
    "terminal_output":          _ext_terminal_output,
    "banking_history":          _ext_banking_history,
    "banking_statement":        _ext_banking_statement,
    "finance_alert":            _ext_finance_alert,
    "finance_news":             _ext_finance_news,
    "reminder_recurring":       _ext_reminder_recurring,
    "task_priority":            _ext_task_priority,
    "task_due":                 _ext_task_due,
    "note_edit":                _ext_note_edit,
    "note_tag":                 _ext_note_tag,
    "calendar_invite":          _ext_calendar_invite,
    "calendar_availability":    _ext_calendar_availability,
    "calendar_recurring":       _ext_calendar_recurring,
    # Wave-3 extractors
    "messaging_send":           _ext_messaging_send,
    "messaging_read":           _ext_messaging_read,
    "messaging_reply":          _ext_messaging_reply,
    "messaging_delete":         _ext_messaging_delete,
    "messaging_search":         _ext_messaging_search,
    "messaging_group_create":   _ext_messaging_group_create,
    "messaging_group_add":      _ext_messaging_group_add,
    "messaging_react":          _ext_messaging_react_msg,
    "messaging_forward":        _ext_messaging_forward,
    "messaging_block":          _ext_messaging_block,
    "messaging_schedule":       _ext_messaging_schedule,
    "music_play":               _ext_music_play,
    "music_pause":              _ext_music_pause,
    "music_skip":               _ext_music_skip,
    "music_volume":             _ext_music_volume,
    "music_like":               _ext_music_like,
    "music_playlist_add":       _ext_music_playlist_add,
    "music_playlist_create":    _ext_music_playlist_create,
    "music_queue":              _ext_music_queue,
    "music_lyrics":             _ext_music_lyrics,
    "music_radio":              _ext_music_radio,
    "music_discover":           _ext_music_discover,
    "music_cast":               _ext_music_cast,
    "music_sleep_timer":        _ext_music_sleep_timer,
    "phone_call":               _ext_phone_call,
    "phone_voicemail":          _ext_phone_voicemail,
    "phone_recent":             _ext_phone_recent,
    "phone_block":              _ext_phone_block,
    "phone_conference":         _ext_phone_conference,
    "phone_record":             _ext_phone_record,
    "camera_photo":             _ext_camera_photo,
    "camera_video":             _ext_camera_video,
    "camera_scan_qr":           _ext_camera_scan_qr,
    "camera_scan_doc":          _ext_camera_scan_doc,
    "camera_ocr":               _ext_camera_ocr,
    "photos_search":            _ext_photos_search,
    "photos_album":             _ext_photos_album,
    "photos_share":             _ext_photos_share,
    "photos_edit":              _ext_photos_edit,
    "smarthome_lights":         _ext_smarthome_lights,
    "smarthome_thermostat":     _ext_smarthome_thermostat,
    "smarthome_lock":           _ext_smarthome_lock,
    "smarthome_camera":         _ext_smarthome_camera,
    "smarthome_appliance":      _ext_smarthome_appliance,
    "smarthome_scene":          _ext_smarthome_scene,
    "smarthome_energy":         _ext_smarthome_energy,
    "payments_send":            _ext_payments_send,
    "payments_request":         _ext_payments_request,
    "payments_split":           _ext_payments_split,
    "payments_history":         _ext_payments_history,
    "payments_balance":         _ext_payments_balance,
    "food_delivery_order":      _ext_food_delivery_order,
    "food_delivery_track":      _ext_food_delivery_track,
    "food_delivery_reorder":    _ext_food_delivery_reorder,
    "food_delivery_browse":     _ext_food_delivery_browse,
    "rideshare_book":           _ext_rideshare_book,
    "rideshare_track":          _ext_rideshare_track,
    "rideshare_schedule":       _ext_rideshare_schedule,
    "rideshare_cancel":         _ext_rideshare_cancel,
    "maps_search":              _ext_maps_search,
    "maps_save_place":          _ext_maps_save_place,
    "maps_explore":             _ext_maps_explore,
    "maps_review":              _ext_maps_review,
    "maps_share_eta":           _ext_maps_share_eta,
    "travel_flight_search":     _ext_travel_flight_search,
    "travel_flight_status":     _ext_travel_flight_status,
    "travel_boarding_pass":     _ext_travel_boarding_pass,
    "travel_hotel_search":      _ext_travel_hotel_search,
    "travel_hotel_book":        _ext_travel_hotel_book,
    "travel_checkin":           _ext_travel_checkin,
    "travel_itinerary":         _ext_travel_itinerary,
    "travel_car_rental":        _ext_travel_car_rental,
    "travel_alert":             _ext_travel_alert,
    "video_play":               _ext_video_play,
    "video_search":             _ext_video_search,
    "video_watchlist":          _ext_video_watchlist,
    "video_browse":             _ext_video_browse,
    "video_recommend":          _ext_video_recommend,
    "video_cast":               _ext_video_cast,
    "video_continue":           _ext_video_continue,
    "video_rate":               _ext_video_rate,
    "health_workout_log":       _ext_health_workout_log,
    "health_workout_start":     _ext_health_workout_start,
    "health_steps":             _ext_health_steps,
    "health_heart_rate":        _ext_health_heart_rate,
    "health_sleep":             _ext_health_sleep,
    "health_food_log":          _ext_health_food_log,
    "health_water":             _ext_health_water,
    "health_weight":            _ext_health_weight,
    "health_medication":        _ext_health_medication,
    "health_mood":              _ext_health_mood,
    "files_upload":             _ext_files_upload,
    "files_download":           _ext_files_download,
    "files_share":              _ext_files_share_file,
    "files_search":             _ext_files_search,
    "files_recent":             _ext_files_recent,
    "alarm_set":                _ext_alarm_set,
    "alarm_delete":             _ext_alarm_delete,
    "alarm_list":               _ext_alarm_list,
    "clock_world":              _ext_clock_world,
    "clock_stopwatch":          _ext_clock_stopwatch,
    "clock_bedtime":            _ext_clock_bedtime,
    "podcast_find":             _ext_podcast_find,
    "podcast_play":             _ext_podcast_play,
    "podcast_subscribe":        _ext_podcast_subscribe,
    "podcast_queue":            _ext_podcast_queue,
    "recipe_find":              _ext_recipe_find,
    "recipe_save":              _ext_recipe_save,
    "recipe_nutrition":         _ext_recipe_nutrition,
    "recipe_scale":             _ext_recipe_scale,
    "grocery_add":              _ext_grocery_add,
    "grocery_list":             _ext_grocery_list,
    "grocery_order":            _ext_grocery_order,
    "translate_text":           _ext_translate_text,
    "translate_detect":         _ext_translate_detect,
    "translate_conversation":   _ext_translate_conversation,
    "book_find":                _ext_book_find,
    "book_read":                _ext_book_read,
    "book_library":             _ext_book_library,
    "book_highlight":           _ext_book_highlight,
    "settings_wifi":            _ext_settings_wifi,
    "settings_bluetooth":       _ext_settings_bluetooth,
    "settings_brightness":      _ext_settings_brightness,
    "settings_dnd":             _ext_settings_dnd,
    "settings_airplane":        _ext_settings_airplane,
    "settings_battery":         _ext_settings_battery,
    "settings_storage":         _ext_settings_storage,
    "settings_notification":    _ext_settings_notification,
    # wave-4
    "notif_view":               _ext_notif_view,
    "notif_clear":              _ext_notif_clear,
    "notif_clear_app":          _ext_notif_clear_app,
    "notif_mark_read":          _ext_notif_mark_read,
    "notif_settings":           _ext_notif_settings,
    "handoff_airdrop":          _ext_handoff_airdrop,
    "handoff_clipboard":        _ext_handoff_clipboard,
    "handoff_continue":         _ext_handoff_continue,
    "handoff_screen_share":     _ext_handoff_screen_share,
    "jira_create":              _ext_jira_create,
    "jira_view":                _ext_jira_view,
    "jira_update":              _ext_jira_update,
    "jira_my_issues":           _ext_jira_my_issues,
    "jira_sprint":              _ext_jira_sprint,
    "github_pr_view":           _ext_github_pr_view,
    "github_issue_create":      _ext_github_issue_create,
    "github_my_prs":            _ext_github_my_prs,
    "github_repo_search":       _ext_github_repo_search,
    "github_commit":            _ext_github_commit,
    "gcal_list":                _ext_gcal_list,
    "gcal_create":              _ext_gcal_create,
    "gcal_update":              _ext_gcal_update,
    "gcal_delete":              _ext_gcal_delete,
    "gdrive_list":              _ext_gdrive_search,
    "gdrive_open":              _ext_gdrive_search,
    "gdrive_create":            _ext_gdrive_create,
    "gdrive_share":             _ext_gdrive_share,
    "slack_send":               _ext_slack_send,
    "slack_read":               _ext_slack_read,
    "slack_search":             _ext_slack_search,
    "slack_status":             _ext_slack_status,
    "slack_reaction":           _ext_slack_reaction,
    "notion_create":            _ext_notion_create,
    "notion_find":              _ext_notion_find,
    "notion_update":            _ext_notion_update,
    "notion_database":          _ext_notion_database,
    "asana_create":             _ext_asana_create,
    "asana_my_tasks":           _ext_asana_my_tasks,
    "asana_update":             _ext_asana_update,
    "asana_project":            _ext_asana_project,
    "wallet_passes":            _ext_wallet_passes,
    "wallet_loyalty":           _ext_wallet_loyalty,
    "wallet_gift_card":         _ext_wallet_gift_card,
    "wallet_coupon":            _ext_wallet_coupon,
    "vpn_connect":              _ext_vpn_connect,
    "vpn_disconnect":           _ext_vpn_disconnect,
    "vpn_status":               _ext_vpn_status,
    "focus_pomodoro":           _ext_focus_pomodoro,
    "focus_session":            _ext_focus_session,
    "focus_block":              _ext_focus_block,
    "focus_stats":              _ext_focus_stats,
    "dict_define":              _ext_dict_define,
    "dict_thesaurus":           _ext_dict_thesaurus,
    "dict_wikipedia":           _ext_dict_wikipedia,
    "dict_etymology":           _ext_dict_etymology,
    "password_find":            _ext_password_find,
    "password_generate":        _ext_password_generate,
    "password_2fa":             _ext_password_2fa,
    "password_update":          _ext_password_update,
    "app_find":                 _ext_app_find,
    "app_install":              _ext_app_install,
    "app_update":               _ext_app_update_apps,
    "reading_save":             _ext_reading_save,
    "reading_list":             _ext_reading_list_view,
    "reading_mark_read":        _ext_reading_mark_read,
    "date_days_until":          _ext_date_days_until,
    "date_countdown":           _ext_date_countdown,
    "date_day_of":              _ext_date_day_of,
    "date_age":                 _ext_date_age,
    "screen_screenshot":        _ext_screen_screenshot,
    "screen_record":            _ext_screen_record,
    "screen_mirror":            _ext_screen_mirror,
    "screen_split":             _ext_screen_split,
    "print_document":           _ext_print_document,
    "print_photo":              _ext_print_photo,
    "print_scan":               _ext_print_scan,
    "backup_now":               _ext_backup_now,
    "backup_status":            _ext_backup_status,
    "access_font":              _ext_access_font,
    "access_voice":             _ext_access_voice,
    "access_zoom":              _ext_access_zoom,
    "access_display":           _ext_access_display,
    "shortcut_run":             _ext_shortcut_run,
    "shortcut_create":          _ext_shortcut_create,
    "shortcut_list":            _ext_shortcut_list,
    "currency_convert":         _ext_currency_convert,
    "currency_rates":           _ext_currency_rates,
    "health_cycle":             _ext_health_cycle,
    "health_streak":            _ext_health_streak,
    "health_goals":             _ext_health_goals,
    "health_hrv":               _ext_health_hrv,
    "connections_manage":       _ext_connections_manage,
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
                  "log expense", "expense", "spent", "paid",
                  "order food", "food delivery", "deliver food", "doordash",
                  "uber eats", "grubhub", "from restaurant", "pizza delivery"],
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
        blockers=["reading list", "read later", "save article", "save to reading",
                  "bookmark this article", "save for later"],
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
                  "create a presentation", "make a presentation", "make a spreadsheet",
                  # Dictionary / password / health blockers — yield to domain intents
                  "define ", "my password", "password for", "generate password",
                  "generate a password", "my hrv", "heart rate variability",
                  "readiness score", "body battery"],
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
                  "add to calendar", "block time", "block off",
                  "uber", "lyft", "ride", "rideshare", "delivery", "doordash"],
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
        blockers=["unfollow", "stop following", "remove",
                  "podcast", "my order", "delivery order", "doordash order",
                  "uber eats", "grubhub", "track my package", "track package"],
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
                  "terminal", "command line", "shell", "a terminal",
                  "hotel", "hotels", "flight", "flights", "podcast", "recipe",
                  "restaurant", "book by", "novel", "book about",
                  "github", "pull request", "my prs", "open prs",
                  "an app for", "find an app", "app for"],
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

    # ── Google Calendar (gcal_* ops) ──────────────────────────────────────

    "gcal.list": Intent(
        id="gcal.list",
        op="gcal_list",
        domain="calendar",
        description="List events from Google Calendar",
        signals=["google calendar", "gcal", "my google events", "show google calendar"],
        patterns=[r"\bgoogle\s+calendar\b", r"\bgcal\b"],
        blockers=["create", "add", "schedule", "update", "delete", "cancel"],
        extractor="gcal_list",
        examples=["show my google calendar", "what's on my google calendar this week"],
        slots={"days": "number of days to look ahead"},
    ),

    "gcal.create": Intent(
        id="gcal.create",
        op="gcal_create",
        domain="calendar",
        description="Create an event in Google Calendar",
        signals=["add to google calendar", "create google calendar event",
                 "schedule on google calendar", "put on google calendar"],
        patterns=[r"\b(?:add|create|schedule)\s+(?:a\s+)?(?:google\s+calendar\s+)?event\b"],
        blockers=["show", "list", "delete", "update"],
        extractor="gcal_create",
        examples=["add a meeting to Google Calendar tomorrow at 3pm"],
        slots={"summary": "event title", "start": "start time", "end": "end time", "location": "location"},
    ),

    "gcal.update": Intent(
        id="gcal.update",
        op="gcal_update",
        domain="calendar",
        description="Update or reschedule a Google Calendar event",
        signals=["update google calendar event", "reschedule google calendar",
                 "change google calendar event", "move google calendar event"],
        patterns=[r"\b(?:update|reschedule|change|move)\s+(?:google\s+calendar\s+)?event\b"],
        extractor="gcal_update",
        examples=["reschedule my Google Calendar meeting to Friday"],
        slots={"summary": "event name", "eventId": "event ID"},
    ),

    "gcal.delete": Intent(
        id="gcal.delete",
        op="gcal_delete",
        domain="calendar",
        description="Delete an event from Google Calendar",
        signals=["delete google calendar event", "remove from google calendar",
                 "cancel google calendar event"],
        patterns=[r"\b(?:delete|remove|cancel)\s+(?:google\s+calendar\s+)?event\b"],
        extractor="gcal_delete",
        examples=["delete the Monday meeting from Google Calendar"],
        slots={"summary": "event name", "eventId": "event ID"},
    ),

    # ── Google Drive (gdrive_* ops) ───────────────────────────────────────

    "gdrive.list": Intent(
        id="gdrive.list",
        op="gdrive_list",
        domain="document",
        description="List files in Google Drive",
        signals=["google drive", "my drive", "show drive files", "gdrive",
                 "files in drive", "drive documents"],
        patterns=[r"\bgoogle\s+drive\b", r"\bmy\s+drive\b", r"\bgdrive\b"],
        blockers=["create", "new", "upload", "share"],
        extractor="gdrive_list",
        examples=["show my Google Drive files", "what's in my Drive"],
        slots={"query": "search query"},
    ),

    "gdrive.open": Intent(
        id="gdrive.open",
        op="gdrive_open",
        domain="document",
        description="Open a file in Google Drive",
        signals=["open drive file", "open google drive", "open in drive",
                 "open the doc", "open the sheet"],
        patterns=[r"\bopen\s+(?:google\s+drive\s+)?(?:file|doc|sheet|folder)\b"],
        extractor="gdrive_open",
        examples=["open the Q4 report in Google Drive", "open my budget spreadsheet"],
        slots={"query": "file name or query"},
    ),

    "gdrive.create": Intent(
        id="gdrive.create",
        op="gdrive_create",
        domain="document",
        description="Create a new file in Google Drive",
        signals=["create google doc", "new google doc", "create google sheet",
                 "new spreadsheet in drive", "create drive folder",
                 "new google slides", "make a google doc"],
        patterns=[r"\b(?:create|new|make)\s+(?:a\s+)?(?:google\s+)?(?:doc|sheet|slide|spreadsheet|presentation|folder)\b"],
        extractor="gdrive_create",
        examples=["create a new Google Doc for meeting notes", "make a Google Sheet for the budget"],
        slots={"name": "file name", "mimeType": "file type"},
    ),

    "gdrive.share": Intent(
        id="gdrive.share",
        op="gdrive_share",
        domain="document",
        description="Share a Google Drive file",
        signals=["share google drive file", "share drive file with",
                 "share the doc with", "share the sheet with"],
        patterns=[r"\bshare\s+(?:google\s+drive\s+)?(?:file|doc|sheet|folder)\b"],
        extractor="gdrive_share",
        examples=["share the Q4 report with sarah@example.com"],
        slots={"query": "file name", "email": "recipient email"},
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

    "location.directions": Intent(
        id="location.directions",
        op="location_directions",
        domain="system",
        description="Get directions or navigate from one place to another",
        signals=["directions to", "navigate to", "take me to", "how do i get to",
                 "get directions", "route to", "way to", "navigate me to"],
        patterns=[
            r"\b(?:directions?|navigate|navigation)\s+to\b",
            r"\bhow\s+(?:do\s+i|to)\s+get\s+to\b",
        ],
        blockers=["nearby", "near me", "around me"],
        extractor="location_directions",
        examples=["directions to O'Hare airport", "navigate to Wrigley Field",
                  "how do I get to downtown Chicago", "route to work"],
        slots={"destination": "where to go", "origin": "starting point",
               "mode": "driving|walking|cycling|transit"},
    ),

    "location.distance": Intent(
        id="location.distance",
        op="location_distance",
        domain="system",
        description="Find the distance between two places",
        signals=["how far is", "how far to", "distance from", "distance to",
                 "how many miles", "how many km", "how long to drive",
                 "how long to walk"],
        extractor="location_distance",
        examples=["how far is it to O'Hare", "distance from here to downtown",
                  "how far is New York from Chicago",
                  "how long to drive to Milwaukee"],
        slots={"from": "origin", "to": "destination"},
    ),

    "location.traffic": Intent(
        id="location.traffic",
        op="location_traffic",
        domain="system",
        description="Check traffic conditions on a route or to a destination",
        signals=["traffic", "how's traffic", "traffic conditions", "traffic report",
                 "congestion", "traffic on", "traffic to", "rush hour",
                 "how bad is traffic", "is there traffic"],
        blockers=["weather", "news"],
        extractor="location_traffic",
        examples=["how's traffic to work", "traffic on I-94",
                  "is there traffic downtown", "traffic report for my commute"],
        slots={"route": "road or destination", "location": "area"},
    ),

    "location.share": Intent(
        id="location.share",
        op="location_share",
        domain="system",
        description="Share the user's current location with someone",
        signals=["share my location", "send my location", "share location with",
                 "let them know where i am", "send where i am"],
        patterns=[r"\bshare\s+(?:my\s+)?location\b"],
        blockers=["share my screen", "share my feed"],
        extractor="location_share",
        examples=["share my location with Sarah",
                  "send my location to John", "let Mike know where I am"],
        slots={"with": "person to share with"},
    ),

    "location.saved": Intent(
        id="location.saved",
        op="location_saved",
        domain="system",
        description="View or manage saved places like home, work, and favorites",
        signals=["saved places", "my saved places", "saved locations", "my locations",
                 "set my home", "set home address", "set work address",
                 "add a saved place", "my home address", "my work address"],
        extractor="location_saved",
        examples=["show my saved places", "set my home to 123 Main St",
                  "update my work address", "my saved locations"],
        slots={"action": "list|add|remove", "label": "home|work|label name",
               "address": "address string"},
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
        blockers=["pay bill", "pay my bill", "pay the bill", "subscription",
                  "venmo", "paypal", "cash app", "cashapp", "peer-to-peer"],
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

    # ── Weather extensions ────────────────────────────────────────────────

    "weather.hourly": Intent(
        id="weather.hourly",
        op="weather_hourly",
        domain="weather",
        description="Get hour-by-hour weather forecast",
        signals=["hourly forecast", "hour by hour", "by the hour", "hourly weather",
                 "every hour", "each hour"],
        extractor="weather_hourly",
        examples=["show me the hourly forecast", "what's the weather hour by hour",
                  "hourly weather for Seattle"],
        slots={"location": "city or place", "window": "hourly"},
    ),

    "weather.radar": Intent(
        id="weather.radar",
        op="weather_radar",
        domain="weather",
        description="Show weather radar or precipitation map",
        signals=["radar", "weather radar", "precipitation map", "rain map",
                 "storm radar", "doppler"],
        extractor="weather_radar",
        examples=["show me the weather radar", "radar for Chicago",
                  "is there rain on the radar"],
        slots={"location": "city or place"},
    ),

    "weather.air_quality": Intent(
        id="weather.air_quality",
        op="weather_air_quality",
        domain="weather",
        description="Get air quality index for a location",
        signals=["air quality", "aqi", "air pollution", "pm2.5", "smog",
                 "ozone", "air index"],
        extractor="weather_air_quality",
        examples=["what's the air quality in LA", "check AQI for Denver",
                  "how's the air pollution today"],
        slots={"location": "city or place"},
    ),

    "weather.astronomy": Intent(
        id="weather.astronomy",
        op="weather_astronomy",
        domain="weather",
        description="Get sunrise, sunset, moon phase or other astronomy info",
        signals=["sunrise", "sunset", "moon phase", "moonrise", "moonset",
                 "golden hour", "dusk", "dawn"],
        extractor="weather_astronomy",
        examples=["what time is sunrise tomorrow", "when does the sun set today",
                  "what's the moon phase tonight"],
        slots={"location": "city or place", "query": "sunrise|sunset|moon|astronomy"},
    ),

    # ── Shopping extensions ───────────────────────────────────────────────

    "shopping.cart": Intent(
        id="shopping.cart",
        op="shopping_cart",
        domain="shopping",
        description="View or manage the shopping cart",
        signals=["my cart", "shopping cart", "view cart", "what's in my cart",
                 "check my cart", "cart total", "add to cart", "remove from cart"],
        blockers=["track", "order", "wishlist"],
        extractor="shopping_cart",
        examples=["show my cart", "what's in my shopping cart",
                  "how much is my cart total"],
        slots={},
    ),

    "shopping.orders": Intent(
        id="shopping.orders",
        op="shopping_orders",
        domain="shopping",
        description="View order history or a specific order",
        signals=["my orders", "order history", "past orders", "previous orders",
                 "recent orders", "order status", "where is my order"],
        blockers=["track package", "track shipment"],
        extractor="shopping_orders",
        examples=["show my recent orders", "what did I order last week",
                  "order history from Amazon"],
        slots={"orderId": "specific order ID if mentioned"},
    ),

    "shopping.compare": Intent(
        id="shopping.compare",
        op="shopping_compare",
        domain="shopping",
        description="Compare two or more products",
        signals=["compare", "vs", "versus", "which is better", "difference between",
                 "side by side"],
        blockers=["stock vs", "price vs", "team vs"],
        extractor="shopping_compare",
        examples=["compare iPhone 15 vs Samsung S24", "which is better: Kindle or iPad",
                  "compare noise canceling headphones"],
        slots={"item1": "first product", "item2": "second product"},
    ),

    "shopping.recommendations": Intent(
        id="shopping.recommendations",
        op="shopping_recommendations",
        domain="shopping",
        description="Get personalized product recommendations",
        signals=["recommend", "suggestions", "what should i buy", "gift ideas",
                 "best products", "top picks", "what's popular"],
        blockers=["movie recommendation", "book recommendation", "music recommendation",
                  "novel", "book to read", "what should i read", "podcast recommendation",
                  "recommend a book", "recommend a movie", "recommend a show"],
        extractor="shopping_recommendations",
        examples=["recommend a good laptop bag", "gift ideas for my dad",
                  "what are the best wireless earbuds"],
        slots={"category": "product category"},
    ),

    # ── News extensions ───────────────────────────────────────────────────

    "news.trending": Intent(
        id="news.trending",
        op="news_trending",
        domain="news",
        description="Show trending topics or viral news",
        signals=["trending", "what's trending", "viral", "going viral",
                 "trending topics", "top trends"],
        extractor="news_trending",
        examples=["what's trending right now", "show me viral news",
                  "what's trending on Twitter"],
        slots={"platform": "social platform if specified"},
    ),

    "news.by_source": Intent(
        id="news.by_source",
        op="news_by_source",
        domain="news",
        description="Get news from a specific publication or outlet",
        signals=["news from", "from the bbc", "from reuters", "from cnn",
                 "from the times", "from wired", "from techcrunch"],
        extractor="news_by_source",
        examples=["news from the BBC", "latest from Reuters",
                  "what does Wired have today"],
        slots={"source": "publication name"},
    ),

    # ── Contacts extensions ───────────────────────────────────────────────

    "contacts.list": Intent(
        id="contacts.list",
        op="contacts_list",
        domain="contacts",
        description="List or browse contacts",
        signals=["my contacts", "list contacts", "show contacts", "all contacts",
                 "contact list", "browse contacts"],
        extractor="contacts_list",
        examples=["show me my contacts", "list all contacts",
                  "open my contact list"],
        slots={},
    ),

    "contacts.edit": Intent(
        id="contacts.edit",
        op="contacts_edit",
        domain="contacts",
        description="Edit or update a contact's information",
        signals=["update contact", "edit contact", "change number", "update email",
                 "update address", "edit phone number"],
        extractor="contacts_edit",
        examples=["update John's phone number", "edit Sarah's contact",
                  "change Mike's email address"],
        slots={"name": "contact name"},
    ),

    "contacts.delete": Intent(
        id="contacts.delete",
        op="contacts_delete",
        domain="contacts",
        description="Delete a contact",
        signals=["delete contact", "remove contact", "delete from contacts",
                 "remove from contacts"],
        extractor="contacts_delete",
        examples=["delete John from contacts", "remove Sarah's contact",
                  "delete the contact for old work"],
        slots={"name": "contact name"},
    ),

    "contacts.favorite": Intent(
        id="contacts.favorite",
        op="contacts_favorite",
        domain="contacts",
        description="Favorite or unfavorite a contact",
        signals=["favorite", "star", "unstar", "unfavorite", "add to favorites",
                 "remove from favorites"],
        blockers=["restaurant", "place", "location"],
        extractor="contacts_favorite",
        examples=["favorite Mom's contact", "star Dad in contacts",
                  "remove Sarah from favorites"],
        slots={"action": "favorite|unfavorite", "name": "contact name"},
    ),

    # ── Social extensions ─────────────────────────────────────────────────

    "social.react": Intent(
        id="social.react",
        op="social_react",
        domain="social",
        description="React to a post with an emoji or reaction",
        signals=["react to", "like that post", "love that", "heart that",
                 "react with", "give a thumbs up"],
        extractor="social_react",
        examples=["react to that post with a heart", "like that tweet",
                  "give it a thumbs up"],
        slots={"reaction": "like|love|haha|wow|sad"},
    ),

    "social.comment": Intent(
        id="social.comment",
        op="social_comment",
        domain="social",
        description="Comment or reply on a social media post",
        signals=["comment on", "reply to that post", "leave a comment",
                 "write a comment", "comment saying"],
        extractor="social_comment",
        examples=["comment on that post saying great job",
                  "leave a comment: love this!", "reply to that tweet"],
        slots={"text": "comment text"},
    ),

    "social.follow": Intent(
        id="social.follow",
        op="social_follow",
        domain="social",
        description="Follow or unfollow someone on social media",
        signals=["follow", "unfollow", "stop following"],
        blockers=["follow up", "follow through", "follow along", "following news"],
        extractor="social_follow_person",
        examples=["follow @elonmusk on Twitter", "unfollow that account",
                  "stop following @nasa"],
        slots={"action": "follow|unfollow", "handle": "username or handle"},
    ),

    "social.notifications": Intent(
        id="social.notifications",
        op="social_notifications",
        domain="social",
        description="View social media notifications",
        signals=["social notifications", "my notifications", "check notifications",
                 "who liked my post", "who followed me"],
        blockers=["app notification", "system notification", "email notification",
                  "show my notifications", "show notifications", "open notifications",
                  "view notifications", "clear notifications", "notification settings"],
        extractor="social_notifications",
        examples=["check my social notifications", "who liked my post",
                  "see my Instagram notifications"],
        slots={},
    ),

    "social.trending": Intent(
        id="social.trending",
        op="social_trending",
        domain="social",
        description="Show trending content or hashtags on social media",
        signals=["trending hashtags", "trending on instagram", "trending on tiktok",
                 "what's hot on", "top hashtags", "viral content"],
        extractor="social_trending",
        examples=["what's trending on Instagram", "top hashtags on TikTok",
                  "viral content on Reddit"],
        slots={"platform": "social platform name"},
    ),

    # ── Terminal extensions ───────────────────────────────────────────────

    "terminal.ssh": Intent(
        id="terminal.ssh",
        op="terminal_ssh",
        domain="terminal",
        description="Open an SSH connection to a remote host",
        signals=["ssh", "ssh into", "ssh to", "connect to server",
                 "connect via ssh", "remote login"],
        extractor="terminal_ssh",
        examples=["ssh into myserver.com", "ssh user@192.168.1.1",
                  "connect to the staging server"],
        slots={"user": "remote username", "host": "hostname or IP"},
    ),

    "terminal.env": Intent(
        id="terminal.env",
        op="terminal_env",
        domain="terminal",
        description="View or set environment variables",
        signals=["environment variable", "env variable", "set env", "export var",
                 "list env", "show env", "env vars"],
        extractor="terminal_env",
        examples=["list all environment variables", "set NODE_ENV to production",
                  "export API_KEY=abc123"],
        slots={"action": "set|list", "var": "variable name", "value": "value to set"},
    ),

    "terminal.output": Intent(
        id="terminal.output",
        op="terminal_output",
        domain="terminal",
        description="Show the last terminal output or command result",
        signals=["last output", "show output", "terminal output", "command output",
                 "what did it print", "show the result", "show logs"],
        extractor="terminal_output",
        examples=["show the last command output", "what did the terminal print",
                  "show me the output"],
        slots={},
    ),

    # ── Banking extensions ────────────────────────────────────────────────

    "banking.history": Intent(
        id="banking.history",
        op="banking_history",
        domain="banking",
        description="View spending history or transaction categories over a period",
        signals=["spending history", "transaction history", "where did i spend",
                 "spending breakdown", "how much did i spend", "spending this month",
                 "spending report"],
        extractor="banking_history",
        examples=["show my spending history this month", "where did I spend money last week",
                  "spending breakdown by category"],
        slots={"window": "time period", "category": "spending category if specified"},
    ),

    "banking.statement": Intent(
        id="banking.statement",
        op="banking_statement",
        domain="banking",
        description="Get a bank account statement for a period",
        signals=["bank statement", "account statement", "monthly statement",
                 "statement for", "download statement"],
        extractor="banking_statement",
        examples=["show my bank statement for January", "get my account statement",
                  "download the March statement"],
        slots={"month": "statement month", "year": "statement year"},
    ),

    # ── Finance extensions ────────────────────────────────────────────────

    "finance.alert": Intent(
        id="finance.alert",
        op="finance_alert",
        domain="finance",
        description="Set a price alert for a stock or asset",
        signals=["price alert", "alert me when", "notify me when stock",
                 "set alert", "price target", "alert when price"],
        extractor="finance_alert",
        examples=["alert me when AAPL hits $200", "set a price alert for TSLA at $300",
                  "notify me if BTC drops below $40k"],
        slots={"symbol": "ticker", "threshold": "price level", "direction": "above|below"},
    ),

    "finance.news": Intent(
        id="finance.news",
        op="finance_news",
        domain="finance",
        description="Get news and analysis for a stock or company",
        signals=["stock news", "company news", "earnings news", "analyst report",
                 "market news for", "news on"],
        blockers=["trending news", "sports news", "weather news", "today's news"],
        extractor="finance_news",
        examples=["show me AAPL news", "what's the latest on Tesla",
                  "earnings news for Google"],
        slots={"symbol": "ticker symbol"},
    ),

    # ── Reminder extensions ───────────────────────────────────────────────

    "reminder.recurring": Intent(
        id="reminder.recurring",
        op="reminder_recurring",
        domain="reminder",
        description="Set a repeating reminder",
        signals=["every day", "every week", "every month", "daily reminder",
                 "weekly reminder", "remind me every", "recurring reminder",
                 "remind me daily"],
        extractor="reminder_recurring",
        examples=["remind me every day to drink water",
                  "set a weekly reminder to call Mom",
                  "daily reminder to take my meds at 8am"],
        slots={"text": "reminder text", "recurrence": "daily|weekly|monthly|weekday name"},
    ),

    # ── Task extensions ───────────────────────────────────────────────────

    "task.priority": Intent(
        id="task.priority",
        op="task_priority",
        domain="task",
        description="Set or change the priority of a task",
        signals=["set priority", "mark urgent", "high priority", "low priority",
                 "prioritize", "make it urgent", "urgent task"],
        extractor="task_priority",
        examples=["mark the budget task as high priority",
                  "set the report as urgent",
                  "low priority: update README"],
        slots={"selector": "task name or selector", "priority": "high|medium|low"},
    ),

    "task.due": Intent(
        id="task.due",
        op="task_due",
        domain="task",
        description="Set or update a due date on a task",
        signals=["due date", "set deadline", "due by", "task deadline",
                 "due tomorrow", "due friday", "due next week"],
        extractor="task_due",
        examples=["set the report due date to Friday",
                  "the invoice task is due tomorrow",
                  "set deadline for the project to next week"],
        slots={"selector": "task name", "date": "due date"},
    ),

    # ── Note extensions ───────────────────────────────────────────────────

    "note.edit": Intent(
        id="note.edit",
        op="note_edit",
        domain="note",
        description="Edit or append to an existing note",
        signals=["edit note", "update note", "change my note", "append to note",
                 "modify the note", "rewrite the note", "add to the note"],
        extractor="note_edit",
        examples=["edit my meeting notes from yesterday",
                  "append to the project note",
                  "update the grocery note"],
        slots={"name": "note name"},
    ),

    "note.tag": Intent(
        id="note.tag",
        op="note_tag",
        domain="note",
        description="Add or remove a tag from a note",
        signals=["tag the note", "label the note", "add tag", "tag it as",
                 "remove tag", "untag"],
        extractor="note_tag",
        examples=["tag the meeting note as work", "label the note as personal",
                  "remove the draft tag from the report note"],
        slots={"action": "tag|remove_tag", "tag": "tag name", "name": "note name"},
    ),

    # ── Calendar extensions ───────────────────────────────────────────────

    "calendar.invite": Intent(
        id="calendar.invite",
        op="calendar_invite",
        domain="calendar",
        description="Invite someone to a calendar event",
        signals=["invite", "add someone to", "include in the meeting",
                 "add attendee", "invite to the event", "send invite"],
        blockers=["invite code", "referral", "party invite"],
        extractor="calendar_invite",
        examples=["invite Sarah to the Monday standup",
                  "add Mike to the project kickoff meeting",
                  "send an invite to the team for Friday"],
        slots={"invitees": "person(s) to invite", "event": "event name"},
    ),

    "calendar.availability": Intent(
        id="calendar.availability",
        op="calendar_availability",
        domain="calendar",
        description="Check someone's availability or free time",
        signals=["availability", "available", "free time", "free slots",
                 "when is free", "open slots", "check schedule"],
        extractor="calendar_availability",
        examples=["check Sarah's availability this week",
                  "when is John free tomorrow",
                  "find a free slot for a meeting"],
        slots={"person": "person name", "date": "date or date range"},
    ),

    "calendar.recurring": Intent(
        id="calendar.recurring",
        op="calendar_recurring",
        domain="calendar",
        description="Create a recurring calendar event",
        signals=["recurring event", "repeating meeting", "weekly standup",
                 "every monday", "every week meeting", "repeat every"],
        extractor="calendar_recurring",
        examples=["create a weekly standup every Monday at 9am",
                  "schedule a monthly team review",
                  "add a recurring gym session every weekday at 7am"],
        slots={"title": "event title", "recurrence": "recurrence pattern",
               "time": "event time"},
    ),

    # ── Document extensions ───────────────────────────────────────────────

    "document.share": Intent(
        id="document.share",
        op="document_share",
        domain="document",
        description="Share a document with another person or team",
        signals=["share the document", "share the report", "share the letter",
                 "send the doc", "collaborate on", "give access to"],
        extractor="document",
        examples=["share the Q4 report with the team",
                  "send the proposal to Sarah",
                  "give John access to the contract"],
        slots={"action": "share", "name": "document name"},
    ),

    "document.rename": Intent(
        id="document.rename",
        op="document_rename",
        domain="document",
        description="Rename or move a document",
        signals=["rename the document", "rename the report", "rename the letter",
                 "move the document", "change document name"],
        extractor="document",
        examples=["rename the proposal to Q4 Proposal Final",
                  "rename my cover letter"],
        slots={"action": "rename", "name": "current name", "newName": "new name"},
    ),

    "document.template": Intent(
        id="document.template",
        op="document_template",
        domain="document",
        description="Create a document from a template",
        signals=["document template", "use a template", "from a template",
                 "start from template", "template for"],
        extractor="document",
        examples=["create a document from the invoice template",
                  "use the contract template",
                  "start a report from a template"],
        slots={"action": "template", "template": "template name"},
    ),

    # ── Spreadsheet extensions ────────────────────────────────────────────

    "spreadsheet.formula": Intent(
        id="spreadsheet.formula",
        op="spreadsheet_formula",
        domain="spreadsheet",
        description="Insert or explain a spreadsheet formula",
        signals=["formula", "function in spreadsheet", "sum the column",
                 "vlookup", "countif", "average the", "formula for"],
        extractor="spreadsheet",
        examples=["add a SUM formula to the budget spreadsheet",
                  "insert a VLOOKUP for the data",
                  "calculate the average in column B"],
        slots={"action": "formula", "formula": "formula type or expression"},
    ),

    "spreadsheet.chart": Intent(
        id="spreadsheet.chart",
        op="spreadsheet_chart",
        domain="spreadsheet",
        description="Create a chart or graph from spreadsheet data",
        signals=["chart", "graph", "bar chart", "pie chart", "line chart",
                 "plot the data", "visualize the spreadsheet", "make a graph"],
        blockers=["weather chart", "stock chart", "sports chart"],
        extractor="spreadsheet",
        examples=["add a bar chart to the sales spreadsheet",
                  "create a pie chart from the budget data",
                  "plot the expense data as a line graph"],
        slots={"action": "chart", "chartType": "bar|line|pie|scatter"},
    ),

    # ── Presentation extensions ───────────────────────────────────────────

    "presentation.template": Intent(
        id="presentation.template",
        op="presentation_template",
        domain="presentation",
        description="Create a presentation from a template",
        signals=["presentation template", "slide template", "deck template",
                 "from a template", "use a template for the deck"],
        extractor="presentation",
        examples=["create a deck from the company template",
                  "use the pitch deck template",
                  "start a presentation from a template"],
        slots={"action": "template", "template": "template name"},
    ),

    "presentation.share": Intent(
        id="presentation.share",
        op="presentation_share",
        domain="presentation",
        description="Share a presentation with others",
        signals=["share the presentation", "share the deck", "share the slides",
                 "send the deck", "share slideshow"],
        extractor="presentation",
        examples=["share the pitch deck with investors",
                  "send the slides to the team",
                  "share the presentation with Sarah"],
        slots={"action": "share", "name": "presentation name"},
    ),

    "presentation.speaker_notes": Intent(
        id="presentation.speaker_notes",
        op="presentation_speaker_notes",
        domain="presentation",
        description="Add or view speaker notes on a slide",
        signals=["speaker notes", "presenter notes", "add notes to slide",
                 "slide notes", "speaking notes"],
        extractor="presentation",
        examples=["add speaker notes to slide 3",
                  "show the presenter notes",
                  "write speaking notes for the intro slide"],
        slots={"action": "speaker_notes", "slide": "slide number or name"},
    ),

    # ── Code extensions ───────────────────────────────────────────────────

    "code.refactor": Intent(
        id="code.refactor",
        op="code_refactor",
        domain="code",
        description="Refactor or restructure code",
        signals=["refactor", "restructure the code", "reorganize", "clean up the code",
                 "improve code structure", "refactor this"],
        extractor="code",
        examples=["refactor the authentication module",
                  "clean up the payment service code",
                  "restructure the database layer"],
        slots={"action": "refactor", "target": "file or module"},
    ),

    "code.optimize": Intent(
        id="code.optimize",
        op="code_optimize",
        domain="code",
        description="Optimize code for performance",
        signals=["optimize", "improve performance", "speed up the code",
                 "make it faster", "performance optimization", "reduce latency"],
        extractor="code",
        examples=["optimize the search function",
                  "speed up the image loader",
                  "improve the performance of the API handler"],
        slots={"action": "optimize", "target": "function or module"},
    ),

    # ── Email extensions ──────────────────────────────────────────────────

    "email.unsubscribe": Intent(
        id="email.unsubscribe",
        op="email_unsubscribe",
        domain="email",
        description="Unsubscribe from an email list or newsletter",
        signals=["unsubscribe", "stop emails from", "opt out of emails",
                 "remove from mailing list", "cancel subscription email"],
        extractor="email",
        examples=["unsubscribe from the newsletter", "stop emails from Groupon",
                  "opt out of marketing emails"],
        slots={"action": "unsubscribe"},
    ),

    "email.label": Intent(
        id="email.label",
        op="email_label",
        domain="email",
        description="Label, tag, or file an email",
        signals=["label the email", "tag this email", "categorize email",
                 "move to folder", "file this email", "move email to"],
        extractor="email",
        examples=["label this email as work", "move to the receipts folder",
                  "categorize this as personal"],
        slots={"action": "label", "label": "label or folder name"},
    ),

    "email.snooze": Intent(
        id="email.snooze",
        op="email_snooze",
        domain="email",
        description="Snooze an email to resurface it later",
        signals=["snooze this email", "snooze the email", "remind me about this email",
                 "come back to this email", "resurface later"],
        extractor="email",
        examples=["snooze this email until tomorrow",
                  "remind me about this email on Monday",
                  "snooze for next week"],
        slots={"action": "snooze", "date": "date to resurface"},
    ),

    # ── Messaging ─────────────────────────────────────────────────────────

    "messaging.send": Intent(
        id="messaging.send",
        op="messaging_send",
        domain="messaging",
        description="Send a text or chat message to someone",
        signals=["text", "message", "send a message", "shoot a text", "msg",
                 "send a text", "iMessage", "whatsapp", "dm"],
        blockers=["email", "voice message", "voicemail",
                  "slack", "in slack", "on slack", "slack message",
                  "text larger", "text smaller", "font size", "text size",
                  "the text larger", "the text smaller", "larger text", "make the text"],
        patterns=[r"\b(?:text|message)\s+[A-Z][a-z]+"],
        extractor="messaging_send",
        examples=["text Mom I'll be late", "send a message to John saying hey",
                  "WhatsApp Sarah that I'm on my way"],
        slots={"recipient": "person name", "body": "message text",
               "platform": "messaging platform"},
    ),

    "messaging.read": Intent(
        id="messaging.read",
        op="messaging_read",
        domain="messaging",
        description="Read messages from a contact or inbox",
        signals=["read my texts", "my messages", "check messages", "show messages",
                 "read messages from", "unread messages", "my inbox",
                 "texts from", "messages from"],
        blockers=["email inbox", "read my email"],
        extractor="messaging_read",
        examples=["show messages from Sarah", "read my unread texts",
                  "check my WhatsApp messages"],
        slots={"contact": "sender name", "platform": "messaging platform"},
    ),

    "messaging.reply": Intent(
        id="messaging.reply",
        op="messaging_reply",
        domain="messaging",
        description="Reply to a message",
        signals=["reply to", "respond to the message", "write back", "reply saying",
                 "text back", "message back"],
        extractor="messaging_reply",
        examples=["reply to Sarah's text saying I'll be there at 7",
                  "respond to John's message with OK",
                  "text back saying on my way"],
        slots={"body": "reply text", "platform": "messaging platform"},
    ),

    "messaging.delete": Intent(
        id="messaging.delete",
        op="messaging_delete",
        domain="messaging",
        description="Delete a conversation or message",
        signals=["delete conversation", "delete messages", "clear chat",
                 "remove the conversation", "delete the thread"],
        extractor="messaging_delete",
        examples=["delete my conversation with John",
                  "clear the chat with Sarah",
                  "remove messages from that number"],
        slots={"contact": "contact name"},
    ),

    "messaging.search": Intent(
        id="messaging.search",
        op="messaging_search",
        domain="messaging",
        description="Search through messages",
        signals=["search messages", "find a message", "look for a text",
                 "search my texts", "find texts about", "search conversations"],
        extractor="messaging_search",
        examples=["search messages for the address Sarah sent",
                  "find texts about the meeting",
                  "look for the message with the tracking number"],
        slots={"query": "search query"},
    ),

    "messaging.group_create": Intent(
        id="messaging.group_create",
        op="messaging_group_create",
        domain="messaging",
        description="Create a group chat",
        signals=["group chat", "create a group", "group message", "new group",
                 "start a group", "group text"],
        extractor="messaging_group_create",
        examples=["create a group chat called Weekend Plans",
                  "start a group text with Mom and Dad",
                  "make a group with the team"],
        slots={"name": "group name", "platform": "messaging platform"},
    ),

    "messaging.group_add": Intent(
        id="messaging.group_add",
        op="messaging_group_add",
        domain="messaging",
        description="Add someone to a group chat",
        signals=["add to the group", "add to the chat", "add someone to",
                 "include in the group"],
        extractor="messaging_group_add",
        examples=["add Sarah to the Weekend Plans group",
                  "add Mike to our chat",
                  "include John in the group chat"],
        slots={"person": "person to add", "group": "group name"},
    ),

    "messaging.react": Intent(
        id="messaging.react",
        op="messaging_react",
        domain="messaging",
        description="React to a message with an emoji",
        signals=["react to the message", "like that message", "heart that text",
                 "thumbs up the message", "react with"],
        extractor="messaging_react",
        examples=["react to that message with a heart",
                  "thumbs up the last text",
                  "like Sarah's message"],
        slots={"reaction": "reaction type"},
    ),

    "messaging.forward": Intent(
        id="messaging.forward",
        op="messaging_forward",
        domain="messaging",
        description="Forward a message to another contact",
        signals=["forward this message", "forward that text", "send this message to",
                 "pass this along to"],
        extractor="messaging_forward",
        examples=["forward this message to Dad",
                  "send that text to my boss",
                  "forward Sarah's message to the group"],
        slots={"recipient": "person to forward to"},
    ),

    "messaging.block": Intent(
        id="messaging.block",
        op="messaging_block",
        domain="messaging",
        description="Block a contact from messaging",
        signals=["block messages from", "block that number", "block this contact",
                 "stop messages from", "block sender"],
        extractor="messaging_block",
        examples=["block messages from that number",
                  "block this contact",
                  "stop texts from John"],
        slots={"contact": "contact or number to block"},
    ),

    "messaging.schedule": Intent(
        id="messaging.schedule",
        op="messaging_schedule",
        domain="messaging",
        description="Schedule a message to be sent later",
        signals=["schedule a message", "send a message later", "schedule a text",
                 "send this text tomorrow", "message later"],
        extractor="messaging_schedule",
        examples=["schedule a text to Mom for 8am",
                  "send this message to John tomorrow morning",
                  "schedule a happy birthday text for midnight"],
        slots={"recipient": "recipient", "body": "message text", "time": "send time"},
    ),

    # ── Music ──────────────────────────────────────────────────────────────

    "music.play": Intent(
        id="music.play",
        op="music_play",
        domain="music",
        description="Play a song, artist, album, or playlist",
        signals=["play", "listen to", "put on", "queue up", "stream",
                 "play some", "play me"],
        blockers=["play a video", "play a podcast", "play an audiobook",
                  "play next episode", "play movie", "play show"],
        extractor="music_play",
        examples=["play Bohemian Rhapsody", "play Taylor Swift on Spotify",
                  "listen to the chill playlist", "put on some jazz"],
        slots={"query": "song/artist/album/playlist", "artist": "artist name",
               "platform": "music platform"},
    ),

    "music.pause": Intent(
        id="music.pause",
        op="music_pause",
        domain="music",
        description="Pause or resume music playback",
        signals=["pause music", "pause the music", "stop the music",
                 "resume music", "resume playback", "unpause"],
        blockers=["pause the video", "pause the show"],
        extractor="music_pause",
        examples=["pause the music", "stop playing", "resume the song"],
        slots={"action": "pause|resume"},
    ),

    "music.skip": Intent(
        id="music.skip",
        op="music_skip",
        domain="music",
        description="Skip to the next or previous track",
        signals=["next song", "skip", "previous song", "go back", "next track",
                 "skip this song", "play the next song"],
        blockers=["skip to next episode", "next episode"],
        extractor="music_skip",
        examples=["skip this song", "next track", "go back to the previous song"],
        slots={"direction": "next|previous"},
    ),

    "music.volume": Intent(
        id="music.volume",
        op="music_volume",
        domain="music",
        description="Adjust music volume",
        signals=["volume up", "volume down", "turn it up", "turn it down",
                 "louder", "quieter", "mute the music", "set volume"],
        blockers=["turn up the thermostat", "turn down the lights"],
        extractor="music_volume",
        examples=["turn the music up", "lower the volume to 50%",
                  "mute", "make it louder"],
        slots={"action": "up|down|mute|set", "level": "volume 0-100"},
    ),

    "music.like": Intent(
        id="music.like",
        op="music_like",
        domain="music",
        description="Like or unlike the current song",
        signals=["like this song", "heart this song", "save this song",
                 "unlike this song", "dislike", "thumbs up the song",
                 "add to liked songs"],
        extractor="music_like",
        examples=["like this song", "heart the current track",
                  "add this to my liked songs"],
        slots={"action": "like|unlike"},
    ),

    "music.playlist_add": Intent(
        id="music.playlist_add",
        op="music_playlist_add",
        domain="music",
        description="Add a song to a playlist",
        signals=["add to playlist", "add this song to", "save to playlist",
                 "add to my playlist", "put this in"],
        extractor="music_playlist_add",
        examples=["add this song to my workout playlist",
                  "save this track to Chill Vibes",
                  "add Blinding Lights to the road trip playlist"],
        slots={"song": "song name", "playlist": "playlist name"},
    ),

    "music.playlist_create": Intent(
        id="music.playlist_create",
        op="music_playlist_create",
        domain="music",
        description="Create a new music playlist",
        signals=["create a playlist", "make a playlist", "new playlist",
                 "start a playlist", "build a playlist"],
        extractor="music_playlist_create",
        examples=["create a playlist called Road Trip",
                  "make a workout playlist",
                  "new playlist for the party"],
        slots={"name": "playlist name"},
    ),

    "music.queue": Intent(
        id="music.queue",
        op="music_queue",
        domain="music",
        description="View or manage the music queue",
        signals=["what's in the queue", "show the queue", "add to queue",
                 "play next", "music queue", "up next"],
        extractor="music_queue",
        examples=["what's in the queue", "add this to the queue",
                  "play next: Hotel California"],
        slots={"action": "view|add", "song": "song to add"},
    ),

    "music.lyrics": Intent(
        id="music.lyrics",
        op="music_lyrics",
        domain="music",
        description="Show lyrics for the current or a specific song",
        signals=["lyrics", "show lyrics", "what are the lyrics",
                 "song lyrics", "lyrics for"],
        extractor="music_lyrics",
        examples=["show lyrics for this song", "what are the lyrics to Bohemian Rhapsody",
                  "lyrics"],
        slots={"song": "song name or blank for current"},
    ),

    "music.radio": Intent(
        id="music.radio",
        op="music_radio",
        domain="music",
        description="Start a radio station based on an artist or song",
        signals=["radio", "start a radio", "artist radio", "song radio",
                 "similar music", "based on"],
        blockers=["news radio", "talk radio", "fm radio", "am radio"],
        extractor="music_radio",
        examples=["start a Beatles radio", "play music like Coldplay",
                  "Fleetwood Mac radio on Spotify"],
        slots={"seed": "artist or song name", "platform": "platform"},
    ),

    "music.discover": Intent(
        id="music.discover",
        op="music_discover",
        domain="music",
        description="Discover new music by mood, genre, or taste",
        signals=["discover music", "recommend music", "music recommendations",
                 "suggest music", "new music", "something new to listen to",
                 "music for working out", "music to relax"],
        extractor="music_discover",
        examples=["recommend some chill music", "discover new hip-hop",
                  "suggest something to listen to while I work"],
        slots={"genre": "music genre", "mood": "mood or activity"},
    ),

    "music.cast": Intent(
        id="music.cast",
        op="music_cast",
        domain="music",
        description="Cast or play music on another device or speaker",
        signals=["play on", "cast to", "send to the speaker", "play on the",
                 "airplay to", "connect to speaker", "play through"],
        blockers=["play on spotify", "play on apple music"],
        extractor="music_cast",
        examples=["play this on the kitchen speaker",
                  "cast to the living room Sonos",
                  "airplay to the HomePod"],
        slots={"device": "target device name"},
    ),

    "music.sleep_timer": Intent(
        id="music.sleep_timer",
        op="music_sleep_timer",
        domain="music",
        description="Set a timer to stop music after a duration",
        signals=["sleep timer", "stop music after", "turn off music in",
                 "stop playing in", "music off after"],
        extractor="music_sleep_timer",
        examples=["set a sleep timer for 30 minutes",
                  "stop the music in an hour",
                  "turn off music after 45 minutes"],
        slots={"duration": "duration number", "unit": "minute|hour"},
    ),

    # ── Phone / Calls ──────────────────────────────────────────────────────

    "phone.call": Intent(
        id="phone.call",
        op="phone_call",
        domain="phone",
        description="Make a phone call to a contact or number",
        signals=["call", "dial", "phone", "ring", "give a call", "make a call",
                 "call up", "facetime"],
        blockers=["call the api", "function call", "called the",
                  "back up", "backup", "my phone now", "back up my phone",
                  "phone storage", "phone battery"],
        patterns=[r"\b(?:call|dial|phone|ring)\s+[A-Z][a-z]+"],
        extractor="phone_call",
        examples=["call Mom", "dial 555-1234", "ring John",
                  "FaceTime Sarah"],
        slots={"contact": "name or number to call"},
    ),

    "phone.voicemail": Intent(
        id="phone.voicemail",
        op="phone_voicemail",
        domain="phone",
        description="Listen to voicemail messages",
        signals=["voicemail", "check voicemail", "listen to voicemail",
                 "my voicemails", "new voicemail"],
        extractor="phone_voicemail",
        examples=["check my voicemail", "listen to my voicemails",
                  "do I have any voicemail"],
        slots={},
    ),

    "phone.recent": Intent(
        id="phone.recent",
        op="phone_recent",
        domain="phone",
        description="View recent or missed calls",
        signals=["recent calls", "missed calls", "call history", "who called",
                 "who did I call", "last call"],
        extractor="phone_recent",
        examples=["show my recent calls", "did I miss any calls",
                  "call history"],
        slots={},
    ),

    "phone.block": Intent(
        id="phone.block",
        op="phone_block",
        domain="phone",
        description="Block a phone number or contact",
        signals=["block this number", "block calls from", "blacklist number",
                 "stop calls from", "block that caller"],
        extractor="phone_block",
        examples=["block this number", "block calls from 555-1234",
                  "blacklist that caller"],
        slots={"contact": "number or contact to block"},
    ),

    "phone.conference": Intent(
        id="phone.conference",
        op="phone_conference",
        domain="phone",
        description="Set up a conference or group call",
        signals=["conference call", "group call", "three-way call", "add to the call",
                 "merge calls", "add someone to the call"],
        extractor="phone_conference",
        examples=["set up a conference call with Sarah and John",
                  "three-way call with Mom and Dad",
                  "add Mike to the current call"],
        slots={"participants": "list of participants"},
    ),

    "phone.record": Intent(
        id="phone.record",
        op="phone_record",
        domain="phone",
        description="Record a phone call",
        signals=["record this call", "record the call", "call recording",
                 "start recording", "save this call"],
        blockers=["my screen", "the screen", "screen recording", "screen record"],
        extractor="phone_record",
        examples=["record this call", "start call recording",
                  "save this conversation"],
        slots={},
    ),

    # ── Camera / Photos ────────────────────────────────────────────────────

    "camera.photo": Intent(
        id="camera.photo",
        op="camera_photo",
        domain="camera",
        description="Take a photo",
        signals=["take a photo", "take a picture", "snap a photo", "selfie",
                 "open camera", "camera", "take a pic"],
        blockers=["attach a photo", "share a photo", "find a photo"],
        extractor="camera_photo",
        examples=["take a photo", "selfie", "snap a picture",
                  "open the camera"],
        slots={},
    ),

    "camera.video": Intent(
        id="camera.video",
        op="camera_video",
        domain="camera",
        description="Record a video",
        signals=["record a video", "take a video", "start recording", "video mode",
                 "shoot a video", "record this"],
        blockers=["watch a video", "play a video", "stream a video",
                  "my screen", "the screen", "screen recording", "screen record"],
        extractor="camera_video",
        examples=["record a video", "take a 30-second video",
                  "start recording"],
        slots={"duration": "seconds if specified"},
    ),

    "camera.scan_qr": Intent(
        id="camera.scan_qr",
        op="camera_scan_qr",
        domain="camera",
        description="Scan a QR code",
        signals=["scan qr", "scan qr code", "read qr", "qr code scanner",
                 "scan this code", "scan barcode"],
        extractor="camera_scan_qr",
        examples=["scan the QR code", "read this barcode",
                  "scan the code on the menu"],
        slots={},
    ),

    "camera.scan_doc": Intent(
        id="camera.scan_doc",
        op="camera_scan_doc",
        domain="camera",
        description="Scan a document using the camera",
        signals=["scan a receipt", "document scanner", "scan and save", "scan to pdf",
                 "scan the receipt", "scan the contract", "scan the form"],
        blockers=["scan this document", "scan document", "use scanner", "use the scanner"],
        extractor="camera_scan_doc",
        examples=["scan this document", "scan the receipt",
                  "scan the contract to PDF"],
        slots={},
    ),

    "camera.ocr": Intent(
        id="camera.ocr",
        op="camera_ocr",
        domain="camera",
        description="Extract text from an image",
        signals=["read the text", "extract text from image", "text in photo",
                 "copy text from", "live text", "what does the sign say",
                 "read what's in the image"],
        extractor="camera_ocr",
        examples=["read the text in this photo", "extract the text from this image",
                  "what does that sign say"],
        slots={},
    ),

    "photos.search": Intent(
        id="photos.search",
        op="photos_search",
        domain="photos",
        description="Search the photo library by person, place, or date",
        signals=["find photos", "search photos", "photos of", "pictures of",
                 "photos from", "photos at", "show me photos"],
        blockers=["find a photo online", "search for a photo online"],
        extractor="photos_search",
        examples=["find photos of Sarah", "photos from last Christmas",
                  "pictures from my trip to Paris"],
        slots={"person": "person name", "date": "date or period",
               "location": "place name"},
    ),

    "photos.album": Intent(
        id="photos.album",
        op="photos_album",
        domain="photos",
        description="Create a photo album",
        signals=["create an album", "make an album", "new album",
                 "photo album", "organize photos into"],
        extractor="photos_album",
        examples=["create a photo album called Summer 2025",
                  "make an album for the wedding photos",
                  "new album: Family Vacation"],
        slots={"name": "album name"},
    ),

    "photos.share": Intent(
        id="photos.share",
        op="photos_share",
        domain="photos",
        description="Share a photo or album with someone",
        signals=["share this photo", "send this picture", "share the photo with",
                 "share these photos", "send these photos"],
        blockers=["share a file", "share a document"],
        extractor="photos_share",
        examples=["share this photo with Sarah",
                  "send these vacation pictures to Mom",
                  "share the album with the family"],
        slots={"recipient": "person to share with"},
    ),

    "photos.edit": Intent(
        id="photos.edit",
        op="photos_edit",
        domain="photos",
        description="Edit a photo — crop, filter, adjust brightness, etc.",
        signals=["edit this photo", "crop the photo", "filter", "adjust brightness",
                 "black and white", "photo edit", "enhance photo", "remove background"],
        extractor="photos_edit",
        examples=["crop this photo", "apply a black and white filter",
                  "brighten this picture", "remove the background"],
        slots={"edit": "edit type"},
    ),

    # ── Smart Home ─────────────────────────────────────────────────────────

    "smarthome.lights": Intent(
        id="smarthome.lights",
        op="smarthome_lights",
        domain="smarthome",
        description="Control smart lights — on, off, dim, or change color",
        signals=["turn on the lights", "turn off the lights", "dim the lights",
                 "lights on", "lights off", "light", "bedroom lights",
                 "kitchen lights", "living room lights", "change light color"],
        blockers=["traffic light", "flash light", "spotlight on"],
        extractor="smarthome_lights",
        examples=["turn off the bedroom lights", "dim the living room to 50%",
                  "set the kitchen lights to warm white",
                  "turn on all the lights"],
        slots={"action": "on|off|dim|color", "room": "room name",
               "level": "0-100", "color": "color name"},
    ),

    "smarthome.thermostat": Intent(
        id="smarthome.thermostat",
        op="smarthome_thermostat",
        domain="smarthome",
        description="Set or check the thermostat temperature",
        signals=["thermostat", "set the temperature", "temperature to",
                 "heat to", "cool to", "what is the temperature", "set heat",
                 "make it warmer", "make it cooler", "hvac"],
        blockers=["weather temperature", "body temperature", "fever"],
        extractor="smarthome_thermostat",
        examples=["set the thermostat to 72", "heat the house to 68 degrees",
                  "what's the thermostat set to", "cool it down"],
        slots={"action": "set|check", "temperature": "degrees",
               "mode": "heat|cool|auto"},
    ),

    "smarthome.lock": Intent(
        id="smarthome.lock",
        op="smarthome_lock",
        domain="smarthome",
        description="Lock or unlock smart door locks",
        signals=["lock the door", "unlock the door", "front door lock",
                 "lock the house", "unlock the front door", "is the door locked",
                 "lock the front door", "lock the back door", "door locked",
                 "smart lock"],
        patterns=[r"\b(?:lock|unlock)\s+(?:the\s+)?(?:front|back|garage|side)?\s*door\b"],
        blockers=["lock the screen", "screen lock"],
        extractor="smarthome_lock",
        examples=["lock the front door", "unlock the back door",
                  "is the front door locked"],
        slots={"action": "lock|unlock", "door": "door identifier"},
    ),

    "smarthome.camera": Intent(
        id="smarthome.camera",
        op="smarthome_camera",
        domain="smarthome",
        description="View a security or home camera feed",
        signals=["security camera", "front door camera", "baby monitor",
                 "show the camera", "check the camera", "driveway camera",
                 "backyard camera", "view the feed"],
        blockers=["take a photo with camera", "camera app"],
        extractor="smarthome_camera",
        examples=["show the front door camera", "check the baby monitor",
                  "view the backyard security camera"],
        slots={"camera": "camera location or name"},
    ),

    "smarthome.appliance": Intent(
        id="smarthome.appliance",
        op="smarthome_appliance",
        domain="smarthome",
        description="Control a smart home appliance",
        signals=["start the dishwasher", "run the washer", "start the dryer",
                 "turn on the oven", "robot vacuum", "roomba", "coffee maker",
                 "start the washer", "run the dishwasher"],
        extractor="smarthome_appliance",
        examples=["start the dishwasher", "run the robot vacuum",
                  "turn on the coffee maker", "start the dryer"],
        slots={"appliance": "appliance name", "action": "on|off|start"},
    ),

    "smarthome.scene": Intent(
        id="smarthome.scene",
        op="smarthome_scene",
        domain="smarthome",
        description="Activate a smart home scene or routine",
        signals=["movie mode", "good morning", "good night", "bedtime mode",
                 "away mode", "home mode", "activate scene", "run routine",
                 "dinner mode", "party mode"],
        extractor="smarthome_scene",
        examples=["activate movie mode", "run the good morning routine",
                  "set the house to away mode",
                  "turn on bedtime mode"],
        slots={"scene": "scene or routine name"},
    ),

    "smarthome.energy": Intent(
        id="smarthome.energy",
        op="smarthome_energy",
        domain="smarthome",
        description="Check home energy usage",
        signals=["energy usage", "power consumption", "electricity bill",
                 "how much power", "energy report", "kwh", "solar output"],
        extractor="smarthome_energy",
        examples=["show my energy usage today", "how much power is the house using",
                  "what's my electricity consumption this month"],
        slots={"window": "time period"},
    ),

    # ── Payments ───────────────────────────────────────────────────────────

    "payments.send": Intent(
        id="payments.send",
        op="payments_send",
        domain="payments",
        description="Send money to someone via Venmo, Zelle, PayPal, etc.",
        signals=["send money", "pay", "venmo", "zelle", "paypal", "cash app",
                 "transfer money", "send payment", "split the bill"],
        blockers=["pay the bill", "pay with credit", "payment failed"],
        patterns=[r"\bsend\s+\$\d+|\bvenmo\s+[A-Z]|\bpay\s+[A-Z][a-z]+\s+\$"],
        extractor="payments_send",
        examples=["send $20 to Sarah on Venmo", "Zelle John $50 for dinner",
                  "PayPal Mom $100"],
        slots={"recipient": "person", "amount": "dollar amount",
               "note": "payment note", "platform": "payment platform"},
    ),

    "payments.request": Intent(
        id="payments.request",
        op="payments_request",
        domain="payments",
        description="Request money from someone",
        signals=["request money", "charge", "ask for money", "request payment",
                 "request $", "collect money", "invoice"],
        extractor="payments_request",
        examples=["request $30 from Mike for lunch", "charge Sarah $15 for the movie",
                  "ask John to pay me back $50"],
        slots={"recipient": "person", "amount": "dollar amount", "note": "reason"},
    ),

    "payments.split": Intent(
        id="payments.split",
        op="payments_split",
        domain="payments",
        description="Split a bill between people",
        signals=["split", "divide the bill", "split the check", "split evenly",
                 "split between", "each person owes"],
        extractor="payments_split",
        examples=["split $120 four ways", "divide the dinner bill with Sarah and Mike",
                  "split the check equally"],
        slots={"amount": "total amount", "people": "list of people",
               "ways": "number of ways"},
    ),

    "payments.history": Intent(
        id="payments.history",
        op="payments_history",
        domain="payments",
        description="View payment history or past transactions",
        signals=["payment history", "past payments", "who did I pay",
                 "payment transactions", "recent payments", "venmo history",
                 "zelle history"],
        extractor="payments_history",
        examples=["show my Venmo history", "who did I pay this week",
                  "recent payment transactions"],
        slots={"contact": "person filter", "window": "time period",
               "platform": "payment platform"},
    ),

    "payments.balance": Intent(
        id="payments.balance",
        op="payments_balance",
        domain="payments",
        description="Check the balance in a payment app",
        signals=["venmo balance", "paypal balance", "cash app balance",
                 "my balance", "how much is in"],
        extractor="payments_balance",
        examples=["what's my Venmo balance", "check my PayPal balance",
                  "how much do I have in Cash App"],
        slots={"platform": "payment platform"},
    ),

    # ── Food Delivery ──────────────────────────────────────────────────────

    "food_delivery.order": Intent(
        id="food_delivery.order",
        op="food_delivery_order",
        domain="food_delivery",
        description="Order food for delivery",
        signals=["order food", "order delivery", "get food delivered",
                 "doordash", "uber eats", "grubhub", "order from",
                 "food delivery", "order dinner", "order lunch"],
        blockers=["track my order", "track delivery", "where is my food",
                  "my delivery", "track my doordash"],
        extractor="food_delivery_order",
        examples=["order pizza from Domino's", "get sushi delivered",
                  "order food from Chipotle on DoorDash"],
        slots={"restaurant": "restaurant name", "platform": "delivery platform"},
    ),

    "food_delivery.track": Intent(
        id="food_delivery.track",
        op="food_delivery_track",
        domain="food_delivery",
        description="Track a food delivery order",
        signals=["track my order", "track delivery", "where is my food",
                 "delivery status", "how far is my food", "when will food arrive",
                 "track my doordash", "track my uber eats", "my doordash order",
                 "my delivery", "where is my order"],
        extractor="food_delivery_track",
        examples=["track my DoorDash order", "where is my food",
                  "how long until my delivery arrives"],
        slots={"platform": "delivery platform"},
    ),

    "food_delivery.reorder": Intent(
        id="food_delivery.reorder",
        op="food_delivery_reorder",
        domain="food_delivery",
        description="Reorder a previous food delivery",
        signals=["reorder", "order again", "get the same order", "order it again",
                 "repeat my last order"],
        extractor="food_delivery_reorder",
        examples=["reorder from last time", "get the same order from Chipotle",
                  "order my usual from DoorDash"],
        slots={"restaurant": "restaurant name", "platform": "delivery platform"},
    ),

    "food_delivery.browse": Intent(
        id="food_delivery.browse",
        op="food_delivery_browse",
        domain="food_delivery",
        description="Browse restaurants or cuisines for delivery",
        signals=["what restaurants deliver", "find food near me", "delivery options",
                 "what's available for delivery", "find pizza delivery",
                 "restaurants nearby", "browse restaurants"],
        blockers=["find a restaurant to eat at", "restaurant reservation"],
        extractor="food_delivery_browse",
        examples=["find sushi delivery near me", "what restaurants deliver to my address",
                  "browse pizza places on DoorDash"],
        slots={"cuisine": "cuisine type", "platform": "delivery platform"},
    ),

    # ── Ride Sharing ───────────────────────────────────────────────────────

    "rideshare.book": Intent(
        id="rideshare.book",
        op="rideshare_book",
        domain="rideshare",
        description="Book a ride with Uber or Lyft",
        signals=["get an uber", "book a ride", "call a lyft", "order a ride",
                 "uber to", "lyft to", "get a ride", "book uber", "hail a cab"],
        extractor="rideshare_book",
        examples=["get an Uber to the airport", "book a Lyft to downtown",
                  "order a ride to 123 Main St"],
        slots={"destination": "drop-off location", "type": "ride type",
               "platform": "uber|lyft"},
    ),

    "rideshare.track": Intent(
        id="rideshare.track",
        op="rideshare_track",
        domain="rideshare",
        description="Track the current ride or driver location",
        signals=["track my uber", "where is my driver", "track my ride",
                 "driver location", "how far is my uber", "eta for my ride"],
        extractor="rideshare_track",
        examples=["where is my Uber driver", "track my current ride",
                  "how far away is my Lyft"],
        slots={"platform": "uber|lyft"},
    ),

    "rideshare.schedule": Intent(
        id="rideshare.schedule",
        op="rideshare_schedule",
        domain="rideshare",
        description="Schedule a ride in advance",
        signals=["schedule a ride", "book a ride for", "schedule an uber",
                 "reserve a ride", "uber for tomorrow morning", "set up a ride"],
        extractor="rideshare_schedule",
        examples=["schedule an Uber for 6am tomorrow to the airport",
                  "book a ride for Friday at 3pm",
                  "reserve a Lyft for my appointment"],
        slots={"destination": "drop-off", "time": "pickup time",
               "platform": "platform"},
    ),

    "rideshare.cancel": Intent(
        id="rideshare.cancel",
        op="rideshare_cancel",
        domain="rideshare",
        description="Cancel a rideshare booking",
        signals=["cancel my uber", "cancel the ride", "cancel my lyft",
                 "cancel rideshare", "don't need the ride"],
        extractor="rideshare_cancel",
        examples=["cancel my Uber", "cancel the current ride",
                  "cancel my Lyft booking"],
        slots={"platform": "uber|lyft"},
    ),

    # ── Maps extras ────────────────────────────────────────────────────────

    "maps.search": Intent(
        id="maps.search",
        op="maps_search",
        domain="maps",
        description="Search for a specific place on the map",
        signals=["find on map", "show on map", "where is", "search maps for",
                 "pull up on maps", "map it", "locate"],
        blockers=["where is my package", "where are my files", "where is my order"],
        extractor="maps_search",
        examples=["find Whole Foods on the map", "where is the nearest hospital",
                  "show me the Empire State Building on maps"],
        slots={"query": "place to search"},
    ),

    "maps.save_place": Intent(
        id="maps.save_place",
        op="maps_save_place",
        domain="maps",
        description="Save a place to favorites or saved locations",
        signals=["save this place", "bookmark this location", "add to saved places",
                 "save to maps", "remember this place", "add to favorites in maps"],
        extractor="maps_save_place",
        examples=["save this restaurant to my places",
                  "bookmark this location",
                  "add the office to saved places"],
        slots={"place": "place name or current location"},
    ),

    "maps.explore": Intent(
        id="maps.explore",
        op="maps_explore",
        domain="maps",
        description="Explore nearby places by category",
        signals=["restaurants near me", "coffee shops near", "atm near me",
                 "nearby pharmacy", "gas stations near", "things to do near",
                 "bars near me", "hotels near", "what's around me"],
        blockers=["directions", "navigate to", "how far"],
        extractor="maps_explore",
        examples=["find coffee shops near me", "restaurants near the office",
                  "what's around me right now", "nearby gas stations"],
        slots={"category": "place category", "location": "search anchor"},
    ),

    "maps.review": Intent(
        id="maps.review",
        op="maps_review",
        domain="maps",
        description="Write a review or rate a place",
        signals=["write a review", "rate this place", "leave a review",
                 "review the restaurant", "star rating", "google review"],
        extractor="maps_review",
        examples=["write a review for that restaurant",
                  "rate the coffee shop 5 stars",
                  "leave a Google review for the hotel"],
        slots={"place": "place name", "rating": "star rating 1-5"},
    ),

    "maps.share_eta": Intent(
        id="maps.share_eta",
        op="maps_share_eta",
        domain="maps",
        description="Share your estimated time of arrival",
        signals=["share eta", "share my location", "send my eta",
                 "let them know my eta", "share arrival time",
                 "share where I am"],
        extractor="maps_share_eta",
        examples=["share my ETA with Sarah",
                  "send my location to Dad",
                  "let Mom know when I'll arrive"],
        slots={},
    ),

    # ── Travel ─────────────────────────────────────────────────────────────

    "travel.flight_search": Intent(
        id="travel.flight_search",
        op="travel_flight_search",
        domain="travel",
        description="Search for flights between destinations",
        signals=["search flights", "find flights", "book a flight", "cheap flights",
                 "flight to", "flights from", "plane tickets", "airfare",
                 "round trip", "one way to"],
        extractor="travel_flight_search",
        examples=["find flights from NYC to LA next weekend",
                  "search for cheap flights to Miami in March",
                  "book a round trip to London"],
        slots={"origin": "departure city/airport", "destination": "arrival city",
               "date": "travel date", "cabin": "economy|business|first"},
    ),

    "travel.flight_status": Intent(
        id="travel.flight_status",
        op="travel_flight_status",
        domain="travel",
        description="Check the status of a specific flight",
        signals=["flight status", "is my flight on time", "flight delay",
                 "check flight", "track flight", "flight AA123"],
        extractor="travel_flight_status",
        examples=["check status of AA456", "is United 1234 on time",
                  "track flight DL789", "is my flight delayed"],
        slots={"flight": "flight number", "airline": "airline code"},
    ),

    "travel.boarding_pass": Intent(
        id="travel.boarding_pass",
        op="travel_boarding_pass",
        domain="travel",
        description="Show a boarding pass for a flight",
        signals=["boarding pass", "show my boarding pass", "mobile boarding pass",
                 "check in for flight", "gate info", "seat assignment"],
        extractor="travel_boarding_pass",
        examples=["show my boarding pass", "pull up my boarding pass for tomorrow",
                  "mobile boarding pass for United"],
        slots={},
    ),

    "travel.hotel_search": Intent(
        id="travel.hotel_search",
        op="travel_hotel_search",
        domain="travel",
        description="Search for hotels in a destination",
        signals=["find hotels", "search hotels", "book a hotel", "hotel in",
                 "hotels near", "best hotels", "hotel deals", "place to stay"],
        extractor="travel_hotel_search",
        examples=["find hotels in Miami for next weekend",
                  "search 4-star hotels in Paris",
                  "best hotels near Times Square"],
        slots={"city": "destination city", "checkin": "check-in date",
               "guests": "number of guests", "stars": "star rating"},
    ),

    "travel.hotel_book": Intent(
        id="travel.hotel_book",
        op="travel_hotel_book",
        domain="travel",
        description="Book a specific hotel",
        signals=["book the hotel", "reserve the hotel", "book a room at",
                 "reserve a room at", "book hilton", "book marriott"],
        extractor="travel_hotel_book",
        examples=["book a room at the Marriott downtown",
                  "reserve the Hilton for two nights",
                  "book the hotel we found"],
        slots={"hotel": "hotel name"},
    ),

    "travel.checkin": Intent(
        id="travel.checkin",
        op="travel_checkin",
        domain="travel",
        description="Check in for a flight online",
        signals=["check in for flight", "online check-in", "check into flight",
                 "check in for my united flight", "flight check-in"],
        extractor="travel_checkin",
        examples=["check in for my American Airlines flight",
                  "do online check-in for UA123",
                  "check in for tomorrow's flight"],
        slots={"airline": "airline name", "flight": "flight number"},
    ),

    "travel.itinerary": Intent(
        id="travel.itinerary",
        op="travel_itinerary",
        domain="travel",
        description="View a travel itinerary or trip plan",
        signals=["my itinerary", "trip itinerary", "travel plans",
                 "show my trip", "trip details", "travel schedule"],
        extractor="travel_itinerary",
        examples=["show my travel itinerary", "what's my trip schedule for London",
                  "view my Paris itinerary"],
        slots={"trip": "destination or trip name"},
    ),

    "travel.car_rental": Intent(
        id="travel.car_rental",
        op="travel_car_rental",
        domain="travel",
        description="Search for or book a rental car",
        signals=["rent a car", "car rental", "rental car", "hire a car",
                 "enterprise", "hertz", "avis", "budget car"],
        extractor="travel_car_rental",
        examples=["rent a car in Miami for the weekend",
                  "find a compact rental car at LAX",
                  "car rental in London for 5 days"],
        slots={"location": "pickup location", "type": "vehicle type"},
    ),

    "travel.alert": Intent(
        id="travel.alert",
        op="travel_alert",
        domain="travel",
        description="Check travel advisories or safety alerts for a destination",
        signals=["travel advisory", "travel alert", "is it safe to travel to",
                 "travel warning", "travel restrictions", "travel ban"],
        extractor="travel_alert",
        examples=["are there travel advisories for Mexico",
                  "travel alerts for Europe",
                  "is it safe to travel to Thailand"],
        slots={"destination": "destination country or region"},
    ),

    # ── Video Streaming ────────────────────────────────────────────────────

    "video.play": Intent(
        id="video.play",
        op="video_play",
        domain="video",
        description="Play a movie, show, or video on a streaming platform",
        signals=["watch", "stream", "put on netflix", "watch on hulu",
                 "play the movie", "watch the show", "start the episode",
                 "play on disney plus"],
        blockers=["watch my steps", "watch out", "music video"],
        extractor="video_play",
        examples=["watch Inception on Netflix", "play the latest episode of The Bear",
                  "stream Oppenheimer on Prime", "put on a movie"],
        slots={"title": "movie or show title", "type": "movie|show|episode",
               "platform": "streaming platform"},
    ),

    "video.search": Intent(
        id="video.search",
        op="video_search",
        domain="video",
        description="Search for movies, shows, or videos to watch",
        signals=["find a movie", "search netflix", "look for a show", "browse movies",
                 "find something to watch", "movie recommendations",
                 "what can i watch"],
        blockers=["search the web", "search google"],
        extractor="video_search",
        examples=["find a good thriller on Netflix",
                  "search Hulu for comedy shows",
                  "what horror movies are on HBO"],
        slots={"query": "search term", "platform": "streaming platform"},
    ),

    "video.watchlist": Intent(
        id="video.watchlist",
        op="video_watchlist",
        domain="video",
        description="Add to or view the watchlist",
        signals=["watchlist", "add to watchlist", "save to watch later",
                 "my list", "watch later", "add this to my list"],
        extractor="video_watchlist",
        examples=["add Dune to my Netflix watchlist",
                  "save this show to watch later",
                  "show my watchlist"],
        slots={"action": "add|view", "title": "title to add"},
    ),

    "video.browse": Intent(
        id="video.browse",
        op="video_browse",
        domain="video",
        description="Browse movies or shows by genre",
        signals=["action movies", "comedy shows", "horror movies",
                 "browse by genre", "sci-fi shows", "drama series",
                 "documentary", "thriller movies"],
        blockers=["news", "sports"],
        extractor="video_browse",
        examples=["show me action movies on Netflix",
                  "browse comedy shows on Hulu",
                  "find documentaries to watch"],
        slots={"genre": "genre", "platform": "streaming platform"},
    ),

    "video.recommend": Intent(
        id="video.recommend",
        op="video_recommend",
        domain="video",
        description="Get personalized movie or show recommendations",
        signals=["recommend a movie", "recommend a show", "what should i watch",
                 "suggest a film", "something good to watch",
                 "what's good on netflix"],
        extractor="video_recommend",
        examples=["recommend a funny movie", "suggest something thrilling to watch",
                  "what's good on Netflix tonight"],
        slots={"mood": "mood or genre preference", "platform": "platform"},
    ),

    "video.cast": Intent(
        id="video.cast",
        op="video_cast",
        domain="video",
        description="Cast video to a TV or external display",
        signals=["cast to the tv", "play on the tv", "chromecast", "airplay to tv",
                 "send to the tv", "play on the big screen", "cast this"],
        extractor="video_cast",
        examples=["cast this to the living room TV",
                  "play this on the Chromecast",
                  "AirPlay to the Apple TV"],
        slots={"device": "target device"},
    ),

    "video.continue": Intent(
        id="video.continue",
        op="video_continue",
        domain="video",
        description="Continue watching something from where you left off",
        signals=["continue watching", "resume the show", "where i left off",
                 "pick up where", "continue the movie", "what was i watching"],
        extractor="video_continue",
        examples=["continue watching The Bear",
                  "resume the movie I was watching",
                  "what was I watching on Netflix"],
        slots={"platform": "streaming platform"},
    ),

    "video.rate": Intent(
        id="video.rate",
        op="video_rate",
        domain="video",
        description="Rate or review a movie or show",
        signals=["rate this movie", "give it stars", "thumbs up this show",
                 "thumbs down", "rate the film", "review this"],
        extractor="video_rate",
        examples=["give Inception 5 stars", "thumbs up this episode",
                  "rate The Bear on Netflix"],
        slots={"rating": "star rating", "thumbs": "up|down"},
    ),

    # ── Health / Fitness ───────────────────────────────────────────────────

    "health.workout_log": Intent(
        id="health.workout_log",
        op="health_workout_log",
        domain="health",
        description="Log a completed workout",
        signals=["log workout", "record workout", "i ran", "i cycled",
                 "log my run", "log a run", "log a walk", "log a bike",
                 "workout log", "record my workout", "i swam", "i did yoga",
                 "i lifted", "log a workout", "log my workout",
                 "i worked out", "finished a run", "just ran"],
        patterns=[r"\blog\s+(?:a\s+)?(?:\d+[\s\-](?:minute|min|hour|hr|mile|km)[\s\-])?"
                  r"(?:run|walk|hike|bike|swim|yoga|lift|workout|jog)\b"],
        extractor="health_workout_log",
        examples=["log a 30-minute run", "I cycled 10 miles today",
                  "record my yoga session", "log my weights workout"],
        slots={"type": "workout type", "duration_min": "minutes",
               "distance": "distance", "calories": "calories burned"},
    ),

    "health.workout_start": Intent(
        id="health.workout_start",
        op="health_workout_start",
        domain="health",
        description="Start a workout session or activity tracking",
        signals=["start a workout", "begin a run", "start running",
                 "start a walk", "start yoga", "track my workout",
                 "start tracking", "begin workout"],
        extractor="health_workout_start",
        examples=["start a running workout", "begin tracking my walk",
                  "start a yoga session"],
        slots={"type": "workout type"},
    ),

    "health.steps": Intent(
        id="health.steps",
        op="health_steps",
        domain="health",
        description="Check daily step count or activity",
        signals=["step count", "how many steps", "my steps today",
                 "steps today", "step goal", "activity rings",
                 "did i hit my step goal"],
        blockers=["set my", "update my goal", "change my goal", "daily step goal",
                  "calorie goal", "health goal", "fitness goal"],
        extractor="health_steps",
        examples=["how many steps did I take today",
                  "check my step count",
                  "did I hit my step goal"],
        slots={},
    ),

    "health.heart_rate": Intent(
        id="health.heart_rate",
        op="health_heart_rate",
        domain="health",
        description="Check heart rate data",
        signals=["heart rate", "bpm", "pulse", "resting heart rate",
                 "my heart rate", "check my pulse"],
        extractor="health_heart_rate",
        examples=["what's my heart rate", "check my resting heart rate",
                  "show my BPM"],
        slots={},
    ),

    "health.sleep": Intent(
        id="health.sleep",
        op="health_sleep",
        domain="health",
        description="View sleep data and sleep quality",
        signals=["sleep data", "how did i sleep", "sleep quality",
                 "hours of sleep", "my sleep last night", "deep sleep",
                 "rem sleep", "sleep score"],
        extractor="health_sleep",
        examples=["how did I sleep last night", "show my sleep data",
                  "what was my sleep score"],
        slots={},
    ),

    "health.food_log": Intent(
        id="health.food_log",
        op="health_food_log",
        domain="health",
        description="Log food intake or calories",
        signals=["log food", "track calories", "log what i ate", "calorie log",
                 "i ate", "food diary", "log my meal", "count calories"],
        extractor="health_food_log",
        examples=["log 500 calories for lunch", "I ate a salad for lunch",
                  "track calories for a Big Mac"],
        slots={"food": "food item", "calories": "calorie count",
               "meal": "breakfast|lunch|dinner|snack"},
    ),

    "health.water": Intent(
        id="health.water",
        op="health_water",
        domain="health",
        description="Log water intake",
        signals=["log water", "water intake", "i drank water", "track hydration",
                 "drank a glass", "log a bottle of water", "hydration"],
        extractor="health_water",
        examples=["log 16 oz of water", "I drank a glass of water",
                  "log water intake: 500ml"],
        slots={"amount": "quantity", "unit": "oz|ml|cup"},
    ),

    "health.weight": Intent(
        id="health.weight",
        op="health_weight",
        domain="health",
        description="Log body weight",
        signals=["log my weight", "weighed myself", "my weight today",
                 "record weight", "i weigh", "body weight log"],
        extractor="health_weight",
        examples=["log my weight: 175 lbs", "I weighed 80kg this morning",
                  "record my weight"],
        slots={"weight": "weight value", "unit": "lbs|kg"},
    ),

    "health.medication": Intent(
        id="health.medication",
        op="health_medication",
        domain="health",
        description="Log medication or set a medication reminder",
        signals=["took my medication", "take my pills", "medication reminder",
                 "log medication", "remind me to take", "pill reminder",
                 "refill prescription"],
        extractor="health_medication",
        examples=["log that I took my blood pressure medication",
                  "remind me to take Advil at 8pm",
                  "medication log: vitamin D"],
        slots={"medication": "medication name", "time": "reminder time"},
    ),

    "health.mood": Intent(
        id="health.mood",
        op="health_mood",
        domain="health",
        description="Log mood or emotional state",
        signals=["log my mood", "how i'm feeling", "mood log", "feeling",
                 "mood tracker", "mental health log", "i feel"],
        blockers=["weather feeling", "feeling hungry", "feeling cold"],
        extractor="health_mood",
        examples=["log my mood as happy", "I'm feeling stressed today",
                  "mood: 7 out of 10"],
        slots={"mood": "mood label", "score": "1-10 score"},
    ),

    # ── Files / Storage ────────────────────────────────────────────────────

    "files.upload": Intent(
        id="files.upload",
        op="files_upload",
        domain="files",
        description="Upload a file to cloud storage",
        signals=["upload", "upload to drive", "upload to icloud",
                 "sync to cloud", "back up this file", "upload file"],
        extractor="files_upload",
        examples=["upload the report to Google Drive",
                  "back up my photos to iCloud",
                  "upload this file to Dropbox"],
        slots={"file": "file name", "destination": "cloud service or folder"},
    ),

    "files.download": Intent(
        id="files.download",
        op="files_download",
        domain="files",
        description="Download a file from cloud storage",
        signals=["download", "download from drive", "save to device",
                 "download the file", "get the file", "pull down"],
        blockers=["download an app", "download music"],
        extractor="files_download",
        examples=["download the Q4 report from Drive",
                  "save that file to my device",
                  "download the presentation"],
        slots={"file": "file name"},
    ),

    "files.share": Intent(
        id="files.share",
        op="files_share",
        domain="files",
        description="Share a file with someone",
        signals=["share the file", "send the file", "share this document",
                 "give access to the file", "share the link"],
        blockers=["share a photo", "share a message", "share on social"],
        extractor="files_share",
        examples=["share the budget file with Sarah",
                  "send the PDF to John",
                  "share the Drive link with the team"],
        slots={"file": "file name", "recipient": "person to share with"},
    ),

    "files.search": Intent(
        id="files.search",
        op="files_search",
        domain="files",
        description="Search for a file in cloud storage or on device",
        signals=["find the file", "search for a file", "where is the document",
                 "find the pdf", "look for my files"],
        blockers=["find a song", "find a photo", "find a contact"],
        extractor="files_search",
        examples=["find the Q4 report", "where is the invoice PDF",
                  "search for files about the project"],
        slots={"query": "file search query"},
    ),

    "files.recent": Intent(
        id="files.recent",
        op="files_recent",
        domain="files",
        description="Show recently accessed or modified files",
        signals=["recent files", "recently opened", "show recent documents",
                 "last files", "what files did i work on"],
        extractor="files_recent",
        examples=["show my recent files", "what documents did I open today",
                  "recently modified files"],
        slots={},
    ),

    # ── Alarm / Clock ──────────────────────────────────────────────────────

    "alarm.set": Intent(
        id="alarm.set",
        op="alarm_set",
        domain="alarm",
        description="Set an alarm for a specific time",
        signals=["set an alarm", "wake me up", "alarm for", "set alarm",
                 "alarm at", "reminder alarm"],
        blockers=["fire alarm", "car alarm", "security alarm"],
        patterns=[r"\bwake\s+me\s+(?:up\s+)?at\b", r"\balarm\s+(?:at|for)\s+\d"],
        extractor="alarm_set",
        examples=["set an alarm for 7am", "wake me up at 6:30",
                  "alarm for Monday at 8am"],
        slots={"time": "alarm time", "label": "alarm label", "days": "repeat days"},
    ),

    "alarm.delete": Intent(
        id="alarm.delete",
        op="alarm_delete",
        domain="alarm",
        description="Delete or turn off an existing alarm",
        signals=["delete alarm", "remove alarm", "cancel alarm",
                 "turn off alarm", "disable the alarm"],
        extractor="alarm_delete",
        examples=["delete the 7am alarm", "remove the morning alarm",
                  "cancel the alarm for tomorrow"],
        slots={"time": "alarm time", "label": "alarm label"},
    ),

    "alarm.list": Intent(
        id="alarm.list",
        op="alarm_list",
        domain="alarm",
        description="View all set alarms",
        signals=["my alarms", "show alarms", "list alarms", "what alarms do i have",
                 "all alarms"],
        extractor="alarm_list",
        examples=["show all my alarms", "what alarms do I have set",
                  "list my alarms"],
        slots={},
    ),

    "clock.world": Intent(
        id="clock.world",
        op="clock_world",
        domain="clock",
        description="Check the time in another city or timezone",
        signals=["time in", "what time is it in", "current time in",
                 "timezone", "time zone", "local time in"],
        extractor="clock_world",
        examples=["what time is it in Tokyo", "current time in London",
                  "what's the time in New York"],
        slots={"city": "city name", "timezone": "timezone code"},
    ),

    "clock.stopwatch": Intent(
        id="clock.stopwatch",
        op="clock_stopwatch",
        domain="clock",
        description="Start, stop, or lap a stopwatch",
        signals=["stopwatch", "start stopwatch", "stop stopwatch",
                 "lap", "start timer", "begin timing"],
        blockers=["set a timer", "countdown timer"],
        extractor="clock_stopwatch",
        examples=["start the stopwatch", "stop the stopwatch",
                  "lap the stopwatch"],
        slots={"action": "start|stop|lap", "lap": "true if lap"},
    ),

    "clock.bedtime": Intent(
        id="clock.bedtime",
        op="clock_bedtime",
        domain="clock",
        description="Set a bedtime or sleep schedule",
        signals=["bedtime", "sleep schedule", "set bedtime", "go to bed reminder",
                 "bedtime reminder", "wind down"],
        extractor="clock_bedtime",
        examples=["set my bedtime to 10:30pm", "bedtime reminder at 11",
                  "set a sleep schedule"],
        slots={"bedtime": "bedtime hour"},
    ),

    # ── Podcasts ───────────────────────────────────────────────────────────

    "podcast.find": Intent(
        id="podcast.find",
        op="podcast_find",
        domain="podcast",
        description="Find or discover a podcast",
        signals=["find a podcast", "podcast about", "discover podcasts",
                 "podcast recommendations", "recommend a podcast",
                 "search for podcasts"],
        extractor="podcast_find",
        examples=["find a podcast about true crime",
                  "recommend a good business podcast",
                  "discover podcasts about history"],
        slots={"query": "podcast name or topic", "topic": "topic if browsing"},
    ),

    "podcast.play": Intent(
        id="podcast.play",
        op="podcast_play",
        domain="podcast",
        description="Play a podcast episode",
        signals=["listen to podcast", "play the podcast", "latest episode",
                 "play the latest", "listen to my podcast", "play episode"],
        extractor="podcast_play",
        examples=["play the latest episode of How I Built This",
                  "listen to the new Joe Rogan podcast",
                  "play the podcast I was listening to"],
        slots={"podcast": "podcast name", "latest": "true if latest episode"},
    ),

    "podcast.subscribe": Intent(
        id="podcast.subscribe",
        op="podcast_subscribe",
        domain="podcast",
        description="Subscribe or follow a podcast",
        signals=["subscribe to podcast", "follow the podcast", "subscribe to",
                 "follow this show", "add podcast to library"],
        extractor="podcast_subscribe",
        examples=["subscribe to Lex Fridman Podcast",
                  "follow Serial",
                  "add Conan O'Brien Needs a Friend to my podcasts"],
        slots={"podcast": "podcast name"},
    ),

    "podcast.queue": Intent(
        id="podcast.queue",
        op="podcast_queue",
        domain="podcast",
        description="Add a podcast episode to the listening queue",
        signals=["add episode to queue", "queue the podcast episode",
                 "add to my queue", "play this episode next"],
        extractor="podcast_queue",
        examples=["add this episode to my podcast queue",
                  "queue up the new Radiolab episode",
                  "play next: the latest Stuff You Should Know"],
        slots={"episode": "episode name or description"},
    ),

    # ── Recipes / Food ─────────────────────────────────────────────────────

    "recipe.find": Intent(
        id="recipe.find",
        op="recipe_find",
        domain="recipe",
        description="Find a recipe for a dish or with specific ingredients",
        signals=["recipe for", "how to make", "how to cook", "recipe",
                 "cooking instructions", "make dinner", "quick recipe"],
        blockers=["restaurant recommendation", "order food"],
        extractor="recipe_find",
        examples=["recipe for chicken tikka masala",
                  "how to make pasta carbonara",
                  "quick vegetarian dinner recipe"],
        slots={"dish": "dish name", "cuisine": "cuisine type",
               "ingredients": "key ingredients"},
    ),

    "recipe.save": Intent(
        id="recipe.save",
        op="recipe_save",
        domain="recipe",
        description="Save a recipe for later",
        signals=["save this recipe", "bookmark recipe", "add recipe to",
                 "save to my recipes", "keep this recipe"],
        extractor="recipe_save",
        examples=["save this pasta recipe", "bookmark the chicken recipe",
                  "add this to my saved recipes"],
        slots={"recipe": "recipe name"},
    ),

    "recipe.nutrition": Intent(
        id="recipe.nutrition",
        op="recipe_nutrition",
        domain="recipe",
        description="Look up nutritional information for a food item",
        signals=["nutrition info", "calories in", "how many calories",
                 "macros", "nutritional value", "how healthy is",
                 "protein in", "carbs in"],
        extractor="recipe_nutrition",
        examples=["how many calories in a Big Mac", "nutrition info for avocado",
                  "what are the macros in oatmeal"],
        slots={"food": "food or dish name"},
    ),

    "recipe.scale": Intent(
        id="recipe.scale",
        op="recipe_scale",
        domain="recipe",
        description="Scale a recipe to a different number of servings",
        signals=["scale the recipe", "adjust servings", "double the recipe",
                 "half the recipe", "make for 8 people", "change servings"],
        extractor="recipe_scale",
        examples=["scale this recipe to 8 servings",
                  "double the recipe",
                  "adjust the recipe for 4 people"],
        slots={"servings": "target number of servings"},
    ),

    "grocery.add": Intent(
        id="grocery.add",
        op="grocery_add",
        domain="grocery",
        description="Add items to a grocery or shopping list",
        signals=["add to grocery list", "add to shopping list",
                 "grocery list", "shopping list", "need to buy",
                 "pick up", "get from store", "add milk"],
        blockers=["add to task list", "add to playlist", "add to watchlist"],
        extractor="grocery_add",
        examples=["add milk and eggs to the grocery list",
                  "I need to buy bananas and bread",
                  "add chicken to my shopping list"],
        slots={"items": "list of grocery items"},
    ),

    "grocery.list": Intent(
        id="grocery.list",
        op="grocery_list",
        domain="grocery",
        description="View the current grocery or shopping list",
        signals=["my grocery list", "show grocery list", "shopping list",
                 "what's on the list", "my shopping list"],
        blockers=["my task list", "my to-do list", "my playlist"],
        extractor="grocery_list",
        examples=["show my grocery list", "what's on my shopping list",
                  "what do I need to buy"],
        slots={},
    ),

    "grocery.order": Intent(
        id="grocery.order",
        op="grocery_order",
        domain="grocery",
        description="Order groceries for delivery",
        signals=["order groceries", "grocery delivery", "instacart", "order from whole foods",
                 "grocery order", "get groceries delivered"],
        extractor="grocery_order",
        examples=["order my groceries from Instacart",
                  "get grocery delivery from Whole Foods",
                  "order groceries online"],
        slots={"platform": "delivery platform"},
    ),

    # ── Translation ────────────────────────────────────────────────────────

    "translate.text": Intent(
        id="translate.text",
        op="translate_text",
        domain="translate",
        description="Translate text from one language to another",
        signals=["translate", "how do you say", "in spanish",
                 "in french", "in german", "in japanese", "translate to",
                 "translation of", "en espanol", "auf deutsch"],
        patterns=[r"\bhow\s+do\s+you\s+say\b", r"\btranslate\b.+\bto\b"],
        blockers=[],
        extractor="translate_text",
        examples=["translate 'hello' to Spanish", "how do you say goodbye in French",
                  "what is 'gracias' in English"],
        slots={"text": "text to translate", "source": "source language",
               "target": "target language"},
    ),

    "translate.detect": Intent(
        id="translate.detect",
        op="translate_detect",
        domain="translate",
        description="Detect what language a piece of text is written in",
        signals=["what language is", "detect language", "what language is this",
                 "identify the language", "which language"],
        extractor="translate_detect",
        examples=["what language is 'bonjour'",
                  "detect the language of this text",
                  "what language is '你好'"],
        slots={"text": "text to identify"},
    ),

    "translate.conversation": Intent(
        id="translate.conversation",
        op="translate_conversation",
        domain="translate",
        description="Start a real-time conversation translation session",
        signals=["conversation mode", "real-time translation", "live translate",
                 "two-way translation", "translate our conversation"],
        extractor="translate_conversation",
        examples=["start conversation translation between English and Spanish",
                  "live translate between me and this person",
                  "two-way translation mode"],
        slots={"lang1": "first language", "lang2": "second language"},
    ),

    # ── Books / Reading ────────────────────────────────────────────────────

    "book.find": Intent(
        id="book.find",
        op="book_find",
        domain="book",
        description="Find or search for a book",
        signals=["find a book", "recommend a book", "book recommendation",
                 "search for book", "book by", "book about", "what should i read",
                 "recommend a novel", "recommend a mystery", "recommend a thriller",
                 "recommend a biography", "good book", "suggest a book",
                 "novel to read", "book to read"],
        blockers=["recipe book", "textbook"],
        extractor="book_find",
        examples=["recommend a mystery novel", "find books by Stephen King",
                  "what should I read next"],
        slots={"title": "book title", "author": "author name", "genre": "genre"},
    ),

    "book.read": Intent(
        id="book.read",
        op="book_read",
        domain="book",
        description="Open and read or listen to a book",
        signals=["read", "open book", "continue reading", "audiobook",
                 "listen to", "start the book", "open the ebook",
                 "continue the audiobook"],
        blockers=["read my email", "read my messages", "read my texts",
                  "read the news"],
        patterns=[r"\b(?:read|listen\s+to)\s+[\"']?.+?[\"']?\s+(?:book|novel|audiobook)\b"],
        extractor="book_read",
        examples=["read Atomic Habits", "listen to The Alchemist audiobook",
                  "continue reading my book"],
        slots={"title": "book title", "audio": "true for audiobook"},
    ),

    "book.library": Intent(
        id="book.library",
        op="book_library",
        domain="book",
        description="View the digital book library",
        signals=["my books", "my library", "book library", "books i own",
                 "my ebooks", "my kindle library", "my audiobooks"],
        extractor="book_library",
        examples=["show my book library", "what books do I have",
                  "open my Kindle library"],
        slots={},
    ),

    "book.highlight": Intent(
        id="book.highlight",
        op="book_highlight",
        domain="book",
        description="Highlight a passage in a book",
        signals=["highlight this", "highlight passage", "bookmark this page",
                 "mark this section", "save this quote"],
        extractor="book_highlight",
        examples=["highlight this paragraph", "save this quote from the book",
                  "mark this passage"],
        slots={"text": "text to highlight"},
    ),

    # ── Device / Settings ──────────────────────────────────────────────────

    "settings.wifi": Intent(
        id="settings.wifi",
        op="settings_wifi",
        domain="settings",
        description="Manage WiFi — connect, disconnect, or toggle",
        signals=["wifi", "wi-fi", "connect to wifi", "turn on wifi",
                 "turn off wifi", "join network", "wireless"],
        blockers=["smart home wifi", "speaker wifi"],
        extractor="settings_wifi",
        examples=["connect to the CoffeeShop WiFi",
                  "turn off WiFi", "join the HomeNetwork"],
        slots={"action": "connect|on|off|settings", "network": "network name"},
    ),

    "settings.bluetooth": Intent(
        id="settings.bluetooth",
        op="settings_bluetooth",
        domain="settings",
        description="Manage Bluetooth — pair devices, toggle on/off",
        signals=["bluetooth", "pair", "connect bluetooth", "turn on bluetooth",
                 "turn off bluetooth", "connect to headphones", "pair airpods"],
        extractor="settings_bluetooth",
        examples=["turn on Bluetooth", "pair my AirPods",
                  "connect Bluetooth headphones", "turn off Bluetooth"],
        slots={"action": "pair|on|off|settings", "device": "device name"},
    ),

    "settings.brightness": Intent(
        id="settings.brightness",
        op="settings_brightness",
        domain="settings",
        description="Adjust screen brightness",
        signals=["brightness", "screen brightness", "dim screen",
                 "make screen brighter", "brighter", "dimmer screen",
                 "turn up brightness", "turn down brightness"],
        blockers=["dim the lights", "smart lights brightness"],
        extractor="settings_brightness",
        examples=["set brightness to 75%", "make the screen brighter",
                  "dim the screen", "full brightness"],
        slots={"action": "up|down|set", "level": "0-100"},
    ),

    "settings.dnd": Intent(
        id="settings.dnd",
        op="settings_dnd",
        domain="settings",
        description="Toggle Do Not Disturb or Focus mode",
        signals=["do not disturb", "dnd", "focus mode", "silence notifications",
                 "quiet mode", "turn off notifications", "focus on"],
        extractor="settings_dnd",
        examples=["turn on Do Not Disturb for 2 hours",
                  "enable focus mode", "silence my phone",
                  "turn off DND"],
        slots={"action": "on|off|toggle", "duration": "duration in hours/minutes"},
    ),

    "settings.airplane": Intent(
        id="settings.airplane",
        op="settings_airplane",
        domain="settings",
        description="Toggle airplane mode",
        signals=["airplane mode", "flight mode", "turn on airplane mode",
                 "turn off airplane mode", "disable airplane mode"],
        extractor="settings_airplane",
        examples=["turn on airplane mode", "disable airplane mode",
                  "toggle flight mode"],
        slots={"action": "on|off"},
    ),

    "settings.battery": Intent(
        id="settings.battery",
        op="settings_battery",
        domain="settings",
        description="Check battery status or enable battery saver",
        signals=["battery level", "how much battery", "battery percentage",
                 "battery saver", "low power mode", "charge level",
                 "battery status"],
        extractor="settings_battery",
        examples=["what's my battery level", "how much charge do I have",
                  "enable battery saver mode"],
        slots={},
    ),

    "settings.storage": Intent(
        id="settings.storage",
        op="settings_storage",
        domain="settings",
        description="Check available storage space",
        signals=["storage space", "available storage", "how much storage",
                 "storage full", "free up storage", "clear storage",
                 "device storage"],
        extractor="settings_storage",
        examples=["how much storage do I have left", "check my storage space",
                  "free up storage on my phone"],
        slots={},
    ),

    "settings.notification": Intent(
        id="settings.notification",
        op="settings_notification",
        domain="settings",
        description="Manage notifications for an app",
        signals=["turn off notifications", "notification settings",
                 "disable notifications for", "stop notifications from",
                 "mute notifications", "app notifications"],
        extractor="settings_notification",
        examples=["turn off Instagram notifications",
                  "disable notifications from Twitter",
                  "mute notifications for games"],
        slots={"app": "app name", "action": "on|off"},
    ),

    # ── Notifications ─────────────────────────────────────────────────────

    "notifications.view": Intent(
        id="notifications.view",
        op="notifications_view",
        domain="notifications",
        description="View all pending notifications",
        signals=["my notifications", "show notifications", "notification center",
                 "check notifications", "open notifications", "view notifications",
                 "what notifications", "any notifications"],
        blockers=["notification settings", "turn off notifications",
                  "disable notifications", "mute notifications"],
        extractor="notif_view",
        examples=["show my notifications", "what notifications do I have",
                  "open notification center"],
        slots={},
    ),

    "notifications.clear": Intent(
        id="notifications.clear",
        op="notifications_clear",
        domain="notifications",
        description="Clear all notifications",
        signals=["clear all notifications", "dismiss all notifications",
                 "clear notifications", "remove all notifications"],
        blockers=["clear notifications from", "clear notifications for",
                  "dismiss notifications from", "remove notifications from"],
        extractor="notif_clear",
        examples=["clear all notifications", "dismiss all my notifications"],
        slots={},
    ),

    "notifications.clear_app": Intent(
        id="notifications.clear_app",
        op="notifications_clear_app",
        domain="notifications",
        description="Clear notifications from a specific app",
        signals=["clear notifications from", "dismiss notifications from",
                 "remove notifications from", "clear notifications for"],
        extractor="notif_clear_app",
        examples=["clear notifications from Slack", "dismiss Instagram notifications"],
        slots={"app": "app name"},
    ),

    "notifications.mark_read": Intent(
        id="notifications.mark_read",
        op="notifications_mark_read",
        domain="notifications",
        description="Mark all notifications as read",
        signals=["mark notifications as read", "mark all read",
                 "mark notifications read"],
        extractor="notif_mark_read",
        examples=["mark all notifications as read"],
        slots={},
    ),

    "notifications.settings": Intent(
        id="notifications.settings",
        op="notifications_settings",
        domain="notifications",
        description="Open notification settings for an app",
        signals=["notification settings for", "notifications settings"],
        extractor="notif_settings",
        examples=["notification settings for WhatsApp"],
        slots={"app": "app name"},
    ),

    # ── Handoff / Continuity ──────────────────────────────────────────────

    "handoff.airdrop": Intent(
        id="handoff.airdrop",
        op="handoff_airdrop",
        domain="handoff",
        description="AirDrop a file or content to a nearby device",
        signals=["airdrop", "air drop", "airdrop this", "airdrop to"],
        extractor="handoff_airdrop",
        examples=["AirDrop this photo to John",
                  "AirDrop this file", "send via AirDrop"],
        slots={"target": "person or device name"},
    ),

    "handoff.clipboard": Intent(
        id="handoff.clipboard",
        op="handoff_clipboard",
        domain="handoff",
        description="Copy to or sync Universal Clipboard across devices",
        signals=["universal clipboard", "copy to mac", "paste from iphone",
                 "clipboard sync", "handoff clipboard"],
        extractor="handoff_clipboard",
        examples=["copy this to my Mac", "sync clipboard",
                  "paste from my iPhone"],
        slots={},
    ),

    "handoff.continue": Intent(
        id="handoff.continue",
        op="handoff_continue",
        domain="handoff",
        description="Continue an activity on another device (Handoff)",
        signals=["continue on", "handoff to", "pick up on", "continue on my mac",
                 "continue on my iphone", "continue on my ipad", "switch to my"],
        extractor="handoff_continue",
        examples=["continue this on my Mac",
                  "pick up on my iPad", "handoff to my iPhone"],
        slots={"device": "target device name"},
    ),

    "handoff.screen_share": Intent(
        id="handoff.screen_share",
        op="handoff_screen_share",
        domain="handoff",
        description="Share or mirror screen to another device",
        signals=["share my screen", "screen sharing", "shareplay", "share screen with",
                 "start screen share"],
        blockers=["screen record", "screenshot"],
        extractor="handoff_screen_share",
        examples=["share my screen with the team",
                  "start SharePlay", "share screen on FaceTime"],
        slots={},
    ),

    # ── Enterprise — Jira ─────────────────────────────────────────────────

    "jira.create": Intent(
        id="jira.create",
        op="jira_create",
        domain="jira",
        description="Create a new Jira ticket or issue",
        signals=["jira ticket", "jira issue", "create a ticket", "file a ticket",
                 "open a ticket", "new jira", "create jira", "jira bug", "jira story"],
        extractor="jira_create",
        examples=["create a Jira ticket for the login bug",
                  "file a bug in Jira", "open a new Jira story"],
        slots={"title": "ticket title", "type": "bug|story|task|epic"},
    ),

    "jira.view": Intent(
        id="jira.view",
        op="jira_view",
        domain="jira",
        description="View a specific Jira ticket by key",
        signals=["jira ticket", "jira issue", "show jira", "view jira", "open jira"],
        patterns=[r"\b[A-Z]+-\d+\b"],
        extractor="jira_view",
        examples=["show me PROJ-123", "view Jira ticket ABC-456",
                  "open PROJ-789"],
        slots={"ticket": "ticket key e.g. PROJ-123"},
    ),

    "jira.update": Intent(
        id="jira.update",
        op="jira_update",
        domain="jira",
        description="Update, assign, or close a Jira ticket",
        signals=["update jira", "close jira ticket", "assign jira ticket",
                 "mark jira done", "set jira status", "resolve jira ticket"],
        patterns=[r"\b(?:update|close|assign|resolve|mark)\b.+\b[A-Za-z]+-\d+\b"],
        extractor="jira_update",
        examples=["close PROJ-123", "assign ABC-456 to John",
                  "mark PROJ-789 as done"],
        slots={"ticket": "ticket key", "action": "close|assign|update", "assignee": "person"},
    ),

    "jira.my_issues": Intent(
        id="jira.my_issues",
        op="jira_my_issues",
        domain="jira",
        description="Show Jira issues assigned to me",
        signals=["my jira issues", "my jira tickets", "assigned to me in jira",
                 "my open tickets", "jira backlog", "jira board"],
        extractor="jira_my_issues",
        examples=["show my Jira issues", "what tickets are assigned to me",
                  "my Jira backlog"],
        slots={},
    ),

    "jira.sprint": Intent(
        id="jira.sprint",
        op="jira_sprint",
        domain="jira",
        description="View current sprint status and tickets",
        signals=["current sprint", "sprint board", "active sprint", "sprint status",
                 "this sprint", "sprint tickets"],
        extractor="jira_sprint",
        examples=["show current sprint", "what's in this sprint",
                  "sprint board status"],
        slots={},
    ),

    # ── Enterprise — GitHub ───────────────────────────────────────────────

    "github.pr_view": Intent(
        id="github.pr_view",
        op="github_pr_view",
        domain="github",
        description="View a GitHub pull request",
        signals=["pull request", "pr review", "github pr", "view pr",
                 "open pr", "check pr", "pr status"],
        extractor="github_pr_view",
        examples=["show PR #123", "view pull request 456",
                  "check the GitHub PR"],
        slots={"pr": "PR number"},
    ),

    "github.issue_create": Intent(
        id="github.issue_create",
        op="github_issue_create",
        domain="github",
        description="Create a new GitHub issue",
        signals=["github issue", "create github issue", "open github issue",
                 "file github issue", "new github issue"],
        extractor="github_issue_create",
        examples=["open a GitHub issue for the crash bug",
                  "create a GitHub issue", "file an issue on GitHub"],
        slots={"title": "issue title"},
    ),

    "github.my_prs": Intent(
        id="github.my_prs",
        op="github_my_prs",
        domain="github",
        description="Show my open pull requests",
        signals=["my pull requests", "my prs", "my github prs",
                 "prs i opened", "my open pull requests", "github dashboard",
                 "open github prs", "my open prs", "github prs"],
        extractor="github_my_prs",
        examples=["show my pull requests", "what PRs do I have open",
                  "my GitHub PRs"],
        slots={},
    ),

    "github.repo_search": Intent(
        id="github.repo_search",
        op="github_repo_search",
        domain="github",
        description="Search for a GitHub repository",
        signals=["github repo", "find a repo", "github repository",
                 "search github", "look for a repo"],
        extractor="github_repo_search",
        examples=["find a GitHub repo for markdown editors",
                  "search GitHub for React templates"],
        slots={"query": "search query"},
    ),

    "github.commit": Intent(
        id="github.commit",
        op="github_commit",
        domain="github",
        description="View recent commits or commit history",
        signals=["github commits", "recent commits", "commit history",
                 "latest commits", "who committed", "git log"],
        extractor="github_commit",
        examples=["show recent commits", "what's the commit history",
                  "who committed last"],
        slots={},
    ),

    # ── Enterprise — Slack ────────────────────────────────────────────────

    "slack.send": Intent(
        id="slack.send",
        op="slack_send",
        domain="slack",
        description="Send a Slack message to a channel or person",
        signals=["slack message", "message in slack", "send in slack",
                 "dm in slack", "post to slack", "message on slack",
                 "send a slack", "slack the team"],
        extractor="slack_send",
        examples=["send a Slack message to #general",
                  "message John on Slack",
                  "post to the #dev channel on Slack"],
        slots={"channel": "channel or person", "message": "message text"},
    ),

    "slack.read": Intent(
        id="slack.read",
        op="slack_read",
        domain="slack",
        description="Read messages from a Slack channel",
        signals=["check slack", "read slack", "slack messages",
                 "what's in slack", "slack channel", "unread slack"],
        extractor="slack_read",
        examples=["check Slack #general", "what's new in #dev on Slack",
                  "read my Slack messages"],
        slots={"channel": "channel name"},
    ),

    "slack.search": Intent(
        id="slack.search",
        op="slack_search",
        domain="slack",
        description="Search for a message in Slack",
        signals=["search slack", "find in slack", "look up slack",
                 "slack search for"],
        extractor="slack_search",
        examples=["search Slack for the deployment notes",
                  "find the standup message in Slack"],
        slots={"query": "search query"},
    ),

    "slack.status": Intent(
        id="slack.status",
        op="slack_status",
        domain="slack",
        description="Set Slack status",
        signals=["slack status", "set slack status", "set my status on slack",
                 "update slack status", "change slack status"],
        extractor="slack_status",
        examples=["set my Slack status to 'in a meeting'",
                  "update Slack status to WFH"],
        slots={"status": "status text"},
    ),

    "slack.reaction": Intent(
        id="slack.reaction",
        op="slack_reaction",
        domain="slack",
        description="Add an emoji reaction to a Slack message",
        signals=["slack react", "slack reaction", "react with emoji on slack",
                 "add reaction in slack"],
        extractor="slack_reaction",
        examples=["react with thumbs up on Slack",
                  "add a 🎉 reaction in Slack"],
        slots={"emoji": "emoji name"},
    ),

    # ── Enterprise — Notion ───────────────────────────────────────────────

    "notion.create": Intent(
        id="notion.create",
        op="notion_create",
        domain="notion",
        description="Create a new Notion page or note",
        signals=["notion page", "notion note", "create notion", "new notion",
                 "add to notion", "notion doc"],
        extractor="notion_create",
        examples=["create a Notion page about the project plan",
                  "new Notion note", "add to Notion"],
        slots={"title": "page title"},
    ),

    "notion.find": Intent(
        id="notion.find",
        op="notion_find",
        domain="notion",
        description="Find a Notion page or document",
        signals=["find notion", "search notion", "open notion page",
                 "notion page about", "my notion notes"],
        extractor="notion_find",
        examples=["find my Notion page about Q4 goals",
                  "open the Notion doc for the project"],
        slots={"query": "search query"},
    ),

    "notion.update": Intent(
        id="notion.update",
        op="notion_update",
        domain="notion",
        description="Update an existing Notion page",
        signals=["update notion", "edit notion page", "add to notion page",
                 "modify notion"],
        extractor="notion_update",
        examples=["update my Notion page for the roadmap",
                  "edit the Notion doc"],
        slots={"query": "page query"},
    ),

    "notion.database": Intent(
        id="notion.database",
        op="notion_database",
        domain="notion",
        description="Access a Notion database",
        signals=["notion database", "notion table", "notion board",
                 "my notion database", "notion kanban"],
        extractor="notion_database",
        examples=["show my Notion database", "open the Notion task board",
                  "view Notion kanban"],
        slots={"name": "database name"},
    ),

    # ── Enterprise — Asana ────────────────────────────────────────────────

    "asana.create": Intent(
        id="asana.create",
        op="asana_create",
        domain="asana",
        description="Create an Asana task",
        signals=["asana task", "create asana", "new asana task",
                 "add asana task", "asana to-do"],
        extractor="asana_create",
        examples=["create an Asana task for the launch",
                  "new Asana task: review PR by Friday"],
        slots={"title": "task title"},
    ),

    "asana.my_tasks": Intent(
        id="asana.my_tasks",
        op="asana_my_tasks",
        domain="asana",
        description="Show my Asana tasks",
        signals=["my asana tasks", "asana my tasks", "asana tasks assigned",
                 "what's on asana", "asana to-do list"],
        extractor="asana_my_tasks",
        examples=["show my Asana tasks", "what do I have on Asana",
                  "my Asana to-do list"],
        slots={},
    ),

    "asana.update": Intent(
        id="asana.update",
        op="asana_update",
        domain="asana",
        description="Update or complete an Asana task",
        signals=["complete asana task", "mark asana done", "update asana task",
                 "close asana task", "asana task complete"],
        extractor="asana_update",
        examples=["mark the Asana task as complete",
                  "update Asana task: review PR"],
        slots={"task": "task name"},
    ),

    "asana.project": Intent(
        id="asana.project",
        op="asana_project",
        domain="asana",
        description="View an Asana project",
        signals=["asana project", "show asana project", "open asana project",
                 "view asana project", "asana board"],
        extractor="asana_project",
        examples=["show the Asana project for website redesign",
                  "open the Q4 Asana project"],
        slots={"project": "project name"},
    ),

    # ── Wallet / Passes ───────────────────────────────────────────────────

    "wallet.passes": Intent(
        id="wallet.passes",
        op="wallet_passes",
        domain="wallet",
        description="View passes, tickets, and boarding passes in wallet",
        signals=["my passes", "wallet passes", "apple wallet", "google wallet",
                 "boarding pass in wallet", "ticket in wallet",
                 "show my wallet", "open wallet"],
        blockers=["payments", "pay with", "send money", "transfer"],
        extractor="wallet_passes",
        examples=["show my wallet passes", "open Apple Wallet",
                  "my boarding pass in Wallet"],
        slots={},
    ),

    "wallet.loyalty": Intent(
        id="wallet.loyalty",
        op="wallet_loyalty",
        domain="wallet",
        description="Find a loyalty or rewards card",
        signals=["loyalty card", "rewards card", "my points", "reward points",
                 "membership card", "starbucks rewards", "airline miles"],
        extractor="wallet_loyalty",
        examples=["show my Starbucks rewards card",
                  "find my airline miles card", "loyalty card for Target"],
        slots={"brand": "brand or program name"},
    ),

    "wallet.gift_card": Intent(
        id="wallet.gift_card",
        op="wallet_gift_card",
        domain="wallet",
        description="View or use a gift card",
        signals=["gift card", "show gift card", "my gift cards",
                 "gift card balance", "use gift card"],
        extractor="wallet_gift_card",
        examples=["show my Amazon gift card",
                  "what's the balance on my gift card",
                  "use my Starbucks gift card"],
        slots={"brand": "brand name"},
    ),

    "wallet.coupon": Intent(
        id="wallet.coupon",
        op="wallet_coupon",
        domain="wallet",
        description="Find a coupon, promo code, or discount offer",
        signals=["coupon", "promo code", "discount code", "my coupons",
                 "available offers", "promotional offer", "deal available"],
        blockers=["show me a deal", "find a deal on"],
        extractor="wallet_coupon",
        examples=["do I have any coupons for Target",
                  "show promo code for Nike",
                  "find discount for my order"],
        slots={"brand": "brand name"},
    ),

    # ── VPN ───────────────────────────────────────────────────────────────

    "vpn.connect": Intent(
        id="vpn.connect",
        op="vpn_connect",
        domain="vpn",
        description="Connect to a VPN server",
        signals=["connect vpn", "enable vpn", "turn on vpn", "start vpn",
                 "activate vpn", "vpn on"],
        extractor="vpn_connect",
        examples=["connect to VPN", "turn on VPN", "enable VPN US server"],
        slots={"server": "server location or name"},
    ),

    "vpn.disconnect": Intent(
        id="vpn.disconnect",
        op="vpn_disconnect",
        domain="vpn",
        description="Disconnect from VPN",
        signals=["disconnect vpn", "disable vpn", "turn off vpn",
                 "stop vpn", "vpn off"],
        extractor="vpn_disconnect",
        examples=["disconnect VPN", "turn off VPN", "disable VPN"],
        slots={},
    ),

    "vpn.status": Intent(
        id="vpn.status",
        op="vpn_status",
        domain="vpn",
        description="Check VPN connection status",
        signals=["vpn status", "am i connected to vpn", "vpn connected",
                 "is vpn on", "vpn connection"],
        extractor="vpn_status",
        examples=["am I connected to VPN", "VPN status",
                  "is my VPN on"],
        slots={},
    ),

    # ── Focus / Productivity ──────────────────────────────────────────────

    "focus.pomodoro": Intent(
        id="focus.pomodoro",
        op="focus_pomodoro",
        domain="focus",
        description="Start a Pomodoro focus timer",
        signals=["pomodoro", "focus timer", "25 minute timer", "work timer",
                 "start pomodoro", "pomodoro timer"],
        extractor="focus_pomodoro",
        examples=["start a Pomodoro", "set a 25 minute focus timer",
                  "start a Pomodoro session"],
        slots={"duration": "minutes (default 25)"},
    ),

    "focus.session": Intent(
        id="focus.session",
        op="focus_session",
        domain="focus",
        description="Start a focus work session",
        signals=["focus session", "deep work", "focus mode", "work session",
                 "start focusing", "focus for"],
        blockers=["do not disturb", "dnd", "silence"],
        extractor="focus_session",
        examples=["start a 2 hour focus session",
                  "enter deep work mode", "focus for 90 minutes"],
        slots={"duration": "duration"},
    ),

    "focus.block": Intent(
        id="focus.block",
        op="focus_block",
        domain="focus",
        description="Block distracting apps or websites during focus",
        signals=["block distractions", "block apps", "block websites",
                 "block social media during", "site blocker",
                 "distraction blocker"],
        extractor="focus_block",
        examples=["block social media for 2 hours",
                  "block Twitter during my focus session",
                  "block distracting apps"],
        slots={"app": "app or site to block"},
    ),

    "focus.stats": Intent(
        id="focus.stats",
        op="focus_stats",
        domain="focus",
        description="View focus or screen time statistics",
        signals=["screen time", "focus stats", "how long i focused",
                 "focus report", "screen time stats", "daily screen time",
                 "phone usage stats", "app usage time"],
        extractor="focus_stats",
        examples=["show my screen time", "how long did I focus today",
                  "my phone usage stats"],
        slots={},
    ),

    # ── Dictionary / Reference ────────────────────────────────────────────

    "dictionary.define": Intent(
        id="dictionary.define",
        op="dict_define",
        domain="dictionary",
        description="Look up the definition of a word",
        signals=["define", "definition of", "what does", "meaning of",
                 "look up the word", "dictionary"],
        blockers=["define a task", "define a goal", "define a project",
                  "define my", "screen time", "focus"],
        extractor="dict_define",
        examples=["define ephemeral", "what does ubiquitous mean",
                  "definition of serendipity"],
        slots={"word": "word to define"},
    ),

    "dictionary.thesaurus": Intent(
        id="dictionary.thesaurus",
        op="dict_thesaurus",
        domain="dictionary",
        description="Find synonyms or antonyms of a word",
        signals=["synonyms for", "synonym of", "antonyms for",
                 "another word for", "thesaurus", "similar words to"],
        extractor="dict_thesaurus",
        examples=["synonyms for happy", "another word for quickly",
                  "antonyms of brave"],
        slots={"word": "word"},
    ),

    "dictionary.wikipedia": Intent(
        id="dictionary.wikipedia",
        op="dict_wikipedia",
        domain="dictionary",
        description="Look up an article on Wikipedia",
        signals=["wikipedia", "wiki article", "look up on wikipedia",
                 "tell me about on wikipedia"],
        extractor="dict_wikipedia",
        examples=["Wikipedia article about black holes",
                  "look up Tokyo on Wikipedia"],
        slots={"query": "search query"},
    ),

    "dictionary.etymology": Intent(
        id="dictionary.etymology",
        op="dict_etymology",
        domain="dictionary",
        description="Look up the etymology or word origin",
        signals=["etymology", "word origin", "origin of the word",
                 "history of the word", "where does the word come from"],
        extractor="dict_etymology",
        examples=["etymology of serendipity", "origin of the word robot",
                  "where does the word 'disaster' come from"],
        slots={"word": "word"},
    ),

    # ── Password Manager ──────────────────────────────────────────────────

    "password.find": Intent(
        id="password.find",
        op="password_find",
        domain="password",
        description="Find a saved password for a service",
        signals=["my password for", "password for", "login for",
                 "credentials for", "find my password"],
        extractor="password_find",
        examples=["what's my password for Netflix",
                  "find login for GitHub",
                  "credentials for Amazon"],
        slots={"service": "service or site name"},
    ),

    "password.generate": Intent(
        id="password.generate",
        op="password_generate",
        domain="password",
        description="Generate a secure random password",
        signals=["generate a password", "create a password", "random password",
                 "strong password", "new password for", "password generator",
                 "generate password", "generate secure password"],
        patterns=[r"\bgenerate\b.{0,30}\bpassword\b"],
        extractor="password_generate",
        examples=["generate a secure password for Twitter",
                  "create a 16-character password",
                  "generate a strong random password"],
        slots={"service": "service name", "length": "length in chars"},
    ),

    "password.2fa": Intent(
        id="password.2fa",
        op="password_2fa",
        domain="password",
        description="Get a 2FA or authenticator code",
        signals=["2fa code", "two factor code", "authenticator code",
                 "totp code", "verification code for", "one time code",
                 "otp for"],
        extractor="password_2fa",
        examples=["get 2FA code for GitHub",
                  "authenticator code for Google",
                  "two-factor code for Slack"],
        slots={"service": "service name"},
    ),

    "password.update": Intent(
        id="password.update",
        op="password_update",
        domain="password",
        description="Update or change a saved password",
        signals=["update my password for", "change my password for",
                 "reset my password for", "new password for",
                 "rotate password for"],
        extractor="password_update",
        examples=["update my password for Netflix",
                  "change password for GitHub",
                  "reset my Amazon password"],
        slots={"service": "service name"},
    ),

    # ── App Store ─────────────────────────────────────────────────────────

    "app.find": Intent(
        id="app.find",
        op="app_find",
        domain="app",
        description="Search the app store for an app",
        signals=["find an app", "search for an app", "app for", "app that",
                 "find app", "look for an app", "recommend an app",
                 "best app for"],
        blockers=["app notification", "mute app", "block app"],
        extractor="app_find",
        examples=["find an app for tracking sleep",
                  "search for a good budgeting app",
                  "best app for meditation"],
        slots={"query": "app query"},
    ),

    "app.install": Intent(
        id="app.install",
        op="app_install",
        domain="app",
        description="Install an app",
        signals=["install", "download the app", "get the app",
                 "install the app", "download and install"],
        blockers=["install update", "install backup", "update apps",
                  "update my apps"],
        extractor="app_install",
        examples=["install Spotify", "download the Duolingo app",
                  "get the Notion app"],
        slots={"name": "app name"},
    ),

    "app.update": Intent(
        id="app.update",
        op="app_update",
        domain="app",
        description="Update apps to the latest version",
        signals=["update my apps", "app updates", "update all apps",
                 "pending app updates", "apps need updating",
                 "check for app updates"],
        extractor="app_update",
        examples=["update all my apps", "check for app updates",
                  "install pending app updates"],
        slots={},
    ),

    # ── Reading List ──────────────────────────────────────────────────────

    "reading.save": Intent(
        id="reading.save",
        op="reading_save",
        domain="reading",
        description="Save an article or page to reading list",
        signals=["save to reading list", "add to reading list",
                 "read later", "save this article", "bookmark this article",
                 "save article for later"],
        extractor="reading_save",
        examples=["save this article to my reading list",
                  "add to read later", "bookmark this page to read later"],
        slots={},
    ),

    "reading.list": Intent(
        id="reading.list",
        op="reading_list",
        domain="reading",
        description="View saved reading list",
        signals=["my reading list", "show reading list",
                 "articles saved to read", "read later list",
                 "saved articles", "things to read"],
        extractor="reading_list",
        examples=["show my reading list", "articles I saved to read later",
                  "open my read later list"],
        slots={},
    ),

    "reading.mark_read": Intent(
        id="reading.mark_read",
        op="reading_mark_read",
        domain="reading",
        description="Mark a reading list article as read",
        signals=["mark as read", "finished reading", "read this article",
                 "mark article read"],
        blockers=["mark notification", "mark email"],
        extractor="reading_mark_read",
        examples=["mark this article as read",
                  "I finished reading this"],
        slots={},
    ),

    # ── Date Calculator ───────────────────────────────────────────────────

    "date.days_until": Intent(
        id="date.days_until",
        op="date_days_until",
        domain="date",
        description="Calculate how many days until an event or date",
        signals=["how many days until", "days until", "days till",
                 "how long until", "when is"],
        blockers=["when is the weather", "when is my flight"],
        extractor="date_days_until",
        examples=["how many days until Christmas",
                  "days until my birthday",
                  "how long until New Year's Eve"],
        slots={"event": "event or date"},
    ),

    "date.countdown": Intent(
        id="date.countdown",
        op="date_countdown",
        domain="date",
        description="Set a countdown timer to an event",
        signals=["countdown to", "countdown timer", "count down to",
                 "time until", "time till"],
        blockers=["time until my flight", "countdown to launch"],
        extractor="date_countdown",
        examples=["countdown to my anniversary",
                  "countdown timer to graduation",
                  "count down to the Super Bowl"],
        slots={"event": "event or date"},
    ),

    "date.day_of": Intent(
        id="date.day_of",
        op="date_day_of",
        domain="date",
        description="Find out what day of the week a date falls on",
        signals=["what day is", "day of the week", "what day was",
                 "day of week for", "what day will"],
        extractor="date_day_of",
        examples=["what day is July 4th", "what day of the week is March 15",
                  "what day was December 25 2020"],
        slots={"date": "date to check"},
    ),

    "date.age": Intent(
        id="date.age",
        op="date_age",
        domain="date",
        description="Calculate age from a birth date",
        signals=["how old am i", "how old is", "calculate my age",
                 "my age if born in", "born in"],
        blockers=["how old is the account", "age of my phone"],
        extractor="date_age",
        examples=["how old am I if I was born in 1990",
                  "calculate my age born March 5 1985"],
        slots={"dob": "date of birth"},
    ),

    # ── Screen Capture ────────────────────────────────────────────────────

    "screen.screenshot": Intent(
        id="screen.screenshot",
        op="screen_screenshot",
        domain="screen",
        description="Take a screenshot",
        signals=["screenshot", "take a screenshot", "capture the screen",
                 "take a screen capture", "snap the screen"],
        extractor="screen_screenshot",
        examples=["take a screenshot", "capture the screen",
                  "take a screen snap"],
        slots={},
    ),

    "screen.record": Intent(
        id="screen.record",
        op="screen_record",
        domain="screen",
        description="Start or stop screen recording",
        signals=["screen record", "record my screen", "start recording screen",
                 "stop recording screen", "screen recorder",
                 "recording my screen", "start recording my screen",
                 "stop recording my screen"],
        blockers=["share my screen", "screen sharing"],
        extractor="screen_record",
        examples=["start recording my screen",
                  "stop screen recording",
                  "record what's on my screen"],
        slots={"action": "start|stop"},
    ),

    "screen.mirror": Intent(
        id="screen.mirror",
        op="screen_mirror",
        domain="screen",
        description="Mirror or cast screen to another display",
        signals=["mirror my screen", "cast my screen", "airplay screen",
                 "project screen", "screen mirroring", "cast to tv"],
        blockers=["share my screen", "screen sharing"],
        extractor="screen_mirror",
        examples=["mirror my screen to the TV",
                  "AirPlay my screen to the Apple TV",
                  "cast screen to Chromecast"],
        slots={"target": "target device"},
    ),

    "screen.split": Intent(
        id="screen.split",
        op="screen_split",
        domain="screen",
        description="Enable split screen or multi-window mode",
        signals=["split screen", "side by side", "split view",
                 "multitasking view", "snap window", "split window",
                 "stage manager"],
        extractor="screen_split",
        examples=["open split screen", "put apps side by side",
                  "enable Stage Manager"],
        slots={},
    ),

    # ── Print / Scan ──────────────────────────────────────────────────────

    "print.document": Intent(
        id="print.document",
        op="print_document",
        domain="print",
        description="Print a document or file",
        signals=["print this", "print the document", "print the file",
                 "print this page", "print the report", "send to printer"],
        blockers=["print photo", "print picture", "print image", "scan"],
        extractor="print_document",
        examples=["print this document", "send to printer",
                  "print the PDF"],
        slots={"name": "document name"},
    ),

    "print.photo": Intent(
        id="print.photo",
        op="print_photo",
        domain="print",
        description="Print a photo or image",
        signals=["print this photo", "print this picture", "print this image",
                 "print the photo", "photo print"],
        extractor="print_photo",
        examples=["print this photo", "print the picture",
                  "print this image"],
        slots={"name": "photo name"},
    ),

    "print.scan": Intent(
        id="print.scan",
        op="print_scan",
        domain="print",
        description="Scan a document with the camera or scanner",
        signals=["scan document", "scan this", "scanner", "scan a document",
                 "scan the receipt", "scan barcode", "scan qr code",
                 "use scanner"],
        extractor="print_scan",
        examples=["scan this document", "scan the receipt",
                  "use the scanner"],
        slots={},
    ),

    # ── Backup ────────────────────────────────────────────────────────────

    "backup.now": Intent(
        id="backup.now",
        op="backup_now",
        domain="backup",
        description="Back up the device now",
        signals=["back up now", "backup now", "start backup", "backup my phone",
                 "backup my device", "icloud backup", "google backup",
                 "run backup", "back up my phone", "back up my device",
                 "back up my"],
        extractor="backup_now",
        examples=["back up my phone now", "start iCloud backup",
                  "backup my device"],
        slots={},
    ),

    "backup.status": Intent(
        id="backup.status",
        op="backup_status",
        domain="backup",
        description="Check the status of the last backup",
        signals=["backup status", "last backup", "when was my last backup",
                 "backup progress", "is backup done"],
        extractor="backup_status",
        examples=["when was my last backup", "backup status",
                  "is my phone backed up"],
        slots={},
    ),

    # ── Accessibility ─────────────────────────────────────────────────────

    "accessibility.font": Intent(
        id="accessibility.font",
        op="access_font",
        domain="accessibility",
        description="Increase or decrease text/font size",
        signals=["font size", "text size", "make text larger", "make text smaller",
                 "increase font size", "decrease font size", "bigger text",
                 "larger text", "smaller text",
                 "make the text larger", "make the text smaller",
                 "make the text bigger", "text larger", "text smaller"],
        extractor="access_font",
        examples=["make the text larger", "increase font size",
                  "make text smaller"],
        slots={"action": "increase|decrease"},
    ),

    "accessibility.voice": Intent(
        id="accessibility.voice",
        op="access_voice",
        domain="accessibility",
        description="Toggle screen reader / VoiceOver / TalkBack",
        signals=["voiceover", "talkback", "screen reader", "voice control",
                 "turn on voiceover", "turn off voiceover",
                 "enable screen reader"],
        extractor="access_voice",
        examples=["turn on VoiceOver", "enable TalkBack",
                  "turn off screen reader"],
        slots={"action": "on|off"},
    ),

    "accessibility.zoom": Intent(
        id="accessibility.zoom",
        op="access_zoom",
        domain="accessibility",
        description="Enable accessibility zoom or magnifier",
        signals=["zoom in", "zoom out", "magnifier", "enable zoom",
                 "magnify screen", "accessibility zoom", "screen magnifier"],
        blockers=["camera zoom", "pinch to zoom", "map zoom"],
        extractor="access_zoom",
        examples=["enable accessibility zoom", "turn on magnifier",
                  "zoom in on the screen"],
        slots={"action": "in|out|toggle"},
    ),

    "accessibility.display": Intent(
        id="accessibility.display",
        op="access_display",
        domain="accessibility",
        description="Toggle display accessibility features",
        signals=["bold text", "invert colors", "reduce motion",
                 "color filter", "high contrast", "reduce transparency",
                 "grayscale mode", "display accessibility"],
        extractor="access_display",
        examples=["turn on bold text", "enable high contrast mode",
                  "turn on invert colors"],
        slots={"feature": "feature name"},
    ),

    # ── Shortcuts / Automations ───────────────────────────────────────────

    "shortcuts.run": Intent(
        id="shortcuts.run",
        op="shortcut_run",
        domain="shortcuts",
        description="Run a named shortcut or automation",
        signals=["run shortcut", "run automation", "execute shortcut",
                 "activate shortcut", "run my shortcut"],
        patterns=[r"\b(?:run|execute|activate)\b.{1,40}\bshortcut\b"],
        extractor="shortcut_run",
        examples=["run my morning routine shortcut",
                  "execute the 'send location' shortcut",
                  "run Good Morning automation"],
        slots={"name": "shortcut name"},
    ),

    "shortcuts.create": Intent(
        id="shortcuts.create",
        op="shortcut_create",
        domain="shortcuts",
        description="Create a new shortcut or automation",
        signals=["create a shortcut", "make a shortcut", "new shortcut",
                 "create an automation", "build a shortcut",
                 "new automation"],
        extractor="shortcut_create",
        examples=["create a shortcut to send my location",
                  "make a new automation for my morning routine"],
        slots={"description": "what the shortcut should do"},
    ),

    "shortcuts.list": Intent(
        id="shortcuts.list",
        op="shortcut_list",
        domain="shortcuts",
        description="List all available shortcuts",
        signals=["my shortcuts", "show shortcuts", "list shortcuts",
                 "available shortcuts", "all my shortcuts",
                 "what shortcuts do i have"],
        extractor="shortcut_list",
        examples=["show my shortcuts", "list all automations",
                  "what shortcuts do I have"],
        slots={},
    ),

    # ── Currency ──────────────────────────────────────────────────────────

    "currency.convert": Intent(
        id="currency.convert",
        op="currency_convert",
        domain="currency",
        description="Convert an amount from one currency to another",
        signals=["convert currency", "exchange rate", "in euros", "in dollars",
                 "usd to eur", "eur to usd", "convert dollars to",
                 "how much is", "currency converter"],
        blockers=["how much is it", "how much does it cost", "how much are"],
        patterns=[
            r"\b\d+(?:\.\d+)?\s+(?:dollars?|euros?|pounds?|yen|yuan|rupees?|pesos?|[A-Z]{3})\s+(?:in|to|into)\b",
            r"\bconvert\s+\d+(?:\.\d+)?\s+[A-Za-z]+\s+to\s+[A-Za-z]+\b",
        ],
        extractor="currency_convert",
        examples=["convert 100 USD to EUR", "how much is 50 euros in dollars",
                  "1000 yen in USD"],
        slots={"amount": "numeric amount", "from": "source currency", "to": "target currency"},
    ),

    "currency.rates": Intent(
        id="currency.rates",
        op="currency_rates",
        domain="currency",
        description="Show current exchange rates",
        signals=["exchange rates", "current exchange rate", "forex rates",
                 "currency rates", "rate for", "fx rate"],
        extractor="currency_rates",
        examples=["show exchange rates for EUR",
                  "current USD to GBP rate",
                  "forex rates today"],
        slots={},
    ),

    # ── Health extensions ─────────────────────────────────────────────────

    "health.cycle": Intent(
        id="health.cycle",
        op="health_cycle",
        domain="health",
        description="Track menstrual cycle or log symptoms",
        signals=["period", "menstrual cycle", "cycle tracking", "log period",
                 "track my cycle", "period symptoms", "ovulation",
                 "cycle log"],
        extractor="health_cycle",
        examples=["log my period", "track my cycle",
                  "record ovulation symptoms"],
        slots={},
    ),

    "health.streak": Intent(
        id="health.streak",
        op="health_streak",
        domain="health",
        description="View health or activity streaks",
        signals=["activity streak", "health streak", "workout streak",
                 "my streak", "step streak", "consecutive days",
                 "how many days in a row"],
        extractor="health_streak",
        examples=["show my workout streak", "how many days in a row have I worked out",
                  "my activity streak"],
        slots={},
    ),

    "health.goals": Intent(
        id="health.goals",
        op="health_goals",
        domain="health",
        description="View or update health and fitness goals",
        signals=["health goal", "fitness goal", "activity goal", "step goal",
                 "set my goal", "update my goal", "calorie goal",
                 "daily goal"],
        extractor="health_goals",
        examples=["set my daily step goal to 10000",
                  "update my calorie goal",
                  "show my health goals"],
        slots={},
    ),

    "health.hrv": Intent(
        id="health.hrv",
        op="health_hrv",
        domain="health",
        description="Check heart rate variability or readiness score",
        signals=["hrv", "heart rate variability", "readiness score",
                 "recovery score", "body battery", "strain score",
                 "how recovered am i"],
        extractor="health_hrv",
        examples=["what's my HRV today", "show my readiness score",
                  "body battery level"],
        slots={},
    ),
    # ── Genome Mesh / Network ─────────────────────────────────────────────────
    "network.view": Intent(
        id="network.view",
        op="network_view",
        domain="network",
        description="Show Genome mesh networks and community messages",
        signals=["local network", "local mesh", "genome mesh", "mesh network",
                 "network messages", "community feed", "neighborhood network",
                 "public mesh", "nearby mesh", "local community", "mesh feed",
                 "public network", "genome network", "who's on the mesh",
                 "local alerts", "local broadcasts"],
        blockers=["wifi", "internet", "vpn", "bluetooth", "network connection",
                  "network speed", "connect to"],
        extractor="generic",
        examples=["show local mesh", "what's on the network",
                  "Genome mesh messages", "local community feed"],
    ),

    # ── Connections management ─────────────────────────────────────────────────
    "connections_manage": Intent(
        id="connections_manage",
        op="connections_status",
        domain="connectors",
        description="Show and manage connected services (Spotify, Gmail, Slack, etc.)",
        signals=["connections", "connected apps", "connected services", "integrations",
                 "manage connections", "connect spotify", "connect gmail",
                 "connect slack", "connect calendar", "connect drive",
                 "link spotify", "link gmail", "link slack",
                 "disconnect spotify", "disconnect gmail", "disconnect slack",
                 "my integrations", "services connected", "what's connected"],
        blockers=["internet connection", "wifi connection", "network connection",
                  "bluetooth connection", "vpn connection"],
        extractor="connections_manage",
        examples=["show my connections", "manage connected apps",
                  "connect Spotify", "what services are connected"],
        slots={},
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
