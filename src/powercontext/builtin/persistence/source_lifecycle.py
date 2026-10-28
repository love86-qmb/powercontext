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

"""Durable per-scope Source lifecycle state."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.codec import dump_model, load_model, stored_bytes
from powercontext.builtin.persistence.errors import GenerationConflictError
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_TABLE
from powercontext.limits import MAX_SCOPE_ID_LENGTH

_STORE_KIND = "source-lifecycle"


class _StoredLifecycleState(BaseModel):
    """The exact canonical payload of one scope's Source lifecycle graph."""

    payload: dict[str, Any]


class SourceLifecycleStore:
    """Load and save one scope's lifecycle graph with generation CAS."""

    async def load(self, connection: AsyncConnection, scope_id: str, /) -> tuple[dict[str, Any], int] | None:
        row = (
            await connection.execute(
                select(SOURCE_LIFECYCLE_TABLE).where(SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        state = load_model(
            _StoredLifecycleState,
            stored_bytes(row["state"], column="state"),
            kind=_STORE_KIND,
            name=scope_id,
        )
        return dict(state.payload), int(row["generation"])

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        payload: dict[str, Any],
        /,
    ) -> int:
        """Write one scope's graph, creating the row the first time.

        The caller already serializes lifecycle writes per Scope, so this only has
        to be atomic, not compare-and-swap.
        """

        stored = dump_model(_StoredLifecycleState(payload=payload), kind=_STORE_KIND, name=scope_id)
        existing = await self.load(connection, scope_id)
        if existing is None:
            generation = 1
            values = {"scope_id": scope_id, "state": stored, "generation": generation}
            if connection.dialect.name == "sqlite":
                statement = sqlite_insert(SOURCE_LIFECYCLE_TABLE).values(**values).on_conflict_do_nothing()
            elif connection.dialect.name == "mysql":
                statement = mysql_insert(SOURCE_LIFECYCLE_TABLE).values(**values).prefix_with("IGNORE")
            else:
                raise GenerationConflictError(_STORE_KIND, None, None)
            result = await connection.execute(statement)
            if result.rowcount != 1:
                raise GenerationConflictError(_STORE_KIND, None, None)
            return generation
        generation = existing[1] + 1
        result = await connection.execute(
            update(SOURCE_LIFECYCLE_TABLE)
            .where(
                SOURCE_LIFECYCLE_TABLE.c.scope_id == scope_id,
                SOURCE_LIFECYCLE_TABLE.c.generation == existing[1],
            )
            .values(state=stored, generation=generation)
        )
        if result.rowcount != 1:
            raise GenerationConflictError(_STORE_KIND, existing[1], None)
        return generation


__all__ = ["SourceLifecycleStore", "MAX_SCOPE_ID_LENGTH"]
