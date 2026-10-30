from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import SDDContextError


@dataclass(frozen=True)
class RecalledMemory:
    scope_id: str
    texts: tuple[str, ...]
    citations: tuple[dict[str, Any], ...]


class PowerContextBridge:
    def __init__(self, *, base_url: str, token: str | None, project_key: str) -> None:
        self.base_url = base_url
        self.token = token
        self.project_key = project_key

    async def recall(self, query: str) -> RecalledMemory:
        from powercontext.client import PowerContextClient
        from powercontext.http import CreateScopeRequest, MemorySearchMode, SearchMemoryRequest

        async with PowerContextClient(self.base_url, token=self.token, timeout=60) as client:
            scope = await client.create_scope(
                CreateScopeRequest(
                    title=f"SDD context: {self.project_key}",
                    summary="MiniMax x OceanBase SDD task context",
                    idempotency_key=f"minimax-sdd-{self.project_key}",
                )
            )
            result = await client.search_memory(
                SearchMemoryRequest(scope_id=scope.scope_id, query=query, mode=MemorySearchMode.FTS, limit=8)
            )
            texts = tuple(hit.text for hit in result.hits)
            citations = tuple(hit.citation.model_dump(mode="json") for hit in result.hits)
            return RecalledMemory(scope_id=scope.scope_id, texts=texts, citations=citations)

    async def remember(self, *, scope_id: str, kind: str, text: str, reason: str) -> dict[str, Any]:
        from powercontext.client import PowerContextClient
        from powercontext.http import RememberMemoryRequest

        async with PowerContextClient(self.base_url, token=self.token, timeout=60) as client:
            response = await client.remember_memory(
                RememberMemoryRequest(scope_id=scope_id, kind=kind, text=text, reason=reason)
            )
            if response.entry is None:
                raise SDDContextError("PowerContext did not return the remembered entry")
            return response.entry.citation.model_dump(mode="json")
