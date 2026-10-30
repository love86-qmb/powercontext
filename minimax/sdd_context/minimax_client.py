from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .errors import SDDContextError
from .models import ModelChange, ModelPlan, sha256_text


@dataclass(frozen=True)
class MiniMaxResponse:
    plan: ModelPlan
    request_id: str | None
    usage: dict[str, Any]
    response_sha256: str


class MiniMaxClient:
    def __init__(self, *, api_key: str, base_url: str, model: str, timeout: float = 180.0) -> None:
        if urlparse(base_url).scheme != "https":
            raise SDDContextError("MiniMax endpoint must use HTTPS")
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> MiniMaxResponse:
        payload = json.dumps(
            {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are the implementation agent in an SDD workflow. Use only the supplied, source-labelled "
                            "context. Return one JSON object and no Markdown. Do not weaken or rewrite tests."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme validated in __init__
            self.base_url,
            data=payload,
            method="POST",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise SDDContextError(f"MiniMax returned HTTP {error.code}: {detail[:1000]}") from error
        value = json.loads(raw)
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            detail = json.dumps(value, ensure_ascii=False, default=str)
            raise SDDContextError(f"MiniMax returned no completion choices: {detail[:1000]}")
        content = choices[0]["message"]["content"]
        plan = parse_model_plan(content)
        return MiniMaxResponse(
            plan=plan,
            request_id=request_id or value.get("request_id") or value.get("id"),
            usage=dict(value.get("usage") or {}),
            response_sha256=sha256_text(raw),
        )


def parse_model_plan(content: str) -> ModelPlan:
    text = content.strip()
    if text.startswith("```"):
        first_line, _, remainder = text.partition("\n")
        del first_line
        text = remainder.rsplit("```", 1)[0].strip()
    value = json.loads(text)
    changes = tuple(ModelChange(path=str(item["path"]), content=str(item["content"])) for item in value["changes"])
    if not changes:
        raise SDDContextError("MiniMax response must contain at least one file change")
    return ModelPlan(
        summary=str(value["summary"]),
        changes=changes,
        test_rationale=str(value.get("test_rationale", "")),
        raw=value,
    )
