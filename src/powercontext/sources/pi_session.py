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

"""Capture the visible events of one Pi session branch as exact Sources.

A Pi session archive is a JSONL tree: every record names its parent, and the
selected leaf plus its ancestors form the captured history. Every record that
carries visible text becomes one ``CAPTURED`` Source identified by the session
and the record, and the complete original record is preserved so a citation can
be resolved back to the untouched event. Control records keep their place in the
graph without producing evidence, so a compaction summary never erases the
evidence that preceded it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources.connectors import (
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
)
from powercontext.sources.definitions import AdapterSourceDefinition
from powercontext.sources.models import Source, SourceMaterialization
from powercontext.sources.projections import TEXT_EVIDENCE_PROJECTION_KEY, TextEvidence

PI_EVENT_SOURCE_NAME = "pi-event"
PI_EVENT_DEFINITION_VERSION = "1"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_SESSION_CONNECTOR_VERSION = "1"

_PI_SESSION_HEADER_TYPE = "session"
_PI_SESSION_FORMAT_VERSION = 3
_PI_EVENT_IDENTITY_SCHEME = "pi"
_PI_EVENT_CHECKPOINT_VERSION = 1
_PI_MAX_IDENTITY_HASH_LENGTH = 64
_UTF8_BOM = b"\xef\xbb\xbf"

_VISIBLE_MESSAGE_ROLES = frozenset({"user", "assistant"})
_SUMMARY_NODE_TYPES = frozenset({"compaction", "branch_summary"})
_CUSTOM_MESSAGE_TYPE = "custom_message"
_MESSAGE_TYPE = "message"
_CUSTOM_ROLE = "custom"
_TOOL_RESULT_ROLE = "toolResult"
_BASH_ROLE = "bashExecution"
_UNKNOWN_TOOL = "unknown"


class PiSessionInput(BaseModel):
    """One offline Pi v3 session archive plus the branch leaf to capture."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One captured Pi session event with its complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """A captured Pi session event whose identity is its session and record id."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class _Node:
    """One validated archive record and its place in the session tree."""

    event_id: str
    parent_id: str | None
    record: dict[str, JsonValue]


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the stable identity of one Pi event Source."""

    _require_identity_part("session_id", session_id)
    _require_identity_part("event_id", event_id)
    identity = (
        f"{_PI_EVENT_IDENTITY_SCHEME}:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    )
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        digest = hashlib.sha256(f"{session_id}\x00{event_id}".encode()).hexdigest()
        identity = (
            f"{_PI_EVENT_IDENTITY_SCHEME}:hashed:{digest[:_PI_MAX_IDENTITY_HASH_LENGTH]}"
        )
    return identity


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse the complete ancestor chain of the selected Pi session leaf.

    The whole archive is decoded and validated before any event is returned, so
    no partially parsed session can be mistaken for a complete one.
    """

    session_id, nodes = _parse_archive(value)
    return _branch_events(session_id, _select_branch(nodes, value.leaf_id))


class PiEventSourceAdapter:
    """Resolve and read Pi event Sources from per-event inputs."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            definition_version=PI_EVENT_DEFINITION_VERSION,
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Expose one Pi session event as textual Artifact evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Capture one Pi session branch and resume from a durable checkpoint."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_SESSION_CONNECTOR_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    @property
    def path(self) -> Path:
        return self._path

    @property
    def leaf_id(self) -> str | None:
        return self._leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        """Import every visible event of the selected branch that is not yet known.

        The archive is parsed completely first: a damaged or rewritten input
        raises ``ValueError`` before the run submits anything, so the committed
        checkpoint always describes the exact records behind the stored Sources.
        """

        value = PiSessionInput(content=self._path.read_bytes(), leaf_id=self._leaf_id)
        session_id, nodes = _parse_archive(value)
        branch = _select_branch(nodes, self._leaf_id)
        digests = {node.event_id: _record_digest(node.record) for node in nodes}
        imported = _imported_events(session.checkpoint)
        for event_id, digest in digests.items():
            known = imported.get(event_id)
            if known is not None and known != digest:
                raise ValueError(
                    f"pi session record {event_id!r} changed after it was imported"
                )

        updated = dict(imported)
        for event in _branch_events(session_id, branch):
            digest = digests[event.event_id]
            if imported.get(event.event_id) == digest:
                continue
            await session.submit(event.event_id, PI_EVENT_SOURCE_NAME, event)
            updated[event.event_id] = digest
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"version": _PI_EVENT_CHECKPOINT_VERSION, "events": updated},
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_DEFINITION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


def _parse_archive(value: PiSessionInput) -> tuple[str, tuple[_Node, ...]]:
    records = _load_records(_decode_content(value.content))
    session_id = _validate_header(records[0])
    return session_id, _build_nodes(records[1:])


def _decode_content(content: bytes) -> str:
    if not isinstance(content, (bytes, bytearray, memoryview)):
        raise ValueError("pi session content must be bytes")
    raw = bytes(content)
    if raw.startswith(_UTF8_BOM):
        raw = raw[len(_UTF8_BOM) :]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("pi session content must be valid UTF-8") from error


def _load_records(text: str) -> list[dict[str, JsonValue]]:
    records: list[dict[str, JsonValue]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"malformed pi session record: {error}") from error
        if not isinstance(record, dict):
            raise ValueError("every pi session record must be a JSON object")
        records.append(record)
    if not records:
        raise ValueError("pi session archive must start with a session header")
    return records


def _validate_header(record: dict[str, JsonValue]) -> str:
    if record.get("type") != _PI_SESSION_HEADER_TYPE:
        raise ValueError("first pi session record must be the session header")
    version = record.get("version")
    if isinstance(version, bool) or version != _PI_SESSION_FORMAT_VERSION:
        raise ValueError(
            f"pi session header version must be {_PI_SESSION_FORMAT_VERSION}"
        )
    session_id = record.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("pi session header id must be a non-empty string")
    return session_id


def _build_nodes(records: list[dict[str, JsonValue]]) -> tuple[_Node, ...]:
    nodes: list[_Node] = []
    seen: set[str] = set()
    for record in records:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("pi session record id must be a non-empty string")
        if event_id in seen:
            raise ValueError(f"duplicate pi session record id {event_id!r}")
        parent_id = record.get("parentId")
        if parent_id is not None:
            if not isinstance(parent_id, str) or not parent_id:
                raise ValueError("pi session parentId must be null or a non-empty string")
            if parent_id not in seen:
                raise ValueError(
                    f"pi session parent {parent_id!r} must precede record {event_id!r}"
                )
        event_type = record.get("type")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("pi session record type must be a non-empty string")
        seen.add(event_id)
        nodes.append(_Node(event_id=event_id, parent_id=parent_id, record=record))
    return tuple(nodes)


def _select_branch(nodes: tuple[_Node, ...], leaf_id: str | None) -> tuple[_Node, ...]:
    """Return the root-to-leaf ancestor chain of the selected node."""

    by_id = {node.event_id: node for node in nodes}
    if leaf_id is None:
        if not nodes:
            return ()
        leaf: _Node | None = nodes[-1]
    else:
        _require_identity_part("leaf_id", leaf_id)
        leaf = by_id.get(leaf_id)
        if leaf is None:
            raise ValueError(f"pi session leaf {leaf_id!r} is not present in the archive")
    chain: list[_Node] = []
    while leaf is not None:
        chain.append(leaf)
        leaf = by_id.get(leaf.parent_id) if leaf.parent_id is not None else None
    chain.reverse()
    return tuple(chain)


def _branch_events(session_id: str, branch: tuple[_Node, ...]) -> tuple[PiEventInput, ...]:
    events: list[PiEventInput] = []
    for node in branch:
        role, body = _event_body(node.record)
        if role is None:
            continue
        events.append(
            PiEventInput(
                session_id=session_id,
                event_id=node.event_id,
                parent_id=node.parent_id,
                event_type=node.record.get("type"),
                role=role,
                content=f"{role}: {body}",
                record=node.record,
            )
        )
    return tuple(events)


def _event_body(record: dict[str, JsonValue]) -> tuple[str | None, str]:
    """Return the visible role and body text of one record, if it has any."""

    event_type = record.get("type")
    if event_type == _MESSAGE_TYPE:
        message = record.get("message")
        if not isinstance(message, dict):
            return None, ""
        return _message_body(message)
    if event_type in _SUMMARY_NODE_TYPES:
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return None, ""
        return str(event_type), summary
    if event_type == _CUSTOM_MESSAGE_TYPE:
        parts = _text_parts(record.get("content"))
        if not parts:
            return None, ""
        return _CUSTOM_ROLE, "\n".join(parts)
    return None, ""


def _message_body(message: dict[str, JsonValue]) -> tuple[str | None, str]:
    role = message.get("role")
    if not isinstance(role, str):
        return None, ""
    if role in _VISIBLE_MESSAGE_ROLES:
        parts = _message_parts(message.get("content"))
        if not parts:
            return None, ""
        return role, "\n".join(parts)
    if role == _TOOL_RESULT_ROLE:
        tool_name = _optional_identifier(message.get("toolName"))
        tool_call_id = _optional_identifier(message.get("toolCallId"))
        text = "\n".join(_text_parts(message.get("content")))
        return _TOOL_RESULT_ROLE, f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == _BASH_ROLE:
        command = message.get("command")
        output = message.get("output")
        body = f"$ {command if isinstance(command, str) else ''}"
        body += f"\n{output if isinstance(output, str) else ''}"
        return _BASH_ROLE, body
    return None, ""


def _message_parts(content: JsonValue) -> list[str]:
    """Collect visible text and tool call fragments in original block order."""

    parts: list[str] = []
    if isinstance(content, str):
        if content:
            parts.append(content)
        return parts
    if not isinstance(content, list):
        return parts
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        elif block_type == "toolCall":
            parts.append(_tool_call_body(block))
    return parts


def _text_parts(content: JsonValue) -> list[str]:
    """Collect the visible string and text blocks of one content value."""

    parts: list[str] = []
    if isinstance(content, str):
        if content:
            parts.append(content)
        return parts
    if not isinstance(content, list):
        return parts
    for block in content:
        if isinstance(block, str):
            if block:
                parts.append(block)
            continue
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return parts


def _tool_call_body(block: dict[str, JsonValue]) -> str:
    name = _optional_identifier(block.get("name"))
    call_id = _optional_identifier(block.get("id"))
    return f"[tool_call {name} {call_id}] {_compact_json(block.get('arguments'))}"


def _compact_json(value: JsonValue) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_digest(record: dict[str, JsonValue]) -> str:
    """Return the content digest used to detect a rewritten imported record."""

    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _imported_events(checkpoint: JsonValue | None) -> dict[str, str]:
    if not isinstance(checkpoint, dict):
        return {}
    events = checkpoint.get("events")
    if not isinstance(events, dict):
        return {}
    return {
        event_id: digest
        for event_id, digest in events.items()
        if isinstance(event_id, str) and isinstance(digest, str)
    }


def _optional_identifier(value: JsonValue) -> str:
    if isinstance(value, str) and value:
        return value
    return _UNKNOWN_TOOL


def _require_identity_part(field: str, value: object) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")


__all__ = [
    "PI_EVENT_DEFINITION_VERSION",
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PI_SESSION_CONNECTOR_VERSION",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
