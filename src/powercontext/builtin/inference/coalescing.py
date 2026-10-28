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

"""Coalesce concurrent Embedding work and reuse successful vectors.

One wrapper instance owns the request budget for every caller that shares it:

* duplicate texts inside one call are sent once;
* texts already being computed by another call join that in-flight batch;
* texts requested inside the coalescing window are merged into one batch;
* successful vectors are kept in a bounded LRU cache so later calls cost nothing.

Waiting callers observe the provider usage of the distinct batches they waited
for, and cancelling one caller never cancels work other callers still need.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol

from powercontext.builtin.inference.models import (
    EmbeddingResult,
    EmbeddingVector,
    InferenceUsage,
)

DEFAULT_MAX_BATCH_SIZE = 64
DEFAULT_COALESCE_MS = 5.0
DEFAULT_CACHE_SIZE = 1024


class _EmbeddingProvider(Protocol):
    """The parts of an Embedding model this wrapper relies on."""

    profile: object

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One text resolved either from the cache or from one provider batch."""

    vector: EmbeddingVector
    batch: object | None = None
    usage: InferenceUsage | None = None


class CoalescingEmbeddingModel:
    """Deduplicate, batch, and cache Embedding work across concurrent callers."""

    def __init__(
        self,
        model: _EmbeddingProvider,
        /,
        *,
        max_batch_size: int = DEFAULT_MAX_BATCH_SIZE,
        coalesce_ms: float = DEFAULT_COALESCE_MS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size must be at least one")
        if coalesce_ms < 0:
            raise ValueError("coalesce_ms must not be negative")
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")
        self._model = model
        self._max_batch_size = max_batch_size
        self._window_seconds = coalesce_ms / 1000.0
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._waiters: dict[str, asyncio.Future[_Resolved]] = {}
        self._pending: list[str] = []
        self._lock = asyncio.Lock()
        self._dispatcher: asyncio.Task[None] | None = None

    @property
    def profile(self) -> object:
        """Expose the wrapped provider profile unchanged."""

        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        """Return one vector per input position, sharing work with other callers."""

        ordered = list(texts)
        unique = list(dict.fromkeys(ordered))
        if not unique:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))

        cached: dict[str, EmbeddingVector] = {}
        joined: dict[str, asyncio.Future[_Resolved]] = {}
        async with self._lock:
            for text in unique:
                hit = self._cache_lookup(text)
                if hit is not None:
                    cached[text] = hit
                    continue
                future = self._waiters.get(text)
                if future is None:
                    future = asyncio.get_running_loop().create_future()
                    self._waiters[text] = future
                    self._pending.append(text)
                joined[text] = future
            if self._pending and (self._dispatcher is None or self._dispatcher.done()):
                self._dispatcher = asyncio.create_task(self._dispatch())

        resolved: dict[str, _Resolved] = {
            text: _Resolved(vector=vector) for text, vector in cached.items()
        }
        # ``shield`` keeps shared provider work alive when this caller is cancelled.
        for text, future in joined.items():
            resolved[text] = await asyncio.shield(future)

        return EmbeddingResult(
            vectors=tuple(resolved[text].vector for text in ordered),
            usage=_aggregate_usage(resolved, unique),
        )

    def _cache_lookup(self, text: str) -> EmbeddingVector | None:
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    async def _dispatch(self) -> None:
        """Collect pending texts into bounded batches while the window is open."""

        if self._window_seconds > 0:
            await asyncio.sleep(self._window_seconds)
        else:
            # Still yield once so callers scheduled in the same tick coalesce.
            await asyncio.sleep(0)
        while True:
            async with self._lock:
                if not self._pending:
                    self._dispatcher = None
                    return
                batch = self._pending[: self._max_batch_size]
                del self._pending[: len(batch)]
            await self._run_batch(batch)

    async def _run_batch(self, batch: list[str]) -> None:
        token = object()
        waiters = dict(self._waiters)
        interested = {text: waiters[text] for text in batch if text in waiters}
        try:
            result = await self._model.embed(tuple(batch))
        except Exception as error:  # noqa: BLE001 - every waiter observes the provider failure.
            self._settle(interested, None, error)
            return

        vectors = tuple(result.vectors)
        if len(vectors) != len(batch):
            self._settle(
                interested,
                None,
                ValueError("embedding provider returned a mismatched vector count"),
            )
            return

        for text, vector in zip(batch, vectors, strict=True):
            if self._cache_size > 0:
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        self._settle(interested, (batch, vectors, token, result.usage), None)

    def _settle(
        self,
        waiters: dict[str, asyncio.Future[_Resolved]],
        outcome: tuple[list[str], tuple[EmbeddingVector, ...], object, InferenceUsage] | None,
        error: BaseException | None,
    ) -> None:
        resolved = dict(zip(*outcome[:2], strict=True)) if outcome is not None else {}
        token = outcome[2] if outcome is not None else None
        usage = outcome[3] if outcome is not None else None
        for text, future in waiters.items():
            if not future.done():
                if error is not None:
                    future.set_exception(error)
                else:
                    future.set_result(
                        _Resolved(vector=resolved[text], batch=token, usage=usage)
                    )
            if self._waiters.get(text) is future:
                del self._waiters[text]


def _aggregate_usage(resolved: dict[str, _Resolved], unique: list[str]) -> InferenceUsage:
    """Count each distinct provider batch a caller waited for exactly once."""

    requests = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    seen: set[int] = set()
    for text in unique:
        item = resolved[text]
        if item.batch is None or item.usage is None:
            continue
        key = id(item.batch)
        if key in seen:
            continue
        seen.add(key)
        requests += item.usage.requests
        if item.usage.input_tokens is not None:
            input_tokens = (input_tokens or 0) + item.usage.input_tokens
        if item.usage.output_tokens is not None:
            output_tokens = (output_tokens or 0) + item.usage.output_tokens
    return InferenceUsage(requests=requests, input_tokens=input_tokens, output_tokens=output_tokens)


__all__ = [
    "DEFAULT_CACHE_SIZE",
    "DEFAULT_COALESCE_MS",
    "DEFAULT_MAX_BATCH_SIZE",
    "CoalescingEmbeddingModel",
]
