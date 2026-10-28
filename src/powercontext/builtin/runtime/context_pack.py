# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Transfer prepared context as a digest-addressed pack plus reuse deltas.

One prepared context becomes an ordered tuple of UTF-8 byte chunks. The pack
carries a digest over exactly those chunks, so a receiver can verify that a
serialized payload still describes the same content. A delta names the pack it
extends, the number of unchanged leading chunks it reuses, and the chunks that
follow them: a receiver holding the base pack rebuilds the target without
retransmitting the shared prefix, and a receiver that lost the base pack can
still consume the full suffix.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
_CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
_DIGEST_PREFIX = "sha256:"
_CONTEXT_PACK_KEYS = frozenset({"schema", "items", "digest", "byte_count"})
_CONTEXT_PACK_DELTA_KEYS = frozenset({
    "schema",
    "base_digest",
    "target_digest",
    "prefix_items",
    "appended",
})
_MAX_BYTE_COUNT_SETTLE_STEPS = 8

CONTEXT_PACK_CHUNK_BYTES = 512
"""Chunk size used when a prepared context is turned into a transferable pack."""


class ContextPackError(ValueError):
    """Raised when a pack, a delta or a wire payload is not self-consistent."""


@dataclass(frozen=True, slots=True)
class ContextPack:
    """One exact prepared context as ordered UTF-8 byte chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes], /) -> ContextPack:
        """Build a pack over already chunked content."""

        resolved = tuple(_require_bytes(item) for item in items)
        digest = _pack_digest(resolved)
        return cls(items=resolved, digest=digest, byte_count=_settled_byte_count(resolved, digest))

    @classmethod
    def from_content(cls, content: str, /, *, chunk_bytes: int = CONTEXT_PACK_CHUNK_BYTES) -> ContextPack:
        """Build a pack by chunking UTF-8 text without splitting a character."""

        if not isinstance(content, str):
            raise ContextPackError("context pack content must be text")
        if not _is_integer(chunk_bytes) or chunk_bytes < 1:
            raise ContextPackError("chunk_bytes must be a positive integer")
        return cls.build(_utf8_chunks(content, chunk_bytes))

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview | str, /) -> ContextPack:
        """Restore a pack from its wire payload and verify every declared value."""

        raw = _as_bytes(data)
        payload = _decode_object(raw, "context pack")
        if frozenset(payload) != _CONTEXT_PACK_KEYS:
            raise ContextPackError(
                "context pack payload must contain exactly schema, items, digest and byte_count"
            )
        if payload["schema"] != _CONTEXT_PACK_SCHEMA:
            raise ContextPackError(f"unsupported context pack schema {payload['schema']!r}")
        items = _decode_items(payload["items"])
        digest = payload["digest"]
        if not isinstance(digest, str):
            raise ContextPackError("context pack digest must be a string")
        if _pack_digest(items) != digest:
            raise ContextPackError("context pack digest does not match its items")
        byte_count = payload["byte_count"]
        if not _is_integer(byte_count) or byte_count < 0:
            raise ContextPackError("context pack byte_count must be a non-negative integer")
        if byte_count != len(raw):
            raise ContextPackError("context pack byte_count does not match the payload length")
        return cls(items=items, digest=digest, byte_count=byte_count)

    def to_bytes(self) -> bytes:
        """Return the canonical wire payload of this pack."""

        return _encode_pack(self.items, self.digest, self.byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """The suffix of a target pack together with the prefix it reuses."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack, /) -> ContextPackDelta:
        """Describe ``target`` as a reuse of the leading chunks of ``base``."""

        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("a delta needs two context packs")
        shared = 0
        limit = min(len(base.items), len(target.items))
        while shared < limit and base.items[shared] == target.items[shared]:
            shared += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=shared,
            appended=target.items[shared:],
        )

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview | str, /) -> ContextPackDelta:
        """Restore a delta from its wire payload and verify every declared value."""

        raw = _as_bytes(data)
        payload = _decode_object(raw, "context pack delta")
        if frozenset(payload) != _CONTEXT_PACK_DELTA_KEYS:
            raise ContextPackError(
                "context pack delta payload must contain exactly schema, base_digest, "
                "target_digest, prefix_items and appended"
            )
        if payload["schema"] != _CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError(f"unsupported context pack delta schema {payload['schema']!r}")
        base_digest = payload["base_digest"]
        target_digest = payload["target_digest"]
        if not isinstance(base_digest, str) or not isinstance(target_digest, str):
            raise ContextPackError("context pack delta digests must be strings")
        prefix_items = payload["prefix_items"]
        if not _is_integer(prefix_items) or prefix_items < 0:
            raise ContextPackError("context pack delta prefix_items must be a non-negative integer")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=_decode_items(payload["appended"]),
        )

    @property
    def byte_count(self) -> int:
        """Return the exact encoded size of this delta."""

        return len(self.to_bytes())

    def to_bytes(self) -> bytes:
        """Return the canonical wire payload of this delta."""

        payload = {
            "schema": _CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": [_encode_item(item) for item in self.appended],
        }
        return _canonical_json(payload)


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta, /) -> ContextPack:
    """Rebuild the target pack from a base pack and a delta."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("the base of a context pack delta must be a context pack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("a context pack delta must be a ContextPackDelta")
    if base.digest != delta.base_digest:
        raise ContextPackError("context pack delta does not extend the given base pack")
    if not _is_integer(delta.prefix_items) or not 0 <= delta.prefix_items <= len(base.items):
        raise ContextPackError("context pack delta prefix is outside the base pack")
    rebuilt = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("rebuilt context pack does not match the delta target digest")
    return rebuilt


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": _CONTEXT_PACK_SCHEMA, "items": [_encode_item(item) for item in items]}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _encode_pack(items: tuple[bytes, ...], digest: str, byte_count: int) -> bytes:
    return _canonical_json({
        "schema": _CONTEXT_PACK_SCHEMA,
        "items": [_encode_item(item) for item in items],
        "digest": digest,
        "byte_count": byte_count,
    })


def _settled_byte_count(items: tuple[bytes, ...], digest: str) -> int:
    """Return the payload length that the payload itself declares."""

    byte_count = 0
    for _ in range(_MAX_BYTE_COUNT_SETTLE_STEPS):
        candidate = len(_encode_pack(items, digest, byte_count))
        if candidate == byte_count:
            return byte_count
        byte_count = candidate
    raise ContextPackError("context pack byte_count did not converge")


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_item(item: bytes) -> str:
    return base64.b64encode(item).decode("ascii")


def _decode_items(value: Any) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError("context pack items must be a list of base64 strings")
    items: list[bytes] = []
    for entry in value:
        if not isinstance(entry, str):
            raise ContextPackError("context pack items must be base64 strings")
        try:
            items.append(base64.b64decode(entry.encode("ascii"), validate=True))
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError("context pack item is not valid base64") from error
    return tuple(items)


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError(f"{label} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"{label} payload must be a JSON object")
    return payload


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode("utf-8")
    raise ContextPackError("a wire payload must be bytes")


def _require_bytes(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ContextPackError("context pack items must be bytes")
    return bytes(value)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_chunks(content: str, chunk_bytes: int) -> tuple[bytes, ...]:
    """Split text into chunks of at most ``chunk_bytes`` without splitting a character."""

    chunks: list[bytes] = []
    pending: list[str] = []
    size = 0
    for character in content:
        encoded = character.encode("utf-8")
        if len(encoded) > chunk_bytes:
            raise ContextPackError("chunk_bytes cannot hold one UTF-8 character")
        if size + len(encoded) > chunk_bytes:
            chunks.append("".join(pending).encode("utf-8"))
            pending = []
            size = 0
        pending.append(character)
        size += len(encoded)
    if pending:
        chunks.append("".join(pending).encode("utf-8"))
    return tuple(chunks)


__all__ = [
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]
