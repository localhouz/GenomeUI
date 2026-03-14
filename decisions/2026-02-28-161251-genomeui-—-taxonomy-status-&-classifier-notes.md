---
tags: ["taxonomy", "classifier", "intents", "semantics", "NLU"]
category: decisions
created: 2026-02-28T16:12:51.582846
---

# GenomeUI — Taxonomy Status & Classifier Notes

# GenomeUI — Taxonomy Status & Classifier Notes

## Taxonomy Status (as of Feb 2026)
- ~58 intents across 15 domains
- Computer layer added: content (find/list/history/branch/revert/share), document (create/edit), spreadsheet (create/edit), presentation (create/edit), code (create/explain/debug/run), terminal (run), calendar (create/list/cancel), email (compose/read/reply/search)
- Still missing: messaging, music, navigation, food delivery, rideshare, payments, device/platform awareness, notifications, cross-device handoff, settings, health
- Sports and weather are the most complete domains; computer layer is scaffolded

## Taxonomy Classifier Notes
- TAXONOMY is an ordered dict — insertion order = priority. New intents added at end lose to earlier catch-alls (web.search) unless blockers are added.
- web.search has "explain", "what is", "search for" as signals — must add specific blockers (e.g. "explain this", "my calendar", "search for emails") to yield to domain intents that come later.
- sports.schedule has "schedule" signal — add "meeting"/"appointment" blockers so calendar.create isn't hijacked.
- `_ext_*` extractors return None to reject a match; returning {} or a dict = accept.

