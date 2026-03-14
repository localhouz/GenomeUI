"""
Genome Mesh Bridge — Python client

Spawns the Nous mesh-bridge Node.js process and provides an async interface
for the GenomeUI backend to broadcast session state over the Nous libp2p mesh
and receive session state from other devices.

Usage:
    from .mesh_bridge import mesh_bridge

    # At startup:
    await mesh_bridge.start()
    mesh_bridge.on_message(my_handler)   # async def my_handler(session_id, payload)

    # After a turn:
    await mesh_bridge.broadcast(session_id, payload_dict)

    # Get our mesh addresses (so other devices can dial in):
    addrs = await mesh_bridge.get_addrs()

    # Dial another device by multiaddr:
    await mesh_bridge.dial("/ip4/192.168.1.5/tcp/42001/p2p/12D3...")

    # At shutdown:
    await mesh_bridge.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any, Awaitable, Callable

_log = logging.getLogger(__name__)

# Path to the compiled mesh bridge script.
# Nous lives at  ../../Nous  relative to this file:
#   this file  → GenomeUI/backend/mesh_bridge.py
#   parent     → GenomeUI/backend
#   parent×2   → GenomeUI
#   parent×3   → Documents
#   / "Nous"   → Documents/Nous
_NOUS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "Nous"
_BRIDGE_JS = _NOUS_DIR / "dist" / "scripts" / "mesh_bridge.js"

MessageHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
NetworkMessageHandler = Callable[[str, str, dict[str, Any]], Awaitable[None]]
# (topic, from_did, envelope_dict)
RelayMessageHandler = Callable[[str, dict[str, Any]], Awaitable[None]]
# (from_did, envelope_dict)


class MeshBridge:
    """Async Python wrapper around the Nous libp2p mesh bridge process."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None  # type: ignore[name-defined]
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._port: int | None = None
        self._handler: MessageHandler | None = None
        self._net_handler: NetworkMessageHandler | None = None
        self._relay_handler: RelayMessageHandler | None = None
        self._recv_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._ready = False
        self._peer_id: str | None = None
        self._addrs: list[str] = []
        self._relay_connected: bool = False
        self._relay_id: str = ""

    # ── Public API ────────────────────────────────────────────────────────────

    def on_message(self, handler: MessageHandler) -> None:
        """Register a coroutine to be called when a personal mesh_sync arrives."""
        self._handler = handler

    def on_network_message(self, handler: NetworkMessageHandler) -> None:
        """Register a coroutine to be called when a signed network_message arrives.

        Signature: async def handler(topic: str, from_did: str, envelope: dict)
        """
        self._net_handler = handler

    def on_relay_message(self, handler: RelayMessageHandler) -> None:
        """Register a coroutine called when a message arrives via the relay tier.

        Signature: async def handler(from_did: str, envelope: dict)
        """
        self._relay_handler = handler

    @property
    def relay_connected(self) -> bool:
        return self._relay_connected

    @property
    def relay_id(self) -> str:
        return self._relay_id

    @property
    def ready(self) -> bool:
        return self._ready

    async def connect_relay(self, relay_url: str) -> None:
        """Connect this node to a Genome relay server for internet-tier delivery."""
        if not self._ready:
            return
        await self._send_cmd({"cmd": "relay_connect", "relayUrl": relay_url})

    async def relay_route(self, to_did: str, envelope: dict[str, Any]) -> None:
        """Route a signed GenomeEnvelope to a target DID via the relay."""
        if not self._ready or not self._relay_connected:
            return
        await self._send_cmd({"cmd": "relay_route", "to": to_did, "envelope": envelope})

    async def relay_status(self) -> dict[str, Any]:
        """Return current relay connection status."""
        return {"connected": self._relay_connected, "relayId": self._relay_id}

    async def start(self, seed_hex: str | None = None, did: str | None = None) -> bool:
        """
        Spawn the Node.js mesh bridge process and connect to its control plane.
        seed_hex — the user's 32-byte identity seed as a hex string, passed to
        the bridge so the libp2p peer ID is derived from and stable as the user's
        permanent Genome identity.
        Returns True if the bridge is ready, False if unavailable (degrades gracefully).
        """
        if not _BRIDGE_JS.exists():
            _log.info(
                "Nous mesh bridge script not found at %s — "
                "run 'npm run build' in %s to enable mesh sync",
                _BRIDGE_JS,
                _NOUS_DIR,
            )
            return False

        try:
            args = ["node", str(_BRIDGE_JS)]
            if seed_hex:
                args.append(seed_hex)
                if did:
                    args.append(did)
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            _log.warning("'node' not found in PATH — Nous mesh sync disabled")
            return False
        except Exception as exc:
            _log.warning("Failed to spawn mesh bridge: %s", exc)
            return False

        # Read the ready event from stdout — "{\"type\":\"ready\",\"port\":<n>}\n"
        assert self._proc.stdout
        try:
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=20.0)
            ready = json.loads(raw.decode().strip())
            self._port = int(ready["port"])
        except Exception as exc:
            _log.warning("Mesh bridge did not send ready event: %s", exc)
            await self._kill()
            return False

        # Connect TCP control socket
        try:
            self._reader, self._writer = await asyncio.open_connection(
                "127.0.0.1", self._port
            )
        except Exception as exc:
            _log.warning("Cannot connect to mesh bridge control plane: %s", exc)
            await self._kill()
            return False

        self._ready = True
        self._recv_task = asyncio.create_task(self._recv_loop(), name="mesh-recv")
        self._stderr_task = asyncio.create_task(self._stderr_loop(), name="mesh-stderr")

        # Resolve our own peer ID and addresses in the background
        asyncio.create_task(self._resolve_identity(), name="mesh-identity")

        _log.info("Nous mesh bridge ready (port=%d)", self._port)
        return True

    async def broadcast(self, session_id: str, payload: dict[str, Any]) -> None:
        """Broadcast session state to personal devices over the default topic."""
        if not self._ready:
            return
        await self._send_cmd({"cmd": "broadcast", "sessionId": session_id, "payload": payload})

    async def join_topic(self, topic: str) -> None:
        """Subscribe the mesh node to a gossipsub topic."""
        if not self._ready:
            return
        await self._send_cmd({"cmd": "join_topic", "topic": topic})

    async def leave_topic(self, topic: str) -> None:
        """Unsubscribe the mesh node from a gossipsub topic."""
        if not self._ready:
            return
        await self._send_cmd({"cmd": "leave_topic", "topic": topic})

    async def network_broadcast(self, topic: str, envelope: dict[str, Any]) -> None:
        """Publish a signed GenomeEnvelope to a specific gossipsub topic.

        The envelope must already be signed (use identity.sign() to produce
        the sig field) before passing it here.
        """
        if not self._ready:
            return
        await self._send_cmd({"cmd": "network_broadcast", "topic": topic, "envelope": envelope})

    async def dial(self, address: str) -> None:
        """Dial a remote mesh node by multiaddr string."""
        if not self._ready:
            return
        await self._send_cmd({"cmd": "dial", "address": address})

    async def get_addrs(self) -> list[str]:
        """Return our libp2p multiaddresses (cached after startup)."""
        return list(self._addrs)

    async def get_peer_id(self) -> str | None:
        """Return our libp2p peer ID (cached after startup)."""
        return self._peer_id

    async def stop(self) -> None:
        """Shut down the bridge cleanly."""
        self._ready = False
        for task in (self._recv_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        await self._kill()

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _send_cmd(self, cmd: dict[str, Any]) -> None:
        if not self._writer:
            return
        try:
            self._writer.write((json.dumps(cmd) + "\n").encode())
            await self._writer.drain()
        except Exception as exc:
            _log.warning("Mesh bridge send failed: %s", exc)
            self._ready = False

    async def _recv_loop(self) -> None:
        """Read newline-delimited JSON events from the bridge."""
        assert self._reader
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                try:
                    event = json.loads(raw.decode().strip())
                    await self._handle_event(event)
                except json.JSONDecodeError:
                    pass
                except Exception as exc:
                    _log.debug("Mesh event handling error: %s", exc)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            _log.debug("Mesh recv loop exited: %s", exc)
        finally:
            if self._ready:
                _log.warning("Nous mesh bridge connection lost")
                self._ready = False

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")

        if etype == "mesh_sync":
            session_id = str(event.get("sessionId") or "")
            payload = event.get("payload") or {}
            if session_id and isinstance(payload, dict) and self._handler:
                await self._handler(session_id, payload)

        elif etype == "addrs":
            self._addrs = list(event.get("addrs") or [])
            _log.debug("Mesh addrs: %s", self._addrs)

        elif etype == "peer_id":
            self._peer_id = str(event.get("peerId") or "")
            _log.debug("Mesh peer ID: %s", self._peer_id)

        elif etype == "network_message":
            topic = str(event.get("topic") or "")
            from_did = str(event.get("from") or "")
            envelope = event.get("envelope") or {}
            if topic and from_did and isinstance(envelope, dict) and self._net_handler:
                await self._net_handler(topic, from_did, envelope)

        elif etype == "relay_message":
            from_did = str(event.get("from") or "")
            envelope = event.get("envelope") or {}
            if from_did and isinstance(envelope, dict) and self._relay_handler:
                await self._relay_handler(from_did, envelope)

        elif etype == "relay_status":
            self._relay_connected = bool(event.get("connected"))
            self._relay_id = str(event.get("relayId") or "")
            _log.info(
                "Relay %s (id=%s)",
                "connected" if self._relay_connected else "disconnected",
                self._relay_id or "—",
            )

        elif etype == "peer_joined":
            _log.info("Mesh peer joined: %s", event.get("peerId"))

        elif etype == "error":
            _log.warning("Mesh bridge error: %s", event.get("message"))

    async def _stderr_loop(self) -> None:
        """Pipe stderr from the Node process to Python logs."""
        assert self._proc and self._proc.stderr
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                _log.debug("[mesh-bridge] %s", line.decode().rstrip())
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def _resolve_identity(self) -> None:
        """Request our peer ID and addresses shortly after startup."""
        await asyncio.sleep(1.0)  # give the mesh node time to finish joining
        await self._send_cmd({"cmd": "get_peer_id"})
        await self._send_cmd({"cmd": "get_addrs"})

    async def _kill(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


# Module-level singleton — imported and used by main.py
mesh_bridge = MeshBridge()
