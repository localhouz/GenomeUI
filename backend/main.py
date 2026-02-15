from __future__ import annotations

import asyncio
import copy
import ipaddress
import json
import os
import pathlib
import re
import uuid
from urllib.parse import urlparse
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="GenomeUI Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL_SMALL = os.getenv("OLLAMA_MODEL_SMALL", "")
OLLAMA_MODEL_LARGE = os.getenv("OLLAMA_MODEL_LARGE", "")
STORE_PATH = pathlib.Path(os.getenv("GENOMEUI_STORE_PATH", "backend/data/sessions.json"))
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TURN_LATENCY_BUDGET_MS = int(os.getenv("TURN_LATENCY_BUDGET_MS", "800"))
SLO_BREACH_STREAK_FOR_THROTTLE = int(os.getenv("SLO_BREACH_STREAK_FOR_THROTTLE", "3"))
SLO_THROTTLE_MS = int(os.getenv("SLO_THROTTLE_MS", "30000"))
CHECKPOINT_MAX_COUNT = int(os.getenv("CHECKPOINT_MAX_COUNT", "40"))
CHECKPOINT_MAX_AGE_MS = int(os.getenv("CHECKPOINT_MAX_AGE_MS", str(7 * 24 * 60 * 60 * 1000)))
JOURNAL_MAX_ENTRIES = int(os.getenv("JOURNAL_MAX_ENTRIES", "500"))
INTENT_CLARIFICATION_THRESHOLD = float(os.getenv("INTENT_CLARIFICATION_THRESHOLD", "0.65"))
GRAPH_ENTITY_KINDS = {"task", "expense", "note"}
GRAPH_RELATION_KINDS = {"depends_on", "references"}
TURN_HISTORY_MAX_ENTRIES = int(os.getenv("TURN_HISTORY_MAX_ENTRIES", "300"))
TURN_IDEMPOTENCY_MAX_ENTRIES = int(os.getenv("TURN_IDEMPOTENCY_MAX_ENTRIES", "120"))
HANDOFF_LATENCY_BUDGET_MS = int(os.getenv("HANDOFF_LATENCY_BUDGET_MS", "500"))
PRESENCE_WRITE_MIN_INTERVAL_MS = int(os.getenv("PRESENCE_WRITE_MIN_INTERVAL_MS", "15000"))
CONTINUITY_HISTORY_MAX_ENTRIES = int(os.getenv("CONTINUITY_HISTORY_MAX_ENTRIES", "300"))
CONTINUITY_ANOMALY_WINDOW = int(os.getenv("CONTINUITY_ANOMALY_WINDOW", "120"))
CONTINUITY_ANOMALY_SCORE_DROP = int(os.getenv("CONTINUITY_ANOMALY_SCORE_DROP", "15"))
CONTINUITY_AUTOPILOT_COOLDOWN_MS = int(os.getenv("CONTINUITY_AUTOPILOT_COOLDOWN_MS", "30000"))
CONTINUITY_AUTOPILOT_HISTORY_MAX = int(os.getenv("CONTINUITY_AUTOPILOT_HISTORY_MAX", "200"))
CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR = int(os.getenv("CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR", "30"))
CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX = int(os.getenv("CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX", "200"))
CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX = int(os.getenv("CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX", "300"))
CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX = int(os.getenv("CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX", "300"))


class SessionInitBody(BaseModel):
    sessionId: str | None = None


class TurnBody(BaseModel):
    intent: str
    sessionId: str | None = None
    baseRevision: int | None = None
    deviceId: str | None = None
    onConflict: str | None = None
    idempotencyKey: str | None = None


class HandoffStartBody(BaseModel):
    deviceId: str


class HandoffClaimBody(BaseModel):
    deviceId: str
    token: str


class PresenceBody(BaseModel):
    deviceId: str
    label: str | None = None
    platform: str | None = None
    userAgent: str | None = None


class PresencePruneBody(BaseModel):
    all: bool = False
    maxAgeMs: int = 120000


class ContinuityAutopilotBody(BaseModel):
    enabled: bool


class ContinuityAutopilotConfigBody(BaseModel):
    cooldownMs: int | None = None
    maxAppliesPerHour: int | None = None
    mode: str | None = None
    autoAlignMode: bool | None = None


class ContinuityAutopilotResetBody(BaseModel):
    clearHistory: bool = False


class RestoreBody(BaseModel):
    apply: bool = False
    limit: int = 500


class CheckpointRestoreBody(BaseModel):
    checkpointId: str | None = None
    replayTail: bool = True


class JournalCompactBody(BaseModel):
    keep: int = 200


class IntentPreviewBody(BaseModel):
    intent: str


@dataclass
class SessionState:
    memory: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: {"tasks": [], "expenses": [], "notes": []})
    graph: dict[str, Any] = field(default_factory=lambda: {"entities": [], "relations": [], "events": []})
    jobs: list[dict[str, Any]] = field(default_factory=list)
    presence: dict[str, Any] = field(default_factory=lambda: {"devices": {}, "updatedAt": 0})
    handoff: dict[str, Any] = field(default_factory=lambda: {"activeDeviceId": None, "pending": None, "lastClaimAt": None})
    idempotency: dict[str, Any] = field(default_factory=lambda: {"entries": []})
    slo: dict[str, Any] = field(default_factory=lambda: {"breachStreak": 0, "throttleUntil": 0, "lastTotalMs": 0, "alerts": []})
    restore: dict[str, Any] = field(default_factory=lambda: {"last": None})
    faults: dict[str, Any] = field(default_factory=lambda: {"persist": {"degraded": False, "lastError": "", "lastFailureAt": 0, "lastSuccessAt": 0, "pendingWrites": 0}})
    revision: int = 0
    last_turn: dict[str, Any] | None = None
    turn_history: list[dict[str, Any]] = field(default_factory=list)
    continuity_history: list[dict[str, Any]] = field(default_factory=list)
    continuity_autopilot: dict[str, Any] = field(default_factory=lambda: {"enabled": False, "cooldownMs": CONTINUITY_AUTOPILOT_COOLDOWN_MS, "lastRunAt": 0, "lastAppliedAt": 0, "applied": 0, "noops": 0})
    continuity_autopilot_history: list[dict[str, Any]] = field(default_factory=list)
    continuity_autopilot_posture_history: list[dict[str, Any]] = field(default_factory=list)
    continuity_autopilot_posture_action_history: list[dict[str, Any]] = field(default_factory=list)
    continuity_autopilot_posture_action_policy_history: list[dict[str, Any]] = field(default_factory=list)
    dead_letters: list[dict[str, Any]] = field(default_factory=list)
    journal: list[dict[str, Any]] = field(default_factory=list)
    undo_stack: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    sockets: set[WebSocket] = field(default_factory=set)


SESSIONS: dict[str, SessionState] = {}
SCHEDULER_TASK: asyncio.Task | None = None
STORE_LOCK = asyncio.Lock()
SIMULATE_PERSIST_FAILURE = False


def normalize_session_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", (value or "").lower())[:32]


def normalize_device_id(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", (value or "").lower())[:32]


def generate_session_id() -> str:
    return str(uuid.uuid4())[:8]


def default_handoff_state() -> dict[str, Any]:
    return {
        "activeDeviceId": None,
        "pending": None,
        "lastClaimAt": None,
        "stats": {
            "budgetMs": int(HANDOFF_LATENCY_BUDGET_MS),
            "starts": 0,
            "claims": 0,
            "expired": 0,
            "invalid": 0,
            "breaches": 0,
            "lastBreachAt": 0,
            "lastMs": 0,
            "maxMs": 0,
            "avgMs": 0,
            "p95Ms": 0,
            "samples": [],
            "alerts": [],
        },
    }


def default_presence_state() -> dict[str, Any]:
    return {
        "devices": {},
        "updatedAt": 0,
        "stats": {
            "heartbeatWrites": 0,
            "heartbeatCoalesced": 0,
            "prunedTotal": 0,
            "lastPruneAt": 0,
            "lastPruneRemoved": 0,
        },
    }


def ensure_presence_state(presence: dict[str, Any] | None) -> dict[str, Any]:
    current = presence if isinstance(presence, dict) else {}
    base = default_presence_state()
    devices_raw = current.get("devices") if isinstance(current.get("devices"), dict) else {}
    devices: dict[str, dict[str, Any]] = {}
    for key, value in devices_raw.items():
        did = normalize_device_id(str(key))
        if not did or not isinstance(value, dict):
            continue
        devices[did] = {
            "deviceId": did,
            "label": str(value.get("label", "") or "")[:48],
            "platform": str(value.get("platform", "") or "")[:32],
            "userAgent": str(value.get("userAgent", "") or "")[:140],
            "lastSeenAt": int(value.get("lastSeenAt", 0) or 0),
        }
    stats_raw = current.get("stats") if isinstance(current.get("stats"), dict) else {}
    return {
        "devices": devices,
        "updatedAt": int(current.get("updatedAt", 0) or 0),
        "stats": {
            "heartbeatWrites": int(stats_raw.get("heartbeatWrites", base["stats"]["heartbeatWrites"]) or 0),
            "heartbeatCoalesced": int(stats_raw.get("heartbeatCoalesced", base["stats"]["heartbeatCoalesced"]) or 0),
            "prunedTotal": int(stats_raw.get("prunedTotal", base["stats"]["prunedTotal"]) or 0),
            "lastPruneAt": int(stats_raw.get("lastPruneAt", base["stats"]["lastPruneAt"]) or 0),
            "lastPruneRemoved": int(stats_raw.get("lastPruneRemoved", base["stats"]["lastPruneRemoved"]) or 0),
        },
    }


def default_continuity_autopilot_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "cooldownMs": int(CONTINUITY_AUTOPILOT_COOLDOWN_MS),
        "maxAppliesPerHour": int(CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR),
        "mode": "normal",
        "autoAlignMode": False,
        "lastRunAt": 0,
        "lastAppliedAt": 0,
        "lastAlignAt": 0,
        "applied": 0,
        "aligned": 0,
        "noops": 0,
        "lastAction": "",
        "lastResult": "",
        "appliedTimestamps": [],
    }


def ensure_continuity_autopilot_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    current = raw if isinstance(raw, dict) else {}
    base = default_continuity_autopilot_state()
    cooldown_raw = current.get("cooldownMs", base["cooldownMs"])
    max_applies_raw = current.get("maxAppliesPerHour", base["maxAppliesPerHour"])
    cooldown_ms = int(base["cooldownMs"] if cooldown_raw is None else cooldown_raw)
    max_applies = int(base["maxAppliesPerHour"] if max_applies_raw is None else max_applies_raw)
    ts_raw = current.get("appliedTimestamps", [])
    ts_items: list[int] = []
    if isinstance(ts_raw, list):
        for item in ts_raw[-1000:]:
            try:
                ts_items.append(int(item or 0))
            except Exception:
                continue
    mode = str(current.get("mode", base["mode"]) or "normal").strip().lower()
    if mode not in {"safe", "normal", "aggressive"}:
        mode = "normal"
    return {
        "enabled": bool(current.get("enabled", base["enabled"])),
        "cooldownMs": max(1000, cooldown_ms),
        "maxAppliesPerHour": max(0, min(500, max_applies)),
        "mode": mode,
        "autoAlignMode": bool(current.get("autoAlignMode", base["autoAlignMode"])),
        "lastRunAt": int(current.get("lastRunAt", base["lastRunAt"]) or 0),
        "lastAppliedAt": int(current.get("lastAppliedAt", base["lastAppliedAt"]) or 0),
        "lastAlignAt": int(current.get("lastAlignAt", base["lastAlignAt"]) or 0),
        "applied": int(current.get("applied", base["applied"]) or 0),
        "aligned": int(current.get("aligned", base["aligned"]) or 0),
        "noops": int(current.get("noops", base["noops"]) or 0),
        "lastAction": str(current.get("lastAction", base["lastAction"]) or "")[:80],
        "lastResult": str(current.get("lastResult", base["lastResult"]) or "")[:160],
        "appliedTimestamps": ts_items[-500:],
    }


def normalize_idempotency_key(value: str | None) -> str:
    key = re.sub(r"[^a-zA-Z0-9:_-]", "", str(value or "").strip())
    return key[:96]


def ensure_idempotency_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    current = raw if isinstance(raw, dict) else {}
    entries_raw = current.get("entries", [])
    entries: list[dict[str, Any]] = []
    if isinstance(entries_raw, list):
        for item in entries_raw[-int(TURN_IDEMPOTENCY_MAX_ENTRIES) :]:
            if not isinstance(item, dict):
                continue
            key = normalize_idempotency_key(item.get("key"))
            if not key:
                continue
            intent = str(item.get("intent", ""))[:240]
            response = item.get("response")
            if not isinstance(response, dict):
                continue
            entries.append(
                {
                    "key": key,
                    "intent": intent,
                    "timestamp": int(item.get("timestamp", 0) or 0),
                    "revision": int(item.get("revision", 0) or 0),
                    "response": response,
                }
            )
    return {"entries": entries[-int(TURN_IDEMPOTENCY_MAX_ENTRIES) :]}


def ensure_handoff_state(handoff: dict[str, Any] | None) -> dict[str, Any]:
    current = handoff if isinstance(handoff, dict) else {}
    base = default_handoff_state()
    merged = {
        "activeDeviceId": current.get("activeDeviceId"),
        "pending": current.get("pending"),
        "lastClaimAt": current.get("lastClaimAt"),
        "stats": copy.deepcopy(base["stats"]),
    }
    current_stats = current.get("stats") if isinstance(current.get("stats"), dict) else {}
    for key in ("budgetMs", "starts", "claims", "expired", "invalid", "breaches", "lastBreachAt", "lastMs", "maxMs", "avgMs", "p95Ms"):
        merged["stats"][key] = int(current_stats.get(key, merged["stats"][key]) or 0)
    raw_samples = current_stats.get("samples", [])
    if isinstance(raw_samples, list):
        merged["stats"]["samples"] = [max(0, int(x or 0)) for x in raw_samples[-200:]]
    raw_alerts = current_stats.get("alerts", [])
    alerts: list[dict[str, Any]] = []
    if isinstance(raw_alerts, list):
        for item in raw_alerts[-100:]:
            if not isinstance(item, dict):
                continue
            alerts.append(
                {
                    "ts": int(item.get("ts", 0) or 0),
                    "claimMs": int(item.get("claimMs", 0) or 0),
                    "budgetMs": int(item.get("budgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS),
                    "deviceId": str(item.get("deviceId", ""))[:32],
                }
            )
    merged["stats"]["alerts"] = alerts[-50:]
    return merged


def ensure_session(session_id: str) -> SessionState:
    if session_id not in SESSIONS:
        SESSIONS[session_id] = SessionState()
    session = SESSIONS[session_id]
    session.presence = ensure_presence_state(session.presence)
    session.handoff = ensure_handoff_state(session.handoff)
    session.idempotency = ensure_idempotency_state(session.idempotency)
    session.continuity_autopilot = ensure_continuity_autopilot_state(session.continuity_autopilot)
    if not isinstance(session.continuity_autopilot_history, list):
        session.continuity_autopilot_history = []
    session.continuity_autopilot_history = [item for item in session.continuity_autopilot_history if isinstance(item, dict)][
        -int(CONTINUITY_AUTOPILOT_HISTORY_MAX) :
    ]
    if not isinstance(session.continuity_autopilot_posture_history, list):
        session.continuity_autopilot_posture_history = []
    session.continuity_autopilot_posture_history = [
        item for item in session.continuity_autopilot_posture_history if isinstance(item, dict)
    ][-int(CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX) :]
    if not isinstance(session.continuity_autopilot_posture_action_history, list):
        session.continuity_autopilot_posture_action_history = []
    session.continuity_autopilot_posture_action_history = [
        item for item in session.continuity_autopilot_posture_action_history if isinstance(item, dict)
    ][-int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX) :]
    if not isinstance(session.continuity_autopilot_posture_action_policy_history, list):
        session.continuity_autopilot_posture_action_policy_history = []
    session.continuity_autopilot_posture_action_policy_history = [
        item for item in session.continuity_autopilot_posture_action_policy_history if isinstance(item, dict)
    ][-int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX) :]
    prune_checkpoints(session)
    # Backward-compatible migration path: seed graph from memory if needed.
    if not session.graph.get("entities"):
        session.graph = memory_to_graph(session.memory)
    violations = validate_graph_contract(session.graph)
    if violations:
        # Repair to a safe baseline projection when persisted graph drifts from contract.
        session.graph = memory_to_graph(session.memory)
        graph_add_event(session.graph, "graph_contract_repair", {"source": "ensure_session", "violations": len(violations)})
    session.memory = graph_to_memory(session.graph)
    return session


def serialize_session_state(session: SessionState) -> dict[str, Any]:
    return {
        "memory": session.memory,
        "graph": session.graph,
        "jobs": session.jobs,
        "presence": session.presence,
        "handoff": session.handoff,
        "idempotency": session.idempotency,
        "slo": session.slo,
        "restore": session.restore,
        "faults": session.faults,
        "revision": session.revision,
        "last_turn": session.last_turn,
        "turn_history": session.turn_history,
        "continuity_history": session.continuity_history,
        "continuity_autopilot": session.continuity_autopilot,
        "continuity_autopilot_history": session.continuity_autopilot_history,
        "continuity_autopilot_posture_history": session.continuity_autopilot_posture_history,
        "continuity_autopilot_posture_action_history": session.continuity_autopilot_posture_action_history,
        "continuity_autopilot_posture_action_policy_history": session.continuity_autopilot_posture_action_policy_history,
        "dead_letters": session.dead_letters,
        "journal": session.journal,
        "undo_stack": session.undo_stack,
        "checkpoints": session.checkpoints,
    }


def deserialize_session_state(payload: dict[str, Any]) -> SessionState:
    return SessionState(
        memory=payload.get("memory") or {"tasks": [], "expenses": [], "notes": []},
        graph=payload.get("graph") or {"entities": [], "relations": [], "events": []},
        jobs=payload.get("jobs") or [],
        presence=ensure_presence_state(payload.get("presence")),
        handoff=ensure_handoff_state(payload.get("handoff")),
        idempotency=ensure_idempotency_state(payload.get("idempotency")),
        slo=payload.get("slo") or {"breachStreak": 0, "throttleUntil": 0, "lastTotalMs": 0, "alerts": []},
        restore=payload.get("restore") or {"last": None},
        faults=payload.get("faults") or {"persist": {"degraded": False, "lastError": "", "lastFailureAt": 0, "lastSuccessAt": 0, "pendingWrites": 0}},
        revision=int(payload.get("revision", 0) or 0),
        last_turn=payload.get("last_turn"),
        turn_history=payload.get("turn_history") or [],
        continuity_history=payload.get("continuity_history") or [],
        continuity_autopilot=ensure_continuity_autopilot_state(payload.get("continuity_autopilot")),
        continuity_autopilot_history=payload.get("continuity_autopilot_history") or [],
        continuity_autopilot_posture_history=payload.get("continuity_autopilot_posture_history") or [],
        continuity_autopilot_posture_action_history=payload.get("continuity_autopilot_posture_action_history") or [],
        continuity_autopilot_posture_action_policy_history=payload.get("continuity_autopilot_posture_action_policy_history") or [],
        dead_letters=payload.get("dead_letters") or [],
        journal=payload.get("journal") or [],
        undo_stack=payload.get("undo_stack") or [],
        checkpoints=payload.get("checkpoints") or [],
    )


async def persist_sessions_to_disk() -> None:
    if SIMULATE_PERSIST_FAILURE:
        raise OSError("simulated persist failure")
    async with STORE_LOCK:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "sessions": {sid: serialize_session_state(session) for sid, session in SESSIONS.items()},
        }
        STORE_PATH.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def mark_persist_fault(session: SessionState, error: str) -> None:
    persist = session.faults.setdefault("persist", {})
    persist["degraded"] = True
    persist["lastError"] = str(error)[:240]
    persist["lastFailureAt"] = now_ms()
    persist["pendingWrites"] = int(persist.get("pendingWrites", 0) or 0) + 1


def clear_persist_fault(session: SessionState) -> None:
    persist = session.faults.setdefault("persist", {})
    persist["degraded"] = False
    persist["lastError"] = ""
    persist["lastSuccessAt"] = now_ms()
    persist["pendingWrites"] = 0


async def persist_sessions_to_disk_safe(context: str = "") -> bool:
    try:
        await persist_sessions_to_disk()
    except Exception as exc:
        for session in SESSIONS.values():
            mark_persist_fault(session, f"{context}:{str(exc)}")
        return False
    for session in SESSIONS.values():
        clear_persist_fault(session)
    return True


async def load_sessions_from_disk() -> None:
    async with STORE_LOCK:
        if not STORE_PATH.exists():
            return
        try:
            raw = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        sessions = raw.get("sessions", {})
        if not isinstance(sessions, dict):
            return
        for sid, payload in sessions.items():
            norm = normalize_session_id(str(sid))
            if not norm or not isinstance(payload, dict):
                continue
            SESSIONS[norm] = deserialize_session_state(payload)


@app.on_event("startup")
async def startup_scheduler() -> None:
    await load_sessions_from_disk()
    global SCHEDULER_TASK
    if SCHEDULER_TASK is None or SCHEDULER_TASK.done():
        SCHEDULER_TASK = asyncio.create_task(run_scheduler_loop())


@app.on_event("shutdown")
async def shutdown_scheduler() -> None:
    global SCHEDULER_TASK
    if SCHEDULER_TASK and not SCHEDULER_TASK.done():
        SCHEDULER_TASK.cancel()
        try:
            await SCHEDULER_TASK
        except asyncio.CancelledError:
            pass
    SCHEDULER_TASK = None
    await persist_sessions_to_disk_safe("shutdown")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "planner": "router(local+ollama-fallback)",
        "model": {
            "small": OLLAMA_MODEL_SMALL or None,
            "large": OLLAMA_MODEL_LARGE or None,
        },
    }


@app.post("/api/session/init")
async def init_session(body: SessionInitBody) -> dict[str, Any]:
    session_id = normalize_session_id(body.sessionId or "") or generate_session_id()
    session = ensure_session(session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "revision": session.revision,
        "memory": session.memory,
        "presence": session.presence,
        "handoff": session.handoff,
        "continuityAutopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "planner": "session-sync-v2",
    }


@app.get("/api/session/{session_id}")
async def get_session(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "revision": session.revision,
        "memory": session.memory,
        "presence": session.presence,
        "handoff": session.handoff,
        "continuityAutopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "slo": session.slo,
        "restore": session.restore,
        "faults": session.faults,
        "checkpointCount": len(session.checkpoints),
        "lastTurn": session.last_turn,
    }


@app.post("/api/session/{session_id}/presence")
async def upsert_session_presence(session_id: str, body: PresenceBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    device_id = normalize_device_id(body.deviceId)
    if not device_id:
        raise HTTPException(status_code=400, detail="invalid device id")
    devices = session.presence.get("devices") if isinstance(session.presence.get("devices"), dict) else {}
    stats = session.presence.get("stats") if isinstance(session.presence.get("stats"), dict) else default_presence_state()["stats"]
    now = now_ms()
    next_item = {
        "deviceId": device_id,
        "label": str(body.label or "")[:48],
        "platform": str(body.platform or "")[:32],
        "userAgent": str(body.userAgent or "")[:140],
        "lastSeenAt": now,
    }
    prior = devices.get(device_id) if isinstance(devices.get(device_id), dict) else None
    coalesced = False
    if prior:
        same_shape = (
            str(prior.get("label", "")) == str(next_item.get("label", ""))
            and str(prior.get("platform", "")) == str(next_item.get("platform", ""))
            and str(prior.get("userAgent", "")) == str(next_item.get("userAgent", ""))
        )
        last_seen = int(prior.get("lastSeenAt", 0) or 0)
        if same_shape and (now - last_seen) < int(PRESENCE_WRITE_MIN_INTERVAL_MS):
            coalesced = True
    devices[device_id] = {
        **(prior or {}),
        **next_item,
        "lastSeenAt": int(prior.get("lastSeenAt", now) or now) if coalesced and prior else now,
    }
    session.presence["devices"] = devices
    if coalesced:
        stats["heartbeatCoalesced"] = int(stats.get("heartbeatCoalesced", 0) or 0) + 1
    else:
        stats["heartbeatWrites"] = int(stats.get("heartbeatWrites", 0) or 0) + 1
        session.presence["updatedAt"] = now
    session.presence["stats"] = stats
    if coalesced:
        payload = build_presence_payload(session, sid)
        payload["coalesced"] = True
        payload["writeMinIntervalMs"] = int(PRESENCE_WRITE_MIN_INTERVAL_MS)
        return payload
    append_continuity_history_snapshot(session, sid, "presence_upsert")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("presence_upsert")
    payload = build_presence_payload(session, sid)
    payload["coalesced"] = False
    payload["writeMinIntervalMs"] = int(PRESENCE_WRITE_MIN_INTERVAL_MS)
    return payload


@app.get("/api/session/{session_id}/presence")
async def get_session_presence(session_id: str, timeout_ms: int = Query(120000, ge=1000, le=1800000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_presence_payload(session, sid, timeout_ms=timeout_ms)


@app.post("/api/session/{session_id}/presence/prune")
async def prune_session_presence(session_id: str, body: PresencePruneBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = prune_presence_entries(
        session,
        max_age_ms=int(body.maxAgeMs or 120000),
        remove_all=bool(body.all),
    )
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("presence_prune")
    payload = build_presence_payload(session, sid)
    payload["pruned"] = report
    return payload


@app.get("/api/session/{session_id}/journal")
async def get_session_journal(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "count": min(limit, len(session.journal)),
        "items": session.journal[-limit:],
    }


@app.post("/api/session/{session_id}/journal/compact")
async def compact_session_journal(session_id: str, body: JournalCompactBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    keep = max(1, min(int(body.keep or 200), 5000))
    removed = compact_journal(session, keep)
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("compact_journal_endpoint")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": session.revision,
        "removed": removed,
        "remaining": len(session.journal),
        "keep": keep,
    }


@app.get("/api/session/{session_id}/audit")
async def get_session_audit(
    session_id: str,
    limit: int = Query(200, ge=1, le=2000),
    domain: str | None = Query(None),
    risk: str | None = Query(None),
    ok: bool | None = Query(None),
    op: str | None = Query(None),
    policy_code: str | None = Query(None),
    format: str = Query("json"),
):
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = filter_journal_entries(
        session.journal,
        domain=domain,
        risk=risk,
        ok=ok,
        op=op,
        policy_code=policy_code,
        limit=limit,
    )
    summary = audit_summary(items)
    if format.lower() == "ndjson":
        lines = [to_json(item) for item in items]
        return PlainTextResponse("\n".join(lines), media_type="application/x-ndjson")
    return {"ok": True, "sessionId": sid, "count": len(items), "summary": summary, "items": items}


@app.post("/api/session/{session_id}/restore")
async def restore_session_from_journal(session_id: str, body: RestoreBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    limit = int(body.limit or 500)
    entries = session.journal[-max(1, min(limit, 5000)) :]
    rebuilt = rebuild_session_from_journal_entries(entries)
    rebuilt_counts = graph_counts(rebuilt.graph)
    memory = graph_to_memory(rebuilt.graph)
    if not body.apply:
        return {
            "ok": True,
            "applied": False,
            "sessionId": sid,
            "entriesReplayed": len(entries),
            "counts": rebuilt_counts,
            "memory": memory,
        }

    session.graph = rebuilt.graph
    session.jobs = rebuilt.jobs
    session.undo_stack = rebuilt.undo_stack
    session.memory = memory
    session.restore["last"] = {
        "ts": now_ms(),
        "source": "journal_restore",
        "applied": True,
        "entriesReplayed": len(entries),
    }
    session.revision += 1
    graph_add_event(session.graph, "restore_from_journal", {"entriesReplayed": len(entries), "applied": True})
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("create_checkpoint_endpoint")
    return {
        "ok": True,
        "applied": True,
        "sessionId": sid,
        "revision": session.revision,
        "entriesReplayed": len(entries),
        "counts": graph_counts(session.graph),
        "memory": session.memory,
    }


@app.get("/api/session/{session_id}/graph")
async def get_session_graph(session_id: str, limit: int = Query(200, ge=1, le=2000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    entities = session.graph.get("entities", [])
    relations = session.graph.get("relations", [])
    events = session.graph.get("events", [])
    return {
        "ok": True,
        "sessionId": sid,
        "counts": graph_counts(session.graph),
        "entities": entities[-limit:],
        "relations": relations[-limit:],
        "events": events[-limit:],
    }


@app.get("/api/session/{session_id}/trace")
async def get_session_trace(
    session_id: str,
    limit: int = Query(50, ge=1, le=500),
    ok: bool | None = Query(None),
    intent_class: str | None = Query(None),
    route_reason: str | None = Query(None),
    format: str = Query("json"),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = filter_turn_history_entries(
        session.turn_history,
        ok=ok,
        intent_class=intent_class,
        route_reason=route_reason,
        limit=limit,
    )
    payload = {
        "ok": True,
        "sessionId": sid,
        "count": len(items),
        "items": items,
    }
    if str(format or "json").strip().lower() in {"ndjson", "jsonl"}:
        lines = [to_json(item) for item in items]
        return PlainTextResponse("\n".join(lines), media_type="application/x-ndjson")
    return payload


@app.get("/api/session/{session_id}/trace/summary")
async def get_session_trace_summary(session_id: str, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = session.turn_history[-limit:]
    summary = summarize_turn_history(items)
    return {
        "ok": True,
        "sessionId": sid,
        "limit": limit,
        "summary": summary,
    }


@app.get("/api/session/{session_id}/graph/dependencies")
async def get_session_graph_dependencies(
    session_id: str,
    task: str = Query(..., min_length=1),
    mode: str = Query("summary"),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    task_entity = find_task_entity(session.graph, task)
    if not task_entity:
        raise HTTPException(status_code=404, detail="task not found")

    payload = dependency_analysis_for_task(session.graph, task_entity)
    mode_norm = str(mode or "summary").strip().lower()
    if mode_norm not in {"summary", "chain", "blockers", "impact"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    if mode_norm == "chain":
        payload = {"chain": payload["chain"]}
    elif mode_norm == "blockers":
        payload = {"blockers": payload["blockers"]}
    elif mode_norm == "impact":
        payload = {"impact": payload["impact"]}

    return {
        "ok": True,
        "sessionId": sid,
        "task": {"id": task_entity.get("id"), "label": graph_entity_label(task_entity)},
        "mode": mode_norm,
        **payload,
    }


@app.get("/api/session/{session_id}/jobs")
async def get_session_jobs(session_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "count": min(limit, len(session.jobs)),
        "items": session.jobs[-limit:],
    }


@app.get("/api/session/{session_id}/dead-letters")
async def get_session_dead_letters(session_id: str, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = session.dead_letters[-limit:]
    return {
        "ok": True,
        "sessionId": sid,
        "count": len(items),
        "items": items,
    }


@app.get("/api/session/{session_id}/runtime/health")
async def get_session_runtime_health(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return get_runtime_health_payload(session, sid)


@app.get("/api/session/{session_id}/runtime/self-check")
async def get_session_runtime_self_check(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_runtime_self_check_report(session, sid)


@app.get("/api/session/{session_id}/runtime/profile")
async def get_session_runtime_profile(session_id: str, limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_runtime_profile_payload(session, sid, limit=limit)


@app.post("/api/session/{session_id}/intent/preview")
async def preview_session_intent(session_id: str, body: IntentPreviewBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    intent = str(body.intent or "").strip()
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")
    return build_intent_preview_report(session, sid, intent)


@app.get("/api/session/{session_id}/diagnostics")
async def get_session_diagnostics(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    health = get_runtime_health_payload(session, sid)
    presence = build_presence_payload(session, sid)
    continuity = build_continuity_payload(session, sid)
    continuity_anomalies = detect_continuity_anomalies(session.continuity_history, limit=20)
    continuity_incidents = build_continuity_incidents(session, sid, limit=20)
    continuity_next = build_continuity_next_actions(session, sid, limit=5)
    continuity_autopilot = ensure_continuity_autopilot_state(session.continuity_autopilot)
    continuity_autopilot_preview = build_continuity_autopilot_preview(session, sid)
    continuity_autopilot_metrics = build_continuity_autopilot_metrics(session, sid, window_ms=3600000)
    continuity_autopilot_guardrails = evaluate_continuity_autopilot_guardrails(session)
    continuity_autopilot_mode = build_continuity_autopilot_mode_recommendation(session, sid)
    continuity_autopilot_drift = build_continuity_autopilot_mode_drift(session, sid)
    self_check = build_runtime_self_check_report(session, sid)
    trace_summary = summarize_turn_history(session.turn_history[-200:])
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "health": health,
        "presence": {
            "activeCount": int(presence.get("activeCount", 0) or 0),
            "staleCount": int(presence.get("staleCount", 0) or 0),
            "count": int(presence.get("count", 0) or 0),
        },
        "continuity": continuity.get("summary", {}),
        "continuityHealth": continuity.get("health", {}),
        "continuityAnomalies": {
            "count": int(len(continuity_anomalies.get("items", []))),
            "window": int(continuity_anomalies.get("window", 0) or 0),
            "topSeverity": str((continuity_anomalies.get("summary", {}) or {}).get("topSeverity", "none")),
        },
        "continuityIncidents": {
            "count": int(len(continuity_incidents.get("items", []))),
            "topSeverity": str((continuity_incidents.get("summary", {}) or {}).get("topSeverity", "none")),
        },
        "continuityNext": {
            "count": int(len(continuity_next.get("items", []))),
            "topPriority": str((continuity_next.get("summary", {}) or {}).get("topPriority", "none")),
        },
        "continuityAutopilot": {
            "enabled": bool(continuity_autopilot.get("enabled", False)),
            "applied": int(continuity_autopilot.get("applied", 0) or 0),
            "noops": int(continuity_autopilot.get("noops", 0) or 0),
            "lastResult": str(continuity_autopilot.get("lastResult", "")),
            "historyCount": int(len(session.continuity_autopilot_history)),
            "previewReason": str((continuity_autopilot_preview.get("preview", {}) or {}).get("reason", "unknown")),
            "recentEvents1h": int((continuity_autopilot_metrics.get("metrics", {}) or {}).get("recentCount", 0) or 0),
            "guardrailBlockers": int(continuity_autopilot_guardrails.get("blockerCount", 0) or 0),
            "recommendedMode": str(continuity_autopilot_mode.get("recommendedMode", "normal")),
            "modeDrifted": bool(continuity_autopilot_drift.get("drifted", False)),
            "alignedCount": int(continuity_autopilot.get("aligned", 0) or 0),
        },
        "selfCheck": self_check,
        "traceSummary": trace_summary,
        "journal": {
            "count": int(len(session.journal)),
            "lastPolicyCode": str((session.journal[-1] if session.journal else {}).get("policyCode", "")),
        },
    }


@app.get("/api/session/{session_id}/continuity")
async def get_session_continuity(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_payload(session, sid)


@app.get("/api/session/{session_id}/continuity/health")
async def get_session_continuity_health(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    continuity = build_continuity_payload(session, sid)
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "health": continuity.get("health", {}),
        "summary": continuity.get("summary", {}),
    }


@app.get("/api/session/{session_id}/continuity/history")
async def get_session_continuity_history(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = session.continuity_history[-max(1, min(int(limit), 500)) :]
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(items)),
        "items": items,
    }


@app.get("/api/session/{session_id}/continuity/anomalies")
async def get_session_continuity_anomalies(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = detect_continuity_anomalies(session.continuity_history, limit=max(1, min(int(limit), 500)))
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(report.get("items", []))),
        "window": int(report.get("window", 0) or 0),
        "items": report.get("items", []),
        "summary": report.get("summary", {}),
    }


@app.get("/api/session/{session_id}/continuity/incidents")
async def get_session_continuity_incidents(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = build_continuity_incidents(session, sid, limit=max(1, min(int(limit), 500)))
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(report.get("items", []))),
        "items": report.get("items", []),
        "summary": report.get("summary", {}),
    }


@app.get("/api/session/{session_id}/continuity/next")
async def get_session_continuity_next(session_id: str, limit: int = Query(5, ge=1, le=20)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = build_continuity_next_actions(session, sid, limit=max(1, min(int(limit), 20)))
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(report.get("items", []))),
        "items": report.get("items", []),
        "summary": report.get("summary", {}),
    }


@app.get("/api/session/{session_id}/continuity/autopilot")
async def get_session_continuity_autopilot(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
    }


@app.get("/api/session/{session_id}/continuity/autopilot/history")
async def get_session_continuity_autopilot_history(session_id: str, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = session.continuity_autopilot_history[-max(1, min(int(limit), 500)) :]
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(items)),
        "items": items,
    }


@app.get("/api/session/{session_id}/continuity/autopilot/preview")
async def get_session_continuity_autopilot_preview(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_preview(session, sid)


@app.get("/api/session/{session_id}/continuity/autopilot/metrics")
async def get_session_continuity_autopilot_metrics(session_id: str, window_ms: int = Query(3600000, ge=60000, le=86400000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_metrics(session, sid, window_ms=int(window_ms))


@app.get("/api/session/{session_id}/continuity/autopilot/dry-run")
async def get_session_continuity_autopilot_dry_run(session_id: str, force: bool = Query(False)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_dry_run(session, sid, force=bool(force))


@app.get("/api/session/{session_id}/continuity/autopilot/guardrails")
async def get_session_continuity_autopilot_guardrails(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "guardrails": evaluate_continuity_autopilot_guardrails(session),
    }


@app.get("/api/session/{session_id}/continuity/autopilot/mode-recommendation")
async def get_session_continuity_autopilot_mode_recommendation(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_mode_recommendation(session, sid)


@app.get("/api/session/{session_id}/continuity/autopilot/mode-drift")
async def get_session_continuity_autopilot_mode_drift(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_mode_drift(session, sid)


@app.get("/api/session/{session_id}/continuity/autopilot/mode-alignment")
async def get_session_continuity_autopilot_mode_alignment(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_mode_alignment(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/mode-policy")
async def get_session_continuity_autopilot_mode_policy(session_id: str, target: str = Query("normal")) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return evaluate_continuity_autopilot_mode_policy(session, sid, target)


@app.get("/api/session/{session_id}/continuity/autopilot/mode-policy/history")
async def get_session_continuity_autopilot_mode_policy_history(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_mode_policy_history(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/mode-policy/matrix")
async def get_session_continuity_autopilot_mode_policy_matrix(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_mode_policy_matrix(session, sid)


@app.get("/api/session/{session_id}/continuity/autopilot/posture")
async def get_session_continuity_autopilot_posture(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture(session, sid)


@app.get("/api/session/{session_id}/continuity/autopilot/posture/history")
async def get_session_continuity_autopilot_posture_history(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_history(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/anomalies")
async def get_session_continuity_autopilot_posture_anomalies(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return detect_continuity_autopilot_posture_anomalies(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions")
async def get_session_continuity_autopilot_posture_actions(session_id: str, limit: int = Query(5, ge=1, le=20)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_actions(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/history")
async def get_session_continuity_autopilot_posture_actions_history(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_history(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/metrics")
async def get_session_continuity_autopilot_posture_actions_metrics(session_id: str, window_ms: int = Query(3600000, ge=60000, le=86400000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_metrics(session, sid, window_ms=int(window_ms))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/anomalies")
async def get_session_continuity_autopilot_posture_actions_anomalies(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return detect_continuity_autopilot_posture_action_anomalies(session, sid, limit=int(limit))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/dry-run")
async def get_session_continuity_autopilot_posture_action_dry_run(session_id: str, index: int = Query(1, ge=1, le=10)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_dry_run(session, sid, index=max(1, int(index)), record=True, source="api_dry_run")


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy-matrix")
async def get_session_continuity_autopilot_posture_action_policy_matrix(session_id: str, limit: int = Query(10, ge=1, le=20)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_matrix(session, sid, limit=max(1, min(int(limit), 20)))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/history")
async def get_session_continuity_autopilot_posture_action_policy_history(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_history(session, sid, limit=max(1, min(int(limit), 200)))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/metrics")
async def get_session_continuity_autopilot_posture_action_policy_metrics(session_id: str, window_ms: int = Query(3600000, ge=60000, le=86400000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_metrics(session, sid, window_ms=max(60000, min(int(window_ms), 86400000)))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies")
async def get_session_continuity_autopilot_posture_action_policy_anomalies(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return detect_continuity_autopilot_posture_action_policy_anomalies(session, sid, limit=max(1, min(int(limit), 200)))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/history")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_history(session_id: str, limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_history(session, sid, limit=max(1, min(int(limit), 200)))


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/trend")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_trend(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_trend(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/offenders")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_offenders(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_offenders(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/state")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_state(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_state(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    threshold_pct: float = Query(35.0, ge=1.0, le=100.0),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        threshold_pct=max(1.0, min(float(threshold_pct), 100.0)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/breaches")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_breaches(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    threshold_pct: float = Query(35.0, ge=1.0, le=100.0),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_breaches(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        threshold_pct=max(1.0, min(float(threshold_pct), 100.0)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    threshold_pct: float = Query(35.0, ge=1.0, le=100.0),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        threshold_pct=max(1.0, min(float(threshold_pct), 100.0)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/matrix")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/dry-run")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run(
    session_id: str,
    index: int = Query(1, ge=1, le=10),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run(
        session,
        sid,
        index=max(1, min(int(index), 10)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/apply")
async def apply_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action(
    session_id: str,
    index: int = Query(1, ge=1, le=10),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action(
        session,
        sid,
        index=max(1, min(int(index), 10)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_guidance_action_apply")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_guidance_action_apply")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/apply-batch")
async def apply_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_batch(
    session_id: str,
    limit: int = Query(3, ge=1, le=10),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_batch(
        session,
        sid,
        limit=max(1, min(int(limit), 10)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_guidance_actions_apply_batch")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_guidance_actions_apply_batch")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/history")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history(
    session_id: str,
    limit: int = Query(30, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history(
        session,
        sid,
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/metrics")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies(
    session_id: str,
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return detect_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies(
        session,
        sid,
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/trend")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/state")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/offenders")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/timeline")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 100)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/summary")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/matrix")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(6, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/dry-run")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/apply")
async def apply_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )
    record_journal_entry(
        session,
        sid,
        {
            "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
            "payload": {
                "windowMs": int(window_ms),
                "limit": int(limit),
                "selectedType": str(report.get("selectedType", "")),
                "selectedCommand": str(report.get("selectedCommand", "")),
                "reason": str(report.get("reason", "")),
            },
        },
        {
            "ok": bool(report.get("applied", False)),
            "message": str(report.get("message", "")),
            "policy": report.get("policy", {}),
            "capability": report.get("capability", {}),
            "diff": report.get("diff", zero_diff()),
        },
    )
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_guidance_actions_anomalies_remediation_apply")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_guidance_actions_anomalies_remediation_apply")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/history")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history(
    session_id: str,
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history(
        session,
        sid,
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/metrics")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/state")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/trend")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/offenders")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/summary")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/timeline")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/matrix")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(6, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/dry-run")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run(
    session_id: str,
    index: int = Query(1, ge=1, le=20),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run(
        session,
        sid,
        index=max(1, min(int(index), 20)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 20)),
    )


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply")
async def apply_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action(
    session_id: str,
    index: int = Query(1, ge=1, le=20),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action(
        session,
        sid,
        index=max(1, min(int(index), 20)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 20)),
    )
    record_journal_entry(
        session,
        sid,
        {
            "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
            "payload": {
                "index": int(index),
                "windowMs": int(window_ms),
                "buckets": int(buckets),
                "limit": int(limit),
                "selectedCommand": str(report.get("selectedCommand", "")),
                "selectedType": str(report.get("selectedType", "")),
                "reason": str(report.get("reason", "")),
            },
        },
        {
            "ok": bool(report.get("applied", False)),
            "message": str(report.get("message", "")),
            "policy": report.get("policy", {}),
            "capability": report.get("capability", {}),
            "diff": report.get("diff", zero_diff()),
        },
    )
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_guidance_actions_anomalies_remediation_guidance_action_apply")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_guidance_actions_anomalies_remediation_guidance_action_apply")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/apply-batch")
async def apply_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_batch(
    session_id: str,
    limit: int = Query(3, ge=1, le=20),
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_batch(
        session,
        sid,
        limit=max(1, min(int(limit), 20)),
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )
    record_journal_entry(
        session,
        sid,
        {
            "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
            "payload": {
                "limit": int(limit),
                "windowMs": int(window_ms),
                "buckets": int(buckets),
                "attempted": int(report.get("attempted", 0)),
                "applied": int(report.get("applied", 0)),
            },
        },
        {
            "ok": bool(report.get("applied", 0) > 0),
            "message": f"Applied {int(report.get('applied', 0) or 0)} remediation guidance action(s).",
            "policy": {"allowed": bool(report.get("applied", 0) > 0), "code": "batch_apply", "reason": "batch remediation guidance apply"},
            "capability": {"name": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch", "domain": "system", "risk": "low", "known": True},
            "diff": report.get("diff", zero_diff()),
        },
    )
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_guidance_actions_anomalies_remediation_guidance_actions_apply_batch")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_guidance_actions_anomalies_remediation_guidance_actions_apply_batch")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/history")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history(
    session_id: str,
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history(
        session,
        sid,
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/metrics")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/state")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/trend")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/offenders")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/summary")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(8, ge=1, le=30),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 30)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/timeline")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(20, ge=1, le=200),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 200)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/matrix")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    limit: int = Query(6, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/budget/forecast/guidance/actions/anomalies/remediation/guidance/actions/guidance")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance(
    session_id: str,
    window_ms: int = Query(3600000, ge=60000, le=86400000),
    buckets: int = Query(6, ge=2, le=24),
    limit: int = Query(5, ge=1, le=20),
) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance(
        session,
        sid,
        window_ms=max(60000, min(int(window_ms), 86400000)),
        buckets=max(2, min(int(buckets), 24)),
        limit=max(1, min(int(limit), 20)),
    )


@app.get("/api/session/{session_id}/continuity/autopilot/posture/actions/policy/anomalies/metrics")
async def get_session_continuity_autopilot_posture_action_policy_anomaly_metrics(session_id: str, window_ms: int = Query(3600000, ge=60000, le=86400000)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_continuity_autopilot_posture_action_policy_anomaly_metrics(session, sid, window_ms=max(60000, min(int(window_ms), 86400000)))


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/apply")
async def apply_session_continuity_autopilot_posture_action(session_id: str, index: int = Query(1, ge=1, le=10)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_action(session, sid, index=max(1, int(index)))
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_posture_action_apply")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_posture_action_apply")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
    }


@app.post("/api/session/{session_id}/continuity/autopilot/posture/actions/apply-batch")
async def apply_session_continuity_autopilot_posture_actions_batch(session_id: str, limit: int = Query(3, ge=1, le=10)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_autopilot_posture_actions_batch(session, sid, limit=max(1, min(int(limit), 10)))
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_posture_action_apply_batch")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_posture_action_apply_batch")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
    }


@app.post("/api/session/{session_id}/continuity/autopilot/mode/apply-recommended")
async def apply_session_continuity_autopilot_recommended_mode(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    recommendation = build_continuity_autopilot_mode_recommendation(session, sid)
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    previous = str(state.get("mode", "normal"))
    recommended = str(recommendation.get("recommendedMode", "normal"))
    mode_policy = evaluate_continuity_autopilot_mode_policy(session, sid, recommended)
    changed = previous != recommended and bool(mode_policy.get("allowed", False))
    if bool(mode_policy.get("allowed", False)):
        state["mode"] = recommended
        if changed:
            state["aligned"] = int(state.get("aligned", 0) or 0) + 1
            state["lastAlignAt"] = now_ms()
        state["lastResult"] = f"mode set to {recommended} via recommendation"
    else:
        state["lastResult"] = f"recommended mode blocked ({str(mode_policy.get('code', 'blocked'))})"
    session.continuity_autopilot = state
    append_continuity_autopilot_history(
        session,
        source="api_mode_apply_recommended",
        reason="mode_recommended_apply",
        changed=changed,
        action={"command": f"continuity_autopilot_mode_{recommended}", "priority": "p2", "applied": changed},
    )
    append_continuity_autopilot_posture_snapshot(session, sid, "api_mode_apply_recommended")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_autopilot_apply_recommended_mode")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "changed": bool(changed),
        "previousMode": previous,
        "recommendedMode": recommended,
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "modePolicy": mode_policy,
    }


@app.post("/api/session/{session_id}/continuity/autopilot")
async def set_session_continuity_autopilot(session_id: str, body: ContinuityAutopilotBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    state["enabled"] = bool(body.enabled)
    state["lastResult"] = "enabled" if bool(body.enabled) else "disabled"
    session.continuity_autopilot = state
    append_continuity_autopilot_history(
        session,
        source="api_set",
        reason="enabled" if bool(body.enabled) else "disabled",
        changed=True,
        action={"command": "continuity_autopilot_on" if bool(body.enabled) else "continuity_autopilot_off", "priority": "p2", "applied": True},
    )
    append_continuity_autopilot_posture_snapshot(session, sid, "api_set")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_autopilot_set")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
    }


@app.post("/api/session/{session_id}/continuity/autopilot/config")
async def set_session_continuity_autopilot_config(session_id: str, body: ContinuityAutopilotConfigBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    changed_parts: list[str] = []
    mode_policy: dict[str, Any] | None = None
    if body.cooldownMs is not None:
        cooldown_ms = max(1000, min(int(body.cooldownMs), 10 * 60 * 1000))
        state["cooldownMs"] = int(cooldown_ms)
        changed_parts.append(f"cooldown {cooldown_ms}ms")
    if body.maxAppliesPerHour is not None:
        max_applies = max(0, min(int(body.maxAppliesPerHour), 500))
        state["maxAppliesPerHour"] = int(max_applies)
        changed_parts.append(f"max/h {max_applies}")
    if body.mode is not None:
        mode = str(body.mode or "").strip().lower()
        if mode not in {"safe", "normal", "aggressive"}:
            raise HTTPException(status_code=400, detail="invalid mode")
        mode_policy = evaluate_continuity_autopilot_mode_policy(session, sid, mode)
        if bool(mode_policy.get("allowed", False)):
            state["mode"] = mode
            changed_parts.append(f"mode {mode}")
        else:
            changed_parts.append(f"mode blocked ({str(mode_policy.get('code', 'blocked'))})")
    if body.autoAlignMode is not None:
        state["autoAlignMode"] = bool(body.autoAlignMode)
        changed_parts.append(f"auto_align {'on' if bool(body.autoAlignMode) else 'off'}")
    if not changed_parts:
        state["lastResult"] = "config unchanged"
    else:
        state["lastResult"] = ", ".join(changed_parts)
    session.continuity_autopilot = state
    history_reason = "mode_policy_blocked" if (mode_policy and not bool(mode_policy.get("allowed", False))) else "cooldown_update"
    append_continuity_autopilot_history(
        session,
        source="api_config",
        reason=history_reason,
        changed=True,
        action={"command": f"continuity_autopilot_config_{state['cooldownMs']}_{state['maxAppliesPerHour']}_{state['mode']}", "priority": "p2", "applied": not bool(mode_policy and not bool(mode_policy.get('allowed', False)))},
    )
    append_continuity_autopilot_posture_snapshot(session, sid, "api_config")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_autopilot_config")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "modePolicy": mode_policy,
    }


@app.post("/api/session/{session_id}/continuity/autopilot/reset")
async def reset_session_continuity_autopilot(session_id: str, body: ContinuityAutopilotResetBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    state["lastRunAt"] = 0
    state["lastAppliedAt"] = 0
    state["applied"] = 0
    state["noops"] = 0
    state["lastAction"] = ""
    state["lastResult"] = "stats reset"
    state["appliedTimestamps"] = []
    session.continuity_autopilot = state
    if bool(body.clearHistory):
        session.continuity_autopilot_history = []
    append_continuity_autopilot_history(
        session,
        source="api_reset",
        reason="stats_reset",
        changed=True,
        action={"command": "continuity_autopilot_reset", "priority": "p2", "applied": True},
    )
    append_continuity_autopilot_posture_snapshot(session, sid, "api_reset")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_autopilot_reset")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "historyCount": int(len(session.continuity_autopilot_history)),
    }


@app.post("/api/session/{session_id}/continuity/autopilot/tick")
async def tick_session_continuity_autopilot(session_id: str, force: bool = Query(False)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = run_continuity_autopilot_tick(session, sid, force=bool(force))
    if bool(report.get("changed", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_autopilot_tick")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_autopilot_tick")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
        "autopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
    }


@app.post("/api/session/{session_id}/continuity/next/apply")
async def apply_session_continuity_next(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = apply_continuity_next_action(session, sid)
    if bool(report.get("applied", False)):
        session.revision += 1
        append_continuity_history_snapshot(session, sid, "continuity_next_apply")
        await broadcast_session(session, session_sync_payload(sid, session))
        await persist_sessions_to_disk_safe("continuity_next_apply")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "report": report,
    }


@app.get("/api/session/{session_id}/continuity/alerts")
async def get_session_continuity_alerts(session_id: str, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    handoff = build_handoff_stats_payload(session, sid)
    stats = handoff.get("stats", {}) if isinstance(handoff.get("stats"), dict) else {}
    alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
    items = alerts[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": sid,
        "count": int(len(items)),
        "items": items,
    }


@app.post("/api/session/{session_id}/continuity/alerts/clear")
async def clear_session_continuity_alerts(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    stats = session.handoff.get("stats") if isinstance(session.handoff.get("stats"), dict) else {}
    alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
    cleared = len(alerts)
    stats["alerts"] = []
    session.handoff["stats"] = stats
    append_continuity_history_snapshot(session, sid, "continuity_alerts_clear")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_alerts_clear")
    return {
        "ok": True,
        "sessionId": sid,
        "cleared": int(cleared),
        "revision": int(session.revision),
    }


@app.post("/api/session/{session_id}/continuity/alerts/drill")
async def drill_session_continuity_alert(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = inject_continuity_breach_alert(session, "drill-device")
    append_continuity_history_snapshot(session, sid, "continuity_alerts_drill")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("continuity_alerts_drill")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": int(session.revision),
        "alert": report,
    }


@app.get("/api/session/{session_id}/snapshot/stats")
async def get_session_snapshot_stats(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return {
        "ok": True,
        "sessionId": sid,
        "snapshot": build_snapshot_stats(session),
    }


@app.get("/api/session/{session_id}/journal/verify")
async def get_session_journal_verify(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    report = verify_journal_integrity(session, sid)
    return {"ok": True, "sessionId": sid, **report}


@app.get("/api/session/{session_id}/checkpoints")
async def get_session_checkpoints(session_id: str, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    items = session.checkpoints[-limit:]
    return {
        "ok": True,
        "sessionId": sid,
        "count": len(items),
        "retention": {"maxCount": int(CHECKPOINT_MAX_COUNT), "maxAgeMs": int(CHECKPOINT_MAX_AGE_MS)},
        "items": items,
    }


@app.post("/api/session/{session_id}/checkpoints")
async def create_session_checkpoint(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    checkpoint = create_checkpoint(session, "manual")
    await persist_sessions_to_disk_safe("restore_checkpoint_endpoint")
    return {"ok": True, "sessionId": sid, "checkpoint": checkpoint}


@app.post("/api/session/{session_id}/restore/checkpoint")
async def restore_session_checkpoint(session_id: str, body: CheckpointRestoreBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    checkpoint = find_checkpoint(session, body.checkpointId)
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    apply_checkpoint_to_session(session, checkpoint, replay_tail=bool(body.replayTail))
    session.restore["last"] = {
        "ts": now_ms(),
        "source": "checkpoint_restore",
        "checkpointId": checkpoint.get("id"),
        "replayTail": bool(body.replayTail),
        "journalBase": int(checkpoint.get("journalSize", 0) or 0),
    }
    session.revision += 1
    graph_add_event(session.graph, "restore_checkpoint", {"checkpointId": checkpoint.get("id"), "replayTail": bool(body.replayTail)})
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("handoff_start")
    return {
        "ok": True,
        "sessionId": sid,
        "revision": session.revision,
        "checkpointId": checkpoint.get("id"),
        "replayTail": bool(body.replayTail),
        "counts": graph_counts(session.graph),
    }


@app.post("/api/session/{session_id}/jobs/tick")
async def tick_session_jobs(session_id: str, force: bool = Query(False)) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    ran = await run_due_jobs_for_session(sid, session, force=force)
    return {
        "ok": True,
        "sessionId": sid,
        "ran": ran,
        "jobsActive": count_active_jobs(session.jobs),
        "jobs": session.jobs[-20:],
    }


@app.post("/api/session/{session_id}/handoff/start")
async def handoff_start(session_id: str, body: HandoffStartBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    stats = (session.handoff.get("stats") if isinstance(session.handoff.get("stats"), dict) else {})
    device_id = normalize_device_id(body.deviceId)
    if not device_id:
        raise HTTPException(status_code=400, detail="invalid device id")
    token = str(uuid.uuid4())[:12]
    expires_at = now_ms() + 60_000
    created_at = now_ms()
    session.handoff["pending"] = {
        "token": token,
        "fromDeviceId": device_id,
        "createdAt": created_at,
        "expiresAt": expires_at,
    }
    stats["starts"] = int(stats.get("starts", 0) or 0) + 1
    session.handoff["stats"] = stats
    if not session.handoff.get("activeDeviceId"):
        session.handoff["activeDeviceId"] = device_id
    append_continuity_history_snapshot(session, sid, "handoff_start")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("handoff_claim")
    return {
        "ok": True,
        "sessionId": sid,
        "token": token,
        "expiresAt": expires_at,
        "revision": session.revision,
        "handoff": session.handoff,
    }


@app.post("/api/session/{session_id}/handoff/claim")
async def handoff_claim(session_id: str, body: HandoffClaimBody) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    stats = (session.handoff.get("stats") if isinstance(session.handoff.get("stats"), dict) else {})
    device_id = normalize_device_id(body.deviceId)
    pending = session.handoff.get("pending")
    if not device_id or not isinstance(pending, dict):
        stats["invalid"] = int(stats.get("invalid", 0) or 0) + 1
        session.handoff["stats"] = stats
        raise HTTPException(status_code=400, detail="no pending handoff")
    if str(body.token) != str(pending.get("token")):
        stats["invalid"] = int(stats.get("invalid", 0) or 0) + 1
        session.handoff["stats"] = stats
        raise HTTPException(status_code=400, detail="invalid handoff token")
    if int(pending.get("expiresAt", 0) or 0) < now_ms():
        stats["expired"] = int(stats.get("expired", 0) or 0) + 1
        session.handoff["stats"] = stats
        session.handoff["pending"] = None
        raise HTTPException(status_code=400, detail="handoff token expired")

    claim_ms = max(0, now_ms() - int(pending.get("createdAt", now_ms()) or now_ms()))
    samples = stats.get("samples", [])
    if not isinstance(samples, list):
        samples = []
    samples = [max(0, int(x or 0)) for x in samples[-199:]]
    samples.append(claim_ms)
    starts = int(stats.get("starts", 0) or 0)
    claims = int(stats.get("claims", 0) or 0) + 1
    stats["claims"] = claims
    stats["samples"] = samples[-200:]
    stats["lastMs"] = int(claim_ms)
    stats["maxMs"] = max(int(stats.get("maxMs", 0) or 0), int(claim_ms))
    stats["avgMs"] = int(round(sum(samples) / len(samples))) if samples else 0
    stats["p95Ms"] = _percentile_int(sorted(samples), 95)
    budget_ms = max(1, int(stats.get("budgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS))
    stats["budgetMs"] = budget_ms
    if int(claim_ms) > int(budget_ms):
        stats["breaches"] = int(stats.get("breaches", 0) or 0) + 1
        stats["lastBreachAt"] = now_ms()
        alerts = stats.get("alerts", [])
        if not isinstance(alerts, list):
            alerts = []
        alerts.append(
            {
                "ts": now_ms(),
                "claimMs": int(claim_ms),
                "budgetMs": int(budget_ms),
                "deviceId": str(device_id),
            }
        )
        stats["alerts"] = alerts[-50:]
    stats["starts"] = starts
    session.handoff["stats"] = stats

    session.handoff["activeDeviceId"] = device_id
    session.handoff["lastClaimAt"] = now_ms()
    session.handoff["pending"] = None
    append_continuity_history_snapshot(session, sid, "handoff_claim")
    session.revision += 1
    await broadcast_session(session, session_sync_payload(sid, session))
    await persist_sessions_to_disk_safe("turn")
    return {
        "ok": True,
        "sessionId": sid,
        "activeDeviceId": device_id,
        "claimedAt": session.handoff["lastClaimAt"],
        "revision": session.revision,
        "handoff": session.handoff,
    }


@app.get("/api/session/{session_id}/handoff/stats")
async def get_session_handoff_stats(session_id: str) -> dict[str, Any]:
    sid = normalize_session_id(session_id)
    if not sid:
        raise HTTPException(status_code=400, detail="invalid session")
    session = ensure_session(sid)
    return build_handoff_stats_payload(session, sid)


@app.get("/api/stream")
async def stream_session(sessionId: str = Query(...)):
    sid = normalize_session_id(sessionId)
    if not sid:
        raise HTTPException(status_code=400, detail="sessionId required")

    session = ensure_session(sid)
    queue: asyncio.Queue = asyncio.Queue()
    session.subscribers.add(queue)

    initial = {
        "type": "session_sync",
        "sessionId": sid,
        "revision": session.revision,
        "memory": session.memory,
        "presence": session.presence,
        "handoff": session.handoff,
        "lastTurn": session.last_turn,
    }

    async def event_gen():
        try:
            yield f"data: {to_json(initial)}\n\n"
            while True:
                payload = await queue.get()
                yield f"data: {to_json(payload)}\n\n"
        finally:
            session.subscribers.discard(queue)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.websocket("/ws")
async def ws_session(websocket: WebSocket):
    await websocket.accept()
    sid = normalize_session_id(websocket.query_params.get("sessionId", ""))
    if not sid:
        await websocket.close(code=1008)
        return

    session = ensure_session(sid)
    session.sockets.add(websocket)

    await websocket.send_json(
        {
            "type": "session_sync",
            "sessionId": sid,
            "revision": session.revision,
            "memory": session.memory,
            "presence": session.presence,
            "handoff": session.handoff,
            "lastTurn": session.last_turn,
        }
    )

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        session.sockets.discard(websocket)


@app.post("/api/turn")
async def turn(body: TurnBody) -> dict[str, Any]:
    started_ms = now_ms()
    intent = (body.intent or "").strip()
    if not intent:
        raise HTTPException(status_code=400, detail="intent is required")

    sid = normalize_session_id(body.sessionId or "") or generate_session_id()
    session = ensure_session(sid)
    idem_key = normalize_idempotency_key(body.idempotencyKey)
    if idem_key:
        reused = find_idempotent_response(session, idem_key, intent)
        if reused is not None:
            reused["idempotency"] = {"reused": True, "key": idem_key}
            return reused

    envelope = compile_intent_envelope(intent)
    clarification = envelope.get("clarification") or {}
    clarification_required = bool(clarification.get("required", False))
    parse_done_ms = now_ms()
    has_writes = bool(envelope["stateIntent"]["writeOperations"])
    merge_info = {"rebased": False, "fromRevision": None, "toRevision": None}
    if has_writes and body.baseRevision is not None and int(body.baseRevision) != int(session.revision):
        op_types = {str(op.get("type", "")).strip() for op in envelope["stateIntent"]["writeOperations"]}
        merge_allowed = (
            str(body.onConflict or "").strip().lower() == "rebase_if_commutative"
            and bool(op_types)
            and op_types.issubset(COMMUTATIVE_MERGE_OPS)
        )
        if merge_allowed:
            merge_info = {"rebased": True, "fromRevision": int(body.baseRevision), "toRevision": int(session.revision)}
        else:
            record_journal_entry(
                session,
                sid,
                {"type": "revision_conflict"},
                {
                    "ok": False,
                    "message": "Write rejected due to stale revision. Refresh and retry.",
                    "policy": {"allowed": False, "reason": "stale base revision", "code": "revision_conflict"},
                    "capability": {"domain": "system", "risk": "low"},
                    "diff": zero_diff(),
                },
            )
            await persist_sessions_to_disk_safe("revision_conflict")
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "revision_conflict",
                    "message": "Write rejected due to stale revision. Refresh and retry.",
                    "serverRevision": session.revision,
                    "sessionId": sid,
                },
            )

    if clarification_required:
        clarification_result = {
            "ok": False,
            "message": str(clarification.get("question") or "Intent needs clarification."),
            "policy": {"allowed": False, "reason": "intent needs clarification", "code": "clarification_required"},
            "capability": {"domain": "system", "risk": "low"},
            "diff": zero_diff(),
            "previewLines": list(clarification.get("examples") or []),
        }
        record_journal_entry(session, sid, {"type": "clarification_needed"}, clarification_result)
        execution = {
            "ok": False,
            "message": clarification_result["message"],
            "toolResults": [{"op": "clarification_needed", **clarification_result}],
            "journalTail": session.journal[-20:],
            "needsClarification": True,
        }
    else:
        execution = await execute_operations(session, sid, envelope["stateIntent"]["writeOperations"])
    execute_done_ms = now_ms()
    planner_memory_fingerprint = stable_memory_fingerprint(session.memory)
    route = planner_route(envelope, execution, session.graph)
    if clarification_required:
        route = {
            "target": "deterministic",
            "reason": "clarification_gate",
            "model": None,
            "intentClass": str(envelope.get("intentClass", "unknown")),
            "confidence": float(envelope.get("confidence", 0.0) or 0.0),
        }
    elif is_slo_throttled(session):
        route = {
            "target": "deterministic",
            "reason": "slo_throttle",
            "model": None,
            "intentClass": str(envelope.get("intentClass", "unknown")),
            "confidence": float(envelope.get("confidence", 0.0) or 0.0),
        }

    local_plan = build_local_plan(envelope, session.graph, execution, session.jobs)
    plan = local_plan
    planner = "local"

    if route["target"].startswith("ollama") and route.get("model"):
        try:
            remote = await generate_plan_with_ollama(intent, envelope, session.graph, execution, local_plan, route["model"])
            plan = normalize_plan(remote)
            planner = route["target"]
        except Exception:
            plan = normalize_plan(local_plan)
            planner = "local-fallback"
    else:
        plan = normalize_plan(local_plan)
    plan_done_ms = now_ms()

    perf_trace = {
        "parseMs": max(0, int(parse_done_ms - started_ms)),
        "executeMs": max(0, int(execute_done_ms - parse_done_ms)),
        "planMs": max(0, int(plan_done_ms - execute_done_ms)),
        "totalMs": max(0, int(plan_done_ms - started_ms)),
        "budgetMs": int(TURN_LATENCY_BUDGET_MS),
    }
    perf_trace["withinBudget"] = bool(perf_trace["totalMs"] <= perf_trace["budgetMs"])
    slo_trace = update_slo_state(session, perf_trace)
    kernel_trace = build_kernel_trace(route, execution, session, perf_trace, slo_trace)

    assert_memory_unchanged(
        planner_memory_fingerprint,
        session.memory,
        context="planner/runtime stage mutated memory outside kernel path",
    )

    session.revision += 1
    if has_writes and session.revision % 12 == 0:
        create_checkpoint(session, "auto_interval")
    session.last_turn = {
        "intent": intent,
        "envelope": envelope,
        "execution": execution,
        "kernelTrace": kernel_trace,
        "plan": plan,
        "planner": planner,
        "route": route,
        "merge": merge_info,
        "timestamp": int(datetime.utcnow().timestamp() * 1000),
    }
    session.turn_history.append(summarize_turn_history_item(session.last_turn))
    if len(session.turn_history) > int(TURN_HISTORY_MAX_ENTRIES):
        session.turn_history[:] = session.turn_history[-int(TURN_HISTORY_MAX_ENTRIES) :]
    append_continuity_history_snapshot(session, sid, "turn")

    payload = session_sync_payload(sid, session)
    await broadcast_session(session, payload)
    await persist_sessions_to_disk_safe("scheduler_tick")

    response = {
        "envelope": envelope,
        "execution": execution,
        "kernelTrace": kernel_trace,
        "plan": plan,
        "memory": session.memory,
        "presence": session.presence,
        "handoff": session.handoff,
        "planner": planner,
        "route": route,
        "merge": merge_info,
        "sessionId": sid,
        "revision": session.revision,
        "idempotency": {"reused": False, "key": idem_key or None},
    }
    if idem_key:
        store_idempotent_response(session, idem_key, intent, response, session.revision)
    return response


async def broadcast_session(session: SessionState, payload: dict[str, Any]) -> None:
    for queue in list(session.subscribers):
        await queue.put(payload)

    for ws in list(session.sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            session.sockets.discard(ws)


async def run_scheduler_loop() -> None:
    while True:
        await asyncio.sleep(1.0)
        for sid, session in list(SESSIONS.items()):
            try:
                await run_due_jobs_for_session(sid, session)
            except Exception:
                # Scheduler failures should not terminate runtime loop.
                continue


async def run_due_jobs_for_session(session_id: str, session: SessionState, force: bool = False) -> bool:
    now = now_ms()
    ran = False
    autopilot_report = run_continuity_autopilot_tick(session, session_id, force=force)
    if bool(autopilot_report.get("ran", False)):
        graph_add_event(
            session.graph,
            "continuity_autopilot_tick",
            {
                "reason": str(autopilot_report.get("reason", "")),
                "changed": bool(autopilot_report.get("changed", False)),
            },
        )
    if bool(autopilot_report.get("changed", False)):
        ran = True
    prune_report = prune_presence_entries(session, max_age_ms=120000, remove_all=False)
    if int(prune_report.get("removed", 0) or 0) > 0:
        ran = True
        graph_add_event(
            session.graph,
            "presence_prune_auto",
            {"removed": int(prune_report.get("removed", 0) or 0), "remaining": int(prune_report.get("remaining", 0) or 0)},
        )
    for job in session.jobs:
        if not job.get("active", True):
            continue
        due_at = int(job.get("nextRunAt", 0) or 0)
        if (not force) and due_at > now:
            continue
        try:
            if force:
                job["lastRunKey"] = None
            execute_scheduled_job(session, session_id, job)
            job["failureCount"] = 0
            job["lastError"] = ""
            interval_ms = int(job.get("intervalMs", 0) or 0)
            if interval_ms <= 0:
                job["active"] = False
            else:
                job["nextRunAt"] = now + interval_ms
            job["lastRunAt"] = now
        except Exception as exc:
            failure_count = int(job.get("failureCount", 0) or 0) + 1
            job["failureCount"] = failure_count
            job["lastError"] = str(exc)[:240]
            job["lastResult"] = f"error: {str(exc)[:80]}"
            if failure_count >= 3:
                enqueue_dead_letter(session, job, str(exc), failure_count)
                session.jobs[:] = [x for x in session.jobs if x.get("id") != job.get("id")]
            else:
                interval_ms = int(job.get("intervalMs", 60_000) or 60_000)
                backoff = min(interval_ms, 5 * 60_000) * failure_count
                job["nextRunAt"] = now + backoff
            record_journal_entry(
                session,
                session_id,
                {"type": "job_tick"},
                {
                    "ok": False,
                    "message": f"job {job.get('id')} failed: {str(exc)[:120]}",
                    "policy": {"allowed": True, "reason": "scheduled", "code": "scheduled_error"},
                    "capability": {"domain": "system", "risk": "low"},
                    "diff": zero_diff(),
                },
            )
        ran = True

    if not ran:
        return False

    session.memory = graph_to_memory(session.graph)
    session.revision += 1
    payload = session_sync_payload(session_id, session)
    await broadcast_session(session, payload)
    await persist_sessions_to_disk_safe("scheduler_tick")
    return True


def session_sync_payload(session_id: str, session: SessionState) -> dict[str, Any]:
    return {
        "type": "session_sync",
        "sessionId": session_id,
        "revision": session.revision,
        "memory": session.memory,
        "presence": session.presence,
        "handoff": session.handoff,
        "continuityAutopilot": ensure_continuity_autopilot_state(session.continuity_autopilot),
        "lastTurn": session.last_turn,
    }


def summarize_turn_history_item(turn: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(turn, dict):
        return {}
    route = turn.get("route") or {}
    execution = turn.get("execution") or {}
    kernel_trace = turn.get("kernelTrace") or {}
    perf = (kernel_trace.get("runtime") or {}).get("performance") or {}
    return {
        "timestamp": int(turn.get("timestamp", 0) or 0),
        "intent": str(turn.get("intent", ""))[:240],
        "ok": bool(execution.get("ok", False)),
        "route": {
            "target": str(route.get("target", "deterministic")),
            "reason": str(route.get("reason", "default")),
            "intentClass": str(route.get("intentClass", "unknown")),
            "confidence": float(route.get("confidence", 0.0) or 0.0),
        },
        "execution": {
            "message": str(execution.get("message", ""))[:240],
            "ops": [str(item.get("op", "")) for item in (execution.get("toolResults") or [])][:8],
        },
        "performance": {
            "totalMs": int(perf.get("totalMs", 0) or 0),
            "withinBudget": bool(perf.get("withinBudget", True)),
        },
    }


def filter_turn_history_entries(
    entries: list[dict[str, Any]],
    ok: bool | None = None,
    intent_class: str | None = None,
    route_reason: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    ic = str(intent_class or "").strip().lower()
    rr = str(route_reason or "").strip().lower()
    out: list[dict[str, Any]] = []
    for item in reversed(entries):
        if ok is not None and bool(item.get("ok")) != bool(ok):
            continue
        route = item.get("route") or {}
        if ic and str(route.get("intentClass", "")).strip().lower() != ic:
            continue
        if rr and str(route.get("reason", "")).strip().lower() != rr:
            continue
        out.append(item)
        if len(out) >= max(1, min(int(limit), 500)):
            break
    out.reverse()
    return out


def summarize_turn_history(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_class: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    ok_count = 0
    denied_count = 0
    total_ms = 0
    within_budget = 0
    for item in items:
        route = item.get("route") or {}
        perf = item.get("performance") or {}
        klass = str(route.get("intentClass", "unknown"))
        reason = str(route.get("reason", "unknown"))
        by_class[klass] = by_class.get(klass, 0) + 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        if bool(item.get("ok", False)):
            ok_count += 1
        else:
            denied_count += 1
        total_ms += int(perf.get("totalMs", 0) or 0)
        if bool(perf.get("withinBudget", False)):
            within_budget += 1
    count = len(items)
    avg_ms = int(round(total_ms / count)) if count else 0
    return {
        "count": count,
        "ok": ok_count,
        "denied": denied_count,
        "avgTotalMs": avg_ms,
        "withinBudgetCount": within_budget,
        "byIntentClass": by_class,
        "byRouteReason": by_reason,
    }


def get_runtime_health_payload(session: SessionState, session_id: str) -> dict[str, Any]:
    perf = (((session.last_turn or {}).get("kernelTrace") or {}).get("runtime") or {}).get("performance", {})
    presence = build_presence_payload(session, session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "revision": int(session.revision),
        "presence": {
            "activeCount": int(presence.get("activeCount", 0) or 0),
            "count": int(presence.get("count", 0) or 0),
        },
        "jobs": {
            "total": int(len(session.jobs)),
            "active": int(count_active_jobs(session.jobs)),
            "nextRunAt": next_due_job_time(session.jobs),
        },
        "deadLetters": {"count": int(len(session.dead_letters))},
        "faults": copy.deepcopy(session.faults),
        "slo": copy.deepcopy(session.slo),
        "performance": {
            "totalMs": int(perf.get("totalMs", 0) or 0),
            "budgetMs": int(perf.get("budgetMs", TURN_LATENCY_BUDGET_MS) or TURN_LATENCY_BUDGET_MS),
            "withinBudget": bool(perf.get("withinBudget", True)),
        },
    }


def build_snapshot_stats(session: SessionState) -> dict[str, Any]:
    counts = graph_counts(session.graph)
    return {
        "revision": int(session.revision),
        "entities": int(counts.get("entities", 0) or 0),
        "relations": int(counts.get("relations", 0) or 0),
        "events": int(counts.get("events", 0) or 0),
        "jobs": int(len(session.jobs)),
        "deadLetters": int(len(session.dead_letters)),
        "journal": int(len(session.journal)),
        "turnHistory": int(len(session.turn_history)),
        "checkpoints": int(len(session.checkpoints)),
        "undoDepth": int(len(session.undo_stack)),
    }


def build_runtime_self_check_report(session: SessionState, session_id: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    graph_violations = validate_graph_contract(session.graph)
    checks.append(
        {
            "name": "graph_contract",
            "ok": not bool(graph_violations),
            "detail": "graph contract valid" if not graph_violations else f"violations={len(graph_violations)}",
        }
    )

    persist = session.faults.get("persist", {}) or {}
    degraded = bool(persist.get("degraded", False))
    checks.append(
        {
            "name": "persist_state",
            "ok": not degraded,
            "detail": "persist healthy" if not degraded else f"degraded pending={int(persist.get('pendingWrites', 0) or 0)}",
        }
    )

    orphan_watch = 0
    for job in session.jobs:
        if str(job.get("kind", "")) != "watch_task":
            continue
        if not find_task_for_job(session.graph, job):
            orphan_watch += 1
    checks.append(
        {
            "name": "scheduler_watch_targets",
            "ok": orphan_watch == 0,
            "detail": f"orphan_watch_jobs={orphan_watch}",
        }
    )

    dead_letters_count = len(session.dead_letters)
    checks.append(
        {
            "name": "dead_letter_backlog",
            "ok": dead_letters_count < 20,
            "detail": f"dead_letters={dead_letters_count}",
        }
    )

    has_turn_trace = len(session.turn_history) > 0
    checks.append(
        {
            "name": "trace_history",
            "ok": has_turn_trace,
            "detail": "trace history present" if has_turn_trace else "no turn history yet",
        }
    )

    overall_ok = all(bool(item.get("ok", False)) for item in checks)
    return {
        "ok": True,
        "sessionId": session_id,
        "overallOk": overall_ok,
        "checks": checks,
        "timestamp": now_ms(),
    }


def _percentile_int(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    p = max(0.0, min(float(percentile), 100.0))
    if len(values) == 1:
        return int(values[0])
    idx = int(round((p / 100.0) * (len(values) - 1)))
    idx = max(0, min(idx, len(values) - 1))
    return int(values[idx])


def build_runtime_profile_payload(session: SessionState, session_id: str, limit: int = 200) -> dict[str, Any]:
    bounded_limit = max(1, min(int(limit or 200), 1000))
    picked = session.turn_history[-bounded_limit:]
    totals = sorted([int((item.get("performance") or {}).get("totalMs", 0) or 0) for item in picked])
    count = len(picked)
    within_budget = int(sum(1 for item in picked if bool((item.get("performance") or {}).get("withinBudget", False))))
    denied = int(sum(1 for item in picked if not bool(item.get("ok", False))))
    avg_ms = int(round(sum(totals) / count)) if count else 0
    return {
        "ok": True,
        "sessionId": session_id,
        "sample": {
            "count": count,
            "limit": bounded_limit,
        },
        "latencyMs": {
            "avg": avg_ms,
            "p50": _percentile_int(totals, 50),
            "p95": _percentile_int(totals, 95),
            "max": int(totals[-1]) if totals else 0,
        },
        "outcomes": {
            "ok": int(count - denied),
            "denied": denied,
            "withinBudget": within_budget,
            "withinBudgetPct": float(round((within_budget / count) * 100.0, 2)) if count else 0.0,
        },
    }


def build_handoff_stats_payload(session: SessionState, session_id: str) -> dict[str, Any]:
    handoff = ensure_handoff_state(session.handoff)
    session.handoff = handoff
    stats = handoff.get("stats", {}) if isinstance(handoff.get("stats"), dict) else {}
    starts = int(stats.get("starts", 0) or 0)
    claims = int(stats.get("claims", 0) or 0)
    expired = int(stats.get("expired", 0) or 0)
    invalid = int(stats.get("invalid", 0) or 0)
    breaches = int(stats.get("breaches", 0) or 0)
    budget_ms = int(stats.get("budgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)
    avg_ms = int(stats.get("avgMs", 0) or 0)
    last_ms = int(stats.get("lastMs", 0) or 0)
    p95_ms = int(stats.get("p95Ms", 0) or 0)
    max_ms = int(stats.get("maxMs", 0) or 0)
    last_breach_at = int(stats.get("lastBreachAt", 0) or 0)
    alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
    success_rate = float(round((claims / starts) * 100.0, 2)) if starts else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "activeDeviceId": handoff.get("activeDeviceId"),
        "pending": handoff.get("pending"),
        "lastClaimAt": handoff.get("lastClaimAt"),
        "stats": {
            "starts": starts,
            "claims": claims,
            "expired": expired,
            "invalid": invalid,
            "breaches": breaches,
            "lastBreachAt": last_breach_at,
            "successRatePct": success_rate,
            "latencyMs": {
                "budget": budget_ms,
                "avg": avg_ms,
                "last": last_ms,
                "p95": p95_ms,
                "max": max_ms,
            },
            "alerts": alerts[-10:],
        },
    }


def build_presence_payload(session: SessionState, session_id: str, timeout_ms: int = 120000) -> dict[str, Any]:
    presence = ensure_presence_state(session.presence)
    session.presence = presence
    now = now_ms()
    active_items: list[dict[str, Any]] = []
    timeout = max(1000, min(int(timeout_ms or 120000), 1800000))
    for device_id, item in sorted((presence.get("devices") or {}).items()):
        if not isinstance(item, dict):
            continue
        last_seen = int(item.get("lastSeenAt", 0) or 0)
        age = max(0, now - last_seen) if last_seen else 10**12
        active = age <= timeout
        active_items.append(
            {
                "deviceId": device_id,
                "label": str(item.get("label", "") or ""),
                "platform": str(item.get("platform", "") or ""),
                "lastSeenAt": last_seen,
                "ageMs": age,
                "active": active,
            }
        )
    active_count = sum(1 for x in active_items if bool(x.get("active")))
    stale_count = len(active_items) - int(active_count)
    stats = presence.get("stats") if isinstance(presence.get("stats"), dict) else {}
    return {
        "ok": True,
        "sessionId": session_id,
        "updatedAt": int(presence.get("updatedAt", 0) or 0),
        "timeoutMs": timeout,
        "activeCount": int(active_count),
        "staleCount": int(stale_count),
        "count": len(active_items),
        "stats": {
            "heartbeatWrites": int(stats.get("heartbeatWrites", 0) or 0),
            "heartbeatCoalesced": int(stats.get("heartbeatCoalesced", 0) or 0),
            "prunedTotal": int(stats.get("prunedTotal", 0) or 0),
            "lastPruneAt": int(stats.get("lastPruneAt", 0) or 0),
            "lastPruneRemoved": int(stats.get("lastPruneRemoved", 0) or 0),
        },
        "items": active_items,
    }


def prune_presence_entries(session: SessionState, max_age_ms: int = 120000, remove_all: bool = False) -> dict[str, Any]:
    presence = ensure_presence_state(session.presence)
    stats = presence.get("stats") if isinstance(presence.get("stats"), dict) else default_presence_state()["stats"]
    devices = presence.get("devices") if isinstance(presence.get("devices"), dict) else {}
    before = len(devices)
    if before <= 0:
        stats["lastPruneAt"] = now_ms()
        stats["lastPruneRemoved"] = 0
        presence["stats"] = stats
        session.presence = presence
        return {"removed": 0, "remaining": 0}
    if bool(remove_all):
        devices.clear()
        presence["updatedAt"] = now_ms()
        stats["prunedTotal"] = int(stats.get("prunedTotal", 0) or 0) + int(before)
        stats["lastPruneAt"] = now_ms()
        stats["lastPruneRemoved"] = int(before)
        presence["stats"] = stats
        session.presence = presence
        return {"removed": before, "remaining": 0}

    now = now_ms()
    threshold = max(1000, min(int(max_age_ms or 120000), 1800000))
    kept: dict[str, Any] = {}
    for did, item in devices.items():
        if not isinstance(item, dict):
            continue
        last_seen = int(item.get("lastSeenAt", 0) or 0)
        age = max(0, now - last_seen) if last_seen else 10**12
        if age <= threshold:
            kept[did] = item
    presence["devices"] = kept
    presence["updatedAt"] = now_ms()
    removed = max(0, before - len(kept))
    stats["prunedTotal"] = int(stats.get("prunedTotal", 0) or 0) + int(removed)
    stats["lastPruneAt"] = now_ms()
    stats["lastPruneRemoved"] = int(removed)
    presence["stats"] = stats
    session.presence = presence
    return {"removed": removed, "remaining": len(kept)}


def inject_continuity_breach_alert(session: SessionState, device_id: str, claim_ms: int | None = None) -> dict[str, Any]:
    handoff = ensure_handoff_state(session.handoff)
    session.handoff = handoff
    stats = handoff.get("stats") if isinstance(handoff.get("stats"), dict) else {}
    budget_ms = max(1, int(stats.get("budgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS))
    simulated_claim_ms = int(claim_ms if claim_ms is not None else (budget_ms + 50))
    alert = {
        "ts": now_ms(),
        "claimMs": int(simulated_claim_ms),
        "budgetMs": int(budget_ms),
        "deviceId": str(normalize_device_id(device_id) or "drill-device"),
    }
    alerts = stats.get("alerts", [])
    if not isinstance(alerts, list):
        alerts = []
    alerts.append(alert)
    stats["alerts"] = alerts[-50:]
    stats["breaches"] = int(stats.get("breaches", 0) or 0) + 1
    stats["lastBreachAt"] = int(alert["ts"])
    stats["lastMs"] = int(simulated_claim_ms)
    stats["maxMs"] = max(int(stats.get("maxMs", 0) or 0), int(simulated_claim_ms))
    handoff["stats"] = stats
    session.handoff = handoff
    return alert


def build_continuity_payload(session: SessionState, session_id: str) -> dict[str, Any]:
    presence = build_presence_payload(session, session_id)
    handoff = build_handoff_stats_payload(session, session_id)
    autopilot = ensure_continuity_autopilot_state(session.continuity_autopilot)
    idem_entries = session.idempotency.get("entries", []) if isinstance(session.idempotency.get("entries"), list) else []
    presence_stats = (session.presence.get("stats") if isinstance(session.presence.get("stats"), dict) else {})
    stats = handoff.get("stats", {}) if isinstance(handoff.get("stats"), dict) else {}
    summary = {
        "activeDevices": int(presence.get("activeCount", 0) or 0),
        "staleDevices": int(presence.get("staleCount", 0) or 0),
        "presenceTotal": int(presence.get("count", 0) or 0),
        "presencePrunedTotal": int(presence_stats.get("prunedTotal", 0) or 0),
        "presenceLastPruneAt": int(presence_stats.get("lastPruneAt", 0) or 0),
        "presenceHeartbeatWrites": int(presence_stats.get("heartbeatWrites", 0) or 0),
        "presenceHeartbeatCoalesced": int(presence_stats.get("heartbeatCoalesced", 0) or 0),
        "handoffSuccessRatePct": float(stats.get("successRatePct", 0.0) or 0.0),
        "handoffBudgetMs": int(((stats.get("latencyMs") or {}).get("budget", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)),
        "handoffP95Ms": int(((stats.get("latencyMs") or {}).get("p95", 0) or 0)),
        "handoffBreaches": int(stats.get("breaches", 0) or 0),
        "idempotencyEntries": int(len(idem_entries)),
        "autopilotEnabled": bool(autopilot.get("enabled", False)),
        "autopilotApplied": int(autopilot.get("applied", 0) or 0),
    }
    health = evaluate_continuity_health(summary)
    return {
        "ok": True,
        "sessionId": session_id,
        "revision": int(session.revision),
        "summary": summary,
        "health": health,
        "presence": presence,
        "handoff": handoff,
        "autopilot": autopilot,
        "idempotency": {
            "entries": int(len(idem_entries)),
            "maxEntries": int(TURN_IDEMPOTENCY_MAX_ENTRIES),
            "lastKey": str((idem_entries[-1] if idem_entries else {}).get("key", "")),
            "lastIntent": str((idem_entries[-1] if idem_entries else {}).get("intent", "")),
        },
    }


def evaluate_continuity_health(summary: dict[str, Any]) -> dict[str, Any]:
    active = int(summary.get("activeDevices", 0) or 0)
    total = int(summary.get("presenceTotal", 0) or 0)
    stale = int(summary.get("staleDevices", 0) or 0)
    breaches = int(summary.get("handoffBreaches", 0) or 0)
    stale_ratio = (float(stale) / float(total)) if total > 0 else 0.0
    score = 100
    score -= int(round(stale_ratio * 40.0))
    score -= int(min(40, breaches * 5))
    reasons: list[str] = []
    status = "healthy"
    if total > 0 and active <= 0:
        status = "critical"
        reasons.append("no_active_devices")
    if stale_ratio >= 0.5:
        status = "degraded" if status != "critical" else status
        reasons.append("stale_ratio_high")
    if breaches >= 5:
        status = "critical"
        reasons.append("handoff_breach_streak_high")
    elif breaches > 0:
        status = "degraded" if status != "critical" else status
        reasons.append("handoff_breaches_present")
    if not reasons:
        reasons.append("steady")
    return {
        "status": status,
        "score": max(0, min(100, int(score))),
        "reasons": reasons[:6],
    }


def append_continuity_history_snapshot(session: SessionState, session_id: str, source: str) -> None:
    report = build_continuity_payload(session, session_id)
    summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
    health = report.get("health", {}) if isinstance(report.get("health"), dict) else {}
    session.continuity_history.append(
        {
            "ts": now_ms(),
            "source": str(source or "unknown")[:40],
            "status": str(health.get("status", "unknown")),
            "score": int(health.get("score", 0) or 0),
            "reasons": list(health.get("reasons", []))[:6] if isinstance(health.get("reasons"), list) else [],
            "activeDevices": int(summary.get("activeDevices", 0) or 0),
            "presenceTotal": int(summary.get("presenceTotal", 0) or 0),
            "staleDevices": int(summary.get("staleDevices", 0) or 0),
            "handoffBreaches": int(summary.get("handoffBreaches", 0) or 0),
            "handoffP95Ms": int(summary.get("handoffP95Ms", 0) or 0),
            "handoffBudgetMs": int(summary.get("handoffBudgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS),
        }
    )
    if len(session.continuity_history) > int(CONTINUITY_HISTORY_MAX_ENTRIES):
        session.continuity_history[:] = session.continuity_history[-int(CONTINUITY_HISTORY_MAX_ENTRIES) :]


def detect_continuity_anomalies(history: list[dict[str, Any]], limit: int = 20) -> dict[str, Any]:
    window = max(2, min(int(CONTINUITY_ANOMALY_WINDOW), max(2, len(history))))
    recent = list(history[-window:])
    anomalies: list[dict[str, Any]] = []
    severity_rank = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    top_severity = "none"

    for index in range(1, len(recent)):
        prev = recent[index - 1] if isinstance(recent[index - 1], dict) else {}
        curr = recent[index] if isinstance(recent[index], dict) else {}
        prev_status = str(prev.get("status", "unknown"))
        curr_status = str(curr.get("status", "unknown"))
        prev_score = int(prev.get("score", 0) or 0)
        curr_score = int(curr.get("score", 0) or 0)
        prev_breaches = int(prev.get("handoffBreaches", 0) or 0)
        curr_breaches = int(curr.get("handoffBreaches", 0) or 0)
        prev_active = int(prev.get("activeDevices", 0) or 0)
        curr_active = int(curr.get("activeDevices", 0) or 0)
        curr_total = int(curr.get("presenceTotal", 0) or 0)
        score_drop = prev_score - curr_score

        if curr_status == "critical" and prev_status != "critical":
            anomalies.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "status_critical",
                    "severity": "critical",
                    "detail": f"{prev_status}->{curr_status}",
                }
            )
        elif curr_status == "degraded" and prev_status == "healthy":
            anomalies.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "status_degraded",
                    "severity": "high",
                    "detail": f"{prev_status}->{curr_status}",
                }
            )

        if score_drop >= int(CONTINUITY_ANOMALY_SCORE_DROP):
            severity = "high" if score_drop >= int(CONTINUITY_ANOMALY_SCORE_DROP) * 2 else "medium"
            anomalies.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "score_drop",
                    "severity": severity,
                    "detail": f"{prev_score}->{curr_score} ({score_drop})",
                }
            )

        if curr_breaches > prev_breaches:
            delta = curr_breaches - prev_breaches
            anomalies.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "breach_spike",
                    "severity": "high" if delta > 1 else "medium",
                    "detail": f"+{delta} ({curr_breaches})",
                }
            )

        if prev_active > 0 and curr_active == 0 and curr_total > 0:
            anomalies.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "active_devices_dropped",
                    "severity": "critical",
                    "detail": f"{prev_active}->0/{curr_total}",
                }
            )

    for item in anomalies:
        level = str(item.get("severity", "low"))
        if severity_rank.get(level, 0) > severity_rank.get(top_severity, 0):
            top_severity = level

    tail = anomalies[-max(1, min(int(limit), 500)) :]
    return {
        "window": int(len(recent)),
        "items": tail,
        "summary": {
            "count": int(len(tail)),
            "totalDetected": int(len(anomalies)),
            "topSeverity": top_severity,
        },
    }


def build_continuity_incidents(session: SessionState, session_id: str, limit: int = 20) -> dict[str, Any]:
    anomalies = detect_continuity_anomalies(session.continuity_history, limit=max(1, min(int(limit), 500)))
    handoff = build_handoff_stats_payload(session, session_id)
    stats = handoff.get("stats", {}) if isinstance(handoff.get("stats"), dict) else {}
    alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
    incidents: list[dict[str, Any]] = []
    severity_rank = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    top_severity = "none"

    for item in anomalies.get("items", []):
        if not isinstance(item, dict):
            continue
        incidents.append(
            {
                "ts": int(item.get("ts", now_ms()) or now_ms()),
                "category": "anomaly",
                "type": str(item.get("type", "anomaly")),
                "severity": str(item.get("severity", "medium")),
                "source": str(item.get("source", "event")),
                "detail": str(item.get("detail", "")),
            }
        )

    for item in alerts[-max(1, min(int(limit), 500)) :]:
        if not isinstance(item, dict):
            continue
        claim_ms = int(item.get("claimMs", 0) or 0)
        budget_ms = int(item.get("budgetMs", HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)
        incidents.append(
            {
                "ts": int(item.get("ts", now_ms()) or now_ms()),
                "category": "alert",
                "type": "handoff_budget_breach",
                "severity": "high",
                "source": str(item.get("deviceId", "-")),
                "detail": f"{claim_ms}ms>{budget_ms}ms",
            }
        )

    incidents.sort(key=lambda it: int(it.get("ts", 0) or 0), reverse=True)
    tail = incidents[: max(1, min(int(limit), 500))]
    for item in tail:
        level = str(item.get("severity", "low"))
        if severity_rank.get(level, 0) > severity_rank.get(top_severity, 0):
            top_severity = level
    return {
        "items": tail,
        "summary": {
            "count": int(len(tail)),
            "topSeverity": top_severity,
            "anomalies": int(len([item for item in tail if str(item.get("category", "")) == "anomaly"])),
            "alerts": int(len([item for item in tail if str(item.get("category", "")) == "alert"])),
        },
    }


def build_continuity_next_actions(session: SessionState, session_id: str, limit: int = 5) -> dict[str, Any]:
    continuity = build_continuity_payload(session, session_id)
    summary = continuity.get("summary", {}) if isinstance(continuity.get("summary"), dict) else {}
    health = continuity.get("health", {}) if isinstance(continuity.get("health"), dict) else {}
    incidents = build_continuity_incidents(session, session_id, limit=50)
    incident_summary = incidents.get("summary", {}) if isinstance(incidents.get("summary"), dict) else {}
    items: list[dict[str, Any]] = []

    def push(priority: str, title: str, command: str, reason: str) -> None:
        items.append(
            {
                "priority": str(priority),
                "title": str(title),
                "command": str(command),
                "reason": str(reason),
            }
        )

    top_severity = str(incident_summary.get("topSeverity", "none"))
    stale = int(summary.get("staleDevices", 0) or 0)
    total = int(summary.get("presenceTotal", 0) or 0)
    breaches = int(summary.get("handoffBreaches", 0) or 0)
    status = str(health.get("status", "unknown"))

    if top_severity in {"critical", "high"}:
        push("p0", "Inspect latest continuity incidents", "show continuity incidents", f"top incident severity is {top_severity}")
    if status in {"critical", "degraded"}:
        push("p0" if status == "critical" else "p1", "Review continuity health reasons", "show continuity health", f"continuity status is {status}")
    if breaches > 0:
        push("p1", "Review handoff breach alerts", "show continuity alerts", f"handoff breaches recorded: {breaches}")
    if total > 0 and stale > 0:
        push("p1", "Prune stale presence entries", "prune presence older than 2m", f"stale devices: {stale}/{total}")
    if int(incident_summary.get("alerts", 0) or 0) > 0:
        push("p2", "Reset continuity alerts after review", "clear continuity alerts", "alerts queue contains historical breaches")
    push("p2", "Inspect continuity trend baseline", "show continuity trend", "compare current posture against recent baseline")

    rank = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
    ordered = sorted(items, key=lambda item: rank.get(str(item.get("priority", "p3")), 3))
    dedup: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in ordered:
        cmd = str(item.get("command", "")).strip().lower()
        if cmd in seen:
            continue
        seen.add(cmd)
        dedup.append(item)

    tail = dedup[: max(1, min(int(limit), 20))]
    return {
        "items": tail,
        "summary": {
            "count": int(len(tail)),
            "topPriority": str((tail[0] if tail else {}).get("priority", "none")),
            "healthStatus": status,
        },
    }


def apply_continuity_next_action(session: SessionState, session_id: str) -> dict[str, Any]:
    report = build_continuity_next_actions(session, session_id, limit=8)
    items = report.get("items", []) if isinstance(report.get("items"), list) else []
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    mode = str(state.get("mode", "normal") or "normal")
    allowed_commands = {
        "prune presence older than 2m": {"type": "prune_presence", "domain": "system", "payload": {"all": False, "maxAgeMs": 120000}},
        "clear continuity alerts": {"type": "clear_continuity_alerts", "domain": "system", "payload": {}},
    }
    if mode == "safe":
        allowed_commands = {
            "prune presence older than 2m": {"type": "prune_presence", "domain": "system", "payload": {"all": False, "maxAgeMs": 120000}},
        }
    for item in items:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command", "")).strip().lower()
        op = allowed_commands.get(command)
        if not op:
            continue
        result = run_operation(session, op)
        return {
            "applied": bool(result.get("ok", False)),
            "command": command,
            "title": str(item.get("title", "action")),
            "priority": str(item.get("priority", "p2")),
            "mode": mode,
            "result": result,
        }
    return {
        "applied": False,
        "command": "",
        "title": "manual_review",
        "priority": "none",
        "mode": mode,
        "result": {"ok": True, "message": "No auto-applicable continuity action available."},
    }


def build_continuity_autopilot_preview(session: SessionState, session_id: str) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    guardrails = evaluate_continuity_autopilot_guardrails(session)
    now = now_ms()
    mode = str(state.get("mode", "normal") or "normal")
    cooldown_ms = max(1000, int(state.get("cooldownMs", CONTINUITY_AUTOPILOT_COOLDOWN_MS) or CONTINUITY_AUTOPILOT_COOLDOWN_MS))
    last_run_at = int(state.get("lastRunAt", 0) or 0)
    base_max_applies = max(0, int(state.get("maxAppliesPerHour", CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR)))
    if mode == "safe":
        max_applies = max(0, min(base_max_applies, 10))
    elif mode == "aggressive":
        max_applies = max(0, min(500, base_max_applies * 2))
    else:
        max_applies = base_max_applies
    applied_ts = state.get("appliedTimestamps", []) if isinstance(state.get("appliedTimestamps"), list) else []
    used = len([int(ts or 0) for ts in applied_ts if int(ts or 0) > (now - 60 * 60 * 1000)])
    next_actions = build_continuity_next_actions(session, session_id, limit=1)
    candidate = (next_actions.get("items", []) or [{}])[0] if isinstance(next_actions.get("items"), list) and next_actions.get("items") else {}
    reason = "ready"
    can_run = True
    next_in_ms = 0
    if not bool(state.get("enabled", False)):
        reason = "disabled"
        can_run = False
    elif not bool(guardrails.get("ok", True)):
        reason = str((guardrails.get("blockers", [{}])[0] if isinstance(guardrails.get("blockers"), list) and guardrails.get("blockers") else {}).get("code", "guardrail_blocked"))
        can_run = False
    elif last_run_at > 0 and (now - last_run_at) < cooldown_ms:
        reason = "cooldown"
        can_run = False
        next_in_ms = int(cooldown_ms - (now - last_run_at))
    elif max_applies <= 0 or used >= max_applies:
        reason = "rate_limited"
        can_run = False
    return {
        "ok": True,
        "sessionId": session_id,
        "autopilot": state,
        "guardrails": guardrails,
        "preview": {
            "canRun": bool(can_run),
            "reason": reason,
            "mode": mode,
            "nextInMs": int(max(0, next_in_ms)),
            "usedAppliesLastHour": int(used),
            "maxAppliesPerHour": int(max_applies),
            "candidate": {
                "priority": str(candidate.get("priority", "none")),
                "title": str(candidate.get("title", "none")),
                "command": str(candidate.get("command", "")),
            },
        },
    }


def build_continuity_autopilot_metrics(session: SessionState, session_id: str, window_ms: int = 3600000) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    now = now_ms()
    window = max(60000, min(int(window_ms), 86400000))
    history = session.continuity_autopilot_history if isinstance(session.continuity_autopilot_history, list) else []
    recent = [item for item in history if isinstance(item, dict) and int(item.get("ts", 0) or 0) > (now - window)]
    reason_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    changed_count = 0
    applied_count = 0
    for item in recent:
        reason = str(item.get("reason", "unknown")) or "unknown"
        source = str(item.get("source", "unknown")) or "unknown"
        reason_counts[reason] = int(reason_counts.get(reason, 0) or 0) + 1
        source_counts[source] = int(source_counts.get(source, 0) or 0) + 1
        if bool(item.get("changed", False)):
            changed_count += 1
        action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
        if bool(action.get("applied", False)):
            applied_count += 1
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "autopilot": {
            "enabled": bool(state.get("enabled", False)),
            "cooldownMs": int(state.get("cooldownMs", CONTINUITY_AUTOPILOT_COOLDOWN_MS)),
            "maxAppliesPerHour": int(state.get("maxAppliesPerHour", CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR)),
        },
        "metrics": {
            "recentCount": int(len(recent)),
            "changedCount": int(changed_count),
            "appliedCount": int(applied_count),
            "reasonCounts": reason_counts,
            "sourceCounts": source_counts,
        },
    }


def evaluate_continuity_autopilot_guardrails(session: SessionState) -> dict[str, Any]:
    blockers: list[dict[str, str]] = []
    persist = session.faults.get("persist", {}) if isinstance(session.faults.get("persist"), dict) else {}
    if bool(persist.get("degraded", False)):
        blockers.append({"code": "persist_degraded", "detail": "persistence layer degraded"})
    pending = session.handoff.get("pending") if isinstance(session.handoff.get("pending"), dict) else None
    if pending and str(pending.get("token", "")):
        blockers.append({"code": "handoff_pending", "detail": "handoff claim pending"})
    return {
        "ok": len(blockers) == 0,
        "blockerCount": int(len(blockers)),
        "blockers": blockers[:6],
    }


def build_continuity_autopilot_dry_run(session: SessionState, session_id: str, force: bool = False) -> dict[str, Any]:
    before = build_snapshot_stats(session)
    projected = SessionState(
        memory=copy.deepcopy(session.memory),
        graph=copy.deepcopy(session.graph),
        jobs=copy.deepcopy(session.jobs),
        presence=copy.deepcopy(session.presence),
        handoff=copy.deepcopy(session.handoff),
        idempotency=copy.deepcopy(session.idempotency),
        slo=copy.deepcopy(session.slo),
        restore=copy.deepcopy(session.restore),
        faults=copy.deepcopy(session.faults),
        revision=int(session.revision),
        last_turn=copy.deepcopy(session.last_turn),
        turn_history=copy.deepcopy(session.turn_history),
        continuity_history=copy.deepcopy(session.continuity_history),
        continuity_autopilot=copy.deepcopy(session.continuity_autopilot),
        continuity_autopilot_history=copy.deepcopy(session.continuity_autopilot_history),
        dead_letters=copy.deepcopy(session.dead_letters),
        journal=copy.deepcopy(session.journal),
        undo_stack=copy.deepcopy(session.undo_stack),
        checkpoints=copy.deepcopy(session.checkpoints),
    )
    tick_report = run_continuity_autopilot_tick(projected, session_id, force=bool(force))
    after = build_snapshot_stats(projected)
    return {
        "ok": True,
        "sessionId": session_id,
        "force": bool(force),
        "report": tick_report,
        "autopilot": ensure_continuity_autopilot_state(projected.continuity_autopilot),
        "snapshot": {
            "before": before,
            "after": after,
            "delta": {
                "tasks": int(after.get("tasks", 0) or 0) - int(before.get("tasks", 0) or 0),
                "expenses": int(after.get("expenses", 0) or 0) - int(before.get("expenses", 0) or 0),
                "notes": int(after.get("notes", 0) or 0) - int(before.get("notes", 0) or 0),
                "jobs": int(after.get("jobs", 0) or 0) - int(before.get("jobs", 0) or 0),
                "journal": int(after.get("journal", 0) or 0) - int(before.get("journal", 0) or 0),
            },
        },
    }


def build_continuity_autopilot_mode_recommendation(session: SessionState, session_id: str) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    guardrails = evaluate_continuity_autopilot_guardrails(session)
    continuity = build_continuity_payload(session, session_id)
    health = continuity.get("health", {}) if isinstance(continuity.get("health"), dict) else {}
    metrics = build_continuity_autopilot_metrics(session, session_id, window_ms=3600000)
    reason_counts = ((metrics.get("metrics", {}) or {}).get("reasonCounts", {}) or {}) if isinstance(((metrics.get("metrics", {}) or {}).get("reasonCounts", {})), dict) else {}
    noops = int(reason_counts.get("noop", 0) or 0)
    rate_limited = int(reason_counts.get("rate_limited", 0) or 0)
    recommended = "normal"
    reasons: list[str] = []

    if not bool(guardrails.get("ok", True)):
        recommended = "safe"
        reasons.append("guardrails_blocked")
    status = str(health.get("status", "unknown"))
    if status == "critical":
        recommended = "safe"
        reasons.append("continuity_critical")
    elif status == "healthy" and noops <= 2 and rate_limited <= 1 and bool(guardrails.get("ok", True)):
        recommended = "aggressive"
        reasons.append("healthy_low_friction")
    elif rate_limited >= 3:
        recommended = "normal"
        reasons.append("rate_limited_recently")
    if not reasons:
        reasons.append("balanced_default")

    return {
        "ok": True,
        "sessionId": session_id,
        "currentMode": str(state.get("mode", "normal")),
        "recommendedMode": recommended,
        "reasons": reasons[:6],
        "signals": {
            "healthStatus": status,
            "guardrailBlockers": int(guardrails.get("blockerCount", 0) or 0),
            "noopCount1h": noops,
            "rateLimitedCount1h": rate_limited,
        },
    }


def build_continuity_autopilot_mode_drift(session: SessionState, session_id: str) -> dict[str, Any]:
    recommendation = build_continuity_autopilot_mode_recommendation(session, session_id)
    current_mode = str(recommendation.get("currentMode", "normal"))
    recommended_mode = str(recommendation.get("recommendedMode", "normal"))
    drifted = current_mode != recommended_mode
    return {
        "ok": True,
        "sessionId": session_id,
        "drifted": bool(drifted),
        "currentMode": current_mode,
        "recommendedMode": recommended_mode,
        "reasons": list(recommendation.get("reasons", []))[:6] if isinstance(recommendation.get("reasons"), list) else [],
        "signals": recommendation.get("signals", {}),
    }


def build_continuity_autopilot_mode_alignment(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    history = session.continuity_autopilot_history if isinstance(session.continuity_autopilot_history, list) else []
    items: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source", ""))
        reason = str(item.get("reason", ""))
        action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
        command = str(action.get("command", ""))
        if reason == "mode_aligned" or source in {"tick_mode_align", "api_mode_apply_recommended", "intent_mode_apply_recommended"} or command.startswith("continuity_autopilot_mode_"):
            items.append(item)
    tail = items[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "aligned": int(state.get("aligned", 0) or 0),
            "lastAlignAt": int(state.get("lastAlignAt", 0) or 0),
            "count": int(len(tail)),
            "currentMode": str(state.get("mode", "normal")),
        },
        "items": tail,
    }


def evaluate_continuity_autopilot_mode_policy(session: SessionState, session_id: str, target_mode: str) -> dict[str, Any]:
    target = str(target_mode or "normal").strip().lower()
    if target not in {"safe", "normal", "aggressive"}:
        return {
            "ok": False,
            "sessionId": session_id,
            "targetMode": target,
            "allowed": False,
            "code": "invalid_mode",
            "reason": "mode must be safe|normal|aggressive",
            "signals": {},
        }
    guardrails = evaluate_continuity_autopilot_guardrails(session)
    continuity = build_continuity_payload(session, session_id)
    health = continuity.get("health", {}) if isinstance(continuity.get("health"), dict) else {}
    status = str(health.get("status", "unknown"))
    blocker_count = int(guardrails.get("blockerCount", 0) or 0)
    if target == "aggressive":
        if blocker_count > 0:
            return {
                "ok": True,
                "sessionId": session_id,
                "targetMode": target,
                "allowed": False,
                "code": "guardrail_blocked",
                "reason": "aggressive mode blocked by active guardrails",
                "signals": {"healthStatus": status, "guardrailBlockers": blocker_count},
            }
        if status != "healthy":
            return {
                "ok": True,
                "sessionId": session_id,
                "targetMode": target,
                "allowed": False,
                "code": "health_not_healthy",
                "reason": "aggressive mode requires healthy continuity",
                "signals": {"healthStatus": status, "guardrailBlockers": blocker_count},
            }
    return {
        "ok": True,
        "sessionId": session_id,
        "targetMode": target,
        "allowed": True,
        "code": "ok",
        "reason": "mode transition allowed",
        "signals": {"healthStatus": status, "guardrailBlockers": blocker_count},
    }


def build_continuity_autopilot_mode_policy_history(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    history = session.continuity_autopilot_history if isinstance(session.continuity_autopilot_history, list) else []
    items: list[dict[str, Any]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason", ""))
        source = str(item.get("source", ""))
        action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
        command = str(action.get("command", ""))
        if "mode_policy" in reason or "mode_recommended" in reason or "mode_" in command or source in {"api_mode_apply_recommended", "intent_mode_apply_recommended"}:
            items.append(item)
    tail = items[-max(1, min(int(limit), 200)) :]
    blocked = 0
    allowed = 0
    for item in tail:
        reason = str(item.get("reason", ""))
        if "blocked" in reason:
            blocked += 1
        else:
            allowed += 1
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "allowed": int(allowed),
            "blocked": int(blocked),
        },
        "items": tail,
    }


def build_continuity_autopilot_mode_policy_matrix(session: SessionState, session_id: str) -> dict[str, Any]:
    rows = []
    for target in ("safe", "normal", "aggressive"):
        policy = evaluate_continuity_autopilot_mode_policy(session, session_id, target)
        rows.append(
            {
                "targetMode": target,
                "allowed": bool(policy.get("allowed", False)),
                "code": str(policy.get("code", "unknown")),
                "reason": str(policy.get("reason", "")),
            }
        )
    allowed_count = len([row for row in rows if bool(row.get("allowed", False))])
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "allowed": int(allowed_count),
            "blocked": int(len(rows) - allowed_count),
        },
        "items": rows,
    }


def build_continuity_autopilot_posture(session: SessionState, session_id: str) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    preview = build_continuity_autopilot_preview(session, session_id)
    drift = build_continuity_autopilot_mode_drift(session, session_id)
    recommendation = build_continuity_autopilot_mode_recommendation(session, session_id)
    guardrails = evaluate_continuity_autopilot_guardrails(session)
    matrix = build_continuity_autopilot_mode_policy_matrix(session, session_id)
    return {
        "ok": True,
        "sessionId": session_id,
        "posture": {
            "enabled": bool(state.get("enabled", False)),
            "mode": str(state.get("mode", "normal")),
            "autoAlignMode": bool(state.get("autoAlignMode", False)),
            "recommendedMode": str(recommendation.get("recommendedMode", "normal")),
            "modeDrifted": bool(drift.get("drifted", False)),
            "previewReason": str((preview.get("preview", {}) or {}).get("reason", "unknown")),
            "guardrailBlockers": int(guardrails.get("blockerCount", 0) or 0),
            "policyAllowed": int(((matrix.get("summary", {}) or {}).get("allowed", 0) or 0)),
            "policyBlocked": int(((matrix.get("summary", {}) or {}).get("blocked", 0) or 0)),
            "alignedCount": int(state.get("aligned", 0) or 0),
            "appliedCount": int(state.get("applied", 0) or 0),
        },
    }


def append_continuity_autopilot_posture_snapshot(session: SessionState, session_id: str, source: str) -> None:
    report = build_continuity_autopilot_posture(session, session_id)
    posture = report.get("posture", {}) if isinstance(report.get("posture"), dict) else {}
    session.continuity_autopilot_posture_history.append(
        {
            "ts": now_ms(),
            "source": str(source or "unknown")[:40],
            "enabled": bool(posture.get("enabled", False)),
            "mode": str(posture.get("mode", "normal"))[:16],
            "recommendedMode": str(posture.get("recommendedMode", "normal"))[:16],
            "modeDrifted": bool(posture.get("modeDrifted", False)),
            "autoAlignMode": bool(posture.get("autoAlignMode", False)),
            "previewReason": str(posture.get("previewReason", "unknown"))[:64],
            "guardrailBlockers": int(posture.get("guardrailBlockers", 0) or 0),
            "policyAllowed": int(posture.get("policyAllowed", 0) or 0),
            "policyBlocked": int(posture.get("policyBlocked", 0) or 0),
            "alignedCount": int(posture.get("alignedCount", 0) or 0),
            "appliedCount": int(posture.get("appliedCount", 0) or 0),
        }
    )
    if len(session.continuity_autopilot_posture_history) > int(CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX):
        session.continuity_autopilot_posture_history[:] = session.continuity_autopilot_posture_history[
            -int(CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX) :
        ]


def build_continuity_autopilot_posture_history(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    items = session.continuity_autopilot_posture_history if isinstance(session.continuity_autopilot_posture_history, list) else []
    tail = items[-max(1, min(int(limit), 200)) :]
    drifted = len([item for item in tail if isinstance(item, dict) and bool(item.get("modeDrifted", False))])
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "drifted": int(drifted),
        },
        "items": tail,
    }


def detect_continuity_autopilot_posture_anomalies(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    history = session.continuity_autopilot_posture_history if isinstance(session.continuity_autopilot_posture_history, list) else []
    window = history[-max(2, min(500, int(CONTINUITY_AUTOPILOT_POSTURE_HISTORY_MAX))) :]
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for index in range(1, len(window)):
        prev = window[index - 1] if isinstance(window[index - 1], dict) else {}
        curr = window[index] if isinstance(window[index], dict) else {}
        prev_guardrails = int(prev.get("guardrailBlockers", 0) or 0)
        curr_guardrails = int(curr.get("guardrailBlockers", 0) or 0)
        prev_drifted = bool(prev.get("modeDrifted", False))
        curr_drifted = bool(curr.get("modeDrifted", False))
        prev_reason = str(prev.get("previewReason", "unknown") or "unknown")
        curr_reason = str(curr.get("previewReason", "unknown") or "unknown")
        prev_mode = str(prev.get("mode", "normal") or "normal")
        curr_mode = str(curr.get("mode", "normal") or "normal")
        if curr_guardrails > prev_guardrails:
            items.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "guardrail_increase",
                    "detail": f"{prev_guardrails}->{curr_guardrails}",
                    "mode": curr_mode,
                    "reason": curr_reason,
                }
            )
        if (not prev_drifted) and curr_drifted:
            items.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "drift_started",
                    "detail": f"{str(prev.get('recommendedMode', prev_mode))}->{str(curr.get('recommendedMode', curr_mode))}",
                    "mode": curr_mode,
                    "reason": curr_reason,
                }
            )
        if prev_reason != curr_reason and curr_reason in {"persist_degraded", "handoff_pending", "rate_limited", "cooldown"}:
            items.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "preview_reason_shift",
                    "detail": f"{prev_reason}->{curr_reason}",
                    "mode": curr_mode,
                    "reason": curr_reason,
                }
            )
        if prev_mode != curr_mode:
            items.append(
                {
                    "ts": int(curr.get("ts", now_ms()) or now_ms()),
                    "source": str(curr.get("source", "event") or "event"),
                    "type": "mode_changed",
                    "detail": f"{prev_mode}->{curr_mode}",
                    "mode": curr_mode,
                    "reason": curr_reason,
                }
            )
    for item in items:
        anomaly_type = str(item.get("type", "unknown"))
        counts[anomaly_type] = int(counts.get(anomaly_type, 0) or 0) + 1
    top_type = "none"
    top_count = 0
    for key, value in counts.items():
        if int(value) > top_count:
            top_type = str(key)
            top_count = int(value)
    tail = items[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "totalDetected": int(len(items)),
            "topType": top_type,
        },
        "counts": counts,
        "items": tail,
    }


def build_continuity_autopilot_posture_actions(session: SessionState, session_id: str, limit: int = 5) -> dict[str, Any]:
    posture_report = build_continuity_autopilot_posture(session, session_id)
    posture = posture_report.get("posture", {}) if isinstance(posture_report.get("posture"), dict) else {}
    anomalies = detect_continuity_autopilot_posture_anomalies(session, session_id, limit=50)
    anomaly_summary = anomalies.get("summary", {}) if isinstance(anomalies.get("summary"), dict) else {}
    items: list[dict[str, str]] = []

    def push(priority: str, title: str, command: str, reason: str) -> None:
        items.append(
            {
                "priority": str(priority),
                "title": str(title),
                "command": str(command),
                "reason": str(reason),
            }
        )

    enabled = bool(posture.get("enabled", False))
    drifted = bool(posture.get("modeDrifted", False))
    guardrail_blockers = int(posture.get("guardrailBlockers", 0) or 0)
    preview_reason = str(posture.get("previewReason", "unknown") or "unknown")
    anomaly_count = int(anomaly_summary.get("totalDetected", 0) or 0)
    top_type = str(anomaly_summary.get("topType", "none") or "none")

    if not enabled:
        push("p1", "Enable continuity autopilot", "enable continuity autopilot", "autopilot is currently disabled")
    if drifted:
        push("p0", "Align autopilot mode", "apply continuity autopilot mode recommendation", "current mode drifted from recommendation")
    if guardrail_blockers > 0:
        push("p0", "Inspect guardrail blockers", "show continuity autopilot guardrails", f"guardrail blockers active: {guardrail_blockers}")
    if preview_reason == "persist_degraded":
        push("p0", "Recover persistence channel", "retry persist now", "autopilot preview blocked by degraded persistence")
    if preview_reason == "rate_limited":
        push("p2", "Tune autopilot rate limit", "set continuity autopilot max applies 60 per hour", "autopilot is currently rate limited")
    if preview_reason == "cooldown":
        push("p2", "Tune autopilot cooldown", "set continuity autopilot cooldown 10s", "autopilot waiting for cooldown window")
    if anomaly_count > 0:
        push("p1", "Review posture anomalies", "show continuity autopilot posture anomalies", f"posture anomalies detected: {anomaly_count}")
    push("p2", "Review posture history", "show continuity autopilot posture history", "inspect recent control-plane posture snapshots")

    rank = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
    ordered = sorted(items, key=lambda item: rank.get(str(item.get("priority", "p3")), 3))
    dedup: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in ordered:
        command = str(item.get("command", "")).strip().lower()
        if command in seen:
            continue
        seen.add(command)
        dedup.append(item)

    tail = dedup[: max(1, min(int(limit), 20))]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "topPriority": str((tail[0] if tail else {}).get("priority", "none")),
            "topType": top_type,
        },
        "items": tail,
    }


def apply_continuity_autopilot_posture_action(session: SessionState, session_id: str, index: int = 1) -> dict[str, Any]:
    report = build_continuity_autopilot_posture_actions(session, session_id, limit=10)
    items = report.get("items", []) if isinstance(report.get("items"), list) else []
    idx = max(1, int(index)) - 1
    if idx >= len(items):
        append_continuity_autopilot_posture_action_history(
            session,
            source="apply_action",
            index=index,
            command="",
            applied=False,
            changed=False,
            reason="index_out_of_range",
            message="No posture action available at that index.",
        )
        return {
            "applied": False,
            "changed": False,
            "index": int(index),
            "reason": "index_out_of_range",
            "result": {"ok": True, "message": "No posture action available at that index."},
        }
    item = items[idx] if isinstance(items[idx], dict) else {}
    command = str(item.get("command", "")).strip().lower()
    dry = build_continuity_autopilot_posture_action_dry_run(session, session_id, index=index, record=True, source="apply_action")
    op = dry.get("op") if isinstance(dry.get("op"), dict) else None
    if not op:
        append_continuity_autopilot_posture_action_history(
            session,
            source="apply_action",
            index=index,
            command=command,
            applied=False,
            changed=False,
            reason="read_only_action",
            message="Selected posture action is informational; execute it manually.",
        )
        return {
            "applied": False,
            "changed": False,
            "index": int(index),
            "command": command,
            "reason": "read_only_action",
            "result": {"ok": True, "message": "Selected posture action is informational; execute it manually."},
        }
    result = run_operation(session, op)
    append_continuity_autopilot_posture_action_history(
        session,
        source="apply_action",
        index=index,
        command=command,
        applied=bool(result.get("ok", False)),
        changed=bool(result.get("ok", False)),
        reason="applied" if bool(result.get("ok", False)) else "failed",
        message=str(result.get("message", "")),
    )
    return {
        "applied": bool(result.get("ok", False)),
        "changed": bool(result.get("ok", False)),
        "index": int(index),
        "command": command,
        "title": str(item.get("title", "action")),
        "priority": str(item.get("priority", "p2")),
        "reason": "applied" if bool(result.get("ok", False)) else "failed",
        "result": result,
    }


def map_continuity_autopilot_posture_action_command(command: str) -> dict[str, Any] | None:
    mapped: dict[str, dict[str, Any]] = {
        "enable continuity autopilot": {"type": "continuity_autopilot_set", "domain": "system", "payload": {"enabled": True}},
        "apply continuity autopilot mode recommendation": {"type": "continuity_autopilot_mode_apply_recommended", "domain": "system", "payload": {}},
        "retry persist now": {"type": "retry_persist", "domain": "system", "payload": {}},
        "set continuity autopilot max applies 60 per hour": {"type": "continuity_autopilot_config", "domain": "system", "payload": {"maxAppliesPerHour": 60}},
        "set continuity autopilot cooldown 10s": {"type": "continuity_autopilot_config", "domain": "system", "payload": {"cooldownMs": 10000}},
    }
    return mapped.get(str(command or "").strip().lower())


def build_continuity_autopilot_posture_action_dry_run(
    session: SessionState, session_id: str, index: int = 1, record: bool = False, source: str = "dry_run"
) -> dict[str, Any]:
    report = build_continuity_autopilot_posture_actions(session, session_id, limit=10)
    items = report.get("items", []) if isinstance(report.get("items"), list) else []
    idx = max(1, int(index)) - 1
    if idx >= len(items):
        result = {
            "ok": True,
            "sessionId": session_id,
            "index": int(index),
            "appliable": False,
            "reason": "index_out_of_range",
            "action": None,
            "op": None,
            "policy": {"allowed": True, "code": "ok", "reason": "no action available"},
        }
        if record:
            append_continuity_autopilot_posture_action_policy_history(
                session, source=source, index=index, command="", allowed=False, code="ok", reason="index_out_of_range"
            )
        return result
    item = items[idx] if isinstance(items[idx], dict) else {}
    command = str(item.get("command", "")).strip().lower()
    op = map_continuity_autopilot_posture_action_command(command)
    if not op:
        result = {
            "ok": True,
            "sessionId": session_id,
            "index": int(index),
            "appliable": False,
            "reason": "read_only_action",
            "action": item,
            "op": None,
            "policy": {"allowed": True, "code": "ok", "reason": "informational action"},
        }
        if record:
            append_continuity_autopilot_posture_action_policy_history(
                session, source=source, index=index, command=command, allowed=False, code="ok", reason="read_only_action"
            )
        return result
    capability = resolve_capability(op)
    policy = evaluate_policy(op, capability)
    result = {
        "ok": True,
        "sessionId": session_id,
        "index": int(index),
        "appliable": bool(policy.get("allowed", False)),
        "reason": "ready" if bool(policy.get("allowed", False)) else str(policy.get("code", "blocked")),
        "action": item,
        "op": op,
        "capability": capability,
        "policy": policy,
    }
    if record:
        append_continuity_autopilot_posture_action_policy_history(
            session,
            source=source,
            index=index,
            command=command,
            allowed=bool(policy.get("allowed", False)),
            code=str(policy.get("code", "ok")),
            reason=str(result.get("reason", "ready")),
        )
    return result


def build_continuity_autopilot_posture_action_policy_matrix(session: SessionState, session_id: str, limit: int = 10) -> dict[str, Any]:
    actions = build_continuity_autopilot_posture_actions(session, session_id, limit=max(1, min(int(limit), 20)))
    items = actions.get("items", []) if isinstance(actions.get("items"), list) else []
    rows: list[dict[str, Any]] = []
    allowed = 0
    blocked = 0
    informational = 0
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        dry = build_continuity_autopilot_posture_action_dry_run(session, session_id, index=idx)
        policy = dry.get("policy", {}) if isinstance(dry.get("policy"), dict) else {}
        command = str(item.get("command", "") or "")
        appliable = bool(dry.get("appliable", False))
        reason = str(dry.get("reason", "unknown") or "unknown")
        if reason == "read_only_action":
            informational += 1
        elif appliable:
            allowed += 1
        else:
            blocked += 1
        rows.append(
            {
                "index": int(idx),
                "priority": str(item.get("priority", "p2")),
                "title": str(item.get("title", "action")),
                "command": command,
                "appliable": appliable,
                "reason": reason,
                "policyCode": str(policy.get("code", "ok")),
            }
        )
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(rows)),
            "allowed": int(allowed),
            "blocked": int(blocked),
            "informational": int(informational),
        },
        "items": rows,
    }


def apply_continuity_autopilot_posture_actions_batch(session: SessionState, session_id: str, limit: int = 3) -> dict[str, Any]:
    max_steps = max(1, min(int(limit), 10))
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    applied = 0
    changed = False
    for _ in range(max_steps):
        step = apply_continuity_autopilot_posture_action(session, session_id, index=1)
        items.append(step)
        command = str(step.get("command", "")).strip().lower()
        if command:
            if command in seen and not bool(step.get("changed", False)):
                break
            seen.add(command)
        if bool(step.get("applied", False)):
            applied += 1
        if bool(step.get("changed", False)):
            changed = True
            continue
        break
    return {
        "changed": bool(changed),
        "attempted": int(len(items)),
        "applied": int(applied),
        "limit": int(max_steps),
        "items": items,
    }


def append_continuity_autopilot_posture_action_history(
    session: SessionState,
    source: str,
    index: int,
    command: str,
    applied: bool,
    changed: bool,
    reason: str,
    message: str,
) -> None:
    session.continuity_autopilot_posture_action_history.append(
        {
            "ts": now_ms(),
            "source": str(source or "unknown")[:40],
            "index": int(index),
            "command": str(command or "")[:120],
            "applied": bool(applied),
            "changed": bool(changed),
            "reason": str(reason or "none")[:64],
            "message": str(message or "")[:200],
        }
    )
    if len(session.continuity_autopilot_posture_action_history) > int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX):
        session.continuity_autopilot_posture_action_history[:] = session.continuity_autopilot_posture_action_history[
            -int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX) :
        ]


def append_continuity_autopilot_posture_action_policy_history(
    session: SessionState, source: str, index: int, command: str, allowed: bool, code: str, reason: str
) -> None:
    session.continuity_autopilot_posture_action_policy_history.append(
        {
            "ts": now_ms(),
            "source": str(source or "unknown")[:40],
            "index": int(index),
            "command": str(command or "")[:120],
            "allowed": bool(allowed),
            "policyCode": str(code or "ok")[:40],
            "reason": str(reason or "unknown")[:80],
        }
    )
    if len(session.continuity_autopilot_posture_action_policy_history) > int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX):
        session.continuity_autopilot_posture_action_policy_history[:] = session.continuity_autopilot_posture_action_policy_history[
            -int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX) :
        ]


def build_continuity_autopilot_posture_action_policy_history(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    items = (
        session.continuity_autopilot_posture_action_policy_history
        if isinstance(session.continuity_autopilot_posture_action_policy_history, list)
        else []
    )
    tail = items[-max(1, min(int(limit), 200)) :]
    allowed = len([item for item in tail if isinstance(item, dict) and bool(item.get("allowed", False))])
    blocked = len([item for item in tail if isinstance(item, dict) and not bool(item.get("allowed", False))])
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "allowed": int(allowed),
            "blocked": int(blocked),
        },
        "items": tail,
    }


def build_continuity_autopilot_posture_action_policy_metrics(session: SessionState, session_id: str, window_ms: int = 3600000) -> dict[str, Any]:
    items = (
        session.continuity_autopilot_posture_action_policy_history
        if isinstance(session.continuity_autopilot_posture_action_policy_history, list)
        else []
    )
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    recent = [item for item in items if isinstance(item, dict) and int(item.get("ts", 0) or 0) >= cutoff]
    allowed = 0
    blocked = 0
    code_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for item in recent:
        is_allowed = bool(item.get("allowed", False))
        if is_allowed:
            allowed += 1
        else:
            blocked += 1
        code = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
        reason = str(item.get("reason", "unknown") or "unknown").strip().lower() or "unknown"
        code_counts[code] = int(code_counts.get(code, 0) or 0) + 1
        reason_counts[reason] = int(reason_counts.get(reason, 0) or 0) + 1
    count = len(recent)
    allowed_pct = (float(allowed) * 100.0 / float(count)) if count > 0 else 0.0
    blocked_pct = (float(blocked) * 100.0 / float(count)) if count > 0 else 0.0
    top_code = "none"
    top_code_count = 0
    for key, value in code_counts.items():
        if int(value) > top_code_count:
            top_code = str(key)
            top_code_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(count),
            "allowed": int(allowed),
            "blocked": int(blocked),
            "allowedPct": float(round(allowed_pct, 2)),
            "blockedPct": float(round(blocked_pct, 2)),
            "topPolicyCode": top_code,
        },
        "counts": {
            "policyCodes": code_counts,
            "reasons": reason_counts,
        },
    }


def detect_continuity_autopilot_posture_action_policy_anomalies(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    items = (
        session.continuity_autopilot_posture_action_policy_history
        if isinstance(session.continuity_autopilot_posture_action_policy_history, list)
        else []
    )
    recent = items[-max(2, min(int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_POLICY_HISTORY_MAX), 500)) :]
    anomalies: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    blocked_streak = 0
    code_streak: dict[str, int] = {}
    for item in recent:
        if not isinstance(item, dict):
            continue
        ts = int(item.get("ts", now_ms()) or now_ms())
        source = str(item.get("source", "event") or "event")
        command = str(item.get("command", "") or "").strip().lower() or "-"
        allowed = bool(item.get("allowed", False))
        code = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
        reason = str(item.get("reason", "unknown") or "unknown").strip().lower() or "unknown"
        if allowed:
            blocked_streak = 0
            code_streak[code] = 0
            continue
        blocked_streak += 1
        code_streak[code] = int(code_streak.get(code, 0) or 0) + 1
        if code not in {"ok"}:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "blocked_policy_code",
                    "detail": f"{code} | {command}",
                    "command": command,
                    "policyCode": code,
                    "reason": reason,
                }
            )
        if blocked_streak >= 3:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "blocked_streak",
                    "detail": f"streak={blocked_streak}",
                    "command": command,
                    "policyCode": code,
                    "reason": reason,
                }
            )
            blocked_streak = 0
        if int(code_streak.get(code, 0) or 0) >= 2:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "policy_code_repeat",
                    "detail": code,
                    "command": command,
                    "policyCode": code,
                    "reason": reason,
                }
            )
            code_streak[code] = 0
    for anomaly in anomalies:
        anomaly_type = str(anomaly.get("type", "unknown"))
        counts[anomaly_type] = int(counts.get(anomaly_type, 0) or 0) + 1
    top_type = "none"
    top_count = 0
    for key, value in counts.items():
        if int(value) > top_count:
            top_type = str(key)
            top_count = int(value)
    tail = anomalies[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "totalDetected": int(len(anomalies)),
            "topType": top_type,
        },
        "counts": counts,
        "items": tail,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_history(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    report = detect_continuity_autopilot_posture_action_policy_anomalies(session, session_id, limit=500)
    items = report.get("items", []) if isinstance(report.get("items"), list) else []
    tail = items[-max(1, min(int(limit), 200)) :]
    counts: dict[str, int] = {}
    for item in tail:
        if not isinstance(item, dict):
            continue
        anomaly_type = str(item.get("type", "unknown") or "unknown")
        counts[anomaly_type] = int(counts.get(anomaly_type, 0) or 0) + 1
    top_type = "none"
    top_count = 0
    for key, value in counts.items():
        if int(value) > top_count:
            top_type = str(key)
            top_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "topType": top_type,
        },
        "counts": counts,
        "items": tail,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_trend(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    history = (
        session.continuity_autopilot_posture_action_policy_history
        if isinstance(session.continuity_autopilot_posture_action_policy_history, list)
        else []
    )
    now = now_ms()
    window = max(60000, min(int(window_ms), 86400000))
    bucket_count = max(2, min(int(buckets), 24))
    bucket_ms = max(1, int(window / bucket_count))
    start_ts = now - window
    series: list[dict[str, Any]] = []
    for idx in range(bucket_count):
        bucket_start = start_ts + (idx * bucket_ms)
        bucket_end = start_ts + ((idx + 1) * bucket_ms) if idx < (bucket_count - 1) else now + 1
        base_count = 0
        anomaly_count = 0
        type_counts: dict[str, int] = {}
        blocked_streak = 0
        code_streak: dict[str, int] = {}
        for item in history:
            if not isinstance(item, dict):
                continue
            ts = int(item.get("ts", 0) or 0)
            if ts < bucket_start or ts >= bucket_end:
                continue
            base_count += 1
            allowed = bool(item.get("allowed", False))
            code = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
            if allowed:
                blocked_streak = 0
                code_streak[code] = 0
                continue
            if code != "ok":
                anomaly_count += 1
                type_counts["blocked_policy_code"] = int(type_counts.get("blocked_policy_code", 0) or 0) + 1
            blocked_streak += 1
            if blocked_streak >= 3:
                anomaly_count += 1
                type_counts["blocked_streak"] = int(type_counts.get("blocked_streak", 0) or 0) + 1
                blocked_streak = 0
            code_streak[code] = int(code_streak.get(code, 0) or 0) + 1
            if int(code_streak.get(code, 0) or 0) >= 2:
                anomaly_count += 1
                type_counts["policy_code_repeat"] = int(type_counts.get("policy_code_repeat", 0) or 0) + 1
                code_streak[code] = 0
        rate = (float(anomaly_count) * 100.0 / float(base_count)) if base_count > 0 else 0.0
        series.append(
            {
                "index": int(idx + 1),
                "startTs": int(bucket_start),
                "endTs": int(bucket_end - 1),
                "count": int(base_count),
                "anomalies": int(anomaly_count),
                "anomalyRatePct": float(round(rate, 2)),
                "types": type_counts,
            }
        )
    trend = "stable"
    if len(series) >= 2:
        first = float(series[0].get("anomalyRatePct", 0.0) or 0.0)
        last = float(series[-1].get("anomalyRatePct", 0.0) or 0.0)
        if last > (first + 1.0):
            trend = "rising"
        elif last < (first - 1.0):
            trend = "falling"
    totals = {
        "count": int(sum(int(item.get("count", 0) or 0) for item in series)),
        "anomalies": int(sum(int(item.get("anomalies", 0) or 0) for item in series)),
    }
    totals["anomalyRatePct"] = float(round((float(totals["anomalies"]) * 100.0 / float(totals["count"])) if totals["count"] > 0 else 0.0, 2))
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "bucketMs": int(bucket_ms),
        "summary": {
            **totals,
            "trend": trend,
        },
        "series": series,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_offenders(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 8,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 30))
    cutoff = now_ms() - window
    report = detect_continuity_autopilot_posture_action_policy_anomalies(session, session_id, limit=500)
    items = report.get("items", []) if isinstance(report.get("items"), list) else []
    recent = [item for item in items if isinstance(item, dict) and int(item.get("ts", 0) or 0) >= cutoff]
    command_counts: dict[str, int] = {}
    code_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for item in recent:
        command = str(item.get("command", "-") or "-").strip().lower() or "-"
        code = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
        anomaly_type = str(item.get("type", "unknown") or "unknown").strip().lower() or "unknown"
        command_counts[command] = int(command_counts.get(command, 0) or 0) + 1
        code_counts[code] = int(code_counts.get(code, 0) or 0) + 1
        type_counts[anomaly_type] = int(type_counts.get(anomaly_type, 0) or 0) + 1
    offenders: list[dict[str, Any]] = []
    for command, count in sorted(command_counts.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]:
        offenders.append(
            {
                "command": command,
                "count": int(count),
            }
        )
    top_code = "none"
    top_code_count = 0
    for key, value in code_counts.items():
        if int(value) > top_code_count:
            top_code = str(key)
            top_code_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(len(recent)),
            "topCode": top_code,
            "offenderCount": int(len(offenders)),
        },
        "counts": {
            "policyCodes": code_counts,
            "types": type_counts,
        },
        "offenders": offenders,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_state(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_metrics(session, session_id, window_ms=window)
    trend = build_continuity_autopilot_posture_action_policy_anomaly_trend(session, session_id, window_ms=window, buckets=6)
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_offenders(session, session_id, window_ms=window, limit=5)
    summary_metrics = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    summary_trend = trend.get("summary", {}) if isinstance(trend.get("summary"), dict) else {}
    summary_offenders = offenders.get("summary", {}) if isinstance(offenders.get("summary"), dict) else {}
    anomaly_rate = float(summary_metrics.get("anomalyRatePct", 0.0) or 0.0)
    trend_state = str(summary_trend.get("trend", "stable") or "stable")
    health = "healthy"
    if anomaly_rate >= 40.0 or trend_state == "rising":
        health = "degraded"
    if anomaly_rate >= 65.0:
        health = "critical"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "health": health,
            "anomalyRatePct": float(round(anomaly_rate, 2)),
            "trend": trend_state,
            "topCode": str(summary_metrics.get("topPolicyCode", "none")),
            "topOffenderCode": str(summary_offenders.get("topCode", "none")),
            "anomalies": int(summary_metrics.get("anomalies", 0) or 0),
            "count": int(summary_metrics.get("count", 0) or 0),
        },
        "metrics": metrics.get("summary", {}),
        "trend": {
            "summary": summary_trend,
            "series": trend.get("series", []),
        },
        "offenders": {
            "summary": summary_offenders,
            "items": offenders.get("offenders", []),
        },
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    threshold_pct: float = 35.0,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    threshold = max(1.0, min(float(threshold_pct), 100.0))
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_metrics(session, session_id, window_ms=window)
    summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    rate = float(summary.get("anomalyRatePct", 0.0) or 0.0)
    remaining = float(threshold - rate)
    within_budget = bool(rate <= threshold)
    status = "within_budget" if within_budget else "exceeded"
    severity = "none"
    if not within_budget:
        severity = "critical" if rate >= (threshold + 20.0) else "degraded"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "status": status,
            "severity": severity,
            "thresholdPct": float(round(threshold, 2)),
            "anomalyRatePct": float(round(rate, 2)),
            "remainingPct": float(round(remaining, 2)),
            "count": int(summary.get("count", 0) or 0),
            "anomalies": int(summary.get("anomalies", 0) or 0),
            "topAnomalyType": str(summary.get("topAnomalyType", "none")),
            "topPolicyCode": str(summary.get("topPolicyCode", "none")),
        },
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_breaches(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    threshold_pct: float = 35.0,
    buckets: int = 6,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    threshold = max(1.0, min(float(threshold_pct), 100.0))
    bucket_count = max(2, min(int(buckets), 24))
    trend = build_continuity_autopilot_posture_action_policy_anomaly_trend(session, session_id, window_ms=window, buckets=bucket_count)
    series = trend.get("series", []) if isinstance(trend.get("series"), list) else []
    breaches: list[dict[str, Any]] = []
    for item in series:
        if not isinstance(item, dict):
            continue
        rate = float(item.get("anomalyRatePct", 0.0) or 0.0)
        if rate <= threshold:
            continue
        over = float(rate - threshold)
        breaches.append(
            {
                "index": int(item.get("index", 0) or 0),
                "startTs": int(item.get("startTs", 0) or 0),
                "endTs": int(item.get("endTs", 0) or 0),
                "anomalyRatePct": float(round(rate, 2)),
                "overPct": float(round(over, 2)),
                "count": int(item.get("count", 0) or 0),
                "anomalies": int(item.get("anomalies", 0) or 0),
            }
        )
    top_over = 0.0
    for item in breaches:
        over = float(item.get("overPct", 0.0) or 0.0)
        if over > top_over:
            top_over = over
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "summary": {
            "thresholdPct": float(round(threshold, 2)),
            "breachCount": int(len(breaches)),
            "topOverPct": float(round(top_over, 2)),
        },
        "items": breaches,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    threshold_pct: float = 35.0,
    buckets: int = 6,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    threshold = max(1.0, min(float(threshold_pct), 100.0))
    bucket_count = max(2, min(int(buckets), 24))
    trend = build_continuity_autopilot_posture_action_policy_anomaly_trend(session, session_id, window_ms=window, buckets=bucket_count)
    series = trend.get("series", []) if isinstance(trend.get("series"), list) else []
    rates = [float(item.get("anomalyRatePct", 0.0) or 0.0) for item in series if isinstance(item, dict)]
    current_rate = rates[-1] if rates else 0.0
    previous_rate = rates[-2] if len(rates) >= 2 else current_rate
    slope = float(current_rate - previous_rate)
    projected_rate = max(0.0, min(100.0, current_rate + slope))
    projected_status = "within_budget" if projected_rate <= threshold else "exceeded"
    risk = "low"
    if projected_rate > threshold:
        risk = "high" if projected_rate >= (threshold + 20.0) else "medium"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "summary": {
            "thresholdPct": float(round(threshold, 2)),
            "currentRatePct": float(round(current_rate, 2)),
            "slopePct": float(round(slope, 2)),
            "projectedRatePct": float(round(projected_rate, 2)),
            "projectedStatus": projected_status,
            "risk": risk,
        },
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    window = max(60000, min(int(window_ms), 86400000))
    bucket_count = max(2, min(int(buckets), 24))
    thresholds = [20.0, 35.0, 50.0]
    items: list[dict[str, Any]] = []
    risk_counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0}
    for threshold in thresholds:
        forecast = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast(
            session,
            session_id,
            window_ms=window,
            threshold_pct=threshold,
            buckets=bucket_count,
        )
        summary = forecast.get("summary", {}) if isinstance(forecast.get("summary"), dict) else {}
        risk = str(summary.get("risk", "low"))
        if risk in risk_counts:
            risk_counts[risk] = int(risk_counts.get(risk, 0) or 0) + 1
        items.append(
            {
                "thresholdPct": float(summary.get("thresholdPct", threshold) or threshold),
                "currentRatePct": float(summary.get("currentRatePct", 0.0) or 0.0),
                "slopePct": float(summary.get("slopePct", 0.0) or 0.0),
                "projectedRatePct": float(summary.get("projectedRatePct", 0.0) or 0.0),
                "projectedStatus": str(summary.get("projectedStatus", "within_budget")),
                "risk": risk,
            }
        )
    top_risk = "low"
    if int(risk_counts.get("high", 0) or 0) > 0:
        top_risk = "high"
    elif int(risk_counts.get("medium", 0) or 0) > 0:
        top_risk = "medium"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "summary": {
            "rows": int(len(items)),
            "topRisk": top_risk,
            "riskCounts": risk_counts,
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    matrix = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    items = matrix.get("items", []) if isinstance(matrix.get("items"), list) else []
    recommendation = "normal"
    reason = "forecast_low_risk"
    target_threshold = 35.0
    high_rows = [item for item in items if isinstance(item, dict) and str(item.get("risk", "low")) == "high"]
    medium_rows = [item for item in items if isinstance(item, dict) and str(item.get("risk", "low")) == "medium"]
    if high_rows:
        recommendation = "safe"
        reason = "forecast_high_risk"
        target_threshold = 20.0
    elif medium_rows:
        recommendation = "normal"
        reason = "forecast_medium_risk"
        target_threshold = 35.0
    else:
        recommendation = "aggressive"
        reason = "forecast_budget_headroom"
        target_threshold = 50.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(matrix.get("windowMs", window_ms) or window_ms),
        "buckets": int(matrix.get("buckets", buckets) or buckets),
        "summary": {
            "recommendation": recommendation,
            "reason": reason,
            "targetThresholdPct": float(target_threshold),
            "topRisk": str((matrix.get("summary", {}) or {}).get("topRisk", "low")),
        },
        "matrix": matrix,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    guidance = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    summary = guidance.get("summary", {}) if isinstance(guidance.get("summary"), dict) else {}
    recommendation = str(summary.get("recommendation", "normal"))
    target_threshold = float(summary.get("targetThresholdPct", 35.0) or 35.0)
    actions: list[dict[str, Any]] = []
    if recommendation == "safe":
        actions.append({"priority": "p0", "command": "set continuity autopilot mode safe", "reason": "forecast guidance recommends conservative mode"})
        actions.append({"priority": "p1", "command": "show continuity autopilot posture actions policy anomalies budget breaches", "reason": "inspect active breach buckets"})
    elif recommendation == "aggressive":
        actions.append({"priority": "p1", "command": "set continuity autopilot mode aggressive", "reason": "forecast guidance indicates sustained budget headroom"})
        actions.append({"priority": "p2", "command": "show continuity autopilot mode policy aggressive", "reason": "confirm aggressive mode policy gate"})
    else:
        actions.append({"priority": "p1", "command": "set continuity autopilot mode normal", "reason": "forecast guidance recommends balanced mode"})
        actions.append({"priority": "p2", "command": "show continuity autopilot posture actions policy anomalies budget", "reason": "monitor budget status after mode alignment"})
    actions.append(
        {
            "priority": "p2",
            "command": f"show continuity autopilot posture actions policy anomalies budget forecast threshold {target_threshold:.0f}",
            "reason": "re-check projected budget posture at guidance threshold",
        }
    )
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(guidance.get("windowMs", window_ms) or window_ms),
        "buckets": int(guidance.get("buckets", buckets) or buckets),
        "summary": {
            "recommendation": recommendation,
            "targetThresholdPct": float(target_threshold),
            "count": int(len(actions)),
        },
        "actions": actions,
        "guidance": guidance.get("summary", {}),
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run(
    session: SessionState,
    session_id: str,
    index: int = 1,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    items = report.get("actions", []) if isinstance(report.get("actions"), list) else []
    idx = max(1, min(int(index), max(1, len(items) if items else 1)))
    action = items[idx - 1] if items and 0 <= (idx - 1) < len(items) else {}
    command = str(action.get("command", "") or "").strip()
    mapped_op: dict[str, Any] | None = None
    capability: dict[str, Any] = {"name": "-", "domain": "unknown", "risk": "low", "known": False}
    policy: dict[str, Any] = {"allowed": True, "reason": "no mapped op", "code": "no_op"}
    if command:
        envelope = compile_intent_envelope(command)
        writes = envelope.get("stateIntent", {}).get("writeOperations", []) if isinstance(envelope.get("stateIntent"), dict) else []
        if isinstance(writes, list) and writes:
            first = writes[0]
            if isinstance(first, dict):
                mapped_op = first
                capability = resolve_capability(first)
                policy = evaluate_policy(first, capability)
    return {
        "ok": True,
        "sessionId": session_id,
        "index": int(idx),
        "action": action,
        "mappedOp": mapped_op,
        "capability": capability,
        "policy": policy,
        "appliable": bool(policy.get("allowed", False) and mapped_op is not None),
    }


def apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action(
    session: SessionState,
    session_id: str,
    index: int = 1,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    dry_run = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run(
        session,
        session_id,
        index=index,
        window_ms=window_ms,
        buckets=buckets,
    )
    action = dry_run.get("action", {}) if isinstance(dry_run.get("action"), dict) else {}
    mapped_op = dry_run.get("mappedOp") if isinstance(dry_run.get("mappedOp"), dict) else None
    policy = dry_run.get("policy", {}) if isinstance(dry_run.get("policy"), dict) else {}
    if mapped_op is None:
        return {
            "index": int(dry_run.get("index", index) or index),
            "command": str(action.get("command", "-") or "-"),
            "applied": False,
            "changed": False,
            "reason": "no_mapped_op",
            "message": "Guidance action has no executable mapped operation.",
        }
    if not bool(policy.get("allowed", False)):
        return {
            "index": int(dry_run.get("index", index) or index),
            "command": str(action.get("command", "-") or "-"),
            "applied": False,
            "changed": False,
            "reason": str(policy.get("code", "blocked") or "blocked"),
            "message": str(policy.get("reason", "policy blocked")),
            "policy": policy,
        }
    op_result = run_operation(session, mapped_op)
    return {
        "index": int(dry_run.get("index", index) or index),
        "command": str(action.get("command", "-") or "-"),
        "applied": bool(op_result.get("ok", False)),
        "changed": bool(op_result.get("ok", False)),
        "reason": "applied" if bool(op_result.get("ok", False)) else "execution_failed",
        "message": str(op_result.get("message", "")),
        "result": op_result,
    }


def apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_batch(
    session: SessionState,
    session_id: str,
    limit: int = 3,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    count = max(1, min(int(limit), 10))
    items: list[dict[str, Any]] = []
    applied = 0
    changed = False
    for idx in range(1, count + 1):
        item = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action(
            session,
            session_id,
            index=idx,
            window_ms=window_ms,
            buckets=buckets,
        )
        items.append(item)
        if bool(item.get("applied", False)):
            applied += 1
            changed = True
    return {
        "attempted": int(count),
        "applied": int(applied),
        "changed": bool(changed),
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history(
    session: SessionState,
    session_id: str,
    limit: int = 30,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    items: list[dict[str, Any]] = []
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        items.append(
            {
                "id": str(entry.get("id", "")),
                "timestamp": int(entry.get("timestamp", 0) or 0),
                "op": op,
                "ok": bool(entry.get("ok", False)),
                "message": str(entry.get("message", "")),
                "policyCode": str(entry.get("policyCode", "ok")),
                "index": int(payload.get("index", 0) or 0),
                "limit": int(payload.get("limit", 0) or 0),
            }
        )
        if len(items) >= max(1, min(int(limit), 200)):
            break
    applied = len([item for item in items if bool(item.get("ok", False))])
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(items)),
            "applied": int(applied),
            "failed": int(len(items) - applied),
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    count = 0
    ok_count = 0
    op_counts: dict[str, int] = {}
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        count += 1
        if bool(entry.get("ok", False)):
            ok_count += 1
        op_counts[op] = int(op_counts.get(op, 0) or 0) + 1
    ok_pct = (float(ok_count) * 100.0 / float(count)) if count > 0 else 0.0
    top_op = "none"
    top_count = 0
    for key, value in op_counts.items():
        if int(value) > top_count:
            top_op = str(key)
            top_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(count),
            "applied": int(ok_count),
            "failed": int(count - ok_count),
            "appliedPct": float(round(ok_pct, 2)),
            "topOp": top_op,
        },
        "counts": {
            "ops": op_counts,
        },
    }


def detect_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies(
    session: SessionState,
    session_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    history = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history(session, session_id, limit=200)
    items = history.get("items", []) if isinstance(history.get("items"), list) else []
    anomalies: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    fail_streak = 0
    reason_streak: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        ok = bool(item.get("ok", False))
        reason = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
        ts = int(item.get("timestamp", now_ms()) or now_ms())
        if ok:
            fail_streak = 0
            reason_streak[reason] = 0
            continue
        fail_streak += 1
        if fail_streak >= 2:
            anomalies.append({"ts": ts, "type": "failure_streak", "detail": f"streak={fail_streak}", "reason": reason})
            fail_streak = 0
        reason_streak[reason] = int(reason_streak.get(reason, 0) or 0) + 1
        if int(reason_streak.get(reason, 0) or 0) >= 2 and reason != "ok":
            anomalies.append({"ts": ts, "type": "repeated_reason", "detail": reason, "reason": reason})
            reason_streak[reason] = 0
    for item in anomalies:
        kind = str(item.get("type", "unknown"))
        counts[kind] = int(counts.get(kind, 0) or 0) + 1
    top_type = "none"
    top_count = 0
    for key, value in counts.items():
        if int(value) > top_count:
            top_type = str(key)
            top_count = int(value)
    tail = anomalies[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {"count": int(len(tail)), "totalDetected": int(len(anomalies)), "topType": top_type},
        "counts": counts,
        "items": tail,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    bucket_count = max(2, min(int(buckets), 24))
    bucket_ms = max(1, int(window / bucket_count))
    start_ts = now_ms() - window
    series: list[dict[str, Any]] = []
    for idx in range(bucket_count):
        b_start = start_ts + (idx * bucket_ms)
        b_end = start_ts + ((idx + 1) * bucket_ms) if idx < (bucket_count - 1) else now_ms() + 1
        count = 0
        anomalies = 0
        for entry in session.journal:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("sessionId", "")).strip() != session_id:
                continue
            ts = int(entry.get("timestamp", 0) or 0)
            if ts < b_start or ts >= b_end:
                continue
            op = str(entry.get("op", "")).strip()
            if op not in target_ops:
                continue
            count += 1
            if not bool(entry.get("ok", False)):
                anomalies += 1
        rate = (float(anomalies) * 100.0 / float(count)) if count > 0 else 0.0
        series.append(
            {
                "index": int(idx + 1),
                "startTs": int(b_start),
                "endTs": int(b_end - 1),
                "count": int(count),
                "anomalies": int(anomalies),
                "anomalyRatePct": float(round(rate, 2)),
            }
        )
    trend = "stable"
    if len(series) >= 2:
        first = float(series[0].get("anomalyRatePct", 0.0) or 0.0)
        last = float(series[-1].get("anomalyRatePct", 0.0) or 0.0)
        if last > (first + 1.0):
            trend = "rising"
        elif last < (first - 1.0):
            trend = "falling"
    total_count = int(sum(int(item.get("count", 0) or 0) for item in series))
    total_anomalies = int(sum(int(item.get("anomalies", 0) or 0) for item in series))
    total_rate = (float(total_anomalies) * 100.0 / float(total_count)) if total_count > 0 else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "bucketMs": int(bucket_ms),
        "summary": {
            "count": int(total_count),
            "anomalies": int(total_anomalies),
            "anomalyRatePct": float(round(total_rate, 2)),
            "trend": trend,
        },
        "series": series,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics(
        session,
        session_id,
        window_ms=window_ms,
    )
    anomalies = detect_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies(
        session,
        session_id,
        limit=50,
    )
    trend = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    metrics_summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    anomalies_summary = anomalies.get("summary", {}) if isinstance(anomalies.get("summary"), dict) else {}
    trend_summary = trend.get("summary", {}) if isinstance(trend.get("summary"), dict) else {}
    anomaly_rate = float(trend_summary.get("anomalyRatePct", 0.0) or 0.0)
    health = "healthy"
    if anomaly_rate >= 25.0 or str(trend_summary.get("trend", "stable")) == "rising":
        health = "degraded"
    if anomaly_rate >= 50.0:
        health = "critical"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(trend.get("windowMs", window_ms) or window_ms),
        "buckets": int(trend.get("buckets", buckets) or buckets),
        "summary": {
            "health": health,
            "trend": str(trend_summary.get("trend", "stable")),
            "anomalyRatePct": float(round(anomaly_rate, 2)),
            "eventCount": int(metrics_summary.get("count", 0) or 0),
            "anomalies": int(anomalies_summary.get("count", 0) or 0),
            "topAnomalyType": str(anomalies_summary.get("topType", "none")),
            "appliedPct": float(metrics_summary.get("appliedPct", 0.0) or 0.0),
        },
        "metrics": metrics_summary,
        "anomalies": anomalies_summary,
        "trendSummary": trend_summary,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 8,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 30))
    cutoff = now_ms() - window
    reason_counts: dict[str, int] = {}
    op_counts: dict[str, int] = {}
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        if bool(entry.get("ok", False)):
            continue
        reason = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        reason_counts[reason] = int(reason_counts.get(reason, 0) or 0) + 1
        op_counts[op] = int(op_counts.get(op, 0) or 0) + 1
    offenders: list[dict[str, Any]] = []
    for reason, count in sorted(reason_counts.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]:
        offenders.append({"reason": reason, "count": int(count)})
    top_op = "none"
    top_op_count = 0
    for key, value in op_counts.items():
        if int(value) > top_op_count:
            top_op = str(key)
            top_op_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(sum(int(v) for v in reason_counts.values())),
            "offenderCount": int(len(offenders)),
            "topOp": top_op,
        },
        "counts": {
            "reasons": reason_counts,
            "ops": op_counts,
        },
        "offenders": offenders,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 20,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 100))
    cutoff = now_ms() - window
    items: list[dict[str, Any]] = []
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        if bool(entry.get("ok", False)):
            continue
        items.append(
            {
                "timestamp": int(ts),
                "op": op,
                "policyCode": str(entry.get("policyCode", "unknown") or "unknown"),
                "message": str(entry.get("policyReason", "") or ""),
            }
        )
        if len(items) >= top_n:
            break
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(len(items)),
            "latestTs": int(items[0]["timestamp"]) if items else None,
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 8,
) -> dict[str, Any]:
    state = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    timeline = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(3, min(int(limit), 12)),
    )
    state_summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    offenders_items = offenders.get("offenders", []) if isinstance(offenders.get("offenders"), list) else []
    timeline_items = timeline.get("items", []) if isinstance(timeline.get("items"), list) else []
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "buckets": int(buckets),
        "summary": {
            "health": str(state_summary.get("health", "healthy")),
            "trend": str(state_summary.get("trend", "stable")),
            "anomalyRatePct": float(state_summary.get("anomalyRatePct", 0.0) or 0.0),
            "topOp": str((offenders.get("summary", {}) or {}).get("topOp", "none")),
            "topReason": str(offenders_items[0].get("reason", "none")) if offenders_items else "none",
            "latestTs": int(timeline_items[0].get("timestamp", 0) or 0) if timeline_items else None,
        },
        "state": state_summary,
        "offenders": offenders_items[: min(len(offenders_items), int(limit))],
        "timeline": timeline_items[: min(len(timeline_items), max(3, min(int(limit), 12)))],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 20))
    cutoff = now_ms() - window
    matrix: dict[str, dict[str, int]] = {}
    row_totals: dict[str, int] = {}
    col_totals: dict[str, int] = {}
    total = 0
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        if bool(entry.get("ok", False)):
            continue
        reason = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        row = matrix.get(reason)
        if not isinstance(row, dict):
            row = {}
            matrix[reason] = row
        row[op] = int(row.get(op, 0) or 0) + 1
        row_totals[reason] = int(row_totals.get(reason, 0) or 0) + 1
        col_totals[op] = int(col_totals.get(op, 0) or 0) + 1
        total += 1
    top_rows = [k for k, _v in sorted(row_totals.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]]
    top_cols = [k for k, _v in sorted(col_totals.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]]
    rows: list[dict[str, Any]] = []
    for reason in top_rows:
        cols: list[dict[str, Any]] = []
        row = matrix.get(reason, {})
        for op in top_cols:
            cols.append({"op": op, "count": int(row.get(op, 0) or 0)})
        rows.append({"reason": reason, "count": int(row_totals.get(reason, 0) or 0), "cells": cols})
    top_reason = top_rows[0] if top_rows else "none"
    top_op = top_cols[0] if top_cols else "none"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(total),
            "reasons": int(len(row_totals)),
            "ops": int(len(col_totals)),
            "topReason": str(top_reason),
            "topOp": str(top_op),
        },
        "rows": rows,
        "columns": [{"op": op, "count": int(col_totals.get(op, 0) or 0)} for op in top_cols],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 5,
) -> dict[str, Any]:
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(1, min(int(limit), 20)),
    )
    summary = offenders.get("summary", {}) if isinstance(offenders.get("summary"), dict) else {}
    offender_items = offenders.get("offenders", []) if isinstance(offenders.get("offenders"), list) else []
    suggestions: list[dict[str, Any]] = []
    reason_to_actions: dict[str, list[str]] = {
        "policy_denied": [
            "set continuity autopilot mode safe",
            "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies summary",
            "show continuity autopilot mode policy matrix",
        ],
        "confirmation_required": [
            "dry run continuity autopilot posture actions policy anomalies budget forecast guidance action",
            "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies timeline",
        ],
        "unknown_capability": [
            "show continuity autopilot posture actions policy matrix",
            "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies matrix",
        ],
        "unknown": [
            "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies state",
            "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies timeline",
        ],
    }
    for item in offender_items[: max(1, min(int(limit), 20))]:
        if not isinstance(item, dict):
            continue
        reason = str(item.get("reason", "unknown") or "unknown").strip().lower() or "unknown"
        actions = reason_to_actions.get(reason) or reason_to_actions.get("unknown") or []
        suggestions.append(
            {
                "reason": reason,
                "count": int(item.get("count", 0) or 0),
                "actions": actions[:3],
            }
        )
    if not suggestions:
        suggestions.append(
            {
                "reason": "none",
                "count": 0,
                "actions": [
                    "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies state",
                ],
            }
        )
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "summary": {
            "count": int(summary.get("count", 0) or 0),
            "offenderCount": int(summary.get("offenderCount", len(offender_items)) or len(offender_items)),
            "topOp": str(summary.get("topOp", "none")),
            "suggestionCount": int(len(suggestions)),
        },
        "suggestions": suggestions,
    }


def session_mutation_fingerprint(session: SessionState) -> str:
    return json.dumps(
        {
            "graph": session.graph,
            "jobs": session.jobs,
            "autopilot": session.continuity_autopilot,
            "deadLetters": session.dead_letters,
            "faults": session.faults,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 5,
) -> dict[str, Any]:
    remediation = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    suggestions = remediation.get("suggestions", []) if isinstance(remediation.get("suggestions"), list) else []
    selected_command = ""
    selected_reason = "none"
    for item in suggestions:
        if not isinstance(item, dict):
            continue
        actions = item.get("actions", []) if isinstance(item.get("actions"), list) else []
        if actions:
            selected_command = str(actions[0]).strip()
            selected_reason = str(item.get("reason", "unknown"))
            break
    parsed: list[dict[str, Any]] = parse_commands(selected_command) if selected_command else []
    command_type = str(parsed[0].get("type", "none")) if parsed else "none"
    known = bool(command_type != "none" and command_type in CAPABILITY_REGISTRY)
    domain = str(CAPABILITY_REGISTRY.get(command_type, {}).get("domain", "system")) if known else "system"
    risk = str(CAPABILITY_REGISTRY.get(command_type, {}).get("risk", "low")) if known else "low"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "summary": {
            "selectedReason": selected_reason,
            "selectedCommand": selected_command or "none",
            "selectedType": command_type,
            "knownCommand": bool(known),
        },
        "dryRun": {
            "reason": selected_reason,
            "command": selected_command or "none",
            "type": command_type,
            "domain": domain,
            "risk": risk,
            "canExecute": bool(known),
        },
    }


def apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 5,
) -> dict[str, Any]:
    dry = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    summary = dry.get("summary", {}) if isinstance(dry.get("summary"), dict) else {}
    selected_command = str(summary.get("selectedCommand", "") or "").strip()
    selected_type = str(summary.get("selectedType", "none") or "none")
    if not selected_command or selected_type == "none":
        return {
            "applied": False,
            "changed": False,
            "reason": "no_action",
            "message": "No remediation command available.",
            "selectedCommand": selected_command or "none",
            "selectedType": selected_type,
            "policy": {"allowed": False, "code": "unknown_capability", "reason": "no selected remediation command"},
            "capability": {"name": selected_type, "domain": "unknown", "risk": "high", "known": False},
            "diff": zero_diff(),
        }
    nested_ops = parse_commands(selected_command)
    if not nested_ops:
        return {
            "applied": False,
            "changed": False,
            "reason": "parse_failed",
            "message": "Selected remediation command could not be parsed.",
            "selectedCommand": selected_command,
            "selectedType": selected_type,
            "policy": {"allowed": False, "code": "unknown_capability", "reason": "selected command did not parse"},
            "capability": {"name": selected_type, "domain": "unknown", "risk": "high", "known": False},
            "diff": zero_diff(),
        }
    nested = nested_ops[0]
    nested_type = str(nested.get("type", "")).strip()
    if nested_type in {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run",
    }:
        return {
            "applied": False,
            "changed": False,
            "reason": "recursive_block",
            "message": "Selected remediation command is recursive and was blocked.",
            "selectedCommand": selected_command,
            "selectedType": nested_type or selected_type,
            "policy": {"allowed": False, "code": "recursive_block", "reason": "recursive remediation command blocked"},
            "capability": {"name": nested_type or selected_type, "domain": "system", "risk": "low", "known": True},
            "diff": zero_diff(),
        }
    capability = resolve_capability(nested)
    policy = evaluate_policy(nested, capability)
    before_counts = memory_counts(session.memory)
    before_fp = session_mutation_fingerprint(session)
    if not bool(policy.get("allowed", False)):
        return {
            "applied": False,
            "changed": False,
            "reason": "policy_denied",
            "message": str(policy.get("reason", "policy denied")),
            "selectedCommand": selected_command,
            "selectedType": nested_type,
            "policy": policy,
            "capability": capability,
            "diff": zero_diff(),
        }
    before_graph = copy.deepcopy(session.graph)
    before_jobs = copy.deepcopy(session.jobs)
    op_result = run_operation(session, nested)
    if bool(op_result.get("ok", False)):
        violations = validate_graph_contract(session.graph)
        if violations:
            session.graph = before_graph
            session.jobs = before_jobs
            op_result = {
                "ok": False,
                "message": "Graph contract violation blocked mutation.",
                "previewLines": violations[:5],
            }
    session.memory = graph_to_memory(session.graph)
    after_counts = memory_counts(session.memory)
    after_fp = session_mutation_fingerprint(session)
    changed = bool(before_fp != after_fp)
    return {
        "applied": bool(op_result.get("ok", False)),
        "changed": bool(changed),
        "reason": "applied" if bool(op_result.get("ok", False)) else "execution_failed",
        "message": str(op_result.get("message", "")),
        "selectedCommand": selected_command,
        "selectedType": nested_type,
        "policy": policy,
        "capability": capability,
        "diff": counts_diff(before_counts, after_counts),
        "result": op_result,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history(
    session: SessionState,
    session_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    max_items = max(1, min(int(limit), 200))
    items: list[dict[str, Any]] = []
    applied = 0
    failed = 0
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        ok = bool(entry.get("ok", False))
        if ok:
            applied += 1
        else:
            failed += 1
        items.append(
            {
                "ts": int(entry.get("timestamp", 0) or 0),
                "ok": bool(ok),
                "message": str(entry.get("message", "")),
                "policyCode": str(entry.get("policyCode", "unknown") or "unknown"),
                "op": op,
            }
        )
        if len(items) >= max_items:
            break
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(items)),
            "applied": int(applied),
            "failed": int(failed),
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    count = 0
    applied = 0
    failed = 0
    policy_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        if int(entry.get("timestamp", 0) or 0) < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        count += 1
        ok = bool(entry.get("ok", False))
        if ok:
            applied += 1
        else:
            failed += 1
        code = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        policy_counts[code] = int(policy_counts.get(code, 0) or 0) + 1
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        type_key = str(payload.get("selectedType", "unknown") or "unknown").strip().lower() or "unknown"
        type_counts[type_key] = int(type_counts.get(type_key, 0) or 0) + 1
    top_policy = "none"
    top_policy_count = 0
    for key, value in policy_counts.items():
        if int(value) > top_policy_count:
            top_policy = str(key)
            top_policy_count = int(value)
    applied_pct = (float(applied) / float(count) * 100.0) if count > 0 else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(count),
            "applied": int(applied),
            "failed": int(failed),
            "appliedPct": float(round(applied_pct, 2)),
            "topPolicyCode": top_policy,
        },
        "counts": {
            "policyCodes": policy_counts,
            "types": type_counts,
        },
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 20,
) -> dict[str, Any]:
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
        session,
        session_id,
        window_ms=window_ms,
    )
    history = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history(
        session,
        session_id,
        limit=limit,
    )
    summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    applied_pct = float(summary.get("appliedPct", 0.0) or 0.0)
    health = "healthy"
    if applied_pct < 80.0:
        health = "degraded"
    if applied_pct < 50.0:
        health = "critical"
    items = history.get("items", []) if isinstance(history.get("items"), list) else []
    half = max(1, int(len(items) // 2))
    newer = items[:half]
    older = items[half:]
    newer_fail = sum(1 for item in newer if isinstance(item, dict) and not bool(item.get("ok", False)))
    older_fail = sum(1 for item in older if isinstance(item, dict) and not bool(item.get("ok", False)))
    trend = "stable"
    if newer_fail > older_fail:
        trend = "worsening"
    elif newer_fail < older_fail:
        trend = "improving"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(metrics.get("windowMs", window_ms) or window_ms),
        "summary": {
            "health": health,
            "trend": trend,
            "count": int(summary.get("count", 0) or 0),
            "applied": int(summary.get("applied", 0) or 0),
            "failed": int(summary.get("failed", 0) or 0),
            "appliedPct": float(round(applied_pct, 2)),
            "topPolicyCode": str(summary.get("topPolicyCode", "none")),
        },
        "metrics": summary,
        "historySummary": history.get("summary", {}),
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    window = max(60000, min(int(window_ms), 86400000))
    bucket_count = max(2, min(int(buckets), 24))
    cutoff = now_ms() - window
    bucket_size = max(1, int(window // bucket_count))
    series: list[dict[str, Any]] = []
    for idx in range(bucket_count):
        series.append({"index": idx, "count": 0, "applied": 0, "failed": 0, "appliedPct": 0.0})
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        if str(entry.get("op", "")).strip() not in target_ops:
            continue
        idx = int((ts - cutoff) // bucket_size)
        if idx < 0:
            idx = 0
        if idx >= bucket_count:
            idx = bucket_count - 1
        bucket = series[idx]
        bucket["count"] = int(bucket.get("count", 0) or 0) + 1
        if bool(entry.get("ok", False)):
            bucket["applied"] = int(bucket.get("applied", 0) or 0) + 1
        else:
            bucket["failed"] = int(bucket.get("failed", 0) or 0) + 1
    for bucket in series:
        c = int(bucket.get("count", 0) or 0)
        a = int(bucket.get("applied", 0) or 0)
        bucket["appliedPct"] = float(round((float(a) / float(c) * 100.0) if c > 0 else 0.0, 2))
    first = series[0]["appliedPct"] if series else 0.0
    last = series[-1]["appliedPct"] if series else 0.0
    trend = "stable"
    if last > first + 5.0:
        trend = "improving"
    elif last + 5.0 < first:
        trend = "worsening"
    total = sum(int(item.get("count", 0) or 0) for item in series)
    applied = sum(int(item.get("applied", 0) or 0) for item in series)
    failed = sum(int(item.get("failed", 0) or 0) for item in series)
    applied_pct = (float(applied) / float(total) * 100.0) if total > 0 else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "summary": {
            "count": int(total),
            "applied": int(applied),
            "failed": int(failed),
            "appliedPct": float(round(applied_pct, 2)),
            "trend": trend,
        },
        "series": series,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 8,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 30))
    cutoff = now_ms() - window
    policy_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    total = 0
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        if int(entry.get("timestamp", 0) or 0) < cutoff:
            continue
        if str(entry.get("op", "")).strip() not in target_ops:
            continue
        if bool(entry.get("ok", False)):
            continue
        total += 1
        code = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        policy_counts[code] = int(policy_counts.get(code, 0) or 0) + 1
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        selected_type = str(payload.get("selectedType", "unknown") or "unknown").strip().lower() or "unknown"
        type_counts[selected_type] = int(type_counts.get(selected_type, 0) or 0) + 1
    offenders: list[dict[str, Any]] = []
    for code, count in sorted(policy_counts.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]:
        offenders.append({"policyCode": code, "count": int(count)})
    top_type = "none"
    top_type_count = 0
    for key, value in type_counts.items():
        if int(value) > top_type_count:
            top_type = str(key)
            top_type_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(total),
            "offenderCount": int(len(offenders)),
            "topType": top_type,
        },
        "counts": {
            "policyCodes": policy_counts,
            "types": type_counts,
        },
        "offenders": offenders,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 8,
) -> dict[str, Any]:
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
        session,
        session_id,
        window_ms=window_ms,
    )
    state = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    trend = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    metrics_summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    state_summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    trend_summary = trend.get("summary", {}) if isinstance(trend.get("summary"), dict) else {}
    offenders_summary = offenders.get("summary", {}) if isinstance(offenders.get("summary"), dict) else {}
    offender_items = offenders.get("offenders", []) if isinstance(offenders.get("offenders"), list) else []
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(metrics.get("windowMs", window_ms) or window_ms),
        "buckets": int(trend.get("buckets", buckets) or buckets),
        "summary": {
            "health": str(state_summary.get("health", "healthy")),
            "trend": str(trend_summary.get("trend", "stable")),
            "count": int(metrics_summary.get("count", 0) or 0),
            "applied": int(metrics_summary.get("applied", 0) or 0),
            "failed": int(metrics_summary.get("failed", 0) or 0),
            "appliedPct": float(metrics_summary.get("appliedPct", 0.0) or 0.0),
            "topPolicyCode": str(metrics_summary.get("topPolicyCode", "none")),
            "topType": str(offenders_summary.get("topType", "none")),
            "offenderCount": int(offenders_summary.get("offenderCount", 0) or 0),
        },
        "metrics": metrics_summary,
        "state": state_summary,
        "trendSummary": trend_summary,
        "offenders": offender_items[: min(len(offender_items), int(limit))],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 20,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 200))
    cutoff = now_ms() - window
    items: list[dict[str, Any]] = []
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        if str(entry.get("op", "")).strip() not in target_ops:
            continue
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        items.append(
            {
                "timestamp": int(ts),
                "ok": bool(entry.get("ok", False)),
                "policyCode": str(entry.get("policyCode", "unknown") or "unknown"),
                "selectedType": str(payload.get("selectedType", "unknown") or "unknown"),
                "selectedCommand": str(payload.get("selectedCommand", "") or ""),
            }
        )
        if len(items) >= top_n:
            break
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(len(items)),
            "latestTs": int(items[0]["timestamp"]) if items else None,
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
    }
    window = max(60000, min(int(window_ms), 86400000))
    top_n = max(1, min(int(limit), 20))
    cutoff = now_ms() - window
    matrix: dict[str, dict[str, int]] = {}
    row_totals: dict[str, int] = {}
    col_totals: dict[str, int] = {}
    total = 0
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        if int(entry.get("timestamp", 0) or 0) < cutoff:
            continue
        if str(entry.get("op", "")).strip() not in target_ops:
            continue
        code = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        selected_type = str(payload.get("selectedType", "unknown") or "unknown").strip().lower() or "unknown"
        row = matrix.get(code)
        if not isinstance(row, dict):
            row = {}
            matrix[code] = row
        row[selected_type] = int(row.get(selected_type, 0) or 0) + 1
        row_totals[code] = int(row_totals.get(code, 0) or 0) + 1
        col_totals[selected_type] = int(col_totals.get(selected_type, 0) or 0) + 1
        total += 1
    top_rows = [k for k, _v in sorted(row_totals.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]]
    top_cols = [k for k, _v in sorted(col_totals.items(), key=lambda kv: int(kv[1]), reverse=True)[:top_n]]
    rows: list[dict[str, Any]] = []
    for code in top_rows:
        row = matrix.get(code, {})
        cells: list[dict[str, Any]] = []
        for t in top_cols:
            cells.append({"selectedType": t, "count": int(row.get(t, 0) or 0)})
        rows.append({"policyCode": code, "count": int(row_totals.get(code, 0) or 0), "cells": cells})
    top_policy = top_rows[0] if top_rows else "none"
    top_type = top_cols[0] if top_cols else "none"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(total),
            "policyCodes": int(len(row_totals)),
            "selectedTypes": int(len(col_totals)),
            "topPolicyCode": str(top_policy),
            "topType": str(top_type),
        },
        "rows": rows,
        "columns": [{"selectedType": t, "count": int(col_totals.get(t, 0) or 0)} for t in top_cols],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 5,
) -> dict[str, Any]:
    state = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(5, min(int(limit) * 4, 200)),
    )
    trend = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(3, min(int(limit), 20)),
    )
    state_summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    trend_summary = trend.get("summary", {}) if isinstance(trend.get("summary"), dict) else {}
    offenders_summary = offenders.get("summary", {}) if isinstance(offenders.get("summary"), dict) else {}
    offender_items = offenders.get("offenders", []) if isinstance(offenders.get("offenders"), list) else []
    guidance: list[dict[str, Any]] = []

    if str(state_summary.get("health", "healthy")) != "healthy":
        guidance.append(
            {
                "priority": "high",
                "reason": "remediation health degraded",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation summary",
            }
        )
    if str(trend_summary.get("trend", "stable")) == "worsening":
        guidance.append(
            {
                "priority": "high",
                "reason": "remediation trend worsening",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation timeline",
            }
        )
    top_policy = str((offender_items[0].get("policyCode", "none")) if offender_items else "none")
    if top_policy not in {"none", "ok", ""}:
        guidance.append(
            {
                "priority": "medium",
                "reason": f"top remediation offender policy {top_policy}",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation offenders",
            }
        )
    if int(offenders_summary.get("count", 0) or 0) == 0:
        guidance.append(
            {
                "priority": "low",
                "reason": "no remediation failures in window",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation metrics",
            }
        )
    if not guidance:
        guidance.append(
            {
                "priority": "low",
                "reason": "state stable",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation state",
            }
        )
    max_items = max(1, min(int(limit), 20))
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "buckets": int(buckets),
        "summary": {
            "health": str(state_summary.get("health", "healthy")),
            "trend": str(trend_summary.get("trend", "stable")),
            "topPolicyCode": str(state_summary.get("topPolicyCode", "none")),
            "offenderCount": int(offenders_summary.get("offenderCount", 0) or 0),
            "guidanceCount": int(min(len(guidance), max_items)),
        },
        "guidance": guidance[:max_items],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 5,
) -> dict[str, Any]:
    report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
        limit=limit,
    )
    guidance = report.get("guidance", []) if isinstance(report.get("guidance"), list) else []
    actions: list[dict[str, Any]] = []
    for idx, item in enumerate(guidance[: max(1, min(int(limit), 20))], start=1):
        if not isinstance(item, dict):
            continue
        command = str(item.get("command", "") or "").strip()
        actions.append(
            {
                "index": int(idx),
                "priority": str(item.get("priority", "low")),
                "reason": str(item.get("reason", "")),
                "command": command,
            }
        )
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(report.get("windowMs", window_ms) or window_ms),
        "buckets": int(report.get("buckets", buckets) or buckets),
        "summary": {
            **(report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}),
            "actionCount": int(len(actions)),
        },
        "actions": actions,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run(
    session: SessionState,
    session_id: str,
    index: int = 1,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 5,
) -> dict[str, Any]:
    report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
        limit=limit,
    )
    items = report.get("actions", []) if isinstance(report.get("actions"), list) else []
    idx = max(1, min(int(index), max(1, len(items) if items else 1)))
    action = items[idx - 1] if items and 0 <= (idx - 1) < len(items) else {}
    command = str(action.get("command", "") or "").strip()
    mapped_op: dict[str, Any] | None = None
    capability: dict[str, Any] = {"name": "-", "domain": "unknown", "risk": "low", "known": False}
    policy: dict[str, Any] = {"allowed": True, "reason": "no mapped op", "code": "no_op"}
    if command:
        envelope = compile_intent_envelope(command)
        writes = envelope.get("stateIntent", {}).get("writeOperations", []) if isinstance(envelope.get("stateIntent"), dict) else []
        if isinstance(writes, list) and writes:
            first = writes[0]
            if isinstance(first, dict):
                mapped_op = first
                capability = resolve_capability(first)
                policy = evaluate_policy(first, capability)
    return {
        "ok": True,
        "sessionId": session_id,
        "index": int(idx),
        "action": action,
        "mappedOp": mapped_op,
        "capability": capability,
        "policy": policy,
        "appliable": bool(policy.get("allowed", False) and mapped_op is not None),
    }


def apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action(
    session: SessionState,
    session_id: str,
    index: int = 1,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 5,
) -> dict[str, Any]:
    dry_run = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run(
        session,
        session_id,
        index=index,
        window_ms=window_ms,
        buckets=buckets,
        limit=limit,
    )
    action = dry_run.get("action", {}) if isinstance(dry_run.get("action"), dict) else {}
    mapped_op = dry_run.get("mappedOp") if isinstance(dry_run.get("mappedOp"), dict) else None
    capability = dry_run.get("capability", {}) if isinstance(dry_run.get("capability"), dict) else {}
    policy = dry_run.get("policy", {}) if isinstance(dry_run.get("policy"), dict) else {}
    selected_command = str(action.get("command", "") or "").strip()
    selected_type = str(mapped_op.get("type", "none") if isinstance(mapped_op, dict) else "none")
    if mapped_op is None:
        return {
            "index": int(dry_run.get("index", index) or index),
            "selectedCommand": selected_command or "none",
            "selectedType": selected_type,
            "applied": False,
            "changed": False,
            "reason": "no_mapped_op",
            "message": "Guidance action has no executable mapped operation.",
            "policy": {"allowed": False, "code": "no_op", "reason": "no mapped op"},
            "capability": capability,
            "diff": zero_diff(),
        }
    if not bool(policy.get("allowed", False)):
        return {
            "index": int(dry_run.get("index", index) or index),
            "selectedCommand": selected_command or "none",
            "selectedType": selected_type,
            "applied": False,
            "changed": False,
            "reason": str(policy.get("code", "policy_denied") or "policy_denied"),
            "message": str(policy.get("reason", "policy denied")),
            "policy": policy,
            "capability": capability,
            "diff": zero_diff(),
        }
    before_counts = memory_counts(session.memory)
    before_fp = session_mutation_fingerprint(session)
    before_graph = copy.deepcopy(session.graph)
    before_jobs = copy.deepcopy(session.jobs)
    op_result = run_operation(session, mapped_op)
    if bool(op_result.get("ok", False)):
        violations = validate_graph_contract(session.graph)
        if violations:
            session.graph = before_graph
            session.jobs = before_jobs
            op_result = {
                "ok": False,
                "message": "Graph contract violation blocked mutation.",
                "previewLines": violations[:5],
            }
    session.memory = graph_to_memory(session.graph)
    after_counts = memory_counts(session.memory)
    after_fp = session_mutation_fingerprint(session)
    changed = bool(before_fp != after_fp)
    return {
        "index": int(dry_run.get("index", index) or index),
        "selectedCommand": selected_command or "none",
        "selectedType": selected_type,
        "applied": bool(op_result.get("ok", False)),
        "changed": bool(changed),
        "reason": "applied" if bool(op_result.get("ok", False)) else "execution_failed",
        "message": str(op_result.get("message", "")),
        "policy": policy,
        "capability": capability,
        "diff": counts_diff(before_counts, after_counts),
        "result": op_result,
    }


def apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_batch(
    session: SessionState,
    session_id: str,
    limit: int = 3,
    window_ms: int = 3600000,
    buckets: int = 6,
    guidance_limit: int = 5,
) -> dict[str, Any]:
    count = max(1, min(int(limit), 20))
    before_counts = memory_counts(session.memory)
    items: list[dict[str, Any]] = []
    applied = 0
    changed = False
    for idx in range(1, count + 1):
        item = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action(
            session,
            session_id,
            index=idx,
            window_ms=window_ms,
            buckets=buckets,
            limit=guidance_limit,
        )
        items.append(item)
        if bool(item.get("applied", False)):
            applied += 1
        if bool(item.get("changed", False)):
            changed = True
    session.memory = graph_to_memory(session.graph)
    after_counts = memory_counts(session.memory)
    return {
        "attempted": int(count),
        "applied": int(applied),
        "changed": bool(changed),
        "items": items,
        "diff": counts_diff(before_counts, after_counts),
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history(
    session: SessionState,
    session_id: str,
    limit: int = 20,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    max_items = max(1, min(int(limit), 200))
    items: list[dict[str, Any]] = []
    applied = 0
    failed = 0
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        ok = bool(entry.get("ok", False))
        if ok:
            applied += 1
        else:
            failed += 1
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        items.append(
            {
                "ts": int(entry.get("timestamp", 0) or 0),
                "ok": bool(ok),
                "message": str(entry.get("message", "")),
                "policyCode": str(entry.get("policyCode", "unknown") or "unknown"),
                "op": op,
                "attempted": int(payload.get("attempted", 0) or 0),
                "applied": int(payload.get("applied", 0) or 0),
            }
        )
        if len(items) >= max_items:
            break
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(items)),
            "applied": int(applied),
            "failed": int(failed),
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    count = 0
    applied = 0
    failed = 0
    batch_attempted = 0
    batch_applied = 0
    op_counts: dict[str, int] = {}
    policy_counts: dict[str, int] = {}
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        if int(entry.get("timestamp", 0) or 0) < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        count += 1
        ok = bool(entry.get("ok", False))
        if ok:
            applied += 1
        else:
            failed += 1
        attempted = int(payload.get("attempted", 0) or 0)
        op_applied = int(payload.get("applied", 0) or 0)
        batch_attempted += attempted
        batch_applied += op_applied
        op_counts[op] = int(op_counts.get(op, 0) or 0) + 1
        policy_code = str(entry.get("policyCode", "unknown") or "unknown")
        policy_counts[policy_code] = int(policy_counts.get(policy_code, 0) or 0) + 1
    applied_pct = (float(applied) * 100.0 / float(count)) if count > 0 else 0.0
    batch_applied_pct = (float(batch_applied) * 100.0 / float(batch_attempted)) if batch_attempted > 0 else 0.0
    top_policy = "none"
    top_policy_count = 0
    for key, value in policy_counts.items():
        if int(value) > top_policy_count:
            top_policy = str(key)
            top_policy_count = int(value)
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(count),
            "applied": int(applied),
            "failed": int(failed),
            "appliedPct": float(round(applied_pct, 2)),
            "batchAttempted": int(batch_attempted),
            "batchApplied": int(batch_applied),
            "batchAppliedPct": float(round(batch_applied_pct, 2)),
            "topPolicyCode": top_policy,
        },
        "counts": {
            "ops": op_counts,
            "policyCodes": policy_counts,
        },
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 20,
) -> dict[str, Any]:
    metrics = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics(
        session,
        session_id,
        window_ms=window_ms,
    )
    history = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history(
        session,
        session_id,
        limit=limit,
    )
    metrics_summary = metrics.get("summary", {}) if isinstance(metrics.get("summary"), dict) else {}
    history_summary = history.get("summary", {}) if isinstance(history.get("summary"), dict) else {}
    applied_pct = float(metrics_summary.get("appliedPct", 0.0) or 0.0)
    failed = int(metrics_summary.get("failed", 0) or 0)
    health = "healthy"
    if failed >= 3 or applied_pct < 50.0:
        health = "degraded"
    elif failed >= 1 or applied_pct < 80.0:
        health = "watch"
    trend = "stable"
    if failed > int(history_summary.get("failed", 0) or 0):
        trend = "worsening"
    elif applied_pct >= 90.0 and failed == 0:
        trend = "improving"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(metrics.get("windowMs", window_ms) or window_ms),
        "summary": {
            "health": health,
            "trend": trend,
            "count": int(metrics_summary.get("count", 0) or 0),
            "applied": int(metrics_summary.get("applied", 0) or 0),
            "failed": int(metrics_summary.get("failed", 0) or 0),
            "appliedPct": float(applied_pct),
            "topPolicyCode": str(metrics_summary.get("topPolicyCode", "none")),
        },
        "metrics": metrics_summary,
        "history": history_summary,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    bucket_count = max(2, min(int(buckets), 24))
    end_ts = now_ms()
    start_ts = end_ts - window
    bucket_ms = max(1, int(window // bucket_count))
    series: list[dict[str, Any]] = []
    for idx in range(bucket_count):
        b_start = start_ts + idx * bucket_ms
        b_end = end_ts if idx == (bucket_count - 1) else b_start + bucket_ms
        count = 0
        applied = 0
        failed = 0
        for entry in session.journal:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("sessionId", "")).strip() != session_id:
                continue
            op = str(entry.get("op", "")).strip()
            if op not in target_ops:
                continue
            ts = int(entry.get("timestamp", 0) or 0)
            if ts < b_start or ts >= b_end:
                continue
            count += 1
            if bool(entry.get("ok", False)):
                applied += 1
            else:
                failed += 1
        applied_pct = (float(applied) * 100.0 / float(count)) if count > 0 else 0.0
        series.append(
            {
                "index": int(idx),
                "start": int(b_start),
                "end": int(b_end),
                "count": int(count),
                "applied": int(applied),
                "failed": int(failed),
                "appliedPct": float(round(applied_pct, 2)),
            }
        )
    total_count = sum(int(item.get("count", 0) or 0) for item in series)
    total_applied = sum(int(item.get("applied", 0) or 0) for item in series)
    total_failed = sum(int(item.get("failed", 0) or 0) for item in series)
    total_applied_pct = (float(total_applied) * 100.0 / float(total_count)) if total_count > 0 else 0.0
    trend = "stable"
    if len(series) >= 2:
        first_pct = float(series[0].get("appliedPct", 0.0) or 0.0)
        last_pct = float(series[-1].get("appliedPct", 0.0) or 0.0)
        if (last_pct - first_pct) >= 10.0:
            trend = "improving"
        elif (first_pct - last_pct) >= 10.0:
            trend = "worsening"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "buckets": int(bucket_count),
        "summary": {
            "count": int(total_count),
            "applied": int(total_applied),
            "failed": int(total_failed),
            "appliedPct": float(round(total_applied_pct, 2)),
            "trend": trend,
        },
        "series": series,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 8,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    counts: dict[str, int] = {}
    op_counts: dict[str, int] = {}
    total = 0
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        if int(entry.get("timestamp", 0) or 0) < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        total += 1
        op_counts[op] = int(op_counts.get(op, 0) or 0) + 1
        if bool(entry.get("ok", False)):
            continue
        code = str(entry.get("policyCode", "unknown") or "unknown").strip().lower() or "unknown"
        counts[code] = int(counts.get(code, 0) or 0) + 1
    offenders: list[dict[str, Any]] = []
    for key, value in sorted(counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
        offenders.append({"policyCode": str(key), "count": int(value)})
    top_op = "none"
    top_op_count = 0
    for key, value in op_counts.items():
        if int(value) > top_op_count:
            top_op = str(key)
            top_op_count = int(value)
    max_items = max(1, min(int(limit), 30))
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(total),
            "offenderCount": int(len(offenders)),
            "topOp": top_op,
        },
        "offenders": offenders[:max_items],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 8,
) -> dict[str, Any]:
    state = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(20, min(int(limit) * 3, 200)),
    )
    trend = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
    )
    offenders = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders(
        session,
        session_id,
        window_ms=window_ms,
        limit=limit,
    )
    state_summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    trend_summary = trend.get("summary", {}) if isinstance(trend.get("summary"), dict) else {}
    offenders_summary = offenders.get("summary", {}) if isinstance(offenders.get("summary"), dict) else {}
    offenders_items = offenders.get("offenders", []) if isinstance(offenders.get("offenders"), list) else []
    top_policy = str((offenders_items[0].get("policyCode", "none")) if offenders_items else "none")
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "buckets": int(buckets),
        "summary": {
            "health": str(state_summary.get("health", "healthy")),
            "trend": str(trend_summary.get("trend", "stable")),
            "count": int(state_summary.get("count", 0) or 0),
            "applied": int(state_summary.get("applied", 0) or 0),
            "failed": int(state_summary.get("failed", 0) or 0),
            "appliedPct": float(state_summary.get("appliedPct", 0.0) or 0.0),
            "topPolicyCode": top_policy,
            "offenderCount": int(offenders_summary.get("offenderCount", 0) or 0),
        },
        "state": state_summary,
        "trendSummary": trend_summary,
        "offenders": offenders_items[: max(1, min(int(limit), 30))],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 20,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    max_items = max(1, min(int(limit), 200))
    items: list[dict[str, Any]] = []
    latest_ts = 0
    for entry in reversed(session.journal):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        latest_ts = max(latest_ts, ts)
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        items.append(
            {
                "ts": int(ts),
                "ok": bool(entry.get("ok", False)),
                "op": op,
                "policyCode": str(entry.get("policyCode", "unknown") or "unknown"),
                "attempted": int(payload.get("attempted", 0) or 0),
                "applied": int(payload.get("applied", 0) or 0),
                "message": str(entry.get("message", "")),
            }
        )
        if len(items) >= max_items:
            break
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(len(items)),
            "latestTs": int(latest_ts),
        },
        "items": items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    limit: int = 6,
) -> dict[str, Any]:
    target_ops = {
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
    }
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    rows: dict[str, int] = {}
    cols: dict[str, int] = {}
    cells: dict[tuple[str, str], int] = {}
    total = 0
    for entry in session.journal:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("sessionId", "")).strip() != session_id:
            continue
        ts = int(entry.get("timestamp", 0) or 0)
        if ts < cutoff:
            continue
        op = str(entry.get("op", "")).strip()
        if op not in target_ops:
            continue
        total += 1
        payload = entry.get("payload", {}) if isinstance(entry.get("payload"), dict) else {}
        policy_code = str(entry.get("policyCode", "unknown") or "unknown")
        selected_type = str(payload.get("selectedType", "none") or "none")
        rows[policy_code] = int(rows.get(policy_code, 0) or 0) + 1
        cols[selected_type] = int(cols.get(selected_type, 0) or 0) + 1
        key = (policy_code, selected_type)
        cells[key] = int(cells.get(key, 0) or 0) + 1
    max_items = max(1, min(int(limit), 20))
    row_items = [
        {"policyCode": key, "count": int(value)}
        for key, value in sorted(rows.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ][:max_items]
    col_items = [
        {"selectedType": key, "count": int(value)}
        for key, value in sorted(cols.items(), key=lambda item: (-int(item[1]), str(item[0])))
    ][:max_items]
    matrix_items = [
        {"policyCode": k[0], "selectedType": k[1], "count": int(v)}
        for k, v in sorted(cells.items(), key=lambda item: (-int(item[1]), str(item[0][0]), str(item[0][1])))
    ][: max_items * 2]
    top_policy = row_items[0]["policyCode"] if row_items else "none"
    top_type = col_items[0]["selectedType"] if col_items else "none"
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(total),
            "policyCodes": int(len(rows)),
            "selectedTypes": int(len(cols)),
            "topPolicyCode": str(top_policy),
            "topType": str(top_type),
        },
        "rows": row_items,
        "columns": col_items,
        "matrix": matrix_items,
    }


def build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance(
    session: SessionState,
    session_id: str,
    window_ms: int = 3600000,
    buckets: int = 6,
    limit: int = 5,
) -> dict[str, Any]:
    summary = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary(
        session,
        session_id,
        window_ms=window_ms,
        buckets=buckets,
        limit=max(5, min(int(limit) * 2, 30)),
    )
    state = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
        session,
        session_id,
        window_ms=window_ms,
        limit=max(20, min(int(limit) * 5, 200)),
    )
    summary_state = summary.get("summary", {}) if isinstance(summary.get("summary"), dict) else {}
    state_summary = state.get("summary", {}) if isinstance(state.get("summary"), dict) else {}
    guidance: list[dict[str, Any]] = []
    if str(summary_state.get("health", "healthy")) != "healthy":
        guidance.append(
            {
                "priority": "high",
                "reason": "guidance-action posture is not healthy",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions summary",
            }
        )
    if str(summary_state.get("trend", "stable")) == "worsening":
        guidance.append(
            {
                "priority": "high",
                "reason": "guidance-action trend is worsening",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions timeline",
            }
        )
    top_policy = str(summary_state.get("topPolicyCode", "none") or "none")
    if top_policy not in {"none", "ok", ""}:
        guidance.append(
            {
                "priority": "medium",
                "reason": f"top offender policy code is {top_policy}",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions offenders",
            }
        )
    if float(state_summary.get("appliedPct", 0.0) or 0.0) < 80.0:
        guidance.append(
            {
                "priority": "medium",
                "reason": "guidance-action applied percentage below target",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions metrics",
            }
        )
    if not guidance:
        guidance.append(
            {
                "priority": "low",
                "reason": "guidance-action posture stable",
                "command": "show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions state",
            }
        )
    max_items = max(1, min(int(limit), 20))
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window_ms),
        "buckets": int(buckets),
        "summary": {
            "health": str(summary_state.get("health", "healthy")),
            "trend": str(summary_state.get("trend", "stable")),
            "topPolicyCode": top_policy,
            "guidanceCount": int(min(len(guidance), max_items)),
        },
        "guidance": guidance[:max_items],
    }


def build_continuity_autopilot_posture_action_policy_anomaly_metrics(session: SessionState, session_id: str, window_ms: int = 3600000) -> dict[str, Any]:
    history = (
        session.continuity_autopilot_posture_action_policy_history
        if isinstance(session.continuity_autopilot_posture_action_policy_history, list)
        else []
    )
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    window_items = [item for item in history if isinstance(item, dict) and int(item.get("ts", 0) or 0) >= cutoff]
    report = detect_continuity_autopilot_posture_action_policy_anomalies(session, session_id, limit=500)
    detected_items = report.get("items", []) if isinstance(report.get("items"), list) else []
    detected_recent = [item for item in detected_items if isinstance(item, dict) and int(item.get("ts", 0) or 0) >= cutoff]
    anomaly_counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
    code_counts: dict[str, int] = {}
    for item in detected_recent:
        if not isinstance(item, dict):
            continue
        code = str(item.get("policyCode", "ok") or "ok").strip().lower() or "ok"
        code_counts[code] = int(code_counts.get(code, 0) or 0) + 1
    top_anomaly = "none"
    top_anomaly_count = 0
    for key, value in anomaly_counts.items():
        if int(value) > top_anomaly_count:
            top_anomaly = str(key)
            top_anomaly_count = int(value)
    top_code = "none"
    top_code_count = 0
    for key, value in code_counts.items():
        if int(value) > top_code_count:
            top_code = str(key)
            top_code_count = int(value)
    base_count = len(window_items)
    anomaly_count = len(detected_recent)
    anomaly_rate = (float(anomaly_count) * 100.0 / float(base_count)) if base_count > 0 else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(base_count),
            "anomalies": int(anomaly_count),
            "anomalyRatePct": float(round(anomaly_rate, 2)),
            "topAnomalyType": top_anomaly,
            "topPolicyCode": top_code,
        },
        "counts": {
            "anomalyTypes": anomaly_counts,
            "policyCodes": code_counts,
        },
    }


def build_continuity_autopilot_posture_action_history(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    items = (
        session.continuity_autopilot_posture_action_history
        if isinstance(session.continuity_autopilot_posture_action_history, list)
        else []
    )
    tail = items[-max(1, min(int(limit), 200)) :]
    applied = len([item for item in tail if isinstance(item, dict) and bool(item.get("applied", False))])
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "applied": int(applied),
            "noops": int(len(tail) - applied),
        },
        "items": tail,
    }


def build_continuity_autopilot_posture_action_metrics(session: SessionState, session_id: str, window_ms: int = 3600000) -> dict[str, Any]:
    history = (
        session.continuity_autopilot_posture_action_history
        if isinstance(session.continuity_autopilot_posture_action_history, list)
        else []
    )
    window = max(60000, min(int(window_ms), 86400000))
    cutoff = now_ms() - window
    recent = [item for item in history if isinstance(item, dict) and int(item.get("ts", 0) or 0) >= cutoff]
    applied = 0
    changed = 0
    command_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    for item in recent:
        if bool(item.get("applied", False)):
            applied += 1
        if bool(item.get("changed", False)):
            changed += 1
        command = str(item.get("command", "") or "").strip().lower() or "-"
        reason = str(item.get("reason", "unknown") or "unknown").strip().lower() or "unknown"
        command_counts[command] = int(command_counts.get(command, 0) or 0) + 1
        reason_counts[reason] = int(reason_counts.get(reason, 0) or 0) + 1
    top_command = "-"
    top_command_count = 0
    for key, value in command_counts.items():
        if int(value) > top_command_count:
            top_command = str(key)
            top_command_count = int(value)
    top_reason = "none"
    top_reason_count = 0
    for key, value in reason_counts.items():
        if int(value) > top_reason_count:
            top_reason = str(key)
            top_reason_count = int(value)
    count = len(recent)
    applied_pct = (float(applied) * 100.0 / float(count)) if count > 0 else 0.0
    changed_pct = (float(changed) * 100.0 / float(count)) if count > 0 else 0.0
    return {
        "ok": True,
        "sessionId": session_id,
        "windowMs": int(window),
        "summary": {
            "count": int(count),
            "applied": int(applied),
            "changed": int(changed),
            "appliedPct": float(round(applied_pct, 2)),
            "changedPct": float(round(changed_pct, 2)),
            "topCommand": top_command,
            "topReason": top_reason,
        },
        "counts": {
            "commands": command_counts,
            "reasons": reason_counts,
        },
    }


def detect_continuity_autopilot_posture_action_anomalies(session: SessionState, session_id: str, limit: int = 30) -> dict[str, Any]:
    history = (
        session.continuity_autopilot_posture_action_history
        if isinstance(session.continuity_autopilot_posture_action_history, list)
        else []
    )
    recent = history[-max(2, min(int(CONTINUITY_AUTOPILOT_POSTURE_ACTION_HISTORY_MAX), 500)) :]
    anomalies: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    noop_streak = 0
    command_noops: dict[str, int] = {}
    for item in recent:
        if not isinstance(item, dict):
            continue
        ts = int(item.get("ts", now_ms()) or now_ms())
        source = str(item.get("source", "event") or "event")
        command = str(item.get("command", "") or "").strip().lower() or "-"
        reason = str(item.get("reason", "unknown") or "unknown").strip().lower() or "unknown"
        applied = bool(item.get("applied", False))
        if not applied:
            noop_streak += 1
            command_noops[command] = int(command_noops.get(command, 0) or 0) + 1
        else:
            noop_streak = 0
            command_noops[command] = 0
        if reason in {"failed", "index_out_of_range"}:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "action_failed",
                    "detail": f"{command} ({reason})",
                    "reason": reason,
                    "command": command,
                }
            )
        if noop_streak >= 3:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "noop_streak",
                    "detail": f"streak={noop_streak}",
                    "reason": reason,
                    "command": command,
                }
            )
            noop_streak = 0
        if command != "-" and int(command_noops.get(command, 0) or 0) >= 2:
            anomalies.append(
                {
                    "ts": ts,
                    "source": source,
                    "type": "command_repeat_noop",
                    "detail": command,
                    "reason": reason,
                    "command": command,
                }
            )
            command_noops[command] = 0
    for item in anomalies:
        anomaly_type = str(item.get("type", "unknown"))
        counts[anomaly_type] = int(counts.get(anomaly_type, 0) or 0) + 1
    top_type = "none"
    top_count = 0
    for key, value in counts.items():
        if int(value) > top_count:
            top_type = str(key)
            top_count = int(value)
    tail = anomalies[-max(1, min(int(limit), 200)) :]
    return {
        "ok": True,
        "sessionId": session_id,
        "summary": {
            "count": int(len(tail)),
            "totalDetected": int(len(anomalies)),
            "topType": top_type,
        },
        "counts": counts,
        "items": tail,
    }


def append_continuity_autopilot_history(session: SessionState, source: str, reason: str, changed: bool, action: dict[str, Any] | None = None) -> None:
    entry = {
        "ts": now_ms(),
        "source": str(source or "unknown")[:40],
        "reason": str(reason or "none")[:64],
        "changed": bool(changed),
        "action": {
            "command": str((action or {}).get("command", ""))[:80],
            "priority": str((action or {}).get("priority", ""))[:8],
            "applied": bool((action or {}).get("applied", False)),
        },
    }
    session.continuity_autopilot_history.append(entry)
    if len(session.continuity_autopilot_history) > int(CONTINUITY_AUTOPILOT_HISTORY_MAX):
        session.continuity_autopilot_history[:] = session.continuity_autopilot_history[-int(CONTINUITY_AUTOPILOT_HISTORY_MAX) :]


def run_continuity_autopilot_tick(session: SessionState, session_id: str, force: bool = False) -> dict[str, Any]:
    state = ensure_continuity_autopilot_state(session.continuity_autopilot)
    session.continuity_autopilot = state
    now = now_ms()
    mode = str(state.get("mode", "normal") or "normal")
    if not bool(state.get("enabled", False)):
        return {"ran": False, "changed": False, "reason": "disabled"}
    guardrails = evaluate_continuity_autopilot_guardrails(session)
    if not bool(guardrails.get("ok", True)):
        blocker = (guardrails.get("blockers", [{}])[0] if isinstance(guardrails.get("blockers"), list) and guardrails.get("blockers") else {})
        reason = str(blocker.get("code", "guardrail_blocked"))
        state["lastRunAt"] = now
        state["noops"] = int(state.get("noops", 0) or 0) + 1
        state["lastResult"] = reason
        session.continuity_autopilot = state
        append_continuity_autopilot_history(
            session,
            source="tick",
            reason=reason,
            changed=True,
            action={"command": "guardrail_blocked", "priority": "p1", "applied": False},
        )
        append_continuity_autopilot_posture_snapshot(session, session_id, "tick_guardrail_blocked")
        return {"ran": True, "changed": True, "reason": reason, "guardrails": guardrails}
    cooldown_raw = state.get("cooldownMs", CONTINUITY_AUTOPILOT_COOLDOWN_MS)
    cooldown_ms = max(1000, int(CONTINUITY_AUTOPILOT_COOLDOWN_MS if cooldown_raw is None else cooldown_raw))
    last_run_at = int(state.get("lastRunAt", 0) or 0)
    if (not force) and (last_run_at > 0) and ((now - last_run_at) < cooldown_ms):
        return {"ran": False, "changed": False, "reason": "cooldown", "nextInMs": int(cooldown_ms - (now - last_run_at))}
    max_applies_raw = state.get("maxAppliesPerHour", CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR)
    base_max_applies = max(0, int(CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR if max_applies_raw is None else max_applies_raw))
    if mode == "safe":
        max_applies = max(0, min(base_max_applies, 10))
    elif mode == "aggressive":
        max_applies = max(0, min(500, base_max_applies * 2))
    else:
        max_applies = base_max_applies
    applied_ts = state.get("appliedTimestamps", []) if isinstance(state.get("appliedTimestamps"), list) else []
    applied_recent = [int(ts or 0) for ts in applied_ts if int(ts or 0) > (now - 60 * 60 * 1000)]
    state["appliedTimestamps"] = applied_recent[-500:]
    if max_applies <= 0 or len(applied_recent) >= max_applies:
        state["lastRunAt"] = now
        state["noops"] = int(state.get("noops", 0) or 0) + 1
        state["lastResult"] = "rate_limited"
        session.continuity_autopilot = state
        append_continuity_autopilot_history(
            session,
            source="tick",
            reason="rate_limited",
            changed=True,
            action={"command": "rate_limit", "priority": "p1", "applied": False},
        )
        append_continuity_autopilot_posture_snapshot(session, session_id, "tick_rate_limited")
        return {"ran": True, "changed": True, "reason": "rate_limited", "limit": max_applies, "used": len(applied_recent), "mode": mode}
    mode_aligned = False
    if bool(state.get("autoAlignMode", False)):
        recommendation = build_continuity_autopilot_mode_recommendation(session, session_id)
        recommended = str(recommendation.get("recommendedMode", mode) or mode)
        if recommended in {"safe", "normal", "aggressive"} and recommended != mode:
            state["mode"] = recommended
            state["aligned"] = int(state.get("aligned", 0) or 0) + 1
            state["lastAlignAt"] = now
            state["lastResult"] = f"mode aligned to {recommended}"
            mode = recommended
            mode_aligned = True
            session.continuity_autopilot = state
            append_continuity_autopilot_history(
                session,
                source="tick_mode_align",
                reason="mode_aligned",
                changed=True,
                action={"command": f"continuity_autopilot_mode_{recommended}", "priority": "p2", "applied": True},
            )
            append_continuity_autopilot_posture_snapshot(session, session_id, "tick_mode_align")
    state["lastRunAt"] = now
    action_report = apply_continuity_next_action(session, session_id)
    action_result = action_report.get("result", {}) if isinstance(action_report.get("result"), dict) else {}
    if bool(action_report.get("applied", False)) and bool(action_result.get("ok", False)):
        state["applied"] = int(state.get("applied", 0) or 0) + 1
        state["lastAppliedAt"] = now
        applied_recent.append(now)
        state["appliedTimestamps"] = applied_recent[-500:]
        state["lastAction"] = str(action_report.get("command", ""))[:80]
        state["lastResult"] = str(action_result.get("message", "applied"))[:160]
        session.continuity_autopilot = state
        append_continuity_autopilot_history(session, source="tick", reason="applied", changed=True, action=action_report)
        append_continuity_autopilot_posture_snapshot(session, session_id, "tick_applied")
        return {"ran": True, "changed": True, "reason": "applied", "action": action_report, "mode": mode, "modeAligned": mode_aligned}
    state["noops"] = int(state.get("noops", 0) or 0) + 1
    state["lastResult"] = str(action_result.get("message", "no-op"))[:160]
    session.continuity_autopilot = state
    append_continuity_autopilot_history(session, source="tick", reason="noop", changed=True, action=action_report)
    append_continuity_autopilot_posture_snapshot(session, session_id, "tick_noop")
    return {"ran": True, "changed": True, "reason": "noop", "action": action_report, "mode": mode, "modeAligned": mode_aligned}


def find_idempotent_response(session: SessionState, key: str, intent: str) -> dict[str, Any] | None:
    entries = session.idempotency.get("entries", []) if isinstance(session.idempotency.get("entries"), list) else []
    for item in reversed(entries):
        if str(item.get("key", "")) != key:
            continue
        if str(item.get("intent", "")) != str(intent):
            return None
        response = item.get("response")
        if isinstance(response, dict):
            return copy.deepcopy(response)
    return None


def store_idempotent_response(session: SessionState, key: str, intent: str, response: dict[str, Any], revision: int) -> None:
    entries = session.idempotency.get("entries", []) if isinstance(session.idempotency.get("entries"), list) else []
    cleaned = [item for item in entries if str(item.get("key", "")) != key]
    cleaned.append(
        {
            "key": key,
            "intent": str(intent)[:240],
            "timestamp": now_ms(),
            "revision": int(revision),
            "response": copy.deepcopy(response),
        }
    )
    session.idempotency["entries"] = cleaned[-int(TURN_IDEMPOTENCY_MAX_ENTRIES) :]


def build_intent_preview_report(session: SessionState, session_id: str, intent: str) -> dict[str, Any]:
    envelope = compile_intent_envelope(intent)
    writes = list(envelope.get("stateIntent", {}).get("writeOperations", []) or [])
    evaluations: list[dict[str, Any]] = []
    for op in writes:
        capability = resolve_capability(op)
        policy = evaluate_policy(op, capability)
        evaluations.append(
            {
                "op": str(op.get("type", "")),
                "domain": str(capability.get("domain", "unknown")),
                "risk": str(capability.get("risk", "unknown")),
                "policy": str(policy.get("code", "unknown")),
                "allowed": bool(policy.get("allowed", False)),
                "reason": str(policy.get("reason", "")),
            }
        )

    pseudo_execution = {
        "ok": bool(all(item.get("allowed", False) for item in evaluations)) if evaluations else True,
        "message": "preview",
        "toolResults": [],
    }
    route = planner_route(envelope, pseudo_execution, session.graph)
    return {
        "ok": True,
        "sessionId": session_id,
        "intent": intent,
        "intentClass": str(envelope.get("intentClass", "unknown")),
        "confidence": float(envelope.get("confidence", 0.0) or 0.0),
        "route": {
            "target": str(route.get("target", "deterministic")),
            "reason": str(route.get("reason", "default")),
            "model": route.get("model"),
            "intentClass": str(route.get("intentClass", "unknown")),
            "confidence": float(route.get("confidence", 0.0) or 0.0),
        },
        "writes": evaluations,
        "readDomains": list(envelope.get("stateIntent", {}).get("readDomains", [])),
        "clarification": envelope.get("clarification", {}),
    }


def execute_scheduled_job(session: SessionState, session_id: str, job: dict[str, Any]) -> None:
    kind = str(job.get("kind", "unknown"))
    run_key = compute_job_run_key(job)
    if run_key and str(job.get("lastRunKey", "")) == run_key:
        return
    if run_key:
        job["lastRunKey"] = run_key

    if kind == "watch_task":
        task = find_task_for_job(session.graph, job)
        if not task:
            summary = "watch target missing"
            job["active"] = False
        else:
            state = "done" if bool(task.get("done", False)) else "open"
            title = str(task.get("title", "")).strip()[:72]
            summary = f"watch task {state}: {title}"
        job["lastResult"] = summary
        graph_add_event(session.graph, "job_tick", {"jobId": job.get("id"), "kind": kind, "summary": summary})
        record_journal_entry(
            session,
            session_id,
            {"type": "job_tick"},
            {
                "ok": True,
                "message": summary,
                "policy": {"allowed": True, "reason": "scheduled", "code": "scheduled"},
                "capability": {"domain": "system", "risk": "low"},
                "diff": zero_diff(),
            },
        )
        return

    if kind == "remind_note":
        text = str(job.get("text", "")).strip()
        if not text:
            text = "Reminder"
        note_text = f"[reminder] {text}"
        job["lastResult"] = note_text[:120]
        graph_add_entity(session.graph, {"kind": "note", "text": note_text, "createdAt": now_ms()})
        graph_add_event(session.graph, "job_tick", {"jobId": job.get("id"), "kind": kind, "summary": note_text[:120]})
        record_journal_entry(
            session,
            session_id,
            {"type": "job_tick"},
            {
                "ok": True,
                "message": f"reminder note created: {text[:72]}",
                "policy": {"allowed": True, "reason": "scheduled", "code": "scheduled"},
                "capability": {"domain": "system", "risk": "low"},
                "diff": {"tasks": 0, "expenses": 0, "notes": 1},
            },
        )
        return

    if kind == "audit_open_tasks":
        projection = graph_projection(session.graph)
        open_tasks = sum(1 for t in projection["tasks"] if not t.get("done"))
        summary = f"[audit] open tasks: {open_tasks}"
        job["lastResult"] = summary
        graph_add_entity(session.graph, {"kind": "note", "text": summary, "createdAt": now_ms()})
        graph_add_event(session.graph, "job_tick", {"jobId": job.get("id"), "kind": kind, "summary": summary})
        record_journal_entry(
            session,
            session_id,
            {"type": "job_tick"},
            {
                "ok": True,
                "message": summary,
                "policy": {"allowed": True, "reason": "scheduled", "code": "scheduled"},
                "capability": {"domain": "system", "risk": "low"},
                "diff": {"tasks": 0, "expenses": 0, "notes": 1},
            },
        )
        return

    if kind == "summarize_expenses_daily":
        projection = graph_projection(session.graph)
        total = sum(float(e.get("amount", 0)) for e in projection["expenses"])
        summary = f"[expenses] total tracked: ${total:,.2f}"
        job["lastResult"] = summary
        graph_add_entity(session.graph, {"kind": "note", "text": summary, "createdAt": now_ms()})
        graph_add_event(session.graph, "job_tick", {"jobId": job.get("id"), "kind": kind, "summary": summary})
        record_journal_entry(
            session,
            session_id,
            {"type": "job_tick"},
            {
                "ok": True,
                "message": summary,
                "policy": {"allowed": True, "reason": "scheduled", "code": "scheduled"},
                "capability": {"domain": "system", "risk": "low"},
                "diff": {"tasks": 0, "expenses": 0, "notes": 1},
            },
        )
        return

    if kind == "failing_probe":
        raise RuntimeError("simulated failing probe")


def compile_intent_envelope(text: str) -> dict[str, Any]:
    lower = text.lower()
    writes = parse_commands(text)
    clarification = build_clarification_signal(text, lower, writes)

    state_domains = [op["domain"] for op in writes] if writes else infer_domains(lower)
    state_domains = list(dict.fromkeys(state_domains))
    confidence = 0.96 if writes else 0.78
    if len(state_domains) >= 2 or "?" in lower:
        confidence = 0.82
    if clarification.get("required"):
        confidence = 0.42
    intent_class = classify_intent(lower, writes, state_domains)

    return {
        "surfaceIntent": {
            "raw": text,
            "normalized": lower,
            "kind": "question" if "?" in lower else ("command" if writes else "statement"),
            "timestamp": int(datetime.utcnow().timestamp() * 1000),
        },
        "taskIntent": {
            "goal": "mutate" if writes else ("overview" if set(state_domains) == {"tasks", "expenses", "notes"} else "inspect"),
            "operation": "write" if writes else "read",
            "targetDomains": state_domains,
            "constraints": [],
        },
        "stateIntent": {
            "readDomains": state_domains,
            "writeOperations": writes,
            "summary": " + ".join([op["type"] for op in writes]) if writes else f"read {'+'.join(state_domains)}",
        },
        "uiIntent": {
            "mode": "default",
            "density": "normal",
            "interactionPattern": "prompt + suggestions",
            "emphasis": "balanced",
        },
        "confidence": confidence,
        "intentClass": intent_class,
        "clarification": clarification,
        "confidencePolicy": {
            "threshold": float(INTENT_CLARIFICATION_THRESHOLD),
            "needsClarification": bool(clarification.get("required")),
        },
    }


def build_clarification_signal(text: str, lower: str, writes: list[dict[str, Any]]) -> dict[str, Any]:
    if writes:
        return {"required": False}

    tokens = [part for part in re.split(r"\s+", lower.strip()) if part]
    if not tokens:
        return {"required": False}

    commandish_verbs = {
        "add",
        "delete",
        "remove",
        "complete",
        "done",
        "finish",
        "clear",
        "link",
        "watch",
        "remind",
        "pause",
        "resume",
        "cancel",
        "reset",
        "restore",
        "compact",
        "retry",
        "simulate",
        "read",
        "fetch",
        "claim",
        "checkpoint",
    }
    nouns_without_action = {"task", "tasks", "expense", "expenses", "note", "notes", "job", "jobs", "file", "files"}

    examples = [
        "add task Draft onboarding checklist",
        "delete task 1",
        "read file README.md",
        "list jobs",
    ]
    if len(tokens) <= 2 and tokens[0] in nouns_without_action:
        return {
            "required": True,
            "reason": "missing_action",
            "question": f"Intent is incomplete: \"{text}\". Add an action and target.",
            "examples": examples,
        }

    if tokens[0] in commandish_verbs:
        return {
            "required": True,
            "reason": "ambiguous_command",
            "question": f"Intent looks like a command but is missing details: \"{text}\".",
            "examples": examples,
        }

    return {"required": False}


def classify_intent(lower: str, writes: list[dict[str, Any]], domains: list[str]) -> str:
    if writes:
        return "mutate"
    if "system" in domains:
        return "ops"
    if "graph" in domains:
        return "graph_query"
    if "?" in lower:
        return "question"
    if len(domains) >= 2:
        return "cross_domain_query"
    return "single_domain_query"


def infer_domains(lower: str) -> list[str]:
    scores = {
        "tasks": any(x in lower for x in ["task", "todo", "ship"]),
        "expenses": any(x in lower for x in ["expense", "money", "budget", "cost"]),
        "notes": any(x in lower for x in ["note", "idea", "research", "nca"]),
        "graph": any(x in lower for x in ["depend", "dependency", "dependencies", "reference", "references", "link"]),
        "files": any(x in lower for x in ["file", "folder", "directory", "path", "readme"]),
        "web": any(x in lower for x in ["web", "url", "http", "https", "site", "fetch"]),
    }
    selected = [k for k, v in scores.items() if v]
    return selected or ["tasks", "expenses", "notes"]


def parse_commands(text: str) -> list[dict[str, Any]]:
    patterns = [
        (r"^add task\s+(.+)$", lambda m: {"type": "add_task", "domain": "tasks", "payload": {"title": m.group(1)}}),
        (r"^(complete|done|finish)\s+task\s+(\S+)$", lambda m: {"type": "toggle_task", "domain": "tasks", "payload": {"selector": m.group(2)}}),
        (r"^(delete|remove)\s+task\s+(\S+)$", lambda m: {"type": "delete_task", "domain": "tasks", "payload": {"selector": m.group(2)}}),
        (r"^(clear completed|clear done)$", lambda _m: {"type": "clear_completed", "domain": "tasks", "payload": {}}),
        (r"^add expense\s+\$?([0-9]+(?:\.[0-9]{1,2})?)\s+([a-zA-Z_-]+)\s*(.*)$", lambda m: {"type": "add_expense", "domain": "expenses", "payload": {"amount": float(m.group(1)), "category": m.group(2), "note": m.group(3)}}),
        (r"^(add note|note)\s+(.+)$", lambda m: {"type": "add_note", "domain": "notes", "payload": {"text": m.group(2)}}),
        (
            r"^link\s+(task|tasks|note|notes|expense|expenses)\s+(\S+)\s+(depends_on|references)\s+(task|tasks|note|notes|expense|expenses)\s+(\S+)$",
            lambda m: {
                "type": "link_entities",
                "domain": "graph",
                "payload": {
                    "sourceKind": normalize_entity_kind(m.group(1)),
                    "sourceSelector": m.group(2),
                    "relation": m.group(3).lower(),
                    "targetKind": normalize_entity_kind(m.group(4)),
                    "targetSelector": m.group(5),
                },
            },
        ),
        (
            r"^watch\s+task\s+(\S+)\s+every\s+(\d+)\s*(m|min|mins|minute|minutes)$",
            lambda m: {
                "type": "schedule_watch_task",
                "domain": "system",
                "payload": {"selector": m.group(1), "intervalMinutes": int(m.group(2))},
            },
        ),
        (
            r"^remind\s+note\s+(.+)\s+every\s+(\d+)\s*(m|min|mins|minute|minutes)$",
            lambda m: {
                "type": "schedule_remind_note",
                "domain": "system",
                "payload": {"text": m.group(1).strip(), "intervalMinutes": int(m.group(2))},
            },
        ),
        (
            r"^audit\s+open\s+tasks\s+every\s+(\d+)\s*(m|min|mins|minute|minutes)$",
            lambda m: {
                "type": "schedule_audit_open_tasks",
                "domain": "system",
                "payload": {"intervalMinutes": int(m.group(1))},
            },
        ),
        (
            r"^summarize\s+expenses\s+daily$",
            lambda _m: {
                "type": "schedule_summarize_expenses_daily",
                "domain": "system",
                "payload": {"intervalMinutes": 24 * 60},
            },
        ),
        (
            r"^schedule\s+failing\s+probe\s+every\s+(\d+)\s*(m|min|mins|minute|minutes)$",
            lambda m: {
                "type": "schedule_failing_probe",
                "domain": "system",
                "payload": {"intervalMinutes": int(m.group(1))},
            },
        ),
        (
            r"^pause\s+job\s+(\S+)$",
            lambda m: {
                "type": "pause_job",
                "domain": "system",
                "payload": {"selector": m.group(1)},
            },
        ),
        (
            r"^resume\s+job\s+(\S+)$",
            lambda m: {
                "type": "resume_job",
                "domain": "system",
                "payload": {"selector": m.group(1)},
            },
        ),
        (
            r"^cancel\s+job\s+(\S+)$",
            lambda m: {
                "type": "cancel_job",
                "domain": "system",
                "payload": {"selector": m.group(1)},
            },
        ),
        (
            r"^(list|show)\s+jobs$",
            lambda _m: {
                "type": "list_jobs",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+dead\s+letters$",
            lambda _m: {
                "type": "list_dead_letters",
                "domain": "system",
                "payload": {"limit": 20},
            },
        ),
        (
            r"^(show|list)\s+runtime\s+health$",
            lambda _m: {
                "type": "runtime_health",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+presence$",
            lambda _m: {
                "type": "show_presence",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^prune\s+presence(?:\s+all)?$",
            lambda m: {
                "type": "prune_presence",
                "domain": "system",
                "payload": {"all": "all" in str(m.group(0)).lower(), "maxAgeMs": 120000},
            },
        ),
        (
            r"^prune\s+presence\s+older\s+than\s+(\d+)\s*(ms|s|m)$",
            lambda m: {
                "type": "prune_presence",
                "domain": "system",
                "payload": {
                    "all": False,
                    "maxAgeMs": int(m.group(1))
                    * (1 if str(m.group(2)).lower() == "ms" else 1000 if str(m.group(2)).lower() == "s" else 60_000),
                },
            },
        ),
        (
            r"^(show|list)\s+handoff\s+stats$",
            lambda _m: {
                "type": "handoff_stats",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+runtime\s+profile(?:\s+(\d+))?$",
            lambda m: {
                "type": "runtime_profile",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 200)},
            },
        ),
        (
            r"^(show|list)\s+diagnostics$",
            lambda _m: {
                "type": "show_diagnostics",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity$",
            lambda _m: {
                "type": "show_continuity",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+health$",
            lambda _m: {
                "type": "continuity_health",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+(trend|history)(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_history",
                "domain": "system",
                "payload": {"limit": int(m.group(3) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+anomalies(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_anomalies",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+incidents(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_incidents",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+(next|actions?)(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_next",
                "domain": "system",
                "payload": {"limit": int(m.group(3) or 5)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(preview|explain)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_preview",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(dry\s*run|simulate)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_dry_run",
                "domain": "system",
                "payload": {"force": True},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+guardrails$",
            lambda _m: {
                "type": "continuity_autopilot_guardrails",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+recommendation$",
            lambda _m: {
                "type": "continuity_autopilot_mode_recommendation",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+drift$",
            lambda _m: {
                "type": "continuity_autopilot_mode_drift",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+alignment(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_mode_alignment",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+policy\s+(safe|normal|aggressive)$",
            lambda m: {
                "type": "continuity_autopilot_mode_policy",
                "domain": "system",
                "payload": {"targetMode": str(m.group(2)).lower()},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+policy\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_mode_policy_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+mode\s+policy\s+matrix$",
            lambda _m: {
                "type": "continuity_autopilot_mode_policy_matrix",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomalies",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+trend(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_trend",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+offenders(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+top\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_offenders",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+state(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_state",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+threshold\s+(\d+(?:\.\d+)?))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "thresholdPct": float(m.group(4) or 35.0),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+breaches(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+threshold\s+(\d+(?:\.\d+)?))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_breaches",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "thresholdPct": float(m.group(4) or 35.0),
                    "buckets": int(m.group(5) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+threshold\s+(\d+(?:\.\d+)?))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "thresholdPct": float(m.group(4) or 35.0),
                    "buckets": int(m.group(5) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+matrix(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(dry\s*run|simulate)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+action(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run",
                "domain": "system",
                "payload": {"index": int(m.group(2) or 1)},
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+action(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply",
                "domain": "system",
                "payload": {"index": int(m.group(2) or 1)},
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 3)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 30)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+trend(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+state(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+offenders(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+timeline(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 20),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+summary(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+matrix(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 5),
                },
            },
        ),
        (
            r"^(dry\s*run|simulate)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 5),
                },
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 5),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+state(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 20),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+trend(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+offenders(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+summary(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+timeline(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 20),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+matrix(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 5),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 5),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+state(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 20),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+trend(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+offenders(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+summary(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 8),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+timeline(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 20),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+matrix(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "limit": int(m.group(4) or 6),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions\s+guidance(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000,
                    "buckets": int(m.group(4) or 6),
                    "limit": int(m.group(5) or 5),
                },
            },
        ),
        (
            r"^(dry\s*run|simulate)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+action(?:\s+(\d+))?(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run",
                "domain": "system",
                "payload": {
                    "index": int(m.group(2) or 1),
                    "windowMs": (
                        int(m.group(3))
                        * (
                            1
                            if str(m.group(4) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(4) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(4) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(3)
                    else 3_600_000,
                    "buckets": int(m.group(5) or 6),
                    "limit": int(m.group(6) or 5),
                },
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+action(?:\s+(\d+))?(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
                "domain": "system",
                "payload": {
                    "index": int(m.group(2) or 1),
                    "windowMs": (
                        int(m.group(3))
                        * (
                            1
                            if str(m.group(4) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(4) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(4) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(3)
                    else 3_600_000,
                    "buckets": int(m.group(5) or 6),
                    "limit": int(m.group(6) or 5),
                },
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+budget\s+forecast\s+guidance\s+actions\s+anomalies\s+remediation\s+guidance\s+actions(?:\s+(\d+))?(?:\s+(\d+)\s*(ms|s|m|h))?(?:\s+(\d+)\s*buckets?)?(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
                "domain": "system",
                "payload": {
                    "limit": int(m.group(2) or 3),
                    "windowMs": (
                        int(m.group(3))
                        * (
                            1
                            if str(m.group(4) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(4) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(4) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(3)
                    else 3_600_000,
                    "buckets": int(m.group(5) or 6),
                    "guidanceLimit": int(m.group(6) or 5),
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+anomalies\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_anomaly_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+policy\s+matrix(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_policy_matrix",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 10)},
            },
        ),
        (
            r"^(dry\s*run|simulate)\s+continuity\s+autopilot\s+posture\s+action(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_dry_run",
                "domain": "system",
                "payload": {"index": int(m.group(2) or 1)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+anomalies(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_anomalies",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+metrics(?:\s+(\d+)\s*(ms|s|m|h))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_metrics",
                "domain": "system",
                "payload": {
                    "windowMs": (
                        int(m.group(2))
                        * (
                            1
                            if str(m.group(3) or "ms").lower() == "ms"
                            else 1000
                            if str(m.group(3) or "ms").lower() == "s"
                            else 60_000
                            if str(m.group(3) or "ms").lower() == "m"
                            else 3_600_000
                        )
                    )
                    if m.group(2)
                    else 3_600_000
                },
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_action_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+actions(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_apply_actions",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 3)},
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+autopilot\s+posture\s+action(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_apply_action",
                "domain": "system",
                "payload": {"index": int(m.group(2) or 1)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+actions(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_actions",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 5)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+anomalies(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_anomalies",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture\s+history(?:\s+(\d+))?$",
            lambda m: {
                "type": "continuity_autopilot_posture_history",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 20)},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot\s+posture$",
            lambda _m: {
                "type": "continuity_autopilot_posture",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(apply|use)\s+continuity\s+autopilot\s+mode\s+recommendation$",
            lambda _m: {
                "type": "continuity_autopilot_mode_apply_recommended",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_show",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(enable|turn on|start)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_set",
                "domain": "system",
                "payload": {"enabled": True},
            },
        ),
        (
            r"^continuity\s+autopilot\s+on$",
            lambda _m: {
                "type": "continuity_autopilot_set",
                "domain": "system",
                "payload": {"enabled": True},
            },
        ),
        (
            r"^(disable|turn off|stop)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_set",
                "domain": "system",
                "payload": {"enabled": False},
            },
        ),
        (
            r"^continuity\s+autopilot\s+off$",
            lambda _m: {
                "type": "continuity_autopilot_set",
                "domain": "system",
                "payload": {"enabled": False},
            },
        ),
        (
            r"^(tick|run)\s+continuity\s+autopilot$",
            lambda _m: {
                "type": "continuity_autopilot_tick",
                "domain": "system",
                "payload": {"force": True},
            },
        ),
        (
            r"^set\s+continuity\s+autopilot\s+cooldown\s+(\d+)\s*(ms|s|m)$",
            lambda m: {
                "type": "continuity_autopilot_config",
                "domain": "system",
                "payload": {
                    "cooldownMs": int(m.group(1))
                    * (1 if str(m.group(2)).lower() == "ms" else 1000 if str(m.group(2)).lower() == "s" else 60_000),
                },
            },
        ),
        (
            r"^set\s+continuity\s+autopilot\s+max\s+applies\s+(\d+)\s+per\s+hour$",
            lambda m: {
                "type": "continuity_autopilot_config",
                "domain": "system",
                "payload": {"maxAppliesPerHour": int(m.group(1))},
            },
        ),
        (
            r"^set\s+continuity\s+autopilot\s+mode\s+(safe|normal|aggressive)$",
            lambda m: {
                "type": "continuity_autopilot_config",
                "domain": "system",
                "payload": {"mode": str(m.group(1)).lower()},
            },
        ),
        (
            r"^set\s+continuity\s+autopilot\s+auto\s*align\s+(on|off)$",
            lambda m: {
                "type": "continuity_autopilot_config",
                "domain": "system",
                "payload": {"autoAlignMode": str(m.group(1)).lower() == "on"},
            },
        ),
        (
            r"^reset\s+continuity\s+autopilot\s+stats(?:\s+clear\s+history)?$",
            lambda m: {
                "type": "continuity_autopilot_reset",
                "domain": "system",
                "payload": {"clearHistory": "clear history" in str(m.group(0)).lower()},
            },
        ),
        (
            r"^(apply|run)\s+continuity\s+(next|actions?)$",
            lambda _m: {
                "type": "continuity_next_apply",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+continuity\s+alerts$",
            lambda _m: {
                "type": "continuity_alerts",
                "domain": "system",
                "payload": {"limit": 10},
            },
        ),
        (
            r"^(clear|reset)\s+continuity\s+alerts$",
            lambda _m: {
                "type": "clear_continuity_alerts",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(drill|simulate)\s+continuity\s+breach$",
            lambda _m: {
                "type": "drill_continuity_breach",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|list)\s+snapshot\s+stats$",
            lambda _m: {
                "type": "snapshot_stats",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(show|verify|list)\s+journal\s+integrity$",
            lambda _m: {
                "type": "verify_journal_integrity",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^repair\s+journal\s+integrity$",
            lambda _m: {
                "type": "repair_journal_integrity",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(run|show|list)\s+self\s+check$",
            lambda _m: {
                "type": "self_check",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^retry\s+dead\s+letter\s+(\S+)$",
            lambda m: {
                "type": "retry_dead_letter",
                "domain": "system",
                "payload": {"selector": m.group(1)},
            },
        ),
        (
            r"^purge\s+dead\s+letters$",
            lambda _m: {
                "type": "purge_dead_letters",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(undo|undo last)$",
            lambda _m: {
                "type": "undo_last",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(list|show)\s+files(?:\s+(.+))?$",
            lambda m: {
                "type": "list_files",
                "domain": "files",
                "payload": {"path": (m.group(2) or ".").strip()},
            },
        ),
        (
            r"^(read|show)\s+file\s+(.+)$",
            lambda m: {
                "type": "read_file",
                "domain": "files",
                "payload": {"path": m.group(2).strip()},
            },
        ),
        (
            r"^fetch\s+url\s+(\S+)$",
            lambda m: {
                "type": "fetch_url",
                "domain": "web",
                "payload": {"url": m.group(1).strip()},
            },
        ),
        (
            r"^(show|list)\s+audit\s+op\s+([a-zA-Z0-9_-]+)$",
            lambda m: {
                "type": "list_audit",
                "domain": "system",
                "payload": {"op": str(m.group(2)).strip().lower()},
            },
        ),
        (
            r"^(show|list)\s+audit\s+policy\s+([a-zA-Z0-9_-]+)$",
            lambda m: {
                "type": "list_audit",
                "domain": "system",
                "payload": {"policyCode": str(m.group(2)).strip().lower()},
            },
        ),
        (
            r"^(show|list)\s+audit(?:\s+domain\s+([a-zA-Z_-]+))?$",
            lambda m: {
                "type": "list_audit",
                "domain": "system",
                "payload": {"domain": (m.group(2) or "").strip().lower()},
            },
        ),
        (
            r"^(show|list)\s+trace\s+summary$",
            lambda _m: {
                "type": "trace_summary",
                "domain": "system",
                "payload": {"limit": 200},
            },
        ),
        (
            r"^(show|list)\s+trace\s+class\s+([a-zA-Z0-9_-]+)$",
            lambda m: {
                "type": "list_trace",
                "domain": "system",
                "payload": {"limit": 20, "intentClass": str(m.group(2)).strip().lower()},
            },
        ),
        (
            r"^(show|list)\s+trace\s+reason\s+([a-zA-Z0-9_-]+)$",
            lambda m: {
                "type": "list_trace",
                "domain": "system",
                "payload": {"limit": 20, "routeReason": str(m.group(2)).strip().lower()},
            },
        ),
        (
            r"^(show|list)\s+trace\s+(ok|denied)$",
            lambda m: {
                "type": "list_trace",
                "domain": "system",
                "payload": {"limit": 20, "ok": str(m.group(2)).strip().lower() == "ok"},
            },
        ),
        (
            r"^(show|list)\s+trace$",
            lambda _m: {
                "type": "list_trace",
                "domain": "system",
                "payload": {"limit": 20},
            },
        ),
        (
            r"^(export|dump)\s+trace(?:\s+limit\s+(\d+))?$",
            lambda m: {
                "type": "export_trace",
                "domain": "system",
                "payload": {"limit": int(m.group(2) or 50)},
            },
        ),
        (
            r"^restore\s+preview$",
            lambda _m: {
                "type": "restore_preview",
                "domain": "system",
                "payload": {"limit": 500},
            },
        ),
        (
            r"^restore\s+apply$",
            lambda _m: {
                "type": "restore_apply",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^confirm\s+restore\s+apply$",
            lambda _m: {
                "type": "restore_apply",
                "domain": "system",
                "payload": {"confirmed": True},
            },
        ),
        (
            r"^checkpoint\s+now$",
            lambda _m: {
                "type": "create_checkpoint",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^(list|show)\s+checkpoints$",
            lambda _m: {
                "type": "list_checkpoints",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^restore\s+checkpoint\s+latest$",
            lambda _m: {
                "type": "restore_checkpoint_latest",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^confirm\s+restore\s+checkpoint\s+latest$",
            lambda _m: {
                "type": "restore_checkpoint_latest",
                "domain": "system",
                "payload": {"confirmed": True},
            },
        ),
        (
            r"^simulate\s+persist\s+failure\s+(on|off)$",
            lambda m: {
                "type": "set_persist_fault_mode",
                "domain": "system",
                "payload": {"enabled": str(m.group(1)).strip().lower() == "on"},
            },
        ),
        (
            r"^(show|list)\s+faults$",
            lambda _m: {
                "type": "list_faults",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^compact\s+journal(?:\s+keep\s+(\d+))?$",
            lambda m: {
                "type": "compact_journal",
                "domain": "system",
                "payload": {"keep": int(m.group(1) or 200)},
            },
        ),
        (
            r"^confirm\s+compact\s+journal(?:\s+keep\s+(\d+))?$",
            lambda m: {
                "type": "compact_journal",
                "domain": "system",
                "payload": {"confirmed": True, "keep": int(m.group(1) or 200)},
            },
        ),
        (
            r"^retry\s+persist\s+now$",
            lambda _m: {
                "type": "retry_persist",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^explain\s+intent\s+(.+)$",
            lambda m: {
                "type": "explain_intent",
                "domain": "system",
                "payload": {"text": m.group(1).strip()},
            },
        ),
        (
            r"^preview\s+intent\s+(.+)$",
            lambda m: {
                "type": "preview_intent",
                "domain": "system",
                "payload": {"text": m.group(1).strip()},
            },
        ),
        (
            r"^drill\s+policy\s+confirm$",
            lambda _m: {
                "type": "policy_drill_confirm",
                "domain": "system",
                "payload": {},
            },
        ),
        (
            r"^drill\s+policy\s+deny$",
            lambda _m: {
                "type": "policy_drill_deny",
                "domain": "system",
                "payload": {},
            },
        ),
        (r"^(confirm reset memory|confirm reset demo)$", lambda _m: {"type": "reset_memory", "domain": "system", "payload": {"confirmed": True}}),
        (r"^(reset demo|reset memory)$", lambda _m: {"type": "reset_memory", "domain": "system", "payload": {}}),
    ]

    for pattern, builder in patterns:
        match = re.match(pattern, text, flags=re.IGNORECASE)
        if match:
            return [builder(match)]
    return []


CAPABILITY_REGISTRY: dict[str, dict[str, Any]] = {
    "explain_intent": {"domain": "system", "risk": "low"},
    "preview_intent": {"domain": "system", "risk": "low"},
    "policy_drill_confirm": {"domain": "system", "risk": "high"},
    "add_task": {"domain": "tasks", "risk": "low"},
    "toggle_task": {"domain": "tasks", "risk": "low"},
    "delete_task": {"domain": "tasks", "risk": "medium"},
    "clear_completed": {"domain": "tasks", "risk": "medium"},
    "add_expense": {"domain": "expenses", "risk": "low"},
    "add_note": {"domain": "notes", "risk": "low"},
    "link_entities": {"domain": "graph", "risk": "low"},
    "schedule_watch_task": {"domain": "system", "risk": "low"},
    "schedule_remind_note": {"domain": "system", "risk": "low"},
    "schedule_audit_open_tasks": {"domain": "system", "risk": "low"},
    "schedule_summarize_expenses_daily": {"domain": "system", "risk": "low"},
    "schedule_failing_probe": {"domain": "system", "risk": "medium"},
    "pause_job": {"domain": "system", "risk": "low"},
    "resume_job": {"domain": "system", "risk": "low"},
    "cancel_job": {"domain": "system", "risk": "low"},
    "list_jobs": {"domain": "system", "risk": "low"},
    "list_dead_letters": {"domain": "system", "risk": "low"},
    "runtime_health": {"domain": "system", "risk": "low"},
    "show_presence": {"domain": "system", "risk": "low"},
    "prune_presence": {"domain": "system", "risk": "low"},
    "handoff_stats": {"domain": "system", "risk": "low"},
    "runtime_profile": {"domain": "system", "risk": "low"},
    "show_diagnostics": {"domain": "system", "risk": "low"},
    "show_continuity": {"domain": "system", "risk": "low"},
    "continuity_health": {"domain": "system", "risk": "low"},
    "continuity_history": {"domain": "system", "risk": "low"},
    "continuity_anomalies": {"domain": "system", "risk": "low"},
    "continuity_incidents": {"domain": "system", "risk": "low"},
    "continuity_next": {"domain": "system", "risk": "low"},
    "continuity_autopilot_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_preview": {"domain": "system", "risk": "low"},
    "continuity_autopilot_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_dry_run": {"domain": "system", "risk": "low"},
    "continuity_autopilot_guardrails": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_recommendation": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_drift": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_alignment": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_policy": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_policy_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_policy_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomalies": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_trend": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_offenders": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_state": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_breaches": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_anomaly_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_policy_matrix": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_dry_run": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_anomalies": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_metrics": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_action_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_apply_actions": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_apply_action": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_actions": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_anomalies": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture_history": {"domain": "system", "risk": "low"},
    "continuity_autopilot_posture": {"domain": "system", "risk": "low"},
    "continuity_autopilot_mode_apply_recommended": {"domain": "system", "risk": "low"},
    "continuity_autopilot_show": {"domain": "system", "risk": "low"},
    "continuity_autopilot_set": {"domain": "system", "risk": "low"},
    "continuity_autopilot_config": {"domain": "system", "risk": "low"},
    "continuity_autopilot_reset": {"domain": "system", "risk": "low"},
    "continuity_autopilot_tick": {"domain": "system", "risk": "low"},
    "continuity_next_apply": {"domain": "system", "risk": "low"},
    "continuity_alerts": {"domain": "system", "risk": "low"},
    "clear_continuity_alerts": {"domain": "system", "risk": "low"},
    "drill_continuity_breach": {"domain": "system", "risk": "low"},
    "snapshot_stats": {"domain": "system", "risk": "low"},
    "verify_journal_integrity": {"domain": "system", "risk": "low"},
    "repair_journal_integrity": {"domain": "system", "risk": "medium"},
    "self_check": {"domain": "system", "risk": "low"},
    "retry_dead_letter": {"domain": "system", "risk": "medium"},
    "purge_dead_letters": {"domain": "system", "risk": "medium"},
    "undo_last": {"domain": "system", "risk": "medium"},
    "list_files": {"domain": "files", "risk": "low"},
    "read_file": {"domain": "files", "risk": "medium"},
    "fetch_url": {"domain": "web", "risk": "medium"},
    "list_audit": {"domain": "system", "risk": "low"},
    "trace_summary": {"domain": "system", "risk": "low"},
    "list_trace": {"domain": "system", "risk": "low"},
    "export_trace": {"domain": "system", "risk": "low"},
    "restore_preview": {"domain": "system", "risk": "medium"},
    "restore_apply": {"domain": "system", "risk": "high"},
    "create_checkpoint": {"domain": "system", "risk": "medium"},
    "list_checkpoints": {"domain": "system", "risk": "low"},
    "restore_checkpoint_latest": {"domain": "system", "risk": "high"},
    "set_persist_fault_mode": {"domain": "system", "risk": "medium"},
    "list_faults": {"domain": "system", "risk": "low"},
    "compact_journal": {"domain": "system", "risk": "high"},
    "retry_persist": {"domain": "system", "risk": "low"},
    "reset_memory": {"domain": "system", "risk": "high"},
}
COMMUTATIVE_MERGE_OPS = {"add_task", "add_note", "add_expense"}


async def execute_operations(session: SessionState, session_id: str, operations: list[dict[str, Any]]) -> dict[str, Any]:
    if not operations:
        return {"ok": True, "message": "No state changes requested.", "toolResults": [], "journalTail": session.journal[-20:]}

    results = []
    ok = True
    message = ""

    for op in operations:
        capability = resolve_capability(op)
        policy = evaluate_policy(op, capability)
        before = memory_counts(session.memory)

        if not policy["allowed"]:
            result = {
                "ok": False,
                "message": policy["reason"],
                "policy": policy,
                "capability": capability,
                "diff": zero_diff(),
            }
        else:
            before_graph = copy.deepcopy(session.graph)
            before_jobs = copy.deepcopy(session.jobs)
            if str(op.get("type", "")) == "retry_persist":
                ok_persist = await persist_sessions_to_disk_safe("manual_retry")
                pending = int((session.faults.get("persist", {}) or {}).get("pendingWrites", 0) or 0)
                op_result = {
                    "ok": bool(ok_persist),
                    "message": "Persist retry succeeded." if ok_persist else f"Persist retry failed. pending={pending}",
                    "previewLines": [
                        f"persist retry: {'ok' if ok_persist else 'failed'}",
                        f"pending writes: {pending}",
                    ],
                }
            else:
                op_result = run_operation(session, op)
            if op_result.get("ok"):
                violations = validate_graph_contract(session.graph)
                if violations:
                    session.graph = before_graph
                    session.jobs = before_jobs
                    op_result = {
                        "ok": False,
                        "message": "Graph contract violation blocked mutation.",
                        "previewLines": violations[:5],
                    }
            session.memory = graph_to_memory(session.graph)
            after = memory_counts(session.memory)
            if op_result.get("ok") and should_record_undo(op.get("type", "")):
                push_undo_snapshot(session, op.get("type", "unknown"), before_graph, before_jobs)
            result = {
                **op_result,
                "policy": policy,
                "capability": capability,
                "diff": counts_diff(before, after),
            }

        record_journal_entry(session, session_id, op, result)
        results.append({"op": op["type"], **result})
        ok = ok and result["ok"]
        message = result["message"]

    return {"ok": ok, "message": message, "toolResults": results, "journalTail": session.journal[-20:]}


def resolve_capability(op: dict[str, Any]) -> dict[str, Any]:
    kind = str(op.get("type", ""))
    spec = CAPABILITY_REGISTRY.get(kind)
    if not spec:
        return {"name": kind, "domain": "unknown", "risk": "high", "known": False}
    return {"name": kind, "domain": spec["domain"], "risk": spec["risk"], "known": True}


def evaluate_policy(op: dict[str, Any], capability: dict[str, Any]) -> dict[str, Any]:
    if not capability.get("known"):
        return {"allowed": False, "reason": f"Policy denied unknown capability: {capability['name']}", "code": "unknown_capability"}

    risk = capability.get("risk", "low")
    payload = op.get("payload", {}) if isinstance(op.get("payload", {}), dict) else {}

    if capability["name"] == "restore_apply" and not bool(payload.get("confirmed")):
        return {
            "allowed": False,
            "reason": "Policy requires confirmation for high-risk action. Try: confirm restore apply",
            "code": "confirmation_required",
        }
    if capability["name"] == "restore_checkpoint_latest" and not bool(payload.get("confirmed")):
        return {
            "allowed": False,
            "reason": "Policy requires confirmation for high-risk action. Try: confirm restore checkpoint latest",
            "code": "confirmation_required",
        }
    if capability["name"] == "compact_journal" and not bool(payload.get("confirmed")):
        keep = max(1, int(payload.get("keep", 200) or 200))
        return {
            "allowed": False,
            "reason": f"Policy requires confirmation for high-risk action. Try: confirm compact journal keep {keep}",
            "code": "confirmation_required",
        }

    if risk == "high" and not bool(payload.get("confirmed")):
        return {
            "allowed": False,
            "reason": "Policy requires confirmation for high-risk action. Try: confirm reset memory",
            "code": "confirmation_required",
        }
    if capability["name"] in {"list_files", "read_file"}:
        target = resolve_workspace_path(str(payload.get("path", ".") or "."))
        if target is None:
            return {
                "allowed": False,
                "reason": "Policy denied path outside workspace root.",
                "code": "path_outside_workspace",
            }
    if capability["name"] == "fetch_url":
        url = str(payload.get("url", "")).strip()
        if not is_allowed_web_url(url):
            return {
                "allowed": False,
                "reason": "Policy denied URL. Only public http/https URLs are allowed.",
                "code": "url_not_allowed",
            }

    return {"allowed": True, "reason": "allowed", "code": "ok"}


def build_kernel_trace(
    route: dict[str, Any],
    execution: dict[str, Any],
    session: SessionState,
    performance: dict[str, Any] | None = None,
    slo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tool_results = execution.get("toolResults", [])
    policy_codes = [str(item.get("policy", {}).get("code", "unknown")) for item in tool_results]
    diff = zero_diff()
    for item in tool_results:
        op_diff = item.get("diff", {})
        diff["tasks"] += int(op_diff.get("tasks", 0) or 0)
        diff["expenses"] += int(op_diff.get("expenses", 0) or 0)
        diff["notes"] += int(op_diff.get("notes", 0) or 0)

    presence = build_presence_payload(session, "local")
    return {
        "route": {
            "target": route.get("target", "deterministic"),
            "reason": route.get("reason", "default"),
            "model": route.get("model"),
            "intentClass": route.get("intentClass", "unknown"),
            "confidence": float(route.get("confidence", 0.0) or 0.0),
        },
        "policy": {
            "allAllowed": bool(execution.get("ok", False)),
            "codes": policy_codes,
        },
        "diff": diff,
        "graph": {
            **graph_counts(session.graph),
            "relationKinds": graph_relation_kinds(session.graph),
            "recentRelationEvents": recent_relation_events(session.graph, 5),
        },
        "runtime": {
            "jobsActive": count_active_jobs(session.jobs),
            "nextRunAt": next_due_job_time(session.jobs),
            "jobsPreview": runtime_jobs_preview(session.jobs, 4),
            "deadLetters": {
                "count": len(session.dead_letters),
                "preview": [
                    {
                        "id": str(item.get("id", ""))[:8],
                        "kind": str(item.get("kind", "job")),
                        "attempts": int(item.get("attempts", 0) or 0),
                    }
                    for item in session.dead_letters[-3:]
                ],
            },
            "undoDepth": len(session.undo_stack),
            "performance": performance or {
                "parseMs": 0,
                "executeMs": 0,
                "planMs": 0,
                "totalMs": 0,
                "budgetMs": int(TURN_LATENCY_BUDGET_MS),
                "withinBudget": True,
            },
            "slo": slo or {
                "breachStreak": int(session.slo.get("breachStreak", 0)),
                "throttleUntil": int(session.slo.get("throttleUntil", 0)),
                "throttled": bool(int(session.slo.get("throttleUntil", 0) or 0) > now_ms()),
                "lastTotalMs": int(session.slo.get("lastTotalMs", 0)),
                "alerts": list(session.slo.get("alerts", []))[-3:],
            },
            "restore": session.restore.get("last"),
            "presence": {
                "activeCount": int(presence.get("activeCount", 0) or 0),
                "count": int(presence.get("count", 0) or 0),
            },
            "faults": {
                "persist": {
                    "degraded": bool((session.faults.get("persist", {}) or {}).get("degraded", False)),
                    "lastError": str((session.faults.get("persist", {}) or {}).get("lastError", ""))[:160],
                    "lastFailureAt": int((session.faults.get("persist", {}) or {}).get("lastFailureAt", 0) or 0),
                    "lastSuccessAt": int((session.faults.get("persist", {}) or {}).get("lastSuccessAt", 0) or 0),
                    "pendingWrites": int((session.faults.get("persist", {}) or {}).get("pendingWrites", 0) or 0),
                    "simulation": bool(SIMULATE_PERSIST_FAILURE),
                }
            },
            "handoff": {
                "activeDeviceId": session.handoff.get("activeDeviceId"),
                "pending": session.handoff.get("pending"),
                "lastClaimAt": session.handoff.get("lastClaimAt"),
            },
        },
        "journalTail": session.journal[-10:],
    }


def memory_counts(memory: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    return {
        "tasks": len(memory.get("tasks", [])),
        "expenses": len(memory.get("expenses", [])),
        "notes": len(memory.get("notes", [])),
    }


def counts_diff(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        "tasks": after.get("tasks", 0) - before.get("tasks", 0),
        "expenses": after.get("expenses", 0) - before.get("expenses", 0),
        "notes": after.get("notes", 0) - before.get("notes", 0),
    }


def zero_diff() -> dict[str, int]:
    return {"tasks": 0, "expenses": 0, "notes": 0}


def is_slo_throttled(session: SessionState) -> bool:
    return int(session.slo.get("throttleUntil", 0) or 0) > now_ms()


def update_slo_state(session: SessionState, performance: dict[str, Any]) -> dict[str, Any]:
    total_ms = int(performance.get("totalMs", 0) or 0)
    budget_ms = int(performance.get("budgetMs", TURN_LATENCY_BUDGET_MS) or TURN_LATENCY_BUDGET_MS)
    breach = bool(total_ms > budget_ms)
    current_streak = int(session.slo.get("breachStreak", 0) or 0)
    next_streak = current_streak + 1 if breach else 0
    throttle_until = int(session.slo.get("throttleUntil", 0) or 0)
    alerts = list(session.slo.get("alerts", []))
    if next_streak >= int(SLO_BREACH_STREAK_FOR_THROTTLE):
        throttle_until = now_ms() + int(SLO_THROTTLE_MS)
        alerts.append({"ts": now_ms(), "type": "throttle", "totalMs": total_ms, "budgetMs": budget_ms, "streak": next_streak})
    session.slo["breachStreak"] = next_streak
    session.slo["throttleUntil"] = throttle_until
    session.slo["lastTotalMs"] = total_ms
    session.slo["alerts"] = alerts[-20:]
    return {
        "breachStreak": next_streak,
        "throttleUntil": throttle_until,
        "throttled": bool(throttle_until > now_ms()),
        "lastTotalMs": total_ms,
        "alerts": alerts[-3:],
    }


def should_record_undo(op_type: str) -> bool:
    return op_type not in {
        "explain_intent",
        "preview_intent",
        "policy_drill_confirm",
        "policy_drill_deny",
        "list_jobs",
        "list_dead_letters",
        "runtime_health",
        "show_presence",
        "prune_presence",
        "handoff_stats",
        "runtime_profile",
        "show_diagnostics",
        "show_continuity",
        "continuity_health",
        "continuity_history",
        "continuity_anomalies",
        "continuity_incidents",
        "continuity_next",
        "continuity_autopilot_history",
        "continuity_autopilot_preview",
        "continuity_autopilot_metrics",
        "continuity_autopilot_dry_run",
        "continuity_autopilot_guardrails",
        "continuity_autopilot_mode_recommendation",
        "continuity_autopilot_mode_drift",
        "continuity_autopilot_mode_alignment",
        "continuity_autopilot_mode_policy",
        "continuity_autopilot_mode_policy_history",
        "continuity_autopilot_mode_policy_matrix",
        "continuity_autopilot_posture_action_policy_anomalies",
        "continuity_autopilot_posture_action_policy_anomaly_history",
        "continuity_autopilot_posture_action_policy_anomaly_trend",
        "continuity_autopilot_posture_action_policy_anomaly_offenders",
        "continuity_autopilot_posture_action_policy_anomaly_state",
        "continuity_autopilot_posture_action_policy_anomaly_budget",
        "continuity_autopilot_posture_action_policy_anomaly_budget_breaches",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply",
        "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch",
        "continuity_autopilot_posture_action_policy_anomaly_metrics",
        "continuity_autopilot_posture_action_policy_metrics",
        "continuity_autopilot_posture_action_policy_history",
        "continuity_autopilot_posture_action_policy_matrix",
        "continuity_autopilot_posture_action_dry_run",
        "continuity_autopilot_posture_action_anomalies",
        "continuity_autopilot_posture_action_metrics",
        "continuity_autopilot_posture_action_history",
        "continuity_autopilot_posture_apply_actions",
        "continuity_autopilot_posture_apply_action",
        "continuity_autopilot_posture_actions",
        "continuity_autopilot_posture_anomalies",
        "continuity_autopilot_posture_history",
        "continuity_autopilot_posture",
        "continuity_autopilot_mode_apply_recommended",
        "continuity_autopilot_show",
        "continuity_autopilot_set",
        "continuity_autopilot_config",
        "continuity_autopilot_reset",
        "continuity_autopilot_tick",
        "continuity_next_apply",
        "continuity_alerts",
        "clear_continuity_alerts",
        "drill_continuity_breach",
        "snapshot_stats",
        "verify_journal_integrity",
        "repair_journal_integrity",
        "self_check",
        "list_files",
        "read_file",
        "fetch_url",
        "undo_last",
        "list_audit",
        "trace_summary",
        "list_trace",
        "export_trace",
        "restore_preview",
        "list_checkpoints",
        "set_persist_fault_mode",
        "list_faults",
        "compact_journal",
        "retry_persist",
    }


def is_replayable_mutation_op(op_type: str) -> bool:
    return op_type in {
        "add_task",
        "toggle_task",
        "delete_task",
        "clear_completed",
        "add_expense",
        "add_note",
        "link_entities",
        "schedule_watch_task",
        "schedule_remind_note",
        "schedule_audit_open_tasks",
        "schedule_summarize_expenses_daily",
        "schedule_failing_probe",
        "pause_job",
        "resume_job",
        "cancel_job",
        "undo_last",
        "reset_memory",
    }


def push_undo_snapshot(session: SessionState, op_type: str, graph: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    session.undo_stack.append(
        {
            "id": str(uuid.uuid4())[:8],
            "timestamp": now_ms(),
            "op": op_type,
            "graph": graph,
            "jobs": jobs,
        }
    )
    if len(session.undo_stack) > 100:
        session.undo_stack[:] = session.undo_stack[-100:]


def record_journal_entry(session: SessionState, session_id: str, op: dict[str, Any], result: dict[str, Any]) -> None:
    entry = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": now_ms(),
        "sessionId": session_id,
        "op": op.get("type", "unknown"),
        "payload": sanitize_op_payload(op.get("payload")),
        "domain": result.get("capability", {}).get("domain", "unknown"),
        "risk": result.get("capability", {}).get("risk", "unknown"),
        "ok": bool(result.get("ok")),
        "message": str(result.get("message", "")),
        "policy": result.get("policy", {}),
        "policyCode": str(result.get("policy", {}).get("code", "unknown")),
        "diff": result.get("diff", zero_diff()),
    }
    session.journal.append(entry)
    excess = len(session.journal) - int(JOURNAL_MAX_ENTRIES)
    if excess > 0:
        compact_journal(session, int(JOURNAL_MAX_ENTRIES))


def sanitize_op_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, (int, float, bool)) or value is None:
            out[str(key)] = value
        else:
            out[str(key)] = str(value)[:240]
    return out


def compact_journal(session: SessionState, keep: int) -> int:
    keep_count = max(1, int(keep))
    size = len(session.journal)
    if size <= keep_count:
        return 0
    removed = size - keep_count
    session.journal[:] = session.journal[-keep_count:]
    for checkpoint in session.checkpoints:
        base = int(checkpoint.get("journalSize", 0) or 0)
        checkpoint["journalSize"] = max(0, base - removed)
    return removed


def verify_journal_integrity(session: SessionState, session_id: str) -> dict[str, Any]:
    issues: list[str] = []
    entries = session.journal
    prev_ts = 0
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            issues.append(f"entry[{idx}] not object")
            continue
        entry_id = str(entry.get("id", "")).strip()
        if not entry_id:
            issues.append(f"entry[{idx}] missing id")
        ts = entry.get("timestamp")
        if not isinstance(ts, int) or int(ts) <= 0:
            issues.append(f"entry[{idx}] invalid timestamp")
            ts = prev_ts
        if int(ts) < int(prev_ts):
            issues.append(f"entry[{idx}] timestamp out of order")
        prev_ts = int(ts)
        if str(entry.get("sessionId", "")).strip() != str(session_id):
            issues.append(f"entry[{idx}] session mismatch")
        if not str(entry.get("op", "")).strip():
            issues.append(f"entry[{idx}] missing op")
        diff = entry.get("diff", {})
        if not isinstance(diff, dict):
            issues.append(f"entry[{idx}] diff not object")
        else:
            for key in ("tasks", "expenses", "notes"):
                if key not in diff:
                    issues.append(f"entry[{idx}] diff missing {key}")
    return {
        "valid": not bool(issues),
        "count": len(entries),
        "issues": issues[:200],
    }


def repair_journal_integrity(session: SessionState, session_id: str) -> dict[str, Any]:
    before = len(session.journal)
    repaired: list[dict[str, Any]] = []
    now = now_ms()
    for idx, raw in enumerate(session.journal):
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op", "")).strip()
        if not op:
            continue
        diff = raw.get("diff", {})
        if not isinstance(diff, dict):
            diff = zero_diff()
        sanitized_diff = {
            "tasks": int(diff.get("tasks", 0) or 0),
            "expenses": int(diff.get("expenses", 0) or 0),
            "notes": int(diff.get("notes", 0) or 0),
        }
        ts = raw.get("timestamp")
        if not isinstance(ts, int) or int(ts) <= 0:
            ts = now + idx
        repaired.append(
            {
                "id": str(raw.get("id", ""))[:8] or str(uuid.uuid4())[:8],
                "timestamp": int(ts),
                "sessionId": session_id,
                "op": op,
                "payload": sanitize_op_payload(raw.get("payload", {})),
                "domain": str(raw.get("domain", "unknown")),
                "risk": str(raw.get("risk", "unknown")),
                "ok": bool(raw.get("ok", False)),
                "message": str(raw.get("message", ""))[:240],
                "policy": raw.get("policy", {}) if isinstance(raw.get("policy", {}), dict) else {},
                "policyCode": str(raw.get("policyCode", "unknown")),
                "diff": sanitized_diff,
            }
        )
    repaired.sort(key=lambda item: int(item.get("timestamp", 0) or 0))
    session.journal[:] = repaired
    return {
        "before": before,
        "after": len(repaired),
        "removed": max(0, before - len(repaired)),
        "report": verify_journal_integrity(session, session_id),
    }


def filter_journal_entries(
    entries: list[dict[str, Any]],
    domain: str | None = None,
    risk: str | None = None,
    ok: bool | None = None,
    op: str | None = None,
    policy_code: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    dom = str(domain or "").strip().lower()
    rk = str(risk or "").strip().lower()
    opname = str(op or "").strip().lower()
    policy = str(policy_code or "").strip().lower()
    out: list[dict[str, Any]] = []
    for entry in reversed(entries):
        if dom and str(entry.get("domain", "")).strip().lower() != dom:
            continue
        if rk and str(entry.get("risk", "")).strip().lower() != rk:
            continue
        if ok is not None and bool(entry.get("ok")) != bool(ok):
            continue
        if opname and str(entry.get("op", "")).strip().lower() != opname:
            continue
        if policy and str(entry.get("policyCode", "")).strip().lower() != policy:
            continue
        out.append(entry)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def audit_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_domain: dict[str, int] = {}
    by_policy: dict[str, int] = {}
    allow = 0
    deny = 0
    for entry in entries:
        domain = str(entry.get("domain", "unknown"))
        by_domain[domain] = by_domain.get(domain, 0) + 1
        policy = str(entry.get("policyCode", "unknown"))
        by_policy[policy] = by_policy.get(policy, 0) + 1
        if bool(entry.get("ok")):
            allow += 1
        else:
            deny += 1
    return {"allowed": allow, "denied": deny, "byDomain": by_domain, "byPolicyCode": by_policy}


def rebuild_session_from_journal_entries(entries: list[dict[str, Any]]) -> SessionState:
    rebuilt = SessionState()
    rebuilt.graph = make_empty_graph()
    rebuilt.jobs = []
    rebuilt.undo_stack = []
    for entry in entries:
        if not bool(entry.get("ok")):
            continue
        op_type = str(entry.get("op", "")).strip()
        if not is_replayable_mutation_op(op_type):
            continue
        op = {"type": op_type, "payload": sanitize_op_payload(entry.get("payload", {}))}
        before_graph = copy.deepcopy(rebuilt.graph)
        before_jobs = copy.deepcopy(rebuilt.jobs)
        result = run_operation(rebuilt, op)
        if result.get("ok") and should_record_undo(op_type):
            push_undo_snapshot(rebuilt, op_type, before_graph, before_jobs)
        rebuilt.memory = graph_to_memory(rebuilt.graph)
    return rebuilt


def create_checkpoint(session: SessionState, reason: str) -> dict[str, Any]:
    prune_checkpoints(session)
    checkpoint = {
        "id": str(uuid.uuid4())[:8],
        "timestamp": now_ms(),
        "reason": str(reason or "manual"),
        "revision": int(session.revision),
        "journalSize": len(session.journal),
        "graph": copy.deepcopy(session.graph),
        "jobs": copy.deepcopy(session.jobs),
        "undoStack": copy.deepcopy(session.undo_stack),
    }
    session.checkpoints.append(checkpoint)
    prune_checkpoints(session)
    return checkpoint


def prune_checkpoints(session: SessionState) -> None:
    if not session.checkpoints:
        return
    cutoff = now_ms() - max(0, int(CHECKPOINT_MAX_AGE_MS))
    keep: list[dict[str, Any]] = []
    for checkpoint in session.checkpoints:
        ts = int(checkpoint.get("timestamp", 0) or 0)
        if CHECKPOINT_MAX_AGE_MS > 0 and ts and ts < cutoff:
            continue
        keep.append(checkpoint)
    if CHECKPOINT_MAX_COUNT > 0 and len(keep) > CHECKPOINT_MAX_COUNT:
        keep = keep[-CHECKPOINT_MAX_COUNT:]
    session.checkpoints[:] = keep


def find_checkpoint(session: SessionState, checkpoint_id: str | None) -> dict[str, Any] | None:
    if not session.checkpoints:
        return None
    if checkpoint_id:
        wanted = str(checkpoint_id).strip()
        for item in reversed(session.checkpoints):
            if str(item.get("id", "")).startswith(wanted):
                return item
        return None
    return session.checkpoints[-1]


def apply_checkpoint_to_session(session: SessionState, checkpoint: dict[str, Any], replay_tail: bool = True) -> None:
    session.graph = copy.deepcopy(checkpoint.get("graph") or make_empty_graph())
    session.jobs = copy.deepcopy(checkpoint.get("jobs") or [])
    session.undo_stack = copy.deepcopy(checkpoint.get("undoStack") or [])
    if replay_tail:
        start = int(checkpoint.get("journalSize", 0) or 0)
        tail = session.journal[start:]
        replay_onto_state(session, tail)
    session.memory = graph_to_memory(session.graph)


def replay_onto_state(session: SessionState, entries: list[dict[str, Any]]) -> None:
    for entry in entries:
        if not bool(entry.get("ok")):
            continue
        op_type = str(entry.get("op", "")).strip()
        if not is_replayable_mutation_op(op_type):
            continue
        op = {"type": op_type, "payload": sanitize_op_payload(entry.get("payload", {}))}
        before_graph = copy.deepcopy(session.graph)
        before_jobs = copy.deepcopy(session.jobs)
        result = run_operation(session, op)
        if result.get("ok") and should_record_undo(op_type):
            push_undo_snapshot(session, op_type, before_graph, before_jobs)
    session.memory = graph_to_memory(session.graph)


def make_empty_graph() -> dict[str, Any]:
    return {"entities": [], "relations": [], "events": []}


def normalize_entity_kind(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw.endswith("s"):
        raw = raw[:-1]
    return raw


def graph_add_entity(graph: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    entity = {"id": str(uuid.uuid4()), **payload}
    graph.setdefault("entities", []).append(entity)
    return entity


def graph_delete_entity(graph: dict[str, Any], entity_id: str) -> None:
    entities = graph.setdefault("entities", [])
    graph["entities"] = [e for e in entities if e.get("id") != entity_id]
    relations = graph.setdefault("relations", [])
    graph["relations"] = [r for r in relations if r.get("sourceId") != entity_id and r.get("targetId") != entity_id]


def graph_add_event(graph: dict[str, Any], kind: str, payload: dict[str, Any]) -> None:
    graph.setdefault("events", []).append(
        {"id": str(uuid.uuid4())[:8], "kind": kind, "payload": payload, "createdAt": now_ms()}
    )
    if len(graph["events"]) > 2000:
        graph["events"] = graph["events"][-2000:]


def graph_add_relation(graph: dict[str, Any], source_id: str, target_id: str, kind: str) -> bool:
    relations = graph.setdefault("relations", [])
    for relation in relations:
        if relation.get("sourceId") == source_id and relation.get("targetId") == target_id and relation.get("kind") == kind:
            return False
    relations.append({"id": str(uuid.uuid4())[:8], "sourceId": source_id, "targetId": target_id, "kind": kind, "createdAt": now_ms()})
    return True


def would_create_dependency_cycle(graph: dict[str, Any], source_id: str, target_id: str) -> bool:
    if source_id == target_id:
        return True
    # For source -> target edge, a cycle exists if target can already reach source.
    outgoing: dict[str, list[str]] = {}
    for rel in graph.get("relations", []):
        if str(rel.get("kind", "")).strip().lower() != "depends_on":
            continue
        src = str(rel.get("sourceId", "")).strip()
        dst = str(rel.get("targetId", "")).strip()
        if not src or not dst:
            continue
        outgoing.setdefault(src, []).append(dst)

    stack = [target_id]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == source_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(outgoing.get(current, []))
    return False


def validate_graph_contract(graph: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    entities_raw = graph.get("entities", [])
    relations_raw = graph.get("relations", [])
    events_raw = graph.get("events", [])
    if not isinstance(entities_raw, list):
        return ["entities must be a list"]
    if not isinstance(relations_raw, list):
        return ["relations must be a list"]
    if not isinstance(events_raw, list):
        return ["events must be a list"]

    entity_ids: set[str] = set()
    entity_kind_by_id: dict[str, str] = {}
    for idx, entity in enumerate(entities_raw):
        if not isinstance(entity, dict):
            violations.append(f"entity[{idx}] must be object")
            continue
        eid = str(entity.get("id", "")).strip()
        if not eid:
            violations.append(f"entity[{idx}] missing id")
            continue
        if eid in entity_ids:
            violations.append(f"entity id duplicate: {eid[:8]}")
            continue
        entity_ids.add(eid)
        kind = normalize_entity_kind(entity.get("kind", ""))
        if kind not in GRAPH_ENTITY_KINDS:
            violations.append(f"entity[{idx}] invalid kind: {kind or '-'}")
            continue
        entity_kind_by_id[eid] = kind

        created_at = entity.get("createdAt")
        if not isinstance(created_at, int) or int(created_at) <= 0:
            violations.append(f"entity[{idx}] invalid createdAt")
        if kind == "task":
            title = str(entity.get("title", "")).strip()
            if not title:
                violations.append(f"task[{idx}] missing title")
            if len(title) > 200:
                violations.append(f"task[{idx}] title too long")
            if not isinstance(entity.get("done", False), bool):
                violations.append(f"task[{idx}] done must be bool")
        elif kind == "expense":
            amount = entity.get("amount", 0)
            try:
                amount_num = float(amount)
            except Exception:
                amount_num = 0.0
            if amount_num <= 0:
                violations.append(f"expense[{idx}] amount must be > 0")
            category = str(entity.get("category", "")).strip()
            if not category:
                violations.append(f"expense[{idx}] missing category")
            if len(category) > 60:
                violations.append(f"expense[{idx}] category too long")
            note = str(entity.get("note", ""))
            if len(note) > 240:
                violations.append(f"expense[{idx}] note too long")
        elif kind == "note":
            text = str(entity.get("text", "")).strip()
            if not text:
                violations.append(f"note[{idx}] missing text")
            if len(text) > 2000:
                violations.append(f"note[{idx}] text too long")

    relation_triplets: set[tuple[str, str, str]] = set()
    for idx, relation in enumerate(relations_raw):
        if not isinstance(relation, dict):
            violations.append(f"relation[{idx}] must be object")
            continue
        source_id = str(relation.get("sourceId", "")).strip()
        target_id = str(relation.get("targetId", "")).strip()
        kind = str(relation.get("kind", "")).strip().lower()
        if not source_id or not target_id:
            violations.append(f"relation[{idx}] missing endpoints")
            continue
        if source_id == target_id:
            violations.append(f"relation[{idx}] self-link not allowed")
        if source_id not in entity_ids or target_id not in entity_ids:
            violations.append(f"relation[{idx}] endpoint not found")
        if kind not in GRAPH_RELATION_KINDS:
            violations.append(f"relation[{idx}] invalid kind: {kind or '-'}")
        if kind == "depends_on":
            if entity_kind_by_id.get(source_id) != "task" or entity_kind_by_id.get(target_id) != "task":
                violations.append(f"relation[{idx}] depends_on must link task->task")
        triplet = (source_id, target_id, kind)
        if triplet in relation_triplets:
            violations.append(f"relation[{idx}] duplicate relation")
        relation_triplets.add(triplet)

    for idx, event in enumerate(events_raw):
        if not isinstance(event, dict):
            violations.append(f"event[{idx}] must be object")
            continue
        if not str(event.get("kind", "")).strip():
            violations.append(f"event[{idx}] missing kind")
        if not isinstance(event.get("payload", {}), dict):
            violations.append(f"event[{idx}] payload must be object")
        created_at = event.get("createdAt")
        if not isinstance(created_at, int) or int(created_at) <= 0:
            violations.append(f"event[{idx}] invalid createdAt")

    return violations[:30]


def graph_reset_domain_entities(graph: dict[str, Any], kinds: set[str]) -> None:
    entities = graph.setdefault("entities", [])
    drop_ids = {e.get("id") for e in entities if e.get("kind") in kinds}
    graph["entities"] = [e for e in entities if e.get("kind") not in kinds]
    relations = graph.setdefault("relations", [])
    graph["relations"] = [r for r in relations if r.get("sourceId") not in drop_ids and r.get("targetId") not in drop_ids]


def graph_clear_completed_tasks(graph: dict[str, Any]) -> int:
    entities = graph.setdefault("entities", [])
    to_drop = [e for e in entities if e.get("kind") == "task" and bool(e.get("done"))]
    if not to_drop:
        return 0
    drop_ids = {e.get("id") for e in to_drop}
    graph["entities"] = [e for e in entities if e.get("id") not in drop_ids]
    relations = graph.setdefault("relations", [])
    graph["relations"] = [r for r in relations if r.get("sourceId") not in drop_ids and r.get("targetId") not in drop_ids]
    return len(drop_ids)


def graph_counts(graph: dict[str, Any]) -> dict[str, Any]:
    entities = graph.get("entities", [])
    per_kind: dict[str, int] = {}
    for entity in entities:
        k = str(entity.get("kind", "unknown"))
        per_kind[k] = per_kind.get(k, 0) + 1
    return {
        "entities": len(entities),
        "relations": len(graph.get("relations", [])),
        "events": len(graph.get("events", [])),
        "byKind": per_kind,
    }


def graph_entities_by_kind(graph: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    k = normalize_entity_kind(kind)
    entities = [e for e in graph.get("entities", []) if normalize_entity_kind(e.get("kind", "")) == k]
    entities.sort(key=lambda x: int(x.get("createdAt", 0) or 0), reverse=True)
    return entities


def graph_to_memory(graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    entities = graph.get("entities", [])
    tasks = [
        {"id": e.get("id"), "title": str(e.get("title", "")), "done": bool(e.get("done", False)), "createdAt": int(e.get("createdAt", now_ms()))}
        for e in entities
        if e.get("kind") == "task"
    ]
    expenses = [
        {
            "id": e.get("id"),
            "amount": float(e.get("amount", 0)),
            "category": str(e.get("category", "general")),
            "note": str(e.get("note", "")),
            "createdAt": int(e.get("createdAt", now_ms())),
        }
        for e in entities
        if e.get("kind") == "expense"
    ]
    notes = [
        {"id": e.get("id"), "text": str(e.get("text", "")), "createdAt": int(e.get("createdAt", now_ms()))}
        for e in entities
        if e.get("kind") == "note"
    ]
    tasks.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    expenses.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    notes.sort(key=lambda x: x.get("createdAt", 0), reverse=True)
    return {"tasks": tasks, "expenses": expenses, "notes": notes}


def graph_projection(graph: dict[str, Any]) -> dict[str, Any]:
    memory = graph_to_memory(graph)
    return {
        **memory,
        "relationCount": len(graph.get("relations", [])),
        "eventCount": len(graph.get("events", [])),
    }


def graph_relation_kinds(graph: dict[str, Any]) -> dict[str, int]:
    kinds: dict[str, int] = {}
    for rel in graph.get("relations", []):
        k = str(rel.get("kind", "unknown"))
        kinds[k] = kinds.get(k, 0) + 1
    return kinds


def recent_relation_events(graph: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    events = [e for e in graph.get("events", []) if str(e.get("kind", "")).startswith("link_")]
    return events[-limit:]


def memory_to_graph(memory: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    graph = make_empty_graph()
    for task in memory.get("tasks", []):
        graph_add_entity(
            graph,
            {
                "kind": "task",
                "title": str(task.get("title", "")),
                "done": bool(task.get("done", False)),
                "createdAt": int(task.get("createdAt", now_ms())),
            },
        )
    for expense in memory.get("expenses", []):
        graph_add_entity(
            graph,
            {
                "kind": "expense",
                "amount": float(expense.get("amount", 0)),
                "category": str(expense.get("category", "general")),
                "note": str(expense.get("note", "")),
                "createdAt": int(expense.get("createdAt", now_ms())),
            },
        )
    for note in memory.get("notes", []):
        graph_add_entity(
            graph,
            {"kind": "note", "text": str(note.get("text", "")), "createdAt": int(note.get("createdAt", now_ms()))},
        )
    return graph


def find_task_entity(graph: dict[str, Any], selector: str) -> dict[str, Any] | None:
    return find_entity_by_kind_selector(graph, "task", selector)


def find_entity_by_kind_selector(graph: dict[str, Any], kind: str, selector: str) -> dict[str, Any] | None:
    entities = graph_entities_by_kind(graph, kind)
    if str(selector).isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(entities):
            return entities[idx]
    for entity in entities:
        if str(entity.get("id", "")).startswith(str(selector)):
            return entity
    return None


def find_task_for_job(graph: dict[str, Any], job: dict[str, Any]) -> dict[str, Any] | None:
    entity_id = str(job.get("taskEntityId", "")).strip()
    if entity_id:
        by_id = next((e for e in graph.get("entities", []) if e.get("id") == entity_id and normalize_entity_kind(e.get("kind", "")) == "task"), None)
        if by_id:
            return by_id
    selector = str(job.get("selector", "")).strip()
    return find_task_entity(graph, selector) if selector else None


def upsert_watch_task_job(jobs: list[dict[str, Any]], task: dict[str, Any], selector: str, interval_minutes: int) -> dict[str, Any]:
    task_id = str(task.get("id", ""))
    return upsert_job(
        jobs,
        {
            "kind": "watch_task",
            "taskEntityId": task_id,
            "selector": selector,
            "intervalMinutes": interval_minutes,
            "intervalMs": interval_minutes * 60_000,
        },
        dedupe_key=f"watch_task:{task_id}",
    )


def upsert_job(jobs: list[dict[str, Any]], payload: dict[str, Any], dedupe_key: str) -> dict[str, Any]:
    for job in jobs:
        if str(job.get("dedupeKey", "")) == dedupe_key:
            job.update(payload)
            job["active"] = True
            interval_ms = int(job.get("intervalMs", 0) or 0)
            job["nextRunAt"] = now_ms() + (interval_ms if interval_ms > 0 else 60_000)
            return job

    interval_ms = int(payload.get("intervalMs", 0) or 0)
    job = {
        "id": str(uuid.uuid4())[:8],
        "dedupeKey": dedupe_key,
        "active": True,
        "createdAt": now_ms(),
        "lastRunAt": None,
        "lastRunKey": None,
        "nextRunAt": now_ms() + (interval_ms if interval_ms > 0 else 60_000),
        **payload,
    }
    jobs.append(job)
    return job


def find_job_by_selector(jobs: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    sel = str(selector or "").strip()
    if not sel:
        return None
    ordered = sorted(jobs, key=lambda x: int(x.get("createdAt", 0) or 0), reverse=True)
    if sel.isdigit():
        idx = int(sel) - 1
        if 0 <= idx < len(ordered):
            return ordered[idx]
    for job in ordered:
        if str(job.get("id", "")).startswith(sel):
            return job
    return None


def enqueue_dead_letter(session: SessionState, job: dict[str, Any], error: str, attempts: int) -> None:
    entry = {
        "id": str(uuid.uuid4())[:8],
        "jobId": str(job.get("id", ""))[:32],
        "kind": str(job.get("kind", "job"))[:64],
        "error": str(error)[:240],
        "attempts": int(attempts or 0),
        "failedAt": now_ms(),
        "job": copy.deepcopy(job),
    }
    session.dead_letters.append(entry)
    if len(session.dead_letters) > 300:
        session.dead_letters[:] = session.dead_letters[-300:]


def find_dead_letter_by_selector(dead_letters: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    sel = str(selector or "").strip()
    if not sel:
        return None
    ordered = list(reversed(dead_letters))
    if sel.isdigit():
        idx = int(sel) - 1
        if 0 <= idx < len(ordered):
            return ordered[idx]
    for item in ordered:
        if str(item.get("id", "")).startswith(sel):
            return item
    return None


def compute_job_run_key(job: dict[str, Any]) -> str:
    interval_ms = int(job.get("intervalMs", 0) or 0)
    if interval_ms <= 0:
        return ""
    bucket = int(now_ms() // interval_ms)
    return f"{job.get('id')}:{bucket}"


def count_active_jobs(jobs: list[dict[str, Any]]) -> int:
    return sum(1 for job in jobs if bool(job.get("active", True)))


def next_due_job_time(jobs: list[dict[str, Any]]) -> int | None:
    due = [int(job.get("nextRunAt", 0) or 0) for job in jobs if bool(job.get("active", True))]
    return min(due) if due else None


def runtime_jobs_preview(jobs: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    ordered = sorted(jobs, key=lambda x: int(x.get("createdAt", 0) or 0), reverse=True)
    out: list[dict[str, Any]] = []
    for job in ordered[:limit]:
        out.append(
            {
                "id": str(job.get("id", ""))[:8],
                "kind": str(job.get("kind", "job")),
                "active": bool(job.get("active", True)),
                "intervalMinutes": int(job.get("intervalMinutes", 0) or 0),
                "nextRunAt": int(job.get("nextRunAt", 0) or 0) if job.get("nextRunAt") else None,
                "lastRunAt": int(job.get("lastRunAt", 0) or 0) if job.get("lastRunAt") else None,
                "lastResult": str(job.get("lastResult", ""))[:120],
                "failureCount": int(job.get("failureCount", 0) or 0),
                "lastError": str(job.get("lastError", ""))[:120],
            }
        )
    return out


def parse_relation_query(raw_text: str) -> dict[str, str] | None:
    text = str(raw_text or "").strip().lower()
    dep = re.match(r"^(show\s+)?dependencies\s+for\s+task\s+(\S+)$", text)
    if dep:
        return {"mode": "dependencies", "kind": "task", "selector": dep.group(2)}
    dep_chain = re.match(r"^(show\s+)?dependency\s+chain\s+for\s+task\s+(\S+)$", text)
    if dep_chain:
        return {"mode": "dependency_chain", "kind": "task", "selector": dep_chain.group(2)}
    blockers = re.match(r"^(show\s+)?blockers\s+for\s+task\s+(\S+)$", text)
    if blockers:
        return {"mode": "blockers", "kind": "task", "selector": blockers.group(2)}
    impact = re.match(r"^(show\s+)?impact\s+for\s+task\s+(\S+)$", text)
    if impact:
        return {"mode": "impact", "kind": "task", "selector": impact.group(2)}
    ref = re.match(r"^(show\s+)?references\s+for\s+note\s+(\S+)$", text)
    if ref:
        return {"mode": "references", "kind": "note", "selector": ref.group(2)}
    return None


def dependency_targets_for(graph: dict[str, Any], source_id: str) -> list[str]:
    out: list[str] = []
    for rel in graph.get("relations", []):
        if str(rel.get("kind", "")).strip().lower() != "depends_on":
            continue
        if str(rel.get("sourceId", "")).strip() != str(source_id):
            continue
        target_id = str(rel.get("targetId", "")).strip()
        if target_id:
            out.append(target_id)
    return out


def dependency_sources_for(graph: dict[str, Any], target_id: str) -> list[str]:
    out: list[str] = []
    for rel in graph.get("relations", []):
        if str(rel.get("kind", "")).strip().lower() != "depends_on":
            continue
        if str(rel.get("targetId", "")).strip() != str(target_id):
            continue
        source_id = str(rel.get("sourceId", "")).strip()
        if source_id:
            out.append(source_id)
    return out


def longest_dependency_chain_ids(graph: dict[str, Any], source_id: str) -> list[str]:
    memo: dict[str, list[str]] = {}
    visiting: set[str] = set()

    def walk(node_id: str) -> list[str]:
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            return [node_id]
        visiting.add(node_id)
        targets = dependency_targets_for(graph, node_id)
        best_suffix: list[str] = []
        for nxt in targets:
            candidate = walk(nxt)
            if len(candidate) > len(best_suffix):
                best_suffix = candidate
        visiting.discard(node_id)
        path = [node_id, *best_suffix]
        memo[node_id] = path
        return path

    return walk(str(source_id))


def dependency_chain_lines(graph: dict[str, Any], source: dict[str, Any]) -> list[str]:
    path_ids = longest_dependency_chain_ids(graph, str(source.get("id", "")))
    entities_by_id = {str(e.get("id", "")): e for e in graph.get("entities", [])}
    chain_labels = [graph_entity_label(entities_by_id[node_id]) for node_id in path_ids if node_id in entities_by_id]
    if len(chain_labels) <= 1:
        return [f"No dependency chain for {graph_entity_label(source)}."]
    depth = max(0, len(chain_labels) - 1)
    return [
        f"chain depth: {depth}",
        f"chain nodes: {len(chain_labels)}",
        " -> ".join(chain_labels[:8]),
    ]


def transitive_dependency_impact_ids(graph: dict[str, Any], target_id: str) -> list[str]:
    impacted: list[str] = []
    stack = list(dependency_sources_for(graph, target_id))
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        impacted.append(current)
        stack.extend(dependency_sources_for(graph, current))
    return impacted


def dependency_analysis_for_task(graph: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    source_id = str(source.get("id", ""))
    entities_by_id = {str(e.get("id", "")): e for e in graph.get("entities", [])}

    blockers_ids = dependency_targets_for(graph, source_id)
    blockers = [graph_entity_label(entities_by_id[item]) for item in blockers_ids if item in entities_by_id]

    impact_ids = transitive_dependency_impact_ids(graph, source_id)
    impact = [graph_entity_label(entities_by_id[item]) for item in impact_ids if item in entities_by_id]

    chain_ids = longest_dependency_chain_ids(graph, source_id)
    chain_nodes = [graph_entity_label(entities_by_id[item]) for item in chain_ids if item in entities_by_id]
    chain_depth = max(0, len(chain_nodes) - 1)

    return {
        "blockers": {"count": len(blockers), "items": blockers[:20]},
        "impact": {"count": len(impact), "items": impact[:20]},
        "chain": {"depth": chain_depth, "nodes": chain_nodes[:20]},
    }


def build_relation_query_block(graph: dict[str, Any], query: dict[str, str]) -> dict[str, Any]:
    source = find_entity_by_kind_selector(graph, query["kind"], query["selector"])
    if not source:
        return block_list("relation-query", "Relation Query", ["Source entity not found."], span=2)

    if query["mode"] == "dependency_chain":
        lines = dependency_chain_lines(graph, source)
        return block_list("relation-query", "Relation Query", lines[:10], span=2)
    if query["mode"] == "blockers":
        blockers = dependency_targets_for(graph, str(source.get("id", "")))
        entities_by_id = {str(e.get("id", "")): e for e in graph.get("entities", [])}
        items = [graph_entity_label(entities_by_id[item]) for item in blockers if item in entities_by_id]
        if not items:
            lines = [f"No blockers for {graph_entity_label(source)}."]
        else:
            lines = [f"blockers: {len(items)}", *items[:8]]
        return block_list("relation-query", "Relation Query", lines[:10], span=2)
    if query["mode"] == "impact":
        impacted_ids = transitive_dependency_impact_ids(graph, str(source.get("id", "")))
        entities_by_id = {str(e.get("id", "")): e for e in graph.get("entities", [])}
        impacted_labels = [graph_entity_label(entities_by_id[item]) for item in impacted_ids if item in entities_by_id]
        if not impacted_labels:
            lines = [f"No transitive impact from {graph_entity_label(source)}."]
        else:
            lines = [f"transitive impact: {len(impacted_labels)}", *impacted_labels[:8]]
        return block_list("relation-query", "Relation Query", lines[:10], span=2)

    if query["mode"] == "dependencies":
        rels = [
            rel for rel in graph.get("relations", [])
            if rel.get("sourceId") == source.get("id") and rel.get("kind") == "depends_on"
        ]
    else:
        rels = [
            rel for rel in graph.get("relations", [])
            if rel.get("sourceId") == source.get("id") and rel.get("kind") == "references"
        ]

    lines: list[str] = []
    for rel in rels:
        target = next((e for e in graph.get("entities", []) if e.get("id") == rel.get("targetId")), None)
        if not target:
            continue
        lines.append(f"{graph_entity_label(source)} -> {graph_entity_label(target)} ({rel.get('kind')})")

    if not lines:
        lines = [f"No {query['mode']} found for {graph_entity_label(source)}."]
    return block_list("relation-query", "Relation Query", lines[:10], span=2)


def graph_entity_label(entity: dict[str, Any]) -> str:
    kind = normalize_entity_kind(entity.get("kind", "entity"))
    if kind == "task":
        return f"task:{str(entity.get('title', '')).strip()[:48]}"
    if kind == "note":
        return f"note:{str(entity.get('text', '')).strip()[:48]}"
    if kind == "expense":
        return f"expense:{str(entity.get('category', 'general'))}:{float(entity.get('amount', 0)):,.2f}"
    return f"{kind}:{str(entity.get('id', ''))[:8]}"


def build_graph_context_lines(graph: dict[str, Any]) -> list[str]:
    counts = graph_counts(graph)
    kinds = graph_relation_kinds(graph)
    out = [
        f"entities: {counts['entities']}",
        f"relations: {counts['relations']}",
        f"events: {counts['events']}",
    ]
    for kind, val in sorted(kinds.items()):
        out.append(f"{kind}: {val}")
    if kinds.get("depends_on", 0):
        tasks = graph_entities_by_kind(graph, "task")
        if tasks:
            max_depth = 0
            blocked = 0
            roots = 0
            for task in tasks:
                path_ids = longest_dependency_chain_ids(graph, str(task.get("id", "")))
                max_depth = max(max_depth, max(0, len(path_ids) - 1))
                if dependency_targets_for(graph, str(task.get("id", ""))):
                    blocked += 1
                if not dependency_sources_for(graph, str(task.get("id", ""))):
                    roots += 1
            out.append(f"dependency max depth: {max_depth}")
            out.append(f"blocked tasks: {blocked}")
            out.append(f"root tasks: {roots}")
    recent = recent_relation_events(graph, 2)
    for event in recent:
        payload = event.get("payload", {})
        out.append(
            f"event {event.get('kind')}: {str(payload.get('relation', ''))} {str(payload.get('sourceId', ''))[:6]}->{str(payload.get('targetId', ''))[:6]}"
        )
    return out[:10]


def format_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    if not jobs:
        return ["No scheduled jobs."]
    ordered = sorted(jobs, key=lambda x: int(x.get("createdAt", 0) or 0), reverse=True)
    lines: list[str] = []
    for idx, job in enumerate(ordered[:8], start=1):
        state = "active" if bool(job.get("active", True)) else "paused"
        interval = int(job.get("intervalMinutes", 0) or 0)
        kind = str(job.get("kind", "job"))
        jid = str(job.get("id", ""))[:8]
        lines.append(f"{idx}. [{state}] {kind} ({interval}m) #{jid}")
    return lines


def run_operation(session: SessionState, op: dict[str, Any]) -> dict[str, Any]:
    kind = op["type"]
    payload = op.get("payload", {})
    graph = session.graph

    if kind == "explain_intent":
        raw = str(payload.get("text", "")).strip()
        if not raw:
            return {"ok": False, "message": "Intent text is empty. Use: explain intent <text>"}
        envelope = compile_intent_envelope(raw)
        writes = envelope["stateIntent"]["writeOperations"]
        fake_execution = {"ok": True, "message": "explain", "toolResults": []}
        route = planner_route(envelope, fake_execution, session.graph)
        capability_risks = [resolve_capability(item).get("risk", "unknown") for item in writes]
        preview = [
            f"class: {envelope.get('intentClass', 'unknown')}",
            f"confidence: {int(float(envelope.get('confidence', 0.0) or 0.0) * 100)}%",
            f"route: {route.get('target', 'deterministic')} / {route.get('reason', 'default')}",
            f"writes: {len(writes)}",
            f"domains: {','.join(envelope.get('stateIntent', {}).get('readDomains', []))}",
        ]
        if capability_risks:
            preview.append(f"risks: {','.join(str(x) for x in capability_risks)}")
        return {"ok": True, "message": "Intent explanation ready.", "previewLines": preview[:10]}

    if kind == "preview_intent":
        raw = str(payload.get("text", "")).strip()
        if not raw:
            return {"ok": False, "message": "Intent text is empty. Use: preview intent <text>"}
        report = build_intent_preview_report(session, "local", raw)
        lines = [
            f"class: {str(report.get('intentClass', 'unknown'))}",
            f"confidence: {int(float(report.get('confidence', 0.0) or 0.0) * 100)}%",
            f"route: {str((report.get('route') or {}).get('target', 'deterministic'))} / {str((report.get('route') or {}).get('reason', 'default'))}",
            f"writes: {len(report.get('writes', []))}",
        ]
        for item in report.get("writes", [])[:4]:
            lines.append(
                f"{item.get('op')} [{item.get('risk')}] -> {item.get('policy')}"
            )
        return {"ok": True, "message": "Intent preview ready.", "previewLines": lines[:10]}

    if kind == "policy_drill_confirm":
        return {
            "ok": False,
            "message": "Policy drill confirmation required. Try: confirm reset memory",
            "previewLines": ["drill: confirmation path", "next: confirm reset memory"],
        }

    if kind == "policy_drill_deny":
        return {
            "ok": False,
            "message": "Policy drill deny path. Unknown capability blocked.",
            "previewLines": ["drill: deny path", "code: unknown_capability"],
        }

    if kind == "add_task":
        title = str(payload.get("title", "")).strip()
        if not title:
            return {"ok": False, "message": "Task title is empty."}
        graph_add_entity(
            graph,
            {
                "kind": "task",
                "title": title,
                "done": False,
                "createdAt": now_ms(),
            },
        )
        graph_add_event(graph, "add_task", {"title": title})
        return {"ok": True, "message": f"Added task: {title}"}

    if kind == "toggle_task":
        task_entity = find_task_entity(graph, str(payload.get("selector", "")))
        if not task_entity:
            return {"ok": False, "message": "Task not found."}
        task_entity["done"] = not bool(task_entity.get("done", False))
        graph_add_event(graph, "toggle_task", {"entityId": task_entity["id"], "done": task_entity["done"]})
        return {"ok": True, "message": ("Completed" if task_entity["done"] else "Reopened") + f": {task_entity.get('title', '')}"}

    if kind == "delete_task":
        task_entity = find_task_entity(graph, str(payload.get("selector", "")))
        if not task_entity:
            return {"ok": False, "message": "Task not found."}
        graph_delete_entity(graph, task_entity["id"])
        graph_add_event(graph, "delete_task", {"entityId": task_entity["id"]})
        return {"ok": True, "message": f"Deleted task: {task_entity.get('title', '')}"}

    if kind == "clear_completed":
        removed = graph_clear_completed_tasks(graph)
        graph_add_event(graph, "clear_completed", {"removed": removed})
        return {"ok": True, "message": f"Cleared {removed} completed task(s)"}

    if kind == "add_expense":
        amount = float(payload.get("amount", 0))
        if amount <= 0:
            return {"ok": False, "message": "Invalid expense amount."}
        graph_add_entity(
            graph,
            {
                "kind": "expense",
                "amount": amount,
                "category": str(payload.get("category", "general")).lower(),
                "note": str(payload.get("note", "")),
                "createdAt": now_ms(),
            },
        )
        graph_add_event(graph, "add_expense", {"amount": amount})
        return {"ok": True, "message": f"Added expense: ${amount:.2f}"}

    if kind == "add_note":
        text = str(payload.get("text", "")).strip()
        if not text:
            return {"ok": False, "message": "Note is empty."}
        graph_add_entity(graph, {"kind": "note", "text": text, "createdAt": now_ms()})
        graph_add_event(graph, "add_note", {"text": text[:120]})
        return {"ok": True, "message": "Note captured."}

    if kind == "link_entities":
        source = find_entity_by_kind_selector(graph, str(payload.get("sourceKind", "")), str(payload.get("sourceSelector", "")))
        target = find_entity_by_kind_selector(graph, str(payload.get("targetKind", "")), str(payload.get("targetSelector", "")))
        relation = str(payload.get("relation", "references")).lower().strip()
        if relation not in {"depends_on", "references"}:
            return {"ok": False, "message": "Invalid relation kind."}
        if not source or not target:
            return {"ok": False, "message": "Link target not found."}
        if str(source.get("id", "")) == str(target.get("id", "")):
            return {"ok": False, "message": "Self-link is not allowed."}
        if relation == "depends_on":
            if normalize_entity_kind(source.get("kind", "")) != "task" or normalize_entity_kind(target.get("kind", "")) != "task":
                return {"ok": False, "message": "depends_on requires task -> task entities."}
            if would_create_dependency_cycle(graph, str(source.get("id", "")), str(target.get("id", ""))):
                return {"ok": False, "message": "depends_on cycle detected. Dependency graph must stay acyclic."}
        created = graph_add_relation(graph, source["id"], target["id"], relation)
        graph_add_event(graph, "link_entities", {"sourceId": source["id"], "targetId": target["id"], "relation": relation, "created": created})
        return {"ok": True, "message": f"Linked {source.get('kind')} -> {target.get('kind')} ({relation})"}

    if kind == "schedule_watch_task":
        selector = str(payload.get("selector", "")).strip()
        minutes = int(payload.get("intervalMinutes", 0) or 0)
        if not selector:
            return {"ok": False, "message": "Watch selector is required."}
        if minutes <= 0:
            return {"ok": False, "message": "Watch interval must be > 0 minute."}
        task = find_task_entity(graph, selector)
        if not task:
            return {"ok": False, "message": "Task not found for watch command."}
        job = upsert_watch_task_job(session.jobs, task, selector, minutes)
        graph_add_event(graph, "schedule_watch_task", {"jobId": job["id"], "taskId": task.get("id"), "intervalMinutes": minutes})
        return {"ok": True, "message": f"Scheduled watch for task {selector} every {minutes}m"}

    if kind == "schedule_remind_note":
        text = str(payload.get("text", "")).strip()
        minutes = int(payload.get("intervalMinutes", 0) or 0)
        if not text:
            return {"ok": False, "message": "Reminder text is empty."}
        if minutes <= 0:
            return {"ok": False, "message": "Reminder interval must be > 0 minute."}
        job = upsert_job(
            session.jobs,
            {
                "kind": "remind_note",
                "intervalMinutes": minutes,
                "intervalMs": minutes * 60_000,
                "text": text,
            },
            dedupe_key=f"remind_note:{text.lower()}",
        )
        graph_add_event(graph, "schedule_remind_note", {"jobId": job["id"], "intervalMinutes": minutes})
        return {"ok": True, "message": f"Scheduled reminder every {minutes}m"}

    if kind == "schedule_audit_open_tasks":
        minutes = int(payload.get("intervalMinutes", 0) or 0)
        if minutes <= 0:
            return {"ok": False, "message": "Audit interval must be > 0 minute."}
        job = upsert_job(
            session.jobs,
            {
                "kind": "audit_open_tasks",
                "intervalMinutes": minutes,
                "intervalMs": minutes * 60_000,
            },
            dedupe_key="audit_open_tasks",
        )
        graph_add_event(graph, "schedule_audit_open_tasks", {"jobId": job["id"], "intervalMinutes": minutes})
        return {"ok": True, "message": f"Scheduled task audit every {minutes}m"}

    if kind == "schedule_summarize_expenses_daily":
        minutes = int(payload.get("intervalMinutes", 24 * 60) or 0)
        if minutes <= 0:
            return {"ok": False, "message": "Summary interval must be > 0 minute."}
        job = upsert_job(
            session.jobs,
            {
                "kind": "summarize_expenses_daily",
                "intervalMinutes": minutes,
                "intervalMs": minutes * 60_000,
            },
            dedupe_key="summarize_expenses_daily",
        )
        graph_add_event(graph, "schedule_summarize_expenses_daily", {"jobId": job["id"]})
        return {"ok": True, "message": "Scheduled daily expense summary"}

    if kind == "schedule_failing_probe":
        minutes = int(payload.get("intervalMinutes", 1) or 0)
        if minutes <= 0:
            return {"ok": False, "message": "Probe interval must be > 0 minute."}
        job = upsert_job(
            session.jobs,
            {
                "kind": "failing_probe",
                "intervalMinutes": minutes,
                "intervalMs": minutes * 60_000,
            },
            dedupe_key="failing_probe",
        )
        graph_add_event(graph, "schedule_failing_probe", {"jobId": job["id"], "intervalMinutes": minutes})
        return {"ok": True, "message": f"Scheduled failing probe every {minutes}m"}

    if kind == "pause_job":
        job = find_job_by_selector(session.jobs, str(payload.get("selector", "")).strip())
        if not job:
            return {"ok": False, "message": "Job not found."}
        job["active"] = False
        graph_add_event(graph, "pause_job", {"jobId": job.get("id")})
        return {"ok": True, "message": f"Paused job {job.get('id')}"}

    if kind == "resume_job":
        job = find_job_by_selector(session.jobs, str(payload.get("selector", "")).strip())
        if not job:
            return {"ok": False, "message": "Job not found."}
        job["active"] = True
        interval_ms = int(job.get("intervalMs", 0) or 0)
        job["nextRunAt"] = now_ms() + (interval_ms if interval_ms > 0 else 60_000)
        graph_add_event(graph, "resume_job", {"jobId": job.get("id")})
        return {"ok": True, "message": f"Resumed job {job.get('id')}"}

    if kind == "cancel_job":
        job = find_job_by_selector(session.jobs, str(payload.get("selector", "")).strip())
        if not job:
            return {"ok": False, "message": "Job not found."}
        session.jobs[:] = [x for x in session.jobs if x.get("id") != job.get("id")]
        graph_add_event(graph, "cancel_job", {"jobId": job.get("id")})
        return {"ok": True, "message": f"Canceled job {job.get('id')}"}

    if kind == "list_jobs":
        active = count_active_jobs(session.jobs)
        return {"ok": True, "message": f"Jobs: {len(session.jobs)} total | {active} active"}

    if kind == "list_dead_letters":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 100))
        items = list(reversed(session.dead_letters[-limit:]))
        if not items:
            return {"ok": True, "message": "No dead letters.", "previewLines": ["No failed jobs queued."]}
        lines = [
            f"{idx+1}. {str(item.get('kind', 'job'))} #{str(item.get('id', ''))} attempts:{int(item.get('attempts', 0) or 0)} err:{str(item.get('error', '-'))[:40]}"
            for idx, item in enumerate(items)
        ]
        return {"ok": True, "message": f"Dead letters: {len(session.dead_letters)}", "previewLines": lines[:10]}

    if kind == "runtime_health":
        perf = (((session.last_turn or {}).get("kernelTrace") or {}).get("runtime") or {}).get("performance", {})
        presence = build_presence_payload(session, "local")
        lines = [
            f"revision: {int(session.revision)}",
            f"presence: {int(presence.get('activeCount', 0) or 0)}/{int(presence.get('count', 0) or 0)} active",
            f"jobs: {len(session.jobs)} total / {count_active_jobs(session.jobs)} active",
            f"dead letters: {len(session.dead_letters)}",
            f"slo breach streak: {int(session.slo.get('breachStreak', 0) or 0)}",
            f"persist degraded: {'yes' if bool((session.faults.get('persist', {}) or {}).get('degraded', False)) else 'no'}",
            f"last total: {int(perf.get('totalMs', 0) or 0)}ms",
        ]
        return {"ok": True, "message": "Runtime health ready.", "previewLines": lines[:10]}

    if kind == "show_presence":
        report = build_presence_payload(session, "local")
        pstats = report.get("stats", {}) if isinstance(report.get("stats"), dict) else {}
        lines = [
            f"active: {int(report.get('activeCount', 0) or 0)} / {int(report.get('count', 0) or 0)}",
            f"stale: {int(report.get('staleCount', 0) or 0)}",
            f"heartbeat writes/coalesced: {int(pstats.get('heartbeatWrites', 0) or 0)}/{int(pstats.get('heartbeatCoalesced', 0) or 0)}",
            f"timeout ms: {int(report.get('timeoutMs', 0) or 0)}",
        ]
        for item in (report.get("items", []) or [])[:6]:
            state = "active" if bool(item.get("active")) else "stale"
            label = str(item.get("label", "") or "").strip()
            platform = str(item.get("platform", "") or "").strip()
            ident = str(item.get("deviceId", "") or "")
            parts = [ident]
            if label:
                parts.append(label)
            if platform:
                parts.append(platform)
            lines.append(f"{state} | {' | '.join(parts)} | age {int(item.get('ageMs', 0) or 0)}ms")
        return {"ok": True, "message": "Presence ready.", "previewLines": lines[:10]}

    if kind == "prune_presence":
        all_flag = bool(payload.get("all", False))
        max_age_ms = int(payload.get("maxAgeMs", 120000) or 120000)
        report = prune_presence_entries(session, max_age_ms=max_age_ms, remove_all=all_flag)
        lines = [
            f"removed: {int(report.get('removed', 0) or 0)}",
            f"remaining: {int(report.get('remaining', 0) or 0)}",
            f"mode: {'all' if all_flag else f'age>{max_age_ms}ms'}",
        ]
        return {"ok": True, "message": "Presence pruned.", "previewLines": lines[:10]}

    if kind == "handoff_stats":
        report = build_handoff_stats_payload(session, "local")
        stats = report.get("stats", {}) or {}
        latency = stats.get("latencyMs", {}) or {}
        lines = [
            f"active: {str(report.get('activeDeviceId') or 'none')}",
            f"pending: {'yes' if bool(report.get('pending')) else 'no'}",
            f"starts/claims: {int(stats.get('starts', 0) or 0)}/{int(stats.get('claims', 0) or 0)}",
            f"breaches: {int(stats.get('breaches', 0) or 0)} (budget {int(latency.get('budget', HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)}ms)",
            f"success: {float(stats.get('successRatePct', 0.0) or 0.0):.2f}%",
            f"latency ms avg/last/p95/max: {int(latency.get('avg', 0) or 0)}/{int(latency.get('last', 0) or 0)}/{int(latency.get('p95', 0) or 0)}/{int(latency.get('max', 0) or 0)}",
        ]
        return {"ok": True, "message": "Handoff stats ready.", "previewLines": lines[:10]}

    if kind == "runtime_profile":
        limit = max(1, min(int(payload.get("limit", 200) or 200), 1000))
        profile = build_runtime_profile_payload(session, "local", limit=limit)
        sample = profile.get("sample", {}) or {}
        latency = profile.get("latencyMs", {}) or {}
        outcomes = profile.get("outcomes", {}) or {}
        lines = [
            f"sample: {int(sample.get('count', 0) or 0)} (limit {int(sample.get('limit', limit) or limit)})",
            f"latency ms avg/p50/p95/max: {int(latency.get('avg', 0) or 0)}/{int(latency.get('p50', 0) or 0)}/{int(latency.get('p95', 0) or 0)}/{int(latency.get('max', 0) or 0)}",
            f"ok/denied: {int(outcomes.get('ok', 0) or 0)}/{int(outcomes.get('denied', 0) or 0)}",
            f"within budget: {int(outcomes.get('withinBudget', 0) or 0)} ({float(outcomes.get('withinBudgetPct', 0.0) or 0.0):.2f}%)",
        ]
        return {"ok": True, "message": "Runtime profile ready.", "previewLines": lines[:10]}

    if kind == "show_diagnostics":
        health = get_runtime_health_payload(session, "local")
        presence = build_presence_payload(session, "local")
        continuity = build_continuity_payload(session, "local")
        self_check = build_runtime_self_check_report(session, "local")
        trace_summary = summarize_turn_history(session.turn_history[-200:])
        lines = [
            f"revision: {int(health.get('revision', 0) or 0)}",
            f"jobs: {int((health.get('jobs') or {}).get('active', 0) or 0)}/{int((health.get('jobs') or {}).get('total', 0) or 0)} active",
            f"presence: {int(presence.get('activeCount', 0) or 0)}/{int(presence.get('count', 0) or 0)} active",
            f"continuity: handoff p95 {int((continuity.get('summary', {}) or {}).get('handoffP95Ms', 0) or 0)}ms",
            f"dead letters: {int((health.get('deadLetters') or {}).get('count', 0) or 0)}",
            f"slo breach streak: {int((health.get('slo') or {}).get('breachStreak', 0) or 0)}",
            f"self-check: {'ok' if self_check.get('overallOk') else 'degraded'}",
            f"trace count: {int(trace_summary.get('count', 0) or 0)}",
        ]
        return {"ok": True, "message": "Diagnostics ready.", "previewLines": lines[:10]}

    if kind == "show_continuity":
        report = build_continuity_payload(session, "local")
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        health = report.get("health", {}) if isinstance(report.get("health"), dict) else {}
        autopilot = report.get("autopilot", {}) if isinstance(report.get("autopilot"), dict) else {}
        lines = [
            f"health: {str(health.get('status', 'unknown'))} ({int(health.get('score', 0) or 0)})",
            f"devices active/total: {int(summary.get('activeDevices', 0) or 0)}/{int(summary.get('presenceTotal', 0) or 0)}",
            f"devices stale: {int(summary.get('staleDevices', 0) or 0)}",
            f"presence pruned total: {int(summary.get('presencePrunedTotal', 0) or 0)}",
            f"handoff success: {float(summary.get('handoffSuccessRatePct', 0.0) or 0.0):.2f}%",
            f"handoff p95/budget: {int(summary.get('handoffP95Ms', 0) or 0)}/{int(summary.get('handoffBudgetMs', HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)}ms",
            f"handoff breaches: {int(summary.get('handoffBreaches', 0) or 0)}",
            f"idempotency cache: {int(summary.get('idempotencyEntries', 0) or 0)}",
            f"autopilot: {'on' if bool(autopilot.get('enabled', False)) else 'off'} ({int(autopilot.get('applied', 0) or 0)} applied)",
        ]
        return {"ok": True, "message": "Continuity report ready.", "previewLines": lines[:10]}

    if kind == "continuity_health":
        report = build_continuity_payload(session, "local")
        health = report.get("health", {}) if isinstance(report.get("health"), dict) else {}
        lines = [
            f"status: {str(health.get('status', 'unknown'))}",
            f"score: {int(health.get('score', 0) or 0)}",
        ]
        reasons = health.get("reasons", []) if isinstance(health.get("reasons"), list) else []
        for reason in reasons[:6]:
            lines.append(f"reason: {str(reason)}")
        return {"ok": True, "message": "Continuity health ready.", "previewLines": lines[:10]}

    if kind == "continuity_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        items = session.continuity_history[-limit:]
        if not items:
            return {"ok": True, "message": "No continuity history yet.", "previewLines": ["No continuity snapshots recorded."]}
        lines: list[str] = []
        for item in items:
            lines.append(
                f"{str(item.get('source', 'event'))} | {str(item.get('status', 'unknown'))} {int(item.get('score', 0) or 0)} | devices {int(item.get('activeDevices', 0) or 0)}/{int(item.get('presenceTotal', 0) or 0)} | breaches {int(item.get('handoffBreaches', 0) or 0)}"
            )
        return {"ok": True, "message": f"Continuity history: {len(items)}", "previewLines": lines[:10]}

    if kind == "continuity_anomalies":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = detect_continuity_anomalies(session.continuity_history, limit=limit)
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if not items:
            return {"ok": True, "message": "No continuity anomalies.", "previewLines": ["No continuity anomalies detected in recent history."]}
        lines = [
            f"anomalies: {int(summary.get('count', len(items)) or len(items))} (window {int(report.get('window', 0) or 0)})",
            f"top severity: {str(summary.get('topSeverity', 'none'))}",
        ]
        for item in items[-8:]:
            lines.append(
                f"{str(item.get('severity', 'low'))} {str(item.get('type', 'anomaly'))} | {str(item.get('source', 'event'))} | {str(item.get('detail', '-'))}"
            )
        return {"ok": True, "message": "Continuity anomalies ready.", "previewLines": lines[:10]}

    if kind == "continuity_incidents":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_incidents(session, "local", limit=limit)
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if not items:
            return {"ok": True, "message": "No continuity incidents.", "previewLines": ["No continuity incidents detected."]}
        lines = [
            f"incidents: {int(summary.get('count', len(items)) or len(items))}",
            f"top severity: {str(summary.get('topSeverity', 'none'))}",
            f"anomalies/alerts: {int(summary.get('anomalies', 0) or 0)}/{int(summary.get('alerts', 0) or 0)}",
        ]
        for item in items[-7:]:
            lines.append(
                f"{str(item.get('severity', 'low'))} {str(item.get('category', 'event'))}:{str(item.get('type', '-'))} | {str(item.get('detail', '-'))}"
            )
        return {"ok": True, "message": "Continuity incidents ready.", "previewLines": lines[:10]}

    if kind == "continuity_next":
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_next_actions(session, "local", limit=limit)
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        if not items:
            return {"ok": True, "message": "No continuity actions.", "previewLines": ["No continuity actions needed right now."]}
        lines = [
            f"next actions: {int(summary.get('count', len(items)) or len(items))}",
            f"top priority: {str(summary.get('topPriority', 'none'))}",
            f"health status: {str(summary.get('healthStatus', 'unknown'))}",
        ]
        for item in items[:7]:
            lines.append(
                f"{str(item.get('priority', 'p2'))} {str(item.get('title', 'action'))} | {str(item.get('command', '-'))}"
            )
        return {"ok": True, "message": "Continuity next actions ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        items = session.continuity_autopilot_history[-limit:]
        if not items:
            return {"ok": True, "message": "No continuity autopilot history.", "previewLines": ["No autopilot events recorded."]}
        lines: list[str] = []
        for item in items:
            action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
            lines.append(
                f"{str(item.get('source', 'tick'))} | {str(item.get('reason', 'none'))} | changed {'yes' if bool(item.get('changed', False)) else 'no'} | {str(action.get('command', '-'))}"
            )
        return {"ok": True, "message": f"Continuity autopilot history: {len(items)}", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_preview":
        report = build_continuity_autopilot_preview(session, "local")
        preview = report.get("preview", {}) if isinstance(report.get("preview"), dict) else {}
        candidate = preview.get("candidate", {}) if isinstance(preview.get("candidate"), dict) else {}
        lines = [
            f"can run: {'yes' if bool(preview.get('canRun', False)) else 'no'}",
            f"reason: {str(preview.get('reason', 'unknown'))}",
            f"mode: {str(preview.get('mode', 'normal'))}",
            f"next in ms: {int(preview.get('nextInMs', 0) or 0)}",
            f"used/max applies: {int(preview.get('usedAppliesLastHour', 0) or 0)}/{int(preview.get('maxAppliesPerHour', 0) or 0)}",
            f"candidate: {str(candidate.get('command', '-'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot preview ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_metrics(session, "local", window_ms=window_ms)
        metrics = report.get("metrics", {}) if isinstance(report.get("metrics"), dict) else {}
        reason_counts = metrics.get("reasonCounts", {}) if isinstance(metrics.get("reasonCounts"), dict) else {}
        top_reason = "none"
        top_reason_count = 0
        for key, value in reason_counts.items():
            iv = int(value or 0)
            if iv > top_reason_count:
                top_reason = str(key)
                top_reason_count = iv
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"recent/changed/applied: {int(metrics.get('recentCount', 0) or 0)}/{int(metrics.get('changedCount', 0) or 0)}/{int(metrics.get('appliedCount', 0) or 0)}",
            f"top reason: {top_reason} ({top_reason_count})",
        ]
        for key in sorted(reason_counts.keys())[:5]:
            lines.append(f"reason {str(key)}: {int(reason_counts.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_dry_run":
        report = build_continuity_autopilot_dry_run(session, "local", force=bool(payload.get("force", False)))
        tick_report = report.get("report", {}) if isinstance(report.get("report"), dict) else {}
        delta = ((report.get("snapshot", {}) or {}).get("delta", {}) or {}) if isinstance((report.get("snapshot", {}) or {}).get("delta", {}), dict) else {}
        lines = [
            f"ran: {'yes' if bool(tick_report.get('ran', False)) else 'no'}",
            f"reason: {str(tick_report.get('reason', '-'))}",
            f"changed: {'yes' if bool(tick_report.get('changed', False)) else 'no'}",
            f"delta tasks/expenses/notes: {int(delta.get('tasks', 0) or 0)}/{int(delta.get('expenses', 0) or 0)}/{int(delta.get('notes', 0) or 0)}",
            f"delta journal: {int(delta.get('journal', 0) or 0)}",
        ]
        return {"ok": True, "message": "Continuity autopilot dry run ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_guardrails":
        guardrails = evaluate_continuity_autopilot_guardrails(session)
        blockers = guardrails.get("blockers", []) if isinstance(guardrails.get("blockers"), list) else []
        if not blockers:
            return {"ok": True, "message": "Continuity autopilot guardrails clear.", "previewLines": ["guardrails: clear"]}
        lines = [f"guardrails blockers: {int(guardrails.get('blockerCount', len(blockers)) or len(blockers))}"]
        for item in blockers[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('code', 'blocker'))}: {str(item.get('detail', ''))}")
        return {"ok": True, "message": "Continuity autopilot guardrails blocked.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_recommendation":
        report = build_continuity_autopilot_mode_recommendation(session, "local")
        lines = [
            f"current mode: {str(report.get('currentMode', 'normal'))}",
            f"recommended mode: {str(report.get('recommendedMode', 'normal'))}",
        ]
        for reason in (report.get("reasons", []) if isinstance(report.get("reasons"), list) else [])[:6]:
            lines.append(f"reason: {str(reason)}")
        signals = report.get("signals", {}) if isinstance(report.get("signals"), dict) else {}
        lines.append(f"health: {str(signals.get('healthStatus', 'unknown'))}")
        lines.append(f"guardrail blockers: {int(signals.get('guardrailBlockers', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot mode recommendation ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_drift":
        report = build_continuity_autopilot_mode_drift(session, "local")
        lines = [
            f"drifted: {'yes' if bool(report.get('drifted', False)) else 'no'}",
            f"current mode: {str(report.get('currentMode', 'normal'))}",
            f"recommended mode: {str(report.get('recommendedMode', 'normal'))}",
        ]
        for reason in (report.get("reasons", []) if isinstance(report.get("reasons"), list) else [])[:5]:
            lines.append(f"reason: {str(reason)}")
        return {"ok": True, "message": "Continuity autopilot mode drift ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_alignment":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_mode_alignment(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"aligned total: {int(summary.get('aligned', 0) or 0)}",
            f"last align at: {int(summary.get('lastAlignAt', 0) or 0)}",
            f"current mode: {str(summary.get('currentMode', 'normal'))}",
            f"events: {int(summary.get('count', len(items)) or len(items))}",
        ]
        for item in items[-6:]:
            if not isinstance(item, dict):
                continue
            action = item.get("action", {}) if isinstance(item.get("action"), dict) else {}
            lines.append(f"{str(item.get('source', 'event'))} | {str(item.get('reason', ''))} | {str(action.get('command', '-'))}")
        return {"ok": True, "message": "Continuity autopilot mode alignment ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_policy":
        target_mode = str(payload.get("targetMode", "normal") or "normal").strip().lower()
        report = evaluate_continuity_autopilot_mode_policy(session, "local", target_mode)
        signals = report.get("signals", {}) if isinstance(report.get("signals"), dict) else {}
        lines = [
            f"target mode: {str(report.get('targetMode', target_mode))}",
            f"allowed: {'yes' if bool(report.get('allowed', False)) else 'no'}",
            f"code: {str(report.get('code', 'unknown'))}",
            f"reason: {str(report.get('reason', ''))}",
            f"health: {str(signals.get('healthStatus', 'unknown'))}",
            f"guardrail blockers: {int(signals.get('guardrailBlockers', 0) or 0)}",
        ]
        return {"ok": True, "message": "Continuity autopilot mode policy ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_policy_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_mode_policy_history(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"allowed/blocked: {int(summary.get('allowed', 0) or 0)}/{int(summary.get('blocked', 0) or 0)}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('source', 'event'))} | {str(item.get('reason', ''))}")
        return {"ok": True, "message": "Continuity autopilot mode policy history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_policy_matrix":
        report = build_continuity_autopilot_mode_policy_matrix(session, "local")
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"allowed/blocked: {int(summary.get('allowed', 0) or 0)}/{int(summary.get('blocked', 0) or 0)}",
        ]
        for item in items:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('targetMode', 'mode'))}: {'allow' if bool(item.get('allowed', False)) else 'block'} ({str(item.get('code', ''))})"
            )
        return {"ok": True, "message": "Continuity autopilot mode policy matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_history(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture history.", "previewLines": ["No posture snapshots recorded."]}
        lines = [
            f"snapshots: {int(summary.get('count', len(items)) or len(items))}",
            f"drifted snapshots: {int(summary.get('drifted', 0) or 0)}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('source', 'event'))} | mode {str(item.get('mode', 'normal'))}->{str(item.get('recommendedMode', 'normal'))} | drifted {'yes' if bool(item.get('modeDrifted', False)) else 'no'} | reason {str(item.get('previewReason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_anomalies":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = detect_continuity_autopilot_posture_anomalies(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture anomalies.", "previewLines": ["No posture anomalies detected in recent snapshots."]}
        lines = [
            f"anomalies: {int(summary.get('count', len(items)) or len(items))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('type', 'anomaly'))} | {str(item.get('detail', '-'))} | mode {str(item.get('mode', 'normal'))} | reason {str(item.get('reason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture anomalies ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_actions":
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_actions(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture actions.", "previewLines": ["No posture actions needed right now."]}
        lines = [
            f"actions: {int(summary.get('count', len(items)) or len(items))}",
            f"top priority: {str(summary.get('topPriority', 'none'))}",
            f"top anomaly type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[:7]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('priority', 'p2'))} {str(item.get('title', 'action'))} | {str(item.get('command', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture actions ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_history(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture action history.", "previewLines": ["No posture action audit events recorded."]}
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"applied/noops: {int(summary.get('applied', 0) or 0)}/{int(summary.get('noops', 0) or 0)}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('source', 'event'))} | idx {int(item.get('index', 0) or 0)} | {str(item.get('command', '-'))} | applied {'yes' if bool(item.get('applied', False)) else 'no'} | {str(item.get('reason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_metrics(session, "local", window_ms=window_ms)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        reasons = counts.get("reasons", {}) if isinstance(counts.get("reasons"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/applied/changed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('changed', 0) or 0)}",
            f"applied/changed pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%/{float(summary.get('changedPct', 0.0) or 0.0):.2f}%",
            f"top command: {str(summary.get('topCommand', '-'))}",
            f"top reason: {str(summary.get('topReason', 'none'))}",
        ]
        for key in sorted(reasons.keys())[:4]:
            lines.append(f"reason {str(key)}: {int(reasons.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot posture action metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_anomalies":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = detect_continuity_autopilot_posture_action_anomalies(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture action anomalies.", "previewLines": ["No posture action anomalies detected in recent events."]}
        lines = [
            f"anomalies: {int(summary.get('count', len(items)) or len(items))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('type', 'anomaly'))} | {str(item.get('detail', '-'))} | reason {str(item.get('reason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action anomalies ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_matrix":
        limit = max(1, min(int(payload.get("limit", 10) or 10), 20))
        report = build_continuity_autopilot_posture_action_policy_matrix(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"rows: {int(summary.get('count', len(items)) or len(items))}",
            f"allowed/blocked/info: {int(summary.get('allowed', 0) or 0)}/{int(summary.get('blocked', 0) or 0)}/{int(summary.get('informational', 0) or 0)}",
        ]
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"#{int(item.get('index', 0) or 0)} {str(item.get('priority', 'p2'))} | {'allow' if bool(item.get('appliable', False)) else 'block'} | {str(item.get('policyCode', 'ok'))} | {str(item.get('command', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_history(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture action policy history.", "previewLines": ["No posture action policy decisions recorded."]}
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"allowed/blocked: {int(summary.get('allowed', 0) or 0)}/{int(summary.get('blocked', 0) or 0)}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('source', 'event'))} | idx {int(item.get('index', 0) or 0)} | {'allow' if bool(item.get('allowed', False)) else 'block'} ({str(item.get('policyCode', 'ok'))}) | {str(item.get('command', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_metrics(session, "local", window_ms=window_ms)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        policy_codes = counts.get("policyCodes", {}) if isinstance(counts.get("policyCodes"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', 0) or 0)}",
            f"allowed/blocked: {int(summary.get('allowed', 0) or 0)}/{int(summary.get('blocked', 0) or 0)}",
            f"allowed/blocked pct: {float(summary.get('allowedPct', 0.0) or 0.0):.2f}%/{float(summary.get('blockedPct', 0.0) or 0.0):.2f}%",
            f"top policy code: {str(summary.get('topPolicyCode', 'none'))}",
        ]
        for key in sorted(policy_codes.keys())[:4]:
            lines.append(f"policy {str(key)}: {int(policy_codes.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot posture action policy metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomalies":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = detect_continuity_autopilot_posture_action_policy_anomalies(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture action policy anomalies.", "previewLines": ["No posture action policy anomalies detected in recent events."]}
        lines = [
            f"anomalies: {int(summary.get('count', len(items)) or len(items))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('type', 'anomaly'))} | {str(item.get('detail', '-'))} | code {str(item.get('policyCode', 'ok'))} | reason {str(item.get('reason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy anomalies ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_history(session, "local", limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No continuity autopilot posture action policy anomaly history.", "previewLines": ["No posture action policy anomaly history available."]}
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[-8:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('type', 'anomaly'))} | {str(item.get('detail', '-'))} | code {str(item.get('policyCode', 'ok'))}"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_trend":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_trend(session, "local", window_ms=window_ms, buckets=buckets)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        series = report.get("series", []) if isinstance(report.get("series"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"count/anomalies: {int(summary.get('count', 0) or 0)}/{int(summary.get('anomalies', 0) or 0)}",
            f"anomaly rate: {float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"trend: {str(summary.get('trend', 'stable'))}",
        ]
        for item in series[-5:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"b{int(item.get('index', 0) or 0)} {int(item.get('count', 0) or 0)}/{int(item.get('anomalies', 0) or 0)} ({float(item.get('anomalyRatePct', 0.0) or 0.0):.2f}%)"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly trend ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_offenders":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_offenders(session, "local", window_ms=window_ms, limit=limit)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        offenders = report.get("offenders", []) if isinstance(report.get("offenders"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"anomalies: {int(summary.get('count', 0) or 0)}",
            f"top code: {str(summary.get('topCode', 'none'))}",
            f"offenders: {int(summary.get('offenderCount', len(offenders)) or len(offenders))}",
        ]
        for item in offenders[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('command', '-'))}: {int(item.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly offenders ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_state":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_anomaly_state(session, "local", window_ms=window_ms)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health: {str(summary.get('health', 'healthy'))}",
            f"anomaly rate: {float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"trend: {str(summary.get('trend', 'stable'))}",
            f"top code/offender: {str(summary.get('topCode', 'none'))}/{str(summary.get('topOffenderCode', 'none'))}",
            f"count/anomalies: {int(summary.get('count', 0) or 0)}/{int(summary.get('anomalies', 0) or 0)}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly state ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        threshold_pct = max(1.0, min(float(payload.get("thresholdPct", 35.0) or 35.0), 100.0))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget(
            session,
            "local",
            window_ms=window_ms,
            threshold_pct=threshold_pct,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"status/severity: {str(summary.get('status', 'within_budget'))}/{str(summary.get('severity', 'none'))}",
            f"threshold/rate: {float(summary.get('thresholdPct', threshold_pct) or threshold_pct):.2f}%/{float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"remaining pct: {float(summary.get('remainingPct', 0.0) or 0.0):.2f}%",
            f"count/anomalies: {int(summary.get('count', 0) or 0)}/{int(summary.get('anomalies', 0) or 0)}",
            f"top anomaly/code: {str(summary.get('topAnomalyType', 'none'))}/{str(summary.get('topPolicyCode', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_breaches":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        threshold_pct = max(1.0, min(float(payload.get("thresholdPct", 35.0) or 35.0), 100.0))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_breaches(
            session,
            "local",
            window_ms=window_ms,
            threshold_pct=threshold_pct,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"threshold pct: {float(summary.get('thresholdPct', threshold_pct) or threshold_pct):.2f}%",
            f"breaches: {int(summary.get('breachCount', len(items)) or len(items))}",
            f"top over pct: {float(summary.get('topOverPct', 0.0) or 0.0):.2f}%",
        ]
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"b{int(item.get('index', 0) or 0)} rate {float(item.get('anomalyRatePct', 0.0) or 0.0):.2f}% (+{float(item.get('overPct', 0.0) or 0.0):.2f}%)"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget breaches ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        threshold_pct = max(1.0, min(float(payload.get("thresholdPct", 35.0) or 35.0), 100.0))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast(
            session,
            "local",
            window_ms=window_ms,
            threshold_pct=threshold_pct,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"threshold/current: {float(summary.get('thresholdPct', threshold_pct) or threshold_pct):.2f}%/{float(summary.get('currentRatePct', 0.0) or 0.0):.2f}%",
            f"slope/projected: {float(summary.get('slopePct', 0.0) or 0.0):.2f}%/{float(summary.get('projectedRatePct', 0.0) or 0.0):.2f}%",
            f"projected status/risk: {str(summary.get('projectedStatus', 'within_budget'))}/{str(summary.get('risk', 'low'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget forecast ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_matrix(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"rows/top risk: {int(summary.get('rows', len(items)) or len(items))}/{str(summary.get('topRisk', 'low'))}",
        ]
        for item in items[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"th {float(item.get('thresholdPct', 0.0) or 0.0):.0f}% -> {float(item.get('projectedRatePct', 0.0) or 0.0):.2f}% ({str(item.get('projectedStatus', 'within_budget'))}/{str(item.get('risk', 'low'))})"
            )
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget forecast matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"recommendation: {str(summary.get('recommendation', 'normal'))}",
            f"reason/top risk: {str(summary.get('reason', 'forecast_low_risk'))}/{str(summary.get('topRisk', 'low'))}",
            f"target threshold pct: {float(summary.get('targetThresholdPct', 35.0) or 35.0):.2f}%",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget forecast guidance ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        actions = report.get("actions", []) if isinstance(report.get("actions"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"recommendation: {str(summary.get('recommendation', 'normal'))}",
            f"target threshold pct: {float(summary.get('targetThresholdPct', 35.0) or 35.0):.2f}%",
            f"actions: {int(summary.get('count', len(actions)) or len(actions))}",
        ]
        for item in actions[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('priority', 'p2'))} {str(item.get('command', '-'))}")
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget forecast guidance actions ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run":
        index = max(1, min(int(payload.get("index", 1) or 1), 10))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_dry_run(
            session,
            "local",
            index=index,
        )
        action = report.get("action", {}) if isinstance(report.get("action"), dict) else {}
        capability = report.get("capability", {}) if isinstance(report.get("capability"), dict) else {}
        policy = report.get("policy", {}) if isinstance(report.get("policy"), dict) else {}
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"command: {str(action.get('command', '-'))}",
            f"capability: {str(capability.get('name', '-'))}",
            f"policy: {str(policy.get('code', 'ok'))}",
            f"appliable: {'yes' if bool(report.get('appliable', False)) else 'no'}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly budget forecast guidance action dry run ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action_apply":
        index = max(1, min(int(payload.get("index", 1) or 1), 10))
        report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_action(
            session,
            "local",
            index=index,
        )
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"command: {str(report.get('command', '-'))}",
            f"applied: {'yes' if bool(report.get('applied', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"message: {str(report.get('message', ''))}",
        ]
        return {"ok": bool(report.get("applied", False)), "message": str(report.get("message", "")), "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_apply_batch":
        limit = max(1, min(int(payload.get("limit", 3) or 3), 10))
        report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_batch(
            session,
            "local",
            limit=limit,
        )
        lines = [
            f"attempted: {int(report.get('attempted', 0) or 0)}",
            f"applied: {int(report.get('applied', 0) or 0)}",
            f"changed: {'yes' if bool(report.get('changed', False)) else 'no'}",
        ]
        for item in (report.get("items", []) if isinstance(report.get("items"), list) else [])[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"#{int(item.get('index', 0) or 0)} {'ok' if bool(item.get('applied', False)) else 'skip'} | {str(item.get('reason', '-'))}"
            )
        return {"ok": bool(report.get("applied", 0) >= 1), "message": f"Applied {int(report.get('applied', 0) or 0)} guidance action(s).", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history":
        limit = max(1, min(int(payload.get("limit", 30) or 30), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_history(
            session,
            "local",
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"applied/failed: {int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
        ]
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('op', '-'))} | {'ok' if bool(item.get('ok', False)) else 'fail'} | idx {int(item.get('index', 0) or 0)} lim {int(item.get('limit', 0) or 0)}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_metrics(
            session,
            "local",
            window_ms=window_ms,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        op_counts = counts.get("ops", {}) if isinstance(counts.get("ops"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', 0) or 0)}",
            f"applied/failed: {int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top op: {str(summary.get('topOp', 'none'))}",
        ]
        for key in sorted(op_counts.keys())[:4]:
            lines.append(f"{str(key)}: {int(op_counts.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = detect_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies(
            session,
            "local",
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        if not items:
            return {"ok": True, "message": "No guidance action anomalies.", "previewLines": ["No guidance action anomalies detected in recent events."]}
        lines = [
            f"anomalies: {int(summary.get('count', len(items)) or len(items))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('type', 'anomaly'))} | {str(item.get('detail', '-'))} | reason {str(item.get('reason', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_trend(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        series = report.get("series", []) if isinstance(report.get("series"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"count/anomalies: {int(summary.get('count', 0) or 0)}/{int(summary.get('anomalies', 0) or 0)}",
            f"anomaly rate: {float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"trend: {str(summary.get('trend', 'stable'))}",
        ]
        for item in series[-5:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"b{int(item.get('index', 0) or 0)} {int(item.get('count', 0) or 0)}/{int(item.get('anomalies', 0) or 0)} ({float(item.get('anomalyRatePct', 0.0) or 0.0):.2f}%)"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies trend ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_state(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"health: {str(summary.get('health', 'healthy'))}",
            f"trend: {str(summary.get('trend', 'stable'))}",
            f"event/anomalies: {int(summary.get('eventCount', 0) or 0)}/{int(summary.get('anomalies', 0) or 0)}",
            f"applied/anomaly rate: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%/{float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"top anomaly type: {str(summary.get('topAnomalyType', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies state ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_offenders(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        offenders = report.get("offenders", []) if isinstance(report.get("offenders"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/offenders: {int(summary.get('count', 0) or 0)}/{int(summary.get('offenderCount', len(offenders)) or len(offenders))}",
            f"top op: {str(summary.get('topOp', 'none'))}",
        ]
        for item in offenders[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('reason', 'unknown'))}: {int(item.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies offenders ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 20) or 20), 100))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_timeline(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', len(items)) or len(items))}",
        ]
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{int(item.get('timestamp', 0) or 0)} | {str(item.get('policyCode', 'unknown'))} | {str(item.get('op', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies timeline ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_summary(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"anomaly rate: {float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"top op/reason: {str(summary.get('topOp', 'none'))}/{str(summary.get('topReason', 'none'))}",
            f"latest ts: {str(summary.get('latestTs', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies summary ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 6) or 6), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_matrix(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        rows = report.get("rows", []) if isinstance(report.get("rows"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/reasons/ops: {int(summary.get('count', 0) or 0)}/{int(summary.get('reasons', 0) or 0)}/{int(summary.get('ops', 0) or 0)}",
            f"top reason/op: {str(summary.get('topReason', 'none'))}/{str(summary.get('topOp', 'none'))}",
        ]
        for row in rows[:4]:
            if not isinstance(row, dict):
                continue
            lines.append(f"{str(row.get('reason', 'unknown'))}: {int(row.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        suggestions = report.get("suggestions", []) if isinstance(report.get("suggestions"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/offenders: {int(summary.get('count', 0) or 0)}/{int(summary.get('offenderCount', 0) or 0)}",
            f"top op: {str(summary.get('topOp', 'none'))}",
        ]
        for item in suggestions[:3]:
            if not isinstance(item, dict):
                continue
            actions = item.get("actions", []) if isinstance(item.get("actions"), list) else []
            cmd = str(actions[0]) if actions else "-"
            lines.append(f"{str(item.get('reason', 'unknown'))}: {cmd}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_dry_run(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        dry = report.get("dryRun", {}) if isinstance(report.get("dryRun"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"selected reason: {str(summary.get('selectedReason', 'none'))}",
            f"selected command: {str(summary.get('selectedCommand', 'none'))}",
            f"type/domain/risk: {str(dry.get('type', 'none'))}/{str(dry.get('domain', 'system'))}/{str(dry.get('risk', 'low'))}",
            f"can execute: {'yes' if bool(dry.get('canExecute', False)) else 'no'}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation dry-run ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_apply":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        lines = [
            f"selected command: {str(report.get('selectedCommand', 'none'))}",
            f"selected type: {str(report.get('selectedType', 'none'))}",
            f"applied/changed: {'yes' if bool(report.get('applied', False)) else 'no'}/{ 'yes' if bool(report.get('changed', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"message: {str(report.get('message', '-'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation apply ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_history(
            session,
            "local",
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"count: {int(summary.get('count', len(items)) or len(items))}",
            f"applied/failed: {int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
        ]
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{int(item.get('ts', 0) or 0)} | {'ok' if bool(item.get('ok', False)) else 'failed'} | {str(item.get('policyCode', 'unknown'))}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_metrics(
            session,
            "local",
            window_ms=window_ms,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        policy_codes = counts.get("policyCodes", {}) if isinstance(counts.get("policyCodes"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', 0) or 0)}",
            f"applied/failed: {int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
        ]
        for key in sorted(policy_codes.keys())[:4]:
            lines.append(f"{str(key)}: {int(policy_codes.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_state(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health: {str(summary.get('health', 'healthy'))}",
            f"trend: {str(summary.get('trend', 'stable'))}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation state ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_trend(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        series = report.get("series", []) if isinstance(report.get("series"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"trend: {str(summary.get('trend', 'stable'))}",
        ]
        for item in series[-5:]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"b{int(item.get('index', 0) or 0)} {int(item.get('applied', 0) or 0)}/{int(item.get('count', 0) or 0)} ({float(item.get('appliedPct', 0.0) or 0.0):.2f}%)"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation trend ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_offenders(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        offenders = report.get("offenders", []) if isinstance(report.get("offenders"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/offenders: {int(summary.get('count', 0) or 0)}/{int(summary.get('offenderCount', len(offenders)) or len(offenders))}",
            f"top type: {str(summary.get('topType', 'none'))}",
        ]
        for item in offenders[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('policyCode', 'unknown'))}: {int(item.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation offenders ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_summary(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top policy/type: {str(summary.get('topPolicyCode', 'none'))}/{str(summary.get('topType', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation summary ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_timeline(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', len(items)) or len(items))}",
            f"latest ts: {str(summary.get('latestTs', 'none'))}",
        ]
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{int(item.get('timestamp', 0) or 0)} | {'ok' if bool(item.get('ok', False)) else 'failed'} | {str(item.get('policyCode', 'unknown'))}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation timeline ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 6) or 6), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_matrix(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        rows = report.get("rows", []) if isinstance(report.get("rows"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/policies/types: {int(summary.get('count', 0) or 0)}/{int(summary.get('policyCodes', 0) or 0)}/{int(summary.get('selectedTypes', 0) or 0)}",
            f"top policy/type: {str(summary.get('topPolicyCode', 'none'))}/{str(summary.get('topType', 'none'))}",
        ]
        for row in rows[:4]:
            if not isinstance(row, dict):
                continue
            lines.append(f"{str(row.get('policyCode', 'unknown'))}: {int(row.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        guidance = report.get("guidance", []) if isinstance(report.get("guidance"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
            f"guidance count: {int(summary.get('guidanceCount', len(guidance)) or len(guidance))}",
        ]
        for item in guidance[:5]:
            if not isinstance(item, dict):
                continue
            lines.append(f"[{str(item.get('priority', 'low'))}] {str(item.get('command', '-'))}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        actions = report.get("actions", []) if isinstance(report.get("actions"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"actions: {int(len(actions))}",
        ]
        for item in actions[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{int(item.get('index', 0) or 0)}. [{str(item.get('priority', 'low'))}] {str(item.get('command', '-'))}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run":
        index = max(1, min(int(payload.get("index", 1) or 1), 20))
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_dry_run(
            session,
            "local",
            index=index,
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        action = report.get("action", {}) if isinstance(report.get("action"), dict) else {}
        policy = report.get("policy", {}) if isinstance(report.get("policy"), dict) else {}
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"appliable: {'yes' if bool(report.get('appliable', False)) else 'no'}",
            f"command: {str(action.get('command', '-'))}",
            f"policy: {str(policy.get('code', 'unknown'))}",
            f"reason: {str(policy.get('reason', '-'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance action dry-run ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action_apply":
        index = max(1, min(int(payload.get("index", 1) or 1), 20))
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_action(
            session,
            "local",
            index=index,
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"selected type: {str(report.get('selectedType', 'none'))}",
            f"selected command: {str(report.get('selectedCommand', 'none'))}",
            f"applied: {'yes' if bool(report.get('applied', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"message: {str(report.get('message', ''))}",
        ]
        return {"ok": bool(report.get("applied", False)), "message": str(report.get("message", "")), "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_apply_batch":
        limit = max(1, min(int(payload.get("limit", 3) or 3), 20))
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        guidance_limit = max(1, min(int(payload.get("guidanceLimit", payload.get("limit", 5)) or 5), 20))
        report = apply_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_batch(
            session,
            "local",
            limit=limit,
            window_ms=window_ms,
            buckets=buckets,
            guidance_limit=guidance_limit,
        )
        lines = [
            f"attempted: {int(report.get('attempted', 0) or 0)}",
            f"applied: {int(report.get('applied', 0) or 0)}",
            f"changed: {'yes' if bool(report.get('changed', False)) else 'no'}",
        ]
        for item in (report.get("items", []) if isinstance(report.get("items"), list) else [])[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"#{int(item.get('index', 0) or 0)} {'ok' if bool(item.get('applied', False)) else 'skip'} | {str(item.get('reason', '-'))}")
        return {"ok": bool(report.get("applied", 0) > 0), "message": f"Applied {int(report.get('applied', 0) or 0)} remediation guidance action(s).", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_history(
            session,
            "local",
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"events: {int(summary.get('count', len(items)) or len(items))}",
            f"applied/failed: {int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
        ]
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{int(item.get('ts', 0) or 0)} | {'ok' if bool(item.get('ok', False)) else 'failed'} | {str(item.get('op', '-'))}"
            )
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions history ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_metrics(
            session,
            "local",
            window_ms=window_ms,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"batch attempted/applied: {int(summary.get('batchAttempted', 0) or 0)}/{int(summary.get('batchApplied', 0) or 0)}",
            f"batch applied pct: {float(summary.get('batchAppliedPct', 0.0) or 0.0):.2f}%",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_state(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions state ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_trend(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        series = report.get("series", []) if isinstance(report.get("series"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"buckets: {int(report.get('buckets', buckets) or buckets)}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"trend: {str(summary.get('trend', 'stable'))}",
        ]
        for item in series[-5:]:
            if not isinstance(item, dict):
                continue
            lines.append(f"b{int(item.get('index', 0) or 0)} {int(item.get('applied', 0) or 0)}/{int(item.get('count', 0) or 0)} ({float(item.get('appliedPct', 0.0) or 0.0):.2f}%)")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions trend ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_offenders(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        offenders = report.get("offenders", []) if isinstance(report.get("offenders"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/offenders: {int(summary.get('count', 0) or 0)}/{int(summary.get('offenderCount', len(offenders)) or len(offenders))}",
            f"top op: {str(summary.get('topOp', 'none'))}",
        ]
        for item in offenders[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{str(item.get('policyCode', 'unknown'))}: {int(item.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions offenders ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 8) or 8), 30))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_summary(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"count/applied/failed: {int(summary.get('count', 0) or 0)}/{int(summary.get('applied', 0) or 0)}/{int(summary.get('failed', 0) or 0)}",
            f"applied pct: {float(summary.get('appliedPct', 0.0) or 0.0):.2f}%",
            f"top policy/offenders: {str(summary.get('topPolicyCode', 'none'))}/{int(summary.get('offenderCount', 0) or 0)}",
        ]
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions summary ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 20) or 20), 200))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_timeline(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        items = report.get("items", []) if isinstance(report.get("items"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', len(items)) or len(items))}",
            f"latest ts: {int(summary.get('latestTs', 0) or 0)}",
        ]
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"{int(item.get('ts', 0) or 0)} | {'ok' if bool(item.get('ok', False)) else 'failed'} | {str(item.get('op', '-'))}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions timeline ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        limit = max(1, min(int(payload.get("limit", 6) or 6), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_matrix(
            session,
            "local",
            window_ms=window_ms,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        rows = report.get("rows", []) if isinstance(report.get("rows"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count/policies/types: {int(summary.get('count', 0) or 0)}/{int(summary.get('policyCodes', 0) or 0)}/{int(summary.get('selectedTypes', 0) or 0)}",
            f"top policy/type: {str(summary.get('topPolicyCode', 'none'))}/{str(summary.get('topType', 'none'))}",
        ]
        for row in rows[:6]:
            if not isinstance(row, dict):
                continue
            lines.append(f"{str(row.get('policyCode', 'unknown'))}: {int(row.get('count', 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions matrix ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        buckets = max(2, min(int(payload.get("buckets", 6) or 6), 24))
        limit = max(1, min(int(payload.get("limit", 5) or 5), 20))
        report = build_continuity_autopilot_posture_action_policy_anomaly_budget_forecast_guidance_actions_anomalies_remediation_guidance_actions_guidance(
            session,
            "local",
            window_ms=window_ms,
            buckets=buckets,
            limit=limit,
        )
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        guidance = report.get("guidance", []) if isinstance(report.get("guidance"), list) else []
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"health/trend: {str(summary.get('health', 'healthy'))}/{str(summary.get('trend', 'stable'))}",
            f"top policy: {str(summary.get('topPolicyCode', 'none'))}",
            f"guidance count: {int(summary.get('guidanceCount', len(guidance)) or len(guidance))}",
        ]
        for item in guidance[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(f"[{str(item.get('priority', 'low'))}] {str(item.get('command', '-'))}")
        return {"ok": True, "message": "Continuity autopilot guidance actions anomalies remediation guidance actions guidance ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_policy_anomaly_metrics":
        window_ms = max(60000, min(int(payload.get("windowMs", 3600000) or 3600000), 86400000))
        report = build_continuity_autopilot_posture_action_policy_anomaly_metrics(session, "local", window_ms=window_ms)
        summary = report.get("summary", {}) if isinstance(report.get("summary"), dict) else {}
        counts = report.get("counts", {}) if isinstance(report.get("counts"), dict) else {}
        anomaly_types = counts.get("anomalyTypes", {}) if isinstance(counts.get("anomalyTypes"), dict) else {}
        lines = [
            f"window ms: {int(report.get('windowMs', window_ms) or window_ms)}",
            f"count: {int(summary.get('count', 0) or 0)}",
            f"anomalies: {int(summary.get('anomalies', 0) or 0)}",
            f"anomaly rate: {float(summary.get('anomalyRatePct', 0.0) or 0.0):.2f}%",
            f"top anomaly/code: {str(summary.get('topAnomalyType', 'none'))}/{str(summary.get('topPolicyCode', 'none'))}",
        ]
        for key in sorted(anomaly_types.keys())[:4]:
            lines.append(f"anomaly {str(key)}: {int(anomaly_types.get(key, 0) or 0)}")
        return {"ok": True, "message": "Continuity autopilot posture action policy anomaly metrics ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_action_dry_run":
        index = max(1, min(int(payload.get("index", 1) or 1), 10))
        report = build_continuity_autopilot_posture_action_dry_run(session, "local", index=index, record=True, source="intent_dry_run")
        action = report.get("action", {}) if isinstance(report.get("action"), dict) else {}
        policy = report.get("policy", {}) if isinstance(report.get("policy"), dict) else {}
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"appliable: {'yes' if bool(report.get('appliable', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"command: {str(action.get('command', '-') or '-')}",
            f"policy: {str(policy.get('code', 'ok'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action dry run ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_apply_action":
        index = max(1, min(int(payload.get("index", 1) or 1), 10))
        report = apply_continuity_autopilot_posture_action(session, "local", index=index)
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        lines = [
            f"index: {int(report.get('index', index) or index)}",
            f"command: {str(report.get('command', '-') or '-')}",
            f"applied: {'yes' if bool(report.get('applied', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"message: {str(result.get('message', ''))}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture action apply complete.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture_apply_actions":
        limit = max(1, min(int(payload.get("limit", 3) or 3), 10))
        report = apply_continuity_autopilot_posture_actions_batch(session, "local", limit=limit)
        lines = [
            f"attempted: {int(report.get('attempted', 0) or 0)}",
            f"applied: {int(report.get('applied', 0) or 0)}",
            f"changed: {'yes' if bool(report.get('changed', False)) else 'no'}",
        ]
        for item in (report.get("items", []) if isinstance(report.get("items"), list) else [])[:6]:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"{str(item.get('priority', 'p2'))} {str(item.get('command', '-'))} | applied {'yes' if bool(item.get('applied', False)) else 'no'}"
            )
        return {"ok": True, "message": "Continuity autopilot posture actions apply complete.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_posture":
        report = build_continuity_autopilot_posture(session, "local")
        posture = report.get("posture", {}) if isinstance(report.get("posture"), dict) else {}
        lines = [
            f"enabled: {'yes' if bool(posture.get('enabled', False)) else 'no'}",
            f"mode/current recommended: {str(posture.get('mode', 'normal'))}/{str(posture.get('recommendedMode', 'normal'))}",
            f"drifted: {'yes' if bool(posture.get('modeDrifted', False)) else 'no'} | auto align: {'on' if bool(posture.get('autoAlignMode', False)) else 'off'}",
            f"preview reason: {str(posture.get('previewReason', 'unknown'))}",
            f"guardrail blockers: {int(posture.get('guardrailBlockers', 0) or 0)}",
            f"policy allow/block: {int(posture.get('policyAllowed', 0) or 0)}/{int(posture.get('policyBlocked', 0) or 0)}",
            f"aligned/applied: {int(posture.get('alignedCount', 0) or 0)}/{int(posture.get('appliedCount', 0) or 0)}",
        ]
        return {"ok": True, "message": "Continuity autopilot posture ready.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_mode_apply_recommended":
        report = build_continuity_autopilot_mode_recommendation(session, "local")
        state = ensure_continuity_autopilot_state(session.continuity_autopilot)
        previous = str(state.get("mode", "normal"))
        recommended = str(report.get("recommendedMode", "normal"))
        mode_policy = evaluate_continuity_autopilot_mode_policy(session, "local", recommended)
        changed = previous != recommended and bool(mode_policy.get("allowed", False))
        if bool(mode_policy.get("allowed", False)):
            state["mode"] = recommended
            if changed:
                state["aligned"] = int(state.get("aligned", 0) or 0) + 1
                state["lastAlignAt"] = now_ms()
            state["lastResult"] = f"mode set to {recommended} via recommendation"
        else:
            state["lastResult"] = f"recommended mode blocked ({str(mode_policy.get('code', 'blocked'))})"
        session.continuity_autopilot = state
        append_continuity_autopilot_history(
            session,
            source="intent_mode_apply_recommended",
            reason="mode_recommended_apply" if bool(mode_policy.get("allowed", False)) else "mode_recommended_blocked",
            changed=changed,
            action={"command": f"continuity_autopilot_mode_{recommended}", "priority": "p2", "applied": changed},
        )
        append_continuity_autopilot_posture_snapshot(session, "local", "intent_mode_apply_recommended")
        lines = [
            f"previous mode: {previous}",
            f"recommended mode: {recommended}",
            f"changed: {'yes' if changed else 'no'}",
            f"policy: {str(mode_policy.get('code', 'ok'))}",
        ]
        return {"ok": True, "message": "Continuity autopilot recommended mode applied.", "previewLines": lines}

    if kind == "continuity_autopilot_show":
        state = ensure_continuity_autopilot_state(session.continuity_autopilot)
        used = len([int(ts or 0) for ts in (state.get("appliedTimestamps", []) if isinstance(state.get("appliedTimestamps"), list) else []) if int(ts or 0) > (now_ms() - 60 * 60 * 1000)])
        max_applies_show = int(state.get("maxAppliesPerHour", CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR))
        lines = [
            f"enabled: {'yes' if bool(state.get('enabled', False)) else 'no'}",
            f"mode: {str(state.get('mode', 'normal'))}",
            f"auto align mode: {'on' if bool(state.get('autoAlignMode', False)) else 'off'}",
            f"cooldown ms: {int(state.get('cooldownMs', CONTINUITY_AUTOPILOT_COOLDOWN_MS) or CONTINUITY_AUTOPILOT_COOLDOWN_MS)}",
            f"max applies/h: {max_applies_show} (used {used})",
            f"aligned total: {int(state.get('aligned', 0) or 0)}",
            f"applied/noops: {int(state.get('applied', 0) or 0)}/{int(state.get('noops', 0) or 0)}",
            f"last action: {str(state.get('lastAction', '-') or '-')}",
            f"last result: {str(state.get('lastResult', '-') or '-')}",
        ]
        return {"ok": True, "message": "Continuity autopilot status.", "previewLines": lines[:10]}

    if kind == "continuity_autopilot_set":
        state = ensure_continuity_autopilot_state(session.continuity_autopilot)
        enabled = bool(payload.get("enabled", False))
        state["enabled"] = enabled
        state["lastResult"] = "enabled" if enabled else "disabled"
        session.continuity_autopilot = state
        append_continuity_autopilot_history(
            session,
            source="intent_set",
            reason="enabled" if enabled else "disabled",
            changed=True,
            action={"command": "continuity_autopilot_on" if enabled else "continuity_autopilot_off", "priority": "p2", "applied": True},
        )
        append_continuity_autopilot_posture_snapshot(session, "local", "intent_set")
        lines = [
            f"enabled: {'yes' if enabled else 'no'}",
            f"cooldown ms: {int(state.get('cooldownMs', CONTINUITY_AUTOPILOT_COOLDOWN_MS) or CONTINUITY_AUTOPILOT_COOLDOWN_MS)}",
        ]
        return {"ok": True, "message": "Continuity autopilot updated.", "previewLines": lines}

    if kind == "continuity_autopilot_config":
        state = ensure_continuity_autopilot_state(session.continuity_autopilot)
        mode_policy: dict[str, Any] | None = None
        if "cooldownMs" in payload:
            cooldown_raw = payload.get("cooldownMs", CONTINUITY_AUTOPILOT_COOLDOWN_MS)
            cooldown_ms = max(1000, min(int(CONTINUITY_AUTOPILOT_COOLDOWN_MS if cooldown_raw is None else cooldown_raw), 10 * 60 * 1000))
            state["cooldownMs"] = int(cooldown_ms)
        if "maxAppliesPerHour" in payload:
            max_applies_raw = payload.get("maxAppliesPerHour", CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR)
            max_applies = max(0, min(int(CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR if max_applies_raw is None else max_applies_raw), 500))
            state["maxAppliesPerHour"] = int(max_applies)
        if "mode" in payload:
            mode = str(payload.get("mode", "normal") or "normal").strip().lower()
            mode_policy = evaluate_continuity_autopilot_mode_policy(session, "local", mode)
            if mode in {"safe", "normal", "aggressive"} and bool(mode_policy.get("allowed", False)):
                state["mode"] = mode
        if "autoAlignMode" in payload:
            state["autoAlignMode"] = bool(payload.get("autoAlignMode", False))
        state["lastResult"] = f"cooldown {int(state['cooldownMs'])}ms, max/h {int(state['maxAppliesPerHour'])}"
        session.continuity_autopilot = state
        append_continuity_autopilot_history(
            session,
            source="intent_config",
            reason="mode_policy_blocked" if (mode_policy and not bool(mode_policy.get("allowed", False))) else "cooldown_update",
            changed=True,
            action={"command": f"continuity_autopilot_config_{state['cooldownMs']}_{state['maxAppliesPerHour']}_{state['mode']}", "priority": "p2", "applied": not bool(mode_policy and not bool(mode_policy.get('allowed', False)))},
        )
        append_continuity_autopilot_posture_snapshot(session, "local", "intent_config")
        lines = [
            f"enabled: {'yes' if bool(state.get('enabled', False)) else 'no'}",
            f"mode: {str(state.get('mode', 'normal'))}",
            f"auto align mode: {'on' if bool(state.get('autoAlignMode', False)) else 'off'}",
            f"cooldown ms: {int(state.get('cooldownMs', CONTINUITY_AUTOPILOT_COOLDOWN_MS) or CONTINUITY_AUTOPILOT_COOLDOWN_MS)}",
            f"max applies/h: {int(state.get('maxAppliesPerHour', CONTINUITY_AUTOPILOT_MAX_APPLIES_PER_HOUR))}",
        ]
        if mode_policy and not bool(mode_policy.get("allowed", False)):
            lines.append(f"mode policy: {str(mode_policy.get('code', 'blocked'))}")
        return {"ok": True, "message": "Continuity autopilot cooldown updated.", "previewLines": lines}

    if kind == "continuity_autopilot_reset":
        state = ensure_continuity_autopilot_state(session.continuity_autopilot)
        state["lastRunAt"] = 0
        state["lastAppliedAt"] = 0
        state["applied"] = 0
        state["noops"] = 0
        state["lastAction"] = ""
        state["lastResult"] = "stats reset"
        state["appliedTimestamps"] = []
        session.continuity_autopilot = state
        if bool(payload.get("clearHistory", False)):
            session.continuity_autopilot_history = []
        append_continuity_autopilot_history(
            session,
            source="intent_reset",
            reason="stats_reset",
            changed=True,
            action={"command": "continuity_autopilot_reset", "priority": "p2", "applied": True},
        )
        append_continuity_autopilot_posture_snapshot(session, "local", "intent_reset")
        lines = [
            "applied/noops: 0/0",
            f"history count: {int(len(session.continuity_autopilot_history))}",
            f"clear history: {'yes' if bool(payload.get('clearHistory', False)) else 'no'}",
        ]
        return {"ok": True, "message": "Continuity autopilot stats reset.", "previewLines": lines}

    if kind == "continuity_autopilot_tick":
        report = run_continuity_autopilot_tick(session, "local", force=bool(payload.get("force", False)))
        lines = [
            f"ran: {'yes' if bool(report.get('ran', False)) else 'no'}",
            f"reason: {str(report.get('reason', '-'))}",
            f"mode: {str(report.get('mode', 'normal'))}",
            f"changed: {'yes' if bool(report.get('changed', False)) else 'no'}",
        ]
        action = report.get("action", {}) if isinstance(report.get("action"), dict) else {}
        if action:
            lines.append(f"action: {str(action.get('command', '-') or '-')}")
        return {"ok": True, "message": "Continuity autopilot tick complete.", "previewLines": lines[:10]}

    if kind == "continuity_next_apply":
        report = apply_continuity_next_action(session, "local")
        if not bool(report.get("applied", False)):
            return {
                "ok": True,
                "message": "No continuity auto-action applied.",
                "previewLines": ["No auto-applicable continuity action available."],
            }
        result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
        lines = [
            f"applied: {str(report.get('command', ''))}",
            f"priority: {str(report.get('priority', 'none'))}",
            f"status: {'ok' if bool(result.get('ok', False)) else 'failed'}",
            f"message: {str(result.get('message', ''))}",
        ]
        for line in (result.get("previewLines", []) if isinstance(result.get("previewLines"), list) else [])[:4]:
            lines.append(str(line))
        return {"ok": bool(result.get("ok", False)), "message": "Continuity next action applied.", "previewLines": lines[:10]}

    if kind == "continuity_alerts":
        limit = max(1, min(int(payload.get("limit", 10) or 10), 200))
        report = build_handoff_stats_payload(session, "local")
        stats = report.get("stats", {}) if isinstance(report.get("stats"), dict) else {}
        alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
        if not alerts:
            return {"ok": True, "message": "No continuity alerts.", "previewLines": ["No handoff budget breaches recorded."]}
        lines: list[str] = []
        for item in list(alerts)[-limit:]:
            lines.append(
                f"breach | {int(item.get('claimMs', 0) or 0)}ms > {int(item.get('budgetMs', HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)}ms | {str(item.get('deviceId', '-'))}"
            )
        return {"ok": True, "message": f"Continuity alerts: {len(alerts)}", "previewLines": lines[:10]}

    if kind == "clear_continuity_alerts":
        report = build_handoff_stats_payload(session, "local")
        stats = report.get("stats", {}) if isinstance(report.get("stats"), dict) else {}
        alerts = stats.get("alerts", []) if isinstance(stats.get("alerts"), list) else []
        cleared = len(alerts)
        if isinstance(session.handoff.get("stats"), dict):
            session.handoff["stats"]["alerts"] = []
        lines = [f"cleared: {int(cleared)}", "alerts: 0"]
        return {"ok": True, "message": "Continuity alerts cleared.", "previewLines": lines}

    if kind == "drill_continuity_breach":
        alert = inject_continuity_breach_alert(session, "drill-device")
        lines = [
            f"breach injected: {int(alert.get('claimMs', 0) or 0)}>{int(alert.get('budgetMs', HANDOFF_LATENCY_BUDGET_MS) or HANDOFF_LATENCY_BUDGET_MS)}ms",
            f"device: {str(alert.get('deviceId', 'drill-device'))}",
        ]
        return {"ok": True, "message": "Continuity breach drill recorded.", "previewLines": lines}

    if kind == "snapshot_stats":
        snap = build_snapshot_stats(session)
        lines = [
            f"revision: {int(snap.get('revision', 0) or 0)}",
            f"graph: e{int(snap.get('entities', 0) or 0)} r{int(snap.get('relations', 0) or 0)} v{int(snap.get('events', 0) or 0)}",
            f"jobs: {int(snap.get('jobs', 0) or 0)} | dead letters: {int(snap.get('deadLetters', 0) or 0)}",
            f"journal: {int(snap.get('journal', 0) or 0)} | trace: {int(snap.get('turnHistory', 0) or 0)}",
            f"checkpoints: {int(snap.get('checkpoints', 0) or 0)} | undo: {int(snap.get('undoDepth', 0) or 0)}",
        ]
        return {"ok": True, "message": "Snapshot stats ready.", "previewLines": lines[:10]}

    if kind == "verify_journal_integrity":
        report = verify_journal_integrity(session, "local")
        lines = [
            f"valid: {'yes' if report.get('valid') else 'no'}",
            f"entries: {int(report.get('count', 0) or 0)}",
            f"issues: {len(report.get('issues', []))}",
        ]
        for issue in (report.get("issues", []) or [])[:4]:
            lines.append(str(issue))
        return {"ok": True, "message": "Journal integrity report ready.", "previewLines": lines[:10]}

    if kind == "repair_journal_integrity":
        repaired = repair_journal_integrity(session, "local")
        report = repaired.get("report", {})
        lines = [
            f"before: {int(repaired.get('before', 0) or 0)}",
            f"after: {int(repaired.get('after', 0) or 0)}",
            f"removed: {int(repaired.get('removed', 0) or 0)}",
            f"valid: {'yes' if report.get('valid') else 'no'}",
        ]
        return {"ok": True, "message": "Journal integrity repaired.", "previewLines": lines[:10]}

    if kind == "self_check":
        report = build_runtime_self_check_report(session, "local")
        checks = report.get("checks", [])
        lines = [f"overall: {'ok' if report.get('overallOk') else 'degraded'}"]
        for item in checks:
            lines.append(f"{'ok' if item.get('ok') else 'fail'} {item.get('name')}: {item.get('detail')}")
        return {"ok": True, "message": "Self-check complete.", "previewLines": lines[:10]}

    if kind == "retry_dead_letter":
        selector = str(payload.get("selector", "")).strip()
        item = find_dead_letter_by_selector(session.dead_letters, selector)
        if not item:
            return {"ok": False, "message": "Dead letter not found."}
        job = copy.deepcopy(item.get("job") or {})
        if not isinstance(job, dict) or not job:
            return {"ok": False, "message": "Dead letter payload invalid."}
        job["id"] = str(uuid.uuid4())[:8]
        job["active"] = True
        job["failureCount"] = 0
        job["lastError"] = ""
        interval_ms = int(job.get("intervalMs", 0) or 0)
        job["nextRunAt"] = now_ms() + (interval_ms if interval_ms > 0 else 60_000)
        job["createdAt"] = now_ms()
        session.jobs.append(job)
        session.dead_letters[:] = [x for x in session.dead_letters if x.get("id") != item.get("id")]
        graph_add_event(graph, "retry_dead_letter", {"deadLetterId": item.get("id"), "jobId": job.get("id"), "kind": job.get("kind")})
        return {"ok": True, "message": f"Retried dead letter {item.get('id')} as job {job.get('id')}"}

    if kind == "purge_dead_letters":
        removed = len(session.dead_letters)
        session.dead_letters.clear()
        graph_add_event(graph, "purge_dead_letters", {"removed": removed})
        return {"ok": True, "message": f"Purged dead letters: {removed}"}

    if kind == "undo_last":
        if not session.undo_stack:
            return {"ok": False, "message": "Nothing to undo."}
        snapshot = session.undo_stack.pop()
        session.graph = copy.deepcopy(snapshot.get("graph") or make_empty_graph())
        session.jobs = copy.deepcopy(snapshot.get("jobs") or [])
        graph_add_event(session.graph, "undo_last", {"restoredOp": snapshot.get("op", "unknown")})
        return {"ok": True, "message": f"Undid: {snapshot.get('op', 'operation')}"}

    if kind == "list_files":
        target = resolve_workspace_path(str(payload.get("path", ".") or "."))
        if target is None:
            return {"ok": False, "message": "Invalid path for workspace listing."}
        if target.is_file():
            items = [target.name]
        elif target.is_dir():
            children = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            items = [f"{child.name}/" if child.is_dir() else child.name for child in children[:20]]
        else:
            return {"ok": False, "message": "Path not found."}
        rel = format_workspace_relpath(target)
        graph_add_event(graph, "list_files", {"path": rel, "count": len(items)})
        lines = items or ["(empty)"]
        return {"ok": True, "message": f"Listed files: {rel}", "previewLines": lines[:10]}

    if kind == "read_file":
        target = resolve_workspace_path(str(payload.get("path", "")).strip())
        if target is None:
            return {"ok": False, "message": "Invalid file path."}
        if not target.exists():
            return {"ok": False, "message": "File not found."}
        if target.is_dir():
            return {"ok": False, "message": "Path is a directory."}
        text = read_text_preview(target)
        rel = format_workspace_relpath(target)
        lines = text.splitlines()[:10] if text else ["(empty file)"]
        graph_add_event(graph, "read_file", {"path": rel, "lines": len(lines)})
        return {"ok": True, "message": f"Read file: {rel}", "previewLines": lines}

    if kind == "fetch_url":
        url = str(payload.get("url", "")).strip()
        try:
            with httpx.Client(timeout=5.0, follow_redirects=True) as client:
                resp = client.get(url)
            content_type = str(resp.headers.get("content-type", "")).lower()
            body = resp.text if "text" in content_type or "json" in content_type or not content_type else ""
            preview = normalize_preview_lines(body)
            graph_add_event(graph, "fetch_url", {"url": url[:160], "statusCode": int(resp.status_code)})
            return {
                "ok": True,
                "message": f"Fetched URL: {url} ({resp.status_code})",
                "previewLines": preview or [f"status={resp.status_code}"],
            }
        except Exception as exc:
            return {"ok": False, "message": f"URL fetch failed: {str(exc)[:120]}"}

    if kind == "list_audit":
        domain = str(payload.get("domain", "")).strip().lower() or None
        op_name = str(payload.get("op", "")).strip().lower() or None
        policy_code = str(payload.get("policyCode", "")).strip().lower() or None
        items = filter_journal_entries(
            session.journal,
            domain=domain,
            risk=None,
            ok=None,
            op=op_name,
            policy_code=policy_code,
            limit=40,
        )
        summary = audit_summary(items)
        lines = [
            f"allowed: {summary['allowed']}",
            f"denied: {summary['denied']}",
        ]
        if op_name:
            lines.append(f"op: {op_name}")
        if policy_code:
            lines.append(f"policy filter: {policy_code}")
        for key, value in sorted(summary.get("byPolicyCode", {}).items()):
            lines.append(f"policy {key}: {value}")
        return {"ok": True, "message": "Audit summary ready.", "previewLines": lines[:10]}

    if kind == "list_trace":
        limit = max(1, min(int(payload.get("limit", 20) or 20), 100))
        items = filter_turn_history_entries(
            session.turn_history,
            ok=(payload.get("ok") if "ok" in payload else None),
            intent_class=(str(payload.get("intentClass", "")).strip().lower() or None),
            route_reason=(str(payload.get("routeReason", "")).strip().lower() or None),
            limit=limit,
        )
        if not items:
            return {"ok": True, "message": "No trace entries yet.", "previewLines": ["No turns recorded."]}
        lines: list[str] = []
        for item in reversed(items):
            route = item.get("route") or {}
            perf = item.get("performance") or {}
            lines.append(
                f"{'ok' if item.get('ok') else 'denied'} | {route.get('intentClass', 'unknown')} | {route.get('reason', 'default')} | {int(perf.get('totalMs', 0) or 0)}ms"
            )
        return {"ok": True, "message": f"Trace entries: {len(items)}", "previewLines": lines[:10]}

    if kind == "trace_summary":
        limit = max(1, min(int(payload.get("limit", 200) or 200), 500))
        items = session.turn_history[-limit:]
        summary = summarize_turn_history(items)
        lines = [
            f"count: {int(summary.get('count', 0))}",
            f"ok: {int(summary.get('ok', 0))}",
            f"denied: {int(summary.get('denied', 0))}",
            f"avg total ms: {int(summary.get('avgTotalMs', 0))}",
        ]
        for key, val in sorted((summary.get("byIntentClass") or {}).items()):
            lines.append(f"class {key}: {int(val)}")
        return {"ok": True, "message": "Trace summary ready.", "previewLines": lines[:10]}

    if kind == "export_trace":
        limit = max(1, min(int(payload.get("limit", 50) or 50), 500))
        items = session.turn_history[-limit:]
        lines = [to_json(item) for item in items[:5]]
        if not lines:
            lines = ["(empty)"]
        return {
            "ok": True,
            "message": f"Trace export ready. Use API: /api/session/<sessionId>/trace?format=ndjson&limit={limit}",
            "previewLines": lines,
        }

    if kind == "restore_preview":
        entries = session.journal[-500:]
        rebuilt = rebuild_session_from_journal_entries(entries)
        counts = graph_counts(rebuilt.graph)
        lines = [
            f"replayed: {len(entries)}",
            f"entities: {counts['entities']}",
            f"relations: {counts['relations']}",
            f"events: {counts['events']}",
            "apply with: confirm restore apply",
        ]
        return {"ok": True, "message": "Restore preview generated.", "previewLines": lines}

    if kind == "restore_apply":
        if not bool(payload.get("confirmed")):
            return {"ok": False, "message": "Policy requires confirmation for high-risk action. Try: confirm restore apply"}
        entries = session.journal[-500:]
        rebuilt = rebuild_session_from_journal_entries(entries)
        session.graph = rebuilt.graph
        session.jobs = rebuilt.jobs
        session.undo_stack = rebuilt.undo_stack
        session.memory = graph_to_memory(session.graph)
        session.restore["last"] = {
            "ts": now_ms(),
            "source": "journal_restore_turn",
            "applied": True,
            "entriesReplayed": len(entries),
        }
        graph_add_event(session.graph, "restore_apply", {"entriesReplayed": len(entries), "source": "turn"})
        return {"ok": True, "message": f"Restored session from {len(entries)} journal entries."}

    if kind == "create_checkpoint":
        checkpoint = create_checkpoint(session, "manual_intent")
        return {"ok": True, "message": f"Checkpoint created: {checkpoint.get('id')}"}

    if kind == "list_checkpoints":
        ordered = list(reversed(session.checkpoints[-10:]))
        if not ordered:
            return {"ok": True, "message": "No checkpoints yet.", "previewLines": ["No checkpoints. Use: checkpoint now"]}
        lines = [
            f"{idx+1}. {cp.get('id')} rev:{cp.get('revision')} journal:{cp.get('journalSize')} reason:{cp.get('reason')}"
            for idx, cp in enumerate(ordered)
        ]
        return {"ok": True, "message": f"Checkpoints: {len(ordered)} listed", "previewLines": lines[:10]}

    if kind == "restore_checkpoint_latest":
        if not bool(payload.get("confirmed")):
            return {"ok": False, "message": "Policy requires confirmation for high-risk action. Try: confirm restore checkpoint latest"}
        checkpoint = find_checkpoint(session, None)
        if checkpoint is None:
            return {"ok": False, "message": "No checkpoint available."}
        apply_checkpoint_to_session(session, checkpoint, replay_tail=True)
        session.restore["last"] = {
            "ts": now_ms(),
            "source": "checkpoint_restore_turn",
            "checkpointId": checkpoint.get("id"),
            "replayTail": True,
            "journalBase": int(checkpoint.get("journalSize", 0) or 0),
        }
        graph_add_event(session.graph, "restore_checkpoint_latest", {"checkpointId": checkpoint.get("id"), "source": "turn"})
        return {"ok": True, "message": f"Restored checkpoint: {checkpoint.get('id')}"}

    if kind == "set_persist_fault_mode":
        global SIMULATE_PERSIST_FAILURE
        enabled = bool(payload.get("enabled"))
        SIMULATE_PERSIST_FAILURE = enabled
        if enabled:
            mark_persist_fault(session, "simulated persist failure enabled")
        else:
            clear_persist_fault(session)
        return {"ok": True, "message": f"Persist fault simulation {'enabled' if enabled else 'disabled'}."}

    if kind == "list_faults":
        persist = session.faults.get("persist", {})
        lines = [
            f"persist degraded: {'yes' if bool(persist.get('degraded')) else 'no'}",
            f"pending writes: {int(persist.get('pendingWrites', 0) or 0)}",
            f"last error: {str(persist.get('lastError', '') or '-')} ",
            f"last failure: {int(persist.get('lastFailureAt', 0) or 0)}",
            f"last success: {int(persist.get('lastSuccessAt', 0) or 0)}",
            f"simulation: {'on' if SIMULATE_PERSIST_FAILURE else 'off'}",
        ]
        return {"ok": True, "message": "Fault status ready.", "previewLines": lines[:10]}

    if kind == "compact_journal":
        keep = max(1, min(int(payload.get("keep", 200) or 200), 5000))
        removed = compact_journal(session, keep)
        session.restore["last"] = {
            "ts": now_ms(),
            "source": "journal_compact_turn",
            "removed": removed,
            "remaining": len(session.journal),
            "keep": keep,
        }
        graph_add_event(session.graph, "compact_journal", {"removed": removed, "keep": keep, "source": "turn"})
        return {"ok": True, "message": f"Compacted journal: removed {removed}, kept {len(session.journal)}."}

    if kind == "reset_memory":
        graph_reset_domain_entities(graph, {"task", "expense", "note"})
        graph_add_event(graph, "reset_memory", {"scope": "task+expense+note"})
        return {"ok": True, "message": "Memory reset."}

    return {"ok": False, "message": f"Unknown operation: {kind}"}


def planner_route(envelope: dict[str, Any], execution: dict[str, Any], graph: dict[str, Any]) -> dict[str, Any]:
    has_writes = bool(envelope["stateIntent"]["writeOperations"])
    domains = envelope["stateIntent"]["readDomains"]
    intent_class = str(envelope.get("intentClass", "unknown"))
    confidence = float(envelope.get("confidence", 0.0) or 0.0)
    low_conf_threshold = float(INTENT_CLARIFICATION_THRESHOLD)

    if has_writes or not execution.get("ok", True):
        return {"target": "deterministic", "reason": "mutation safety", "model": None, "intentClass": intent_class, "confidence": confidence}

    if confidence < low_conf_threshold:
        return {"target": "deterministic", "reason": "low_confidence", "model": None, "intentClass": intent_class, "confidence": confidence}

    if intent_class in {"ops", "graph_query"}:
        return {"target": "deterministic", "reason": "intent_class_safe", "model": None, "intentClass": intent_class, "confidence": confidence}

    if len(domains) >= 2 or envelope["surfaceIntent"]["kind"] == "question":
        if OLLAMA_MODEL_LARGE:
            return {"target": "ollama-large", "reason": "cross-domain", "model": OLLAMA_MODEL_LARGE, "intentClass": intent_class, "confidence": confidence}
        if OLLAMA_MODEL_SMALL:
            return {"target": "ollama-small", "reason": "fallback-small", "model": OLLAMA_MODEL_SMALL, "intentClass": intent_class, "confidence": confidence}
        return {"target": "deterministic", "reason": "no local model", "model": None, "intentClass": intent_class, "confidence": confidence}

    if OLLAMA_MODEL_SMALL:
        return {"target": "ollama-small", "reason": "single-domain", "model": OLLAMA_MODEL_SMALL, "intentClass": intent_class, "confidence": confidence}

    return {"target": "deterministic", "reason": "default", "model": None, "intentClass": intent_class, "confidence": confidence}


async def generate_plan_with_ollama(intent: str, envelope: dict[str, Any], graph: dict[str, Any], execution: dict[str, Any], fallback_plan: dict[str, Any], model: str) -> dict[str, Any]:
    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": "Return only valid UIPlan JSON."},
            {"role": "user", "content": to_json({"intent": intent, "envelope": envelope, "memory": summarize_graph(graph), "execution": execution, "fallbackPlan": fallback_plan})},
        ],
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{OLLAMA_BASE_URL}/api/chat", json=payload)
        resp.raise_for_status()
        content = resp.json().get("message", {}).get("content", "{}")
        return json_loads(content)


def build_local_plan(envelope: dict[str, Any], graph: dict[str, Any], execution: dict[str, Any], jobs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    memory = graph_projection(graph)
    jobs = jobs or []
    domains = envelope["stateIntent"]["readDomains"]
    primary = domains[0] if domains else "tasks"
    relation_query = parse_relation_query(envelope["surfaceIntent"]["raw"])

    blocks: list[dict[str, Any]] = [
        block_narrative("system", "Workspace", execution.get("message", f"Intent: {envelope['surfaceIntent']['raw']}"), span=2),
        block_metric("operation", "Operation", envelope["taskIntent"]["operation"].upper(), meta=envelope["stateIntent"]["summary"]),
        block_metric("objects", "Objects", str(len(memory["tasks"]) + len(memory["expenses"]) + len(memory["notes"]))),
        block_metric("relations", "Relations", str(memory["relationCount"])),
    ]

    if relation_query:
        relation_block = build_relation_query_block(graph, relation_query)
        blocks.append(relation_block)

    if primary == "tasks":
        blocks.append(block_list("tasks", "Task Queue", [f"{i+1}. {'[done]' if t.get('done') else '[open]'} {t.get('title','')}" for i, t in enumerate(memory["tasks"][:8])], span=2))
    elif primary == "expenses":
        total = sum(float(e.get("amount", 0)) for e in memory["expenses"])
        blocks.append(block_metric("spend", "Total Spend", f"${total:,.2f}", color="var(--danger)"))
        blocks.append(block_table("expenses", "Recent Expenses", ["Category", "Amount", "Note"], [[str(e.get("category", "")), f"${float(e.get('amount', 0)):,.2f}", str(e.get("note", "") or "-")] for e in memory["expenses"][:8]]))
    elif primary == "notes":
        blocks.append(block_list("notes", "Notes", [f"{fmt_date(n.get('createdAt'))} - {n.get('text','')}" for n in memory["notes"][:8]], span=2))
    elif primary == "graph":
        blocks.append(block_list("graph-relations", "Graph Context", build_graph_context_lines(graph), span=2))
    elif primary == "system":
        blocks.append(block_list("jobs", "Job Queue", format_jobs(jobs), span=2))
    elif primary == "files":
        blocks.append(block_list("files", "File Surface", extract_preview_lines(execution), span=2))
    elif primary == "web":
        blocks.append(block_list("web", "Web Surface", extract_preview_lines(execution), span=2))

    secondary: list[str] = []
    if primary != "tasks":
        secondary.append(f"open tasks: {sum(1 for t in memory['tasks'] if not t.get('done'))}")
    if primary != "expenses":
        secondary.append(f"expenses: ${sum(float(e.get('amount', 0)) for e in memory['expenses']):,.2f}")
    if primary != "notes":
        secondary.append(f"notes: {len(memory['notes'])}")
    if secondary:
        blocks.append(block_list("secondary", "Other Signals", secondary, span=2))

    return {
        "version": "1.0.0",
        "title": f"Generated Surface: {primary.capitalize()}",
        "subtitle": execution.get("message") if not execution.get("ok", True) else f"Intent: \"{envelope['surfaceIntent']['raw']}\"",
        "layout": {"columns": 2, "density": envelope["uiIntent"]["density"]},
        "suggestions": build_suggestions(domains, graph, jobs),
        "blocks": blocks,
        "trace": {"planVersion": "py-local-v1", "focusDomains": domains, "mode": envelope["uiIntent"]["mode"]},
    }


def build_suggestions(domains: list[str], graph: dict[str, Any], jobs: list[dict[str, Any]] | None = None) -> list[str]:
    memory = graph_projection(graph)
    jobs = jobs or []
    out: list[str] = []
    if "tasks" in domains:
        out.append("add task Draft onboarding checklist")
        if memory["tasks"]:
            out.append("complete task 1")
            out.append("watch task 1 every 10m")
    if "expenses" in domains:
        out.append("add expense 16.4 transport train")
    if "notes" in domains:
        out.append("add note Users ask for direct outputs")
    if "files" in domains:
        out.append("list files .")
        out.append("read file README.md")
    if "web" in domains:
        out.append("fetch url https://example.com")
    if memory["tasks"] and memory["notes"]:
        out.append("link task 1 references note 1")
    if memory["tasks"]:
        out.append("show dependencies for task 1")
        out.append("show dependency chain for task 1")
        out.append("show blockers for task 1")
    if memory["notes"]:
        out.append("show references for note 1")
    if jobs:
        out.append("list jobs")
        out.append("pause job 1")
    out.append("show dead letters")
    out.append("show runtime health")
    out.append("show presence")
    out.append("prune presence all")
    out.append("show continuity")
    out.append("show continuity health")
    out.append("show continuity trend")
    out.append("show continuity anomalies")
    out.append("show continuity incidents")
    out.append("show continuity next")
    out.append("show continuity autopilot")
    out.append("show continuity autopilot mode recommendation")
    out.append("show continuity autopilot mode drift")
    out.append("show continuity autopilot mode alignment")
    out.append("show continuity autopilot mode policy aggressive")
    out.append("show continuity autopilot mode policy history")
    out.append("show continuity autopilot mode policy matrix")
    out.append("show continuity autopilot posture actions policy anomalies")
    out.append("show continuity autopilot posture actions policy anomalies history")
    out.append("show continuity autopilot posture actions policy anomalies trend")
    out.append("show continuity autopilot posture actions policy anomalies offenders")
    out.append("show continuity autopilot posture actions policy anomalies state")
    out.append("show continuity autopilot posture actions policy anomalies budget")
    out.append("show continuity autopilot posture actions policy anomalies budget breaches")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast matrix")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions")
    out.append("dry run continuity autopilot posture actions policy anomalies budget forecast guidance action")
    out.append("apply continuity autopilot posture actions policy anomalies budget forecast guidance action")
    out.append("apply continuity autopilot posture actions policy anomalies budget forecast guidance actions")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions history")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions metrics")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies trend")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies state")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies offenders")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies timeline")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies summary")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies matrix")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation")
    out.append("dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation")
    out.append("apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation history")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation metrics")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation state")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation trend")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation offenders")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation summary")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation timeline")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation matrix")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions history")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions metrics")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions state")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions trend")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions offenders")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions summary")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions timeline")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions matrix")
    out.append("show continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions guidance")
    out.append("dry run continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action")
    out.append("apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance action")
    out.append("apply continuity autopilot posture actions policy anomalies budget forecast guidance actions anomalies remediation guidance actions")
    out.append("show continuity autopilot posture actions policy anomalies metrics")
    out.append("show continuity autopilot posture actions policy metrics")
    out.append("show continuity autopilot posture actions policy history")
    out.append("show continuity autopilot posture actions policy matrix")
    out.append("dry run continuity autopilot posture action")
    out.append("show continuity autopilot posture actions anomalies")
    out.append("show continuity autopilot posture actions metrics")
    out.append("show continuity autopilot posture actions history")
    out.append("apply continuity autopilot posture actions")
    out.append("apply continuity autopilot posture action")
    out.append("show continuity autopilot posture actions")
    out.append("show continuity autopilot posture anomalies")
    out.append("show continuity autopilot posture history")
    out.append("show continuity autopilot posture")
    out.append("apply continuity autopilot mode recommendation")
    out.append("show continuity autopilot guardrails")
    out.append("show continuity autopilot history")
    out.append("preview continuity autopilot")
    out.append("dry run continuity autopilot")
    out.append("show continuity autopilot metrics")
    out.append("enable continuity autopilot")
    out.append("disable continuity autopilot")
    out.append("set continuity autopilot cooldown 30s")
    out.append("set continuity autopilot max applies 30 per hour")
    out.append("set continuity autopilot mode safe")
    out.append("set continuity autopilot auto align on")
    out.append("reset continuity autopilot stats")
    out.append("tick continuity autopilot")
    out.append("apply continuity next")
    out.append("show continuity alerts")
    out.append("clear continuity alerts")
    out.append("drill continuity breach")
    out.append("show handoff stats")
    out.append("show runtime profile")
    out.append("show diagnostics")
    out.append("show snapshot stats")
    out.append("verify journal integrity")
    out.append("drill policy deny")
    out.append("run self check")
    out.append("preview intent add task Ship launch checklist")
    out.append("explain intent add task Ship launch checklist")
    out.append("show audit")
    out.append("show audit op add_task")
    out.append("show trace")
    out.append("export trace")
    out.append("show faults")
    out.append("restore preview")
    out.append("compact journal")
    out.append("checkpoint now")
    out.append("list checkpoints")
    out.append("restore checkpoint latest")
    out.append("undo last")
    if not out:
        out.extend(["show tasks and expenses", "add task Ship first usable flow"])
    return out[:6]


def normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    try:
        parsed = UI_PLAN_MODEL.model_validate(plan)
        return parsed.model_dump()
    except Exception:
        return build_local_plan(compile_intent_envelope("show overview"), make_empty_graph(), {"message": "Fallback", "ok": True}, [])


class PlanBlock(BaseModel):
    id: str
    type: str
    label: str
    value: str = ""
    meta: str = ""
    color: str = ""
    span: int = 1
    items: list[str] = []
    headers: list[str] = []
    rows: list[list[str]] = []
    text: str = ""


class UIPlan(BaseModel):
    version: str
    title: str
    subtitle: str
    layout: dict[str, Any]
    suggestions: list[str]
    blocks: list[PlanBlock]
    trace: dict[str, Any]


UI_PLAN_MODEL = UIPlan


def resolve_workspace_path(raw_path: str) -> pathlib.Path | None:
    value = str(raw_path or ".").strip().strip("\"'")
    if not value:
        value = "."
    candidate = pathlib.Path(value)
    resolved = (REPO_ROOT / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return resolved


def format_workspace_relpath(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def read_text_preview(path: pathlib.Path, max_chars: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def normalize_preview_lines(text: str, limit: int = 10) -> list[str]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return []
    return [line[:140] for line in lines[:limit]]


def extract_preview_lines(execution: dict[str, Any], limit: int = 10) -> list[str]:
    tool_results = execution.get("toolResults", []) if isinstance(execution, dict) else []
    for item in reversed(tool_results):
        preview = item.get("previewLines")
        if isinstance(preview, list) and preview:
            return [str(line)[:140] for line in preview[:limit]]
    message = str(execution.get("message", "No output available.")) if isinstance(execution, dict) else "No output available."
    return [message]


def is_allowed_web_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = str(parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1"}:
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return False
    except ValueError:
        pass
    return True


def block_metric(id_: str, label: str, value: str, meta: str = "", color: str = "", span: int = 1) -> dict[str, Any]:
    return {"id": id_, "type": "metric", "label": label, "value": value, "meta": meta, "color": color, "span": span, "items": [], "headers": [], "rows": [], "text": ""}


def block_list(id_: str, label: str, items: list[str], span: int = 1) -> dict[str, Any]:
    return {"id": id_, "type": "list", "label": label, "value": "", "meta": "", "color": "", "span": span, "items": items, "headers": [], "rows": [], "text": ""}


def block_table(id_: str, label: str, headers: list[str], rows: list[list[str]], span: int = 2) -> dict[str, Any]:
    return {"id": id_, "type": "table", "label": label, "value": "", "meta": "", "color": "", "span": span, "items": [], "headers": headers, "rows": rows, "text": ""}


def block_narrative(id_: str, label: str, text: str, span: int = 2) -> dict[str, Any]:
    return {"id": id_, "type": "narrative", "label": label, "value": "", "meta": "", "color": "", "span": span, "items": [], "headers": [], "rows": [], "text": text}


def find_task(tasks: list[dict[str, Any]], selector: str) -> dict[str, Any] | None:
    if selector.isdigit():
        idx = int(selector) - 1
        if 0 <= idx < len(tasks):
            return tasks[idx]
    for t in tasks:
        if t.get("id", "").startswith(selector):
            return t
    return None


def fmt_date(ts: Any) -> str:
    try:
        dt = datetime.fromtimestamp((float(ts) or 0) / 1000)
        return dt.strftime("%b %d %H:%M")
    except Exception:
        return "-"


def now_ms() -> int:
    return int(datetime.utcnow().timestamp() * 1000)


def summarize_graph(graph: dict[str, Any]) -> dict[str, Any]:
    view = graph_projection(graph)
    return {
        "tasks": view["tasks"][:8],
        "expenses": view["expenses"][:8],
        "notes": view["notes"][:8],
        "relationCount": view["relationCount"],
        "eventCount": view["eventCount"],
    }


def stable_memory_fingerprint(memory: dict[str, Any]) -> str:
    import json
    return json.dumps(memory, sort_keys=True, separators=(",", ":"))


def assert_memory_unchanged(before_fingerprint: str, memory: dict[str, Any], context: str) -> None:
    after = stable_memory_fingerprint(memory)
    if after != before_fingerprint:
        raise RuntimeError(context)


def to_json(value: Any) -> str:
    import json
    return json.dumps(value, separators=(",", ":"))


def json_loads(value: str) -> Any:
    import json
    return json.loads(value)
