# ruff: noqa: TRY300
from __future__ import annotations

import asyncio
import difflib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import SDDContextError
from .minimax_client import MiniMaxClient
from .models import ContextItem, RetrievedContext, TaskDefinition, sha256_text
from .powercontext_bridge import PowerContextBridge, RecalledMemory
from .store import ContextStore


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SDDRunner:
    def __init__(
        self,
        *,
        settings: Settings,
        store: ContextStore | None = None,
        model: Any | None = None,
        powercontext: Any | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or ContextStore(settings.database_url)
        self.model = model or MiniMaxClient(
            api_key=settings.minimax_api_key,
            base_url=settings.minimax_base_url,
            model=settings.minimax_model,
        )
        self._powercontext_override = powercontext

    def initialize(self) -> None:
        self.store.initialize()

    def ingest(self, task: TaskDefinition) -> RetrievedContext:
        sources = (
            (("specification", path) for path in task.specifications),
            (("code", path) for path in task.code_paths),
            (("test", path) for path in task.test_paths),
            (("decision", path) for path in task.decision_paths),
            (("progress", path) for path in task.progress_paths),
        )
        for group in sources:
            for kind, relative in group:
                path = self._safe_path(task.repository, relative)
                content = path.read_text(encoding="utf-8")
                self.store.upsert_context(
                    task.project_key,
                    task.task_id,
                    ContextItem(kind=kind, source_path=relative, source_digest=sha256_text(content), content=content),
                )
        return RetrievedContext.from_items(self.store.retrieve_active(task.project_key, task.task_id))

    def run(self, task: TaskDefinition) -> dict[str, Any]:
        self.initialize()
        context = self.ingest(task)
        input_code_digest = self._code_digest(task)
        recalled = asyncio.run(self._recall(task))
        reusable = self.store.find_reusable_run(
            project_key=task.project_key,
            task_id=task.task_id,
            definition_digest=task.definition_digest,
            context_digest=context.context_digest,
            code_digest=input_code_digest,
        )
        run_id = str(uuid.uuid4())
        common = {
            "run_id": run_id,
            "project_key": task.project_key,
            "task_id": task.task_id,
            "definition_digest": task.definition_digest,
            "context_digest": context.context_digest,
            "input_code_digest": input_code_digest,
            "verification_command": json.dumps(task.verification_command, ensure_ascii=False),
            "started_at": _utc_now(),
        }
        if reusable is not None:
            self.store.start_run(
                {
                    **common,
                    "result_code_digest": input_code_digest,
                    "status": "reused",
                    "model": reusable.model,
                    "verification_output": reusable.verification_output,
                    "reused_from_run_id": reusable.run_id,
                    "completed_at": _utc_now(),
                }
            )
            self.store.record_powercontext_event(
                run_id, "recall", recalled.scope_id, {"citations": list(recalled.citations)}
            )
            result = {
                "run_id": run_id,
                "status": "reused",
                "reused_from_run_id": reusable.run_id,
                "model_called": False,
                "code_changed": False,
                "powercontext_recalled": len(recalled.texts),
                "verification_output": reusable.verification_output,
            }
            self._write_evidence(run_id, "result.json", result)
            self._write_evidence(
                run_id,
                "database-evidence.json",
                self.store.evidence_snapshot(task.project_key, task.task_id),
            )
            self._write_evidence(run_id, "sql-audit.json", self.store.audit)
            return result

        self.store.start_run({**common, "status": "running", "model": self.settings.minimax_model})
        self.store.record_powercontext_event(run_id, "recall", recalled.scope_id, {"citations": list(recalled.citations)})
        prompt = self._prompt(task, context, recalled)
        self._write_evidence(run_id, "prompt.txt", prompt)
        backups: dict[Path, str] = {}
        try:
            response = self.model.generate(prompt)
            self._write_evidence(run_id, "minimax-response.json", response.plan.raw)
            patch_text = self._apply_plan(task, response.plan.changes, backups)
            self._write_evidence(run_id, "change.patch", patch_text)
            verification = self._verify(task)
            self._write_evidence(run_id, "verification.txt", verification)
            result_code_digest = self._code_digest(task)
            result_context = self.ingest(task)
            outcome = (
                f"Task {task.task_id} completed by {self.settings.minimax_model}. {response.plan.summary} "
                f"Verification passed: {' '.join(task.verification_command)}. Result digest: {result_code_digest}."
            )
            citation = asyncio.run(
                self._remember(
                    task,
                    scope_id=recalled.scope_id,
                    kind="progress",
                    text=outcome,
                    reason="Preserve a verified SDD task outcome for a later run.",
                )
            )
            self.store.record_powercontext_event(run_id, "remember", recalled.scope_id, citation)
            self.store.finish_run(
                run_id,
                {
                    "status": "completed",
                    "context_digest": result_context.context_digest,
                    "result_code_digest": result_code_digest,
                    "prompt_sha256": sha256_text(prompt),
                    "response_sha256": response.response_sha256,
                    "patch_sha256": sha256_text(patch_text),
                    "verification_output": verification,
                    "completed_at": _utc_now(),
                },
            )
            result = {
                "run_id": run_id,
                "status": "completed",
                "model": self.settings.minimax_model,
                "model_called": True,
                "minimax_request_id": response.request_id,
                "usage": response.usage,
                "code_changed": True,
                "result_code_digest": result_code_digest,
                "powercontext_scope_id": recalled.scope_id,
                "powercontext_citation": citation,
                "verification_output": verification,
            }
            self._write_evidence(run_id, "result.json", result)
            self._write_evidence(
                run_id,
                "database-evidence.json",
                self.store.evidence_snapshot(task.project_key, task.task_id),
            )
            self._write_evidence(run_id, "sql-audit.json", self.store.audit)
            return result
        except Exception as error:
            for path, content in backups.items():
                path.write_text(content, encoding="utf-8")
            self.store.finish_run(
                run_id,
                {"status": "failed", "verification_output": str(error), "completed_at": _utc_now()},
            )
            self._write_evidence(run_id, "failure.txt", str(error))
            self._write_evidence(run_id, "sql-audit.json", self.store.audit)
            raise

    async def _recall(self, task: TaskDefinition) -> RecalledMemory:
        bridge = self._powercontext_override or PowerContextBridge(
            base_url=self.settings.powercontext_url,
            token=self.settings.powercontext_token,
            project_key=task.project_key,
        )
        return await bridge.recall(f"{task.task_id} {task.description}")

    async def _remember(self, task: TaskDefinition, **kwargs: Any) -> dict[str, Any]:
        bridge = self._powercontext_override or PowerContextBridge(
            base_url=self.settings.powercontext_url,
            token=self.settings.powercontext_token,
            project_key=task.project_key,
        )
        return await bridge.remember(**kwargs)

    def _prompt(self, task: TaskDefinition, context: RetrievedContext, recalled: RecalledMemory) -> str:
        blocks = []
        for item in context.items:
            blocks.append(
                f"<context kind=\"{item.kind}\" source=\"{item.source_path}\" sha256=\"{item.source_digest}\">\n"
                f"{item.content}\n</context>"
            )
        memory = "\n".join(f"- {text}" for text in recalled.texts) or "- No prior matching PowerContext memory."
        return (
            f"Task ID: {task.task_id}\nDescription: {task.description}\n"
            f"Allowed file changes: {json.dumps(task.allowed_changes, ensure_ascii=False)}\n"
            f"Immutable tests: {json.dumps(task.test_paths, ensure_ascii=False)}\n"
            f"Verification command: {json.dumps(task.verification_command, ensure_ascii=False)}\n\n"
            f"PowerContext recall:\n{memory}\n\nRetrieved authoritative context:\n"
            + "\n\n".join(blocks)
            + "\n\nReturn exactly this JSON shape: "
            '{"summary":"...","changes":[{"path":"allowed/path.py","content":"complete UTF-8 file"}],'
            '"test_rationale":"..."}. Change only allowed files and satisfy the immutable specification and tests.'
        )

    def _apply_plan(self, task: TaskDefinition, changes: tuple[Any, ...], backups: dict[Path, str]) -> str:
        allowed = set(task.allowed_changes)
        seen: set[str] = set()
        patches: list[str] = []
        for change in changes:
            if change.path not in allowed or change.path in seen:
                raise SDDContextError(f"MiniMax attempted a disallowed or duplicate path: {change.path}")
            seen.add(change.path)
            path = self._safe_path(task.repository, change.path)
            before = path.read_text(encoding="utf-8")
            backups[path] = before
            path.write_text(change.content, encoding="utf-8")
            patches.extend(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    change.content.splitlines(keepends=True),
                    fromfile=f"a/{change.path}",
                    tofile=f"b/{change.path}",
                )
            )
        patch = "".join(patches)
        if not patch:
            raise SDDContextError("MiniMax returned no effective code change")
        return patch

    def _verify(self, task: TaskDefinition) -> str:
        # The command is an explicit, reviewed part of the committed task definition.
        command = list(task.verification_command)
        if command[0] in {"python", "python3"}:
            command[0] = sys.executable
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=task.repository,
            text=True,
            capture_output=True,
            timeout=180,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode:
            raise SDDContextError(f"verification failed with exit code {completed.returncode}:\n{output}")
        return output

    def _code_digest(self, task: TaskDefinition) -> str:
        material = []
        for relative in sorted(task.allowed_changes):
            path = self._safe_path(task.repository, relative)
            material.append(relative + "\0" + path.read_text(encoding="utf-8"))
        return sha256_text("\n".join(material))

    @staticmethod
    def _safe_path(repository: Path, relative: str) -> Path:
        root = repository.resolve()
        path = (root / relative).resolve()
        if path != root and root not in path.parents:
            raise SDDContextError(f"path escapes repository: {relative}")
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    def _write_evidence(self, run_id: str, name: str, value: Any) -> None:
        directory = self.settings.evidence_dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
