"""Protocol v1 envelopes over a Noise session inside an aiohttp WebSocket."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from . import noise
from .const import PROTOCOL_VERSION

_SCHEMA_DIR = Path(__file__).parent / "schemas"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class ProtocolError(Exception):
    """Malformed, undecryptable or schema-invalid traffic. Fatal for the connection."""


def _open_objects(node: Any) -> None:
    """Drop `additionalProperties: false` (except objects marked
    `"x-receiver": "closed"`), so received messages may carry fields a newer
    minor version added (COMPATIBILITY.md: lenient receivers)."""
    if isinstance(node, dict):
        if node.get("additionalProperties") is False and node.get("x-receiver") != "closed":
            del node["additionalProperties"]
        for child in node.values():
            _open_objects(child)
    elif isinstance(node, list):
        for child in node:
            _open_objects(child)


def load_validator(receiver: bool = False) -> Draft202012Validator:
    """Build the envelope validator: strict for sending, or (receiver=True)
    lenient about unknown fields for receiving. Blocking I/O: call in an executor."""
    resources = []
    envelope = None
    for path in sorted(_SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text())
        if receiver:
            _open_objects(schema)
        resources.append((schema["$id"], Resource.from_contents(schema)))
        if path.name == "envelope.schema.json":
            envelope = schema
    if envelope is None:
        raise RuntimeError("envelope schema missing")
    return Draft202012Validator(
        envelope,
        registry=Registry().with_resources(resources),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def ulid() -> str:
    """ULID (ms timestamp + 80 random bits) in Crockford base32."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in range(25, -1, -1))


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class Channel:
    """One encrypted, schema-validated message channel: strict for what we
    send, lenient about unknown fields in what we receive."""

    def __init__(self, ws: web.WebSocketResponse, session: noise.Session, validator: Draft202012Validator, receiver: Draft202012Validator | None = None):
        self.ws = ws
        self.session = session
        self._validator = validator
        self._receiver = receiver or validator
        self._seq = 0
        self._lock = asyncio.Lock()

    async def send(self, msg_type: str, body: dict[str, Any]) -> str:
        async with self._lock:
            self._seq += 1
            env = {"v": PROTOCOL_VERSION, "type": msg_type, "id": ulid(), "ts": now_iso(), "seq": self._seq, "body": body}
            errors = list(self._validator.iter_errors(env))
            if errors:
                raise ProtocolError(f"refusing to send invalid {msg_type}: {errors[0].message}")
            await self.ws.send_bytes(self.session.seal(json.dumps(env, separators=(",", ":")).encode()))
            return env["id"]

    async def recv(self, timeout: float | None) -> dict[str, Any]:
        try:
            async with asyncio.timeout(timeout):
                msg = await self.ws.receive()
        except TimeoutError as err:
            raise ProtocolError("receive timeout") from err
        if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            raise ConnectionResetError("websocket closed")
        if msg.type != WSMsgType.BINARY:
            raise ProtocolError("unexpected frame type")
        try:
            raw = self.session.open(msg.data)
            env = json.loads(raw)
        except (noise.NoiseError, ValueError) as err:
            raise ProtocolError(f"undecodable message: {err}") from err
        if not isinstance(env, dict):
            raise ProtocolError("envelope is not an object")
        errors = list(self._receiver.iter_errors(env))
        if errors:
            raise InvalidMessage(env, errors[0].message)
        return env


class InvalidMessage(ProtocolError):
    """Schema-invalid but decryptable message (lenient handling possible)."""

    def __init__(self, env: dict[str, Any], reason: str) -> None:
        super().__init__(f"invalid {env.get('type')!r}: {reason}")
        self.env = env


async def responder_handshake(ws: web.WebSocketResponse, pattern: str, prologue: bytes, static: noise.KeyPair, timeout: float) -> noise.Session:
    """Run a Noise handshake as responder; raises ProtocolError on any failure."""
    hs = noise.Handshake(pattern, False, prologue, static)
    try:
        async with asyncio.timeout(timeout):
            while not hs.done:
                if hs.my_turn:
                    await ws.send_bytes(hs.write_message())
                    continue
                msg = await ws.receive()
                if msg.type != WSMsgType.BINARY:
                    raise ProtocolError("handshake expects binary frames")
                hs.read_message(msg.data)
    except TimeoutError as err:
        raise ProtocolError("handshake timeout") from err
    except noise.NoiseError as err:
        raise ProtocolError(f"handshake failed: {err}") from err
    return hs.session()
