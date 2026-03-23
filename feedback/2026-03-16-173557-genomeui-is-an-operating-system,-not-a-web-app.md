---
tags: ["architecture", "os", "critical", "always-apply"]
category: feedback
created: 2026-03-16T17:35:57.261644
---

# GenomeUI is an Operating System, not a web app

GenomeUI IS AN OPERATING SYSTEM. It is not a web app, not a web-first app, not a browser-based app.

**This has deep implications for every decision:**
- Services (Nous, Backend) are OS kernel services — they boot with the OS, always run, auto-restart on crash
- Electron IS the OS shell — not a browser wrapper around a web app
- Caching: OS always boots clean, no browser cache semantics
- No service workers, no PWA patterns, no "hard refresh" concept
- Processes are OS processes — they register at logon, supervised by the OS scheduler
- The session graph is the OS repository (git-style) — not a web session
- Auth is OS-level (WebAuthn/passkey, OS keychain) — not web auth
- The right mental model is Windows/macOS/Linux — not React/Next.js/PWA

**When making architectural decisions, ask: "How would a real OS do this?" — not "How would a web app do this?"**

Why this matters: Claude repeatedly defaults to web-app patterns (service workers, browser cache, decoupled microservices, "refresh the page"). All of these are wrong for GenomeUI.

