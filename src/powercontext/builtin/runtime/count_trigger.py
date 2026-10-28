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

"""Start a Family once enough new Source journal identities accumulate.

The count is per Scope and per Family: the number of journal identities after
the Family's last successfully committed Source Cursor. Duplicate captures and
receipt trust upgrades reuse their existing journal identity, so they never
raise the count, while a lineage-only server Source still occupies one identity
even though it stays excluded from generation evidence. Reaching the threshold
accepts exactly the same processing call the time condition already accepts:
``source_window_limit`` remains the single-call read cap, so Memory re-checks the
remaining count after every window and Topic Memory finishes the wave it took.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from powercontext._logging import log_safely
from powercontext.builtin.artifacts.topic_memory import TOPIC_MEMORY_SOURCE_WINDOW_BINDING
from powercontext.builtin.triggers import SOURCE_WINDOW_TRIGGER_NAME

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

logger = logging.getLogger(__name__)

FAMILY_TRIGGER_NAMES = {
    "memory": SOURCE_WINDOW_TRIGGER_NAME,
    "topic-memory": TOPIC_MEMORY_SOURCE_WINDOW_BINDING,
}
DEFAULT_SOURCE_COUNT_POLL_SECONDS = 0.25
_REJECT_COOLDOWN_SECONDS = 1.0


class SourceCountProcessor:
    """Admit one bounded Family processing call when the pending count is met."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        thresholds: Mapping[str, int],
        /,
        *,
        poll_seconds: float = DEFAULT_SOURCE_COUNT_POLL_SECONDS,
        clock: Any = None,
    ) -> None:
        self._runtime = runtime
        self._thresholds = {
            family: threshold
            for family, threshold in thresholds.items()
            if family in FAMILY_TRIGGER_NAMES and isinstance(threshold, int) and threshold >= 1
        }
        self._poll_seconds = poll_seconds
        self._clock = time.monotonic if clock is None else clock
        self._accepted: dict[tuple[str, str], int] = {}
        self._cooldown: dict[tuple[str, str], float] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def thresholds(self) -> dict[str, int]:
        """Return the Family thresholds this processor admits work for."""

        return dict(self._thresholds)

    def start(self) -> None:
        """Begin admitting counted work in the background."""

        if not self._thresholds or self._task is not None:
            return
        self._task = asyncio.create_task(self._poll(), name="powercontext.source-count")

    async def close(self) -> None:
        """Stop admitting counted work and wait for the poll loop to finish."""

        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _poll(self) -> None:
        while True:
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log_safely(
                    logger,
                    logging.WARNING,
                    "Source count admission sweep failed",
                    exc_info=error,
                    extra={
                        "event": "source_count.sweep.failed",
                        "outcome": "failure",
                        "unit": "source-count",
                    },
                )
            await asyncio.sleep(self._poll_seconds)

    async def _sweep(self) -> None:
        if self._runtime._closing or self._runtime._closed:
            return
        scope_ids = await self._runtime._source_count_scope_ids()
        if not scope_ids:
            return
        for scope_id in scope_ids:
            for family, threshold in self._thresholds.items():
                try:
                    await self._activate(scope_id, family, threshold)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log_safely(
                        logger,
                        logging.WARNING,
                        "Counted Source processing failed",
                        exc_info=error,
                        extra={
                            "event": "source_count.processing.failed",
                            "outcome": "failure",
                            "unit": family,
                            "scope_id": scope_id,
                        },
                    )

    async def _activate(self, scope_id: str, family: str, threshold: int, /) -> None:
        key = (scope_id, family)
        now = self._clock()
        cooldown = self._cooldown.get(key)
        if cooldown is not None:
            if now < cooldown:
                return
            del self._cooldown[key]
        frontier = await self._runtime._family_source_frontier(scope_id, FAMILY_TRIGGER_NAMES[family])
        if frontier is None:
            return
        consumed, high_watermark = frontier
        if high_watermark - consumed < threshold:
            # Consumed history never re-enters the count, so a resolved scope forgets
            # both the accepted wave and any earlier rejection.
            self._accepted.pop(key, None)
            return
        if family == "topic-memory":
            await self._accept_topic_wave(scope_id, key, high_watermark)
            return
        try:
            result = await self._runtime.memory.for_scope(scope_id).flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._cooldown[key] = now + _REJECT_COOLDOWN_SECONDS
            raise
        if not getattr(result, "processed", False):
            self._cooldown[key] = now + _REJECT_COOLDOWN_SECONDS

    async def _accept_topic_wave(self, scope_id: str, key: tuple[str, str], high_watermark: int) -> None:
        """Accept one wave per frontier so the taken wave finishes on its own windows."""

        if self._accepted.get(key) == high_watermark:
            return
        try:
            result = await self._runtime.topic_memory.for_scope(scope_id).flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._cooldown[key] = self._clock() + _REJECT_COOLDOWN_SECONDS
            raise
        self._accepted[key] = high_watermark
        if getattr(result, "status", "accepted") == "idle":
            # Nothing was pending after all; let the next poll re-evaluate.
            self._accepted.pop(key, None)


__all__ = [
    "DEFAULT_SOURCE_COUNT_POLL_SECONDS",
    "FAMILY_TRIGGER_NAMES",
    "SourceCountProcessor",
]
