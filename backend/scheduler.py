"""
backend/scheduler.py
--------------------
Background task scheduler for GenomeUI.

Runs as a companion to the FastAPI app inside the same process.
Tasks execute in a thread pool so they never block request handling.

Tasks defined:
  - gmail_sync      every 5 min   — fetch new emails, push desktop notification
  - gcal_sync       every 5 min   — fetch upcoming events, notify ≤5 min before start
  - relay_heartbeat every 30 sec  — keep-alive to connected relay node
  - session_persist every 2 min   — flush in-memory sessions to SQLite

Results are stored in an in-memory ring buffer (last 100 per task) so the
/api/scheduler endpoint can return recent history without hitting the DB.

Usage (from main.py):
    from . import scheduler as _scheduler
    await _scheduler.start(app_context)   # in startup handler
    await _scheduler.stop()               # in shutdown handler
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Coroutine

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import db as _db

_log = logging.getLogger("genomeui.scheduler")

# ── Ring buffer for task run history ──────────────────────────────────────────
_HISTORY_SIZE = 100
_history: dict[str, deque[dict[str, Any]]] = {}
_paused: set[str] = set()
_scheduler: AsyncIOScheduler | None = None

# Injected at start — avoids circular imports
_ctx: "_AppContext | None" = None


class _AppContext:
    """Thin wrapper around what scheduler tasks need from main.py."""
    def __init__(
        self,
        get_token: Callable[[str], str | None],
        gmail_list_snapshot: Callable[[], dict[str, Any]],
        gcal_list_snapshot: Callable[[], dict[str, Any]],
        persist_sessions: Callable[[], Coroutine[Any, Any, bool]],
        relay_ping: Callable[[], Coroutine[Any, Any, None]] | None = None,
        push_notification: Callable[[str, str, str], Coroutine[Any, Any, None]] | None = None,
    ):
        self.get_token = get_token
        self.gmail_list_snapshot = gmail_list_snapshot
        self.gcal_list_snapshot = gcal_list_snapshot
        self.persist_sessions = persist_sessions
        self.relay_ping = relay_ping
        self.push_notification = push_notification  # async (title, body, route)


# ── History helpers ────────────────────────────────────────────────────────────
def _record(task: str, ok: bool, detail: str, data: dict | None = None) -> None:
    entry = {
        "ts": int(time.time() * 1000),
        "ok": ok,
        "detail": detail[:200],
        "data": data or {},
    }
    if task not in _history:
        _history[task] = deque(maxlen=_HISTORY_SIZE)
    _history[task].appendleft(entry)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(_persist_record(task, entry), name=f"scheduler-record:{task}")


async def _persist_record(task: str, entry: dict[str, Any]) -> None:
    try:
        await _db.save_scheduler_run(
            task_id=task,
            ok=bool(entry.get("ok")),
            detail=str(entry.get("detail", "")),
            data=entry.get("data") if isinstance(entry.get("data"), dict) else {},
            created_at=int(entry.get("ts") or 0),
        )
    except Exception as exc:
        _log.debug("scheduler history persist error for %s: %s", task, exc)


def _is_paused(task: str) -> bool:
    return task in _paused


# ── Task implementations ───────────────────────────────────────────────────────
async def _task_gmail_sync() -> None:
    if _ctx is None or _is_paused("gmail_sync"):
        return
    try:
        snap = await asyncio.to_thread(_ctx.gmail_list_snapshot)
        msgs = snap.get("messages", [])
        unread = snap.get("unread_count", sum(1 for m in msgs if m.get("unread")))
        source = snap.get("source", "scaffold")
        _record("gmail_sync", snap.get("ok", True), f"{unread} unread | source={source}", {
            "unread": unread, "count": len(msgs), "source": source,
        })
        # Notify on new unread — only when live data
        if source == "live" and unread > 0 and _ctx.push_notification:
            first = msgs[0] if msgs else {}
            subject = str(first.get("subject", "New email"))[:60]
            sender  = str(first.get("from", ""))[:40]
            await _ctx.push_notification(
                f"Gmail — {unread} unread",
                f"{subject}{' from ' + sender if sender else ''}",
                "show my gmail",
            )
    except Exception as exc:
        _record("gmail_sync", False, str(exc)[:200])
        _log.debug("gmail_sync error: %s", exc)


async def _task_gcal_sync() -> None:
    if _ctx is None or _is_paused("gcal_sync"):
        return
    try:
        snap = await asyncio.to_thread(_ctx.gcal_list_snapshot)
        events = snap.get("events", [])
        source = snap.get("source", "scaffold")
        now_ms = int(time.time() * 1000)
        soon_events = []
        for ev in events:
            # Look for events starting within 5 minutes
            start = ev.get("start_ms") or ev.get("startMs") or 0
            if start and 0 < (start - now_ms) <= 5 * 60 * 1000:
                soon_events.append(ev)
        _record("gcal_sync", snap.get("ok", True),
                f"{len(events)} events | {len(soon_events)} soon | source={source}",
                {"count": len(events), "soon": len(soon_events), "source": source})
        if source == "live" and soon_events and _ctx.push_notification:
            ev = soon_events[0]
            title = str(ev.get("title", ev.get("summary", "Event starting soon")))[:60]
            mins  = max(1, int((ev.get("start_ms", now_ms) - now_ms) / 60000))
            await _ctx.push_notification(
                f"Calendar — starting in {mins} min",
                title,
                "show my calendar",
            )
    except Exception as exc:
        _record("gcal_sync", False, str(exc)[:200])
        _log.debug("gcal_sync error: %s", exc)


async def _task_relay_heartbeat() -> None:
    if _ctx is None or _is_paused("relay_heartbeat") or _ctx.relay_ping is None:
        return
    try:
        await _ctx.relay_ping()
        _record("relay_heartbeat", True, "ping ok")
    except Exception as exc:
        _record("relay_heartbeat", False, str(exc)[:200])
        _log.debug("relay_heartbeat error: %s", exc)


async def _task_session_persist() -> None:
    if _ctx is None or _is_paused("session_persist"):
        return
    try:
        ok = await _ctx.persist_sessions()
        _record("session_persist", ok, "flushed" if ok else "flush failed")
    except Exception as exc:
        _record("session_persist", False, str(exc)[:200])
        _log.debug("session_persist error: %s", exc)


# ── Lifecycle ──────────────────────────────────────────────────────────────────
async def start(ctx: _AppContext) -> None:
    global _scheduler, _ctx
    _ctx = ctx

    _scheduler = AsyncIOScheduler(timezone="UTC")

    _scheduler.add_job(
        _task_gmail_sync,
        trigger=IntervalTrigger(minutes=5),
        id="gmail_sync",
        name="Gmail sync",
        replace_existing=True,
        misfire_grace_time=60,
    )
    _scheduler.add_job(
        _task_gcal_sync,
        trigger=IntervalTrigger(minutes=5),
        id="gcal_sync",
        name="Google Calendar sync",
        replace_existing=True,
        misfire_grace_time=60,
    )
    _scheduler.add_job(
        _task_relay_heartbeat,
        trigger=IntervalTrigger(seconds=30),
        id="relay_heartbeat",
        name="Relay heartbeat",
        replace_existing=True,
        misfire_grace_time=15,
    )
    _scheduler.add_job(
        _task_session_persist,
        trigger=IntervalTrigger(minutes=2),
        id="session_persist",
        name="Session persist",
        replace_existing=True,
        misfire_grace_time=30,
    )

    _scheduler.start()
    _log.info("Background scheduler started — 4 tasks registered")


async def stop() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _log.info("Background scheduler stopped")
    _scheduler = None


# ── Control API helpers ────────────────────────────────────────────────────────
def get_status() -> dict[str, Any]:
    jobs = []
    if _scheduler:
        for job in _scheduler.get_jobs():
            next_run = job.next_run_time
            jobs.append({
                "id":      job.id,
                "name":    job.name,
                "paused":  job.id in _paused,
                "next_run": int(next_run.timestamp() * 1000) if next_run else None,
                "history": list(_history.get(job.id, []))[:5],
            })
    return {
        "running": bool(_scheduler and _scheduler.running),
        "jobs": jobs,
    }


async def get_status_async(history_limit: int = 5) -> dict[str, Any]:
    status = get_status()
    jobs = status.get("jobs", [])
    if not isinstance(jobs, list):
        return status
    for job in jobs:
        task_id = str(job.get("id", "")).strip()
        if not task_id:
            continue
        try:
            persisted = await _db.list_scheduler_runs(task_id=task_id, limit=history_limit)
        except Exception:
            persisted = []
        if persisted:
            job["history"] = persisted
    return status


def pause_task(task_id: str) -> bool:
    if _scheduler is None:
        return False
    try:
        _scheduler.pause_job(task_id)
        _paused.add(task_id)
        _log.info("Paused scheduler task: %s", task_id)
        return True
    except Exception:
        return False


def resume_task(task_id: str) -> bool:
    if _scheduler is None:
        return False
    try:
        _scheduler.resume_job(task_id)
        _paused.discard(task_id)
        _log.info("Resumed scheduler task: %s", task_id)
        return True
    except Exception:
        return False


def trigger_now(task_id: str) -> bool:
    """Run a task immediately (out of schedule). Useful for manual refresh."""
    _task_map = {
        "gmail_sync":       _task_gmail_sync,
        "gcal_sync":        _task_gcal_sync,
        "relay_heartbeat":  _task_relay_heartbeat,
        "session_persist":  _task_session_persist,
    }
    fn = _task_map.get(task_id)
    if fn is None:
        return False
    asyncio.ensure_future(fn())
    return True
