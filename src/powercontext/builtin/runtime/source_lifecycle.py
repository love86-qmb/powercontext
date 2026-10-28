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

"""Withdraw and restore derived Memory and Topic evidence by Source lineage.

Evidence keeps its immutable identity: withdrawing an origin Source deactivates
the Memory entries and Topic Memories that depended on it instead of deleting
anything, and restoring the origin brings them back at the same Artifact and
Entry identities. ``purpose="generation"`` only resolves evidence that is
currently usable, while ``purpose="audit"`` always resolves the stored
reference so the original revision stays reviewable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryService
from powercontext.builtin.artifacts.memory.models import MemoryCitation
from powercontext.builtin.sources.journal import validate_scope_id
from powercontext.errors import ArtifactNotFoundError, PowerContextError
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

LifecyclePurpose = Literal["generation", "audit"]
LifecycleFamily = Literal["source", "memory", "topic"]
_SOURCE = "source"
_MEMORY = "memory"
_TOPIC = "topic"
_WITHDRAWAL_REASON = "source lifecycle withdrawal"
_STATE_KEY_NODES = "nodes"
_STATE_KEY_APPLIED = "applied"


class SourceLifecycleError(PowerContextError, RuntimeError):
    """Report an unsupported or inconsistent Source lifecycle operation."""


class SourceLifecycleEvent(BaseModel):
    """One reported availability change of one registered lifecycle node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    sequence: int = Field(ge=1)
    available: bool
    reason: str


class LifecycleApplication:
    """Select the Source lifecycle service for one scope."""

    def __init__(self, runtime: BuiltinRuntime) -> None:
        self._runtime = runtime

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycle:
        return ScopedSourceLifecycle(self._runtime, scope_id)


class ScopedSourceLifecycle:
    """Register one lineage graph and apply its availability reports."""

    def __init__(self, runtime: BuiltinRuntime, scope_id: str) -> None:
        self._runtime = runtime
        self.scope_id = validate_scope_id(scope_id)

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        /,
        *,
        revision: int = 1,
    ) -> None:
        """Register one origin Source that later reports may withdraw."""

        _require_node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("a lifecycle source node needs a SourceRef")
        await self._register(
            node_id,
            {
                "family": _SOURCE,
                "revision": _require_positive("revision", revision),
                "available": True,
                "sequence": 0,
                "parents": [],
                "policy": "all",
                "reference": {
                    "source_type": source_ref.source_type,
                    "source_id": source_ref.source_id,
                },
            },
        )

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        """Register one Memory entry and the lifecycle nodes that support it."""

        _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("a lifecycle memory node needs a MemoryCitation")
        await self._register(
            node_id,
            {
                "family": _MEMORY,
                "revision": 1,
                "available": True,
                "sequence": 0,
                "parents": list(_require_parents(node_id, parents)),
                "policy": _require_policy(policy),
                "reference": {
                    "memory_ref": {
                        "family": citation.memory_ref.family,
                        "artifact_id": citation.memory_ref.artifact_id,
                        "revision": citation.memory_ref.revision,
                    },
                    "entry_id": citation.entry_id,
                    "entry_version_id": citation.entry_version_id,
                },
            },
        )

    async def register_topic(
        self,
        node_id: str,
        reference: ArtifactRef,
        /,
        *,
        parents: tuple[str, ...],
        policy: str,
    ) -> None:
        """Register one Topic Memory Revision and its supporting nodes."""

        _require_node_id(node_id)
        if not isinstance(reference, ArtifactRef):
            raise SourceLifecycleError("a lifecycle topic node needs an ArtifactRef")
        await self._register(
            node_id,
            {
                "family": _TOPIC,
                "revision": 1,
                "available": True,
                "sequence": 0,
                "parents": list(_require_parents(node_id, parents)),
                "policy": _require_policy(policy),
                "reference": {
                    "family": reference.family,
                    "artifact_id": reference.artifact_id,
                    "revision": reference.revision,
                },
            },
        )

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        """Apply one availability report and return the supported node identities."""

        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply needs a SourceLifecycleEvent")
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                payload, generation = await self._load(context)
                nodes = payload[_STATE_KEY_NODES]
                applied = set(payload[_STATE_KEY_APPLIED])
                node = nodes.get(event.node_id)
                if node is None:
                    raise SourceLifecycleError(f"lifecycle node {event.node_id!r} is not registered")
                if not 1 <= event.revision:
                    raise SourceLifecycleError("a lifecycle event needs a positive revision")
                if event.revision != node["revision"]:
                    raise SourceLifecycleError(
                        f"lifecycle node {event.node_id!r} declares revision {node['revision']}"
                    )
                if event.sequence < node["sequence"]:
                    raise SourceLifecycleError(
                        f"lifecycle node {event.node_id!r} already reported sequence {node['sequence']}"
                    )
                if event.sequence == node["sequence"] and event.available != node["available"]:
                    raise SourceLifecycleError(
                        f"lifecycle node {event.node_id!r} already reported sequence {event.sequence}"
                    )
                node["sequence"] = event.sequence
                node["available"] = bool(event.available)
                active = await self._reconcile(context, nodes, applied)
                await self._save(context, nodes, applied, generation)
        return sorted(active)

    async def active(self) -> tuple[str, ...]:
        """Return the node identities the lineage graph currently supports."""

        async with self._runtime._context(self.scope_id) as context:
            payload, _generation = await self._load(context)
        return tuple(sorted(_supported(payload[_STATE_KEY_NODES])))

    async def resolve(self, node_id: str, /, *, purpose: LifecyclePurpose = "generation") -> Any:
        """Resolve the stored reference of one node for generation or audit."""

        _require_node_id(node_id)
        if purpose not in {"generation", "audit"}:
            raise SourceLifecycleError("a lifecycle purpose is either generation or audit")
        async with self._runtime._context(self.scope_id) as context:
            payload, _generation = await self._load(context)
        nodes = payload[_STATE_KEY_NODES]
        node = nodes.get(node_id)
        if node is None:
            raise SourceLifecycleError(f"lifecycle node {node_id!r} is not registered")
        if purpose == "generation" and node_id not in _supported(nodes):
            raise SourceLifecycleError(f"lifecycle node {node_id!r} is withdrawn from generation")
        return _reference(node)

    async def _register(self, node_id: str, node: dict[str, Any]) -> None:
        async with self._runtime._context(self.scope_id) as context:
            async with self._runtime._locked(self.scope_id):
                payload, generation = await self._load(context)
                nodes = payload[_STATE_KEY_NODES]
                applied = set(payload[_STATE_KEY_APPLIED])
                if node_id in nodes:
                    raise SourceLifecycleError(f"lifecycle node {node_id!r} is already registered")
                known = set(nodes)
                for parent in node["parents"]:
                    if parent not in known:
                        raise SourceLifecycleError(f"lifecycle parent {parent!r} is not registered")
                nodes[node_id] = node
                if node["family"] != _SOURCE:
                    # A freshly written derivative is live, so reconciling only has to
                    # withdraw it when its registered parents cannot support it.
                    applied.add(node_id)
                await self._reconcile(context, nodes, applied)
                await self._save(context, nodes, applied, generation)

    @staticmethod
    async def _load(context: Any) -> tuple[dict[str, Any], int | None]:
        stored = await context.triggers.lifecycle_load()
        if stored is None:
            return {_STATE_KEY_NODES: {}, _STATE_KEY_APPLIED: []}, None
        return {
            _STATE_KEY_NODES: dict(stored.get(_STATE_KEY_NODES) or {}),
            _STATE_KEY_APPLIED: list(stored.get(_STATE_KEY_APPLIED) or []),
        }, None

    @staticmethod
    async def _save(
        context: Any,
        nodes: dict[str, Any],
        applied: set[str],
        generation: int | None,
    ) -> None:
        payload = {
            _STATE_KEY_NODES: nodes,
            _STATE_KEY_APPLIED: sorted(applied),
        }
        await context.triggers.lifecycle_save(payload)

    async def _reconcile(
        self,
        context: Any,
        nodes: dict[str, Any],
        applied: set[str],
    ) -> set[str]:
        supported = _supported(nodes)
        for node_id, node in nodes.items():
            if node["family"] == _SOURCE:
                continue
            should = node_id in supported
            was = node_id in applied
            if should == was:
                continue
            if node["family"] == _MEMORY:
                await _set_memory_state(context, node, active=should)
            else:
                await _set_topic_state(context, node, active=should)
            if should:
                applied.add(node_id)
            else:
                applied.discard(node_id)
        return supported


def _supported(nodes: dict[str, Any]) -> set[str]:
    """Return every node the current lineage graph supports."""

    active = {
        node_id
        for node_id, node in nodes.items()
        if node["family"] == _SOURCE and node["available"]
    }
    for node_id, node in nodes.items():
        if node["family"] == _SOURCE:
            continue
        support = [parent in active for parent in node["parents"]]
        satisfied = all(support) if node["policy"] == "all" else any(support)
        if satisfied:
            active.add(node_id)
    return active


async def _set_memory_state(context: Any, node: dict[str, Any], *, active: bool) -> None:
    service: MemoryService = context.artifacts.memory
    try:
        head = await service.head(context.artifacts.memory_artifact_id)
    except ArtifactNotFoundError:
        return
    try:
        version = await service.validate_citation(_memory_citation(node))
    except (PowerContextError, ValueError):
        return
    if active:
        await service.reactivate(head, entries=(version,), reason=_WITHDRAWAL_REASON)
    else:
        await service.forget(head, entries=(version,), reason=_WITHDRAWAL_REASON)


async def _set_topic_state(context: Any, node: dict[str, Any], *, active: bool) -> None:
    reference = _artifact_ref(node)
    if active:
        await context.triggers.lifecycle_restore_topic(reference.artifact_id, reference.revision)
    else:
        await context.triggers.lifecycle_withdraw_topic(reference.artifact_id, reference.revision)


def _reference(node: dict[str, Any]) -> Any:
    family = node["family"]
    if family == _SOURCE:
        return SourceRef(**node["reference"])
    if family == _MEMORY:
        return _memory_citation(node)
    return _artifact_ref(node)


def _memory_citation(node: dict[str, Any]) -> MemoryCitation:
    stored = node["reference"]
    return MemoryCitation(
        memory_ref=ArtifactRef(**stored["memory_ref"]),
        entry_id=stored["entry_id"],
        entry_version_id=stored["entry_version_id"],
    )


def _artifact_ref(node: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(**node["reference"])


def _require_node_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SourceLifecycleError("a lifecycle node identity must be a non-empty trimmed string")
    return value


def _require_positive(field: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise SourceLifecycleError(f"{field} must be a positive integer")
    return value


def _require_parents(node_id: str, parents: object) -> tuple[str, ...]:
    if not isinstance(parents, (tuple, list)) or not parents:
        raise SourceLifecycleError("a derived lifecycle node needs at least one parent")
    resolved = tuple(_require_node_id(parent) for parent in parents)
    if node_id in resolved:
        raise SourceLifecycleError("a lifecycle node cannot support itself")
    return resolved


def _require_policy(value: object) -> str:
    if value not in {"all", "any"}:
        raise SourceLifecycleError("a lifecycle policy is either all or any")
    return str(value)


__all__ = [
    "LifecycleApplication",
    "LifecyclePurpose",
    "ScopedSourceLifecycle",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]
