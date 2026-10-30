from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .errors import SDDContextError


@dataclass(frozen=True)
class Settings:
    database_url: str
    minimax_api_key: str
    minimax_base_url: str
    minimax_model: str
    powercontext_url: str
    powercontext_token: str | None
    evidence_dir: Path

    @classmethod
    def from_environment(cls, *, evidence_dir: Path) -> Settings:
        required = ("SDD_DATABASE_URL", "MINIMAX_API_KEY", "POWERCONTEXT_URL")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SDDContextError("missing required environment variables: " + ", ".join(missing))
        return cls(
            database_url=os.environ["SDD_DATABASE_URL"],
            minimax_api_key=os.environ["MINIMAX_API_KEY"],
            minimax_base_url=os.environ.get(
                "MINIMAX_BASE_URL", "https://api.minimaxi.com/v1/text/chatcompletion_v2"
            ),
            minimax_model=os.environ.get("MINIMAX_MODEL", "MiniMax-M3"),
            powercontext_url=os.environ["POWERCONTEXT_URL"],
            powercontext_token=os.environ.get("POWERCONTEXT_TOKEN") or None,
            evidence_dir=evidence_dir,
        )
