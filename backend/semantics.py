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
    """Collapse whitespace and strip leading filler phrases."""
    s = re.sub(r"\s+", " ", text.strip())
    return _FILLER_RE.sub("", s).strip()


_LOC_PREP_RE = re.compile(
    r"\b(?:in|at|for)\s+(.+?)"
    r"(?:\s+(?:today|tomorrow|tonight|now|right\s+now|this\s+week|next\s+week))?$",
    re.IGNORECASE,
)


def _extract_location(raw: str, lower: str) -> str:
    """Extract explicit location string or return '__current__' / '' if none."""
    # Explicit "in/at/for <place>"
    m = _LOC_PREP_RE.search(raw)
    if m:
        candidate = _normalize(m.group(1))
        if candidate.lower() not in {"today", "tomorrow", "now", "right now", "tonight"}:
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


def _extract_time_window(lower: str) -> str:
    """Extract weather/forecast time window: now | tonight | tomorrow | 7day."""
    if re.search(r"\b(tomorrow|tom(?:rw)?)\b", lower):
        return "tomorrow"
    if re.search(r"\b(tonight|this\s+(?:evening|night))\b", lower):
        return "tonight"
    if re.search(
        r"\b(this\s+week|next\s+week|weekend|7[\s\-]?day|weekly|extended|"
        r"week(?:ly)?|this\s+weekend)\b", lower
    ):
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
}


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
        # Blockers: don't hijack personal data queries
        blockers=["my tasks", "my todos", "my notes", "my expenses",
                  "my reminders", "my balance", "my account", "my feed"],
        extractor="web_search",
        examples=["what is quantum computing", "who was Alan Turing",
                  "how does DNS work", "explain neural networks",
                  "when did the Berlin Wall fall"],
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
