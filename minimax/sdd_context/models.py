from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    project_key: str
    description: str
    repository: Path
    specifications: tuple[str, ...]
    code_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    decision_paths: tuple[str, ...]
    progress_paths: tuple[str, ...]
    allowed_changes: tuple[str, ...]
    verification_command: tuple[str, ...]

    @classmethod
    def load(cls, path: Path, *, repository: Path | None = None) -> TaskDefinition:
        value = json.loads(path.read_text(encoding="utf-8"))
        root = (repository or path.parents[2]).resolve()
        return cls(
            task_id=str(value["task_id"]),
            project_key=str(value["project_key"]),
            description=str(value["description"]),
            repository=root,
            specifications=tuple(value.get("specifications", ())),
            code_paths=tuple(value.get("code_paths", ())),
            test_paths=tuple(value.get("test_paths", ())),
            decision_paths=tuple(value.get("decision_paths", ())),
            progress_paths=tuple(value.get("progress_paths", ())),
            allowed_changes=tuple(value["allowed_changes"]),
            verification_command=tuple(value["verification_command"]),
        )

    @property
    def definition_digest(self) -> str:
        payload = {
            "task_id": self.task_id,
            "project_key": self.project_key,
            "description": self.description,
            "specifications": self.specifications,
            "code_paths": self.code_paths,
            "test_paths": self.test_paths,
            "decision_paths": self.decision_paths,
            "progress_paths": self.progress_paths,
            "allowed_changes": self.allowed_changes,
            "verification_command": self.verification_command,
        }
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class ContextItem:
    kind: str
    source_path: str
    source_digest: str
    content: str
    status: str = "active"


@dataclass(frozen=True)
class RetrievedContext:
    items: tuple[ContextItem, ...]
    context_digest: str

    @classmethod
    def from_items(cls, items: list[ContextItem]) -> RetrievedContext:
        ordered = tuple(sorted(items, key=lambda item: (item.kind, item.source_path, item.source_digest)))
        material = "\n".join(
            f"{item.kind}\0{item.source_path}\0{item.source_digest}\0{item.content}" for item in ordered
        )
        return cls(items=ordered, context_digest=sha256_text(material))


@dataclass(frozen=True)
class ModelChange:
    path: str
    content: str


@dataclass(frozen=True)
class ModelPlan:
    summary: str
    changes: tuple[ModelChange, ...]
    test_rationale: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CompletedRun:
    run_id: str
    task_id: str
    result_code_digest: str
    verification_output: str
    model: str
