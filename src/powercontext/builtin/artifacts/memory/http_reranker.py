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

"""HTTP scoring-service reranking for coarse Memory search candidates.

The adapter keeps candidate identity on the caller side: candidates are sent with
their original coarse ranks and the service returns a score per rank, so the
selected ranks always map back onto the unmodified candidate sequence.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import (
    MemoryRerankCandidate,
    MemoryRerankDecision,
    _validate_bounds,
)
from powercontext.builtin.inference import InferenceUsage

HTTP_MEMORY_RERANK_POLICY_ID = "powercontext.memory.rerank.http.v1"
DEFAULT_HTTP_RERANK_BATCH_SIZE = 16
DEFAULT_HTTP_RERANK_TIMEOUT_SECONDS = 30.0


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _HTTPScore(_StrictModel):
    """One scored candidate returned by the scoring service."""

    rank: int = Field(ge=1)
    score: float = Field(allow_inf_nan=False)


class _HTTPResponse(_StrictModel):
    """The complete scoring response for one request batch."""

    scores: tuple[_HTTPScore, ...]


class HTTPMemoryReranker:
    """Select Memory hits by scoring candidates through an HTTP service.

    Candidates are sent in bounded batches. Every batch must score exactly the
    ranks it was given, once each; scores are then ordered globally so the final
    selection does not depend on the batch a candidate happened to fall into.
    """

    policy_id = HTTP_MEMORY_RERANK_POLICY_ID

    def __init__(
        self,
        url: str,
        /,
        *,
        client: httpx.AsyncClient | None = None,
        batch_size: int = DEFAULT_HTTP_RERANK_BATCH_SIZE,
        timeout: float = DEFAULT_HTTP_RERANK_TIMEOUT_SECONDS,
    ) -> None:
        if batch_size < 1:
            raise ValueError("HTTP rerank batch_size must be at least one")
        if timeout <= 0:
            raise ValueError("HTTP rerank timeout must be positive")
        self._url = url
        self._client = client
        self._batch_size = batch_size
        self._timeout = timeout

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Score every candidate and return the best original ranks."""

        _validate_bounds(len(candidates), limit)

        ranked = tuple(
            MemoryRerankCandidate(rank=rank, text=candidate.text)
            for rank, candidate in enumerate(candidates, start=1)
        )
        scores: list[tuple[int, float]] = []
        requests = 0
        for start in range(0, len(ranked), self._batch_size):
            batch = ranked[start : start + self._batch_size]
            payload = {
                "query": query,
                "candidates": [
                    {"rank": candidate.rank, "text": candidate.text} for candidate in batch
                ],
            }
            body = await self._post(payload)
            requests += 1
            scores.extend(_validated_scores(body, batch))

        ordered = sorted(scores, key=lambda item: (-item[1], item[0]))
        selected = tuple(rank for rank, _score in ordered[:limit])
        return MemoryRerankDecision(
            selected_ranks=selected,
            usage=InferenceUsage(requests=requests),
        )

    async def _post(self, payload: dict[str, Any]) -> Any:
        client = self._client
        if client is None:
            async with httpx.AsyncClient(timeout=self._timeout) as ephemeral:
                response = await ephemeral.post(self._url, json=payload)
                return _response_json(response)
        response = await client.post(self._url, json=payload)
        return _response_json(response)


def _response_json(response: httpx.Response) -> Any:
    response.raise_for_status()
    try:
        return response.json()
    except ValueError as error:
        raise ValueError("HTTP rerank response must be valid JSON") from error


def _validated_scores(body: Any, batch: tuple[MemoryRerankCandidate, ...]) -> list[tuple[int, float]]:
    """Validate one scoring response against the exact ranks that were sent."""

    try:
        parsed = _HTTPResponse.model_validate(body)
    except ValueError as error:
        raise ValueError("HTTP rerank response does not match the declared schema") from error

    expected = {candidate.rank for candidate in batch}
    seen: set[int] = set()
    collected: list[tuple[int, float]] = []
    for item in parsed.scores:
        if item.rank not in expected:
            raise ValueError(f"HTTP rerank returned unexpected rank {item.rank}")
        if item.rank in seen:
            raise ValueError(f"HTTP rerank returned duplicate rank {item.rank}")
        if not math.isfinite(item.score):
            raise ValueError(f"HTTP rerank returned a non-finite score for rank {item.rank}")
        seen.add(item.rank)
        collected.append((item.rank, item.score))

    missing = expected - seen
    if missing:
        raise ValueError(f"HTTP rerank omitted ranks {sorted(missing)}")
    return collected


__all__ = [
    "DEFAULT_HTTP_RERANK_BATCH_SIZE",
    "DEFAULT_HTTP_RERANK_TIMEOUT_SECONDS",
    "HTTP_MEMORY_RERANK_POLICY_ID",
    "HTTPMemoryReranker",
]
