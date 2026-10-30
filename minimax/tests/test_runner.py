from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from minimax.sdd_context.config import Settings
from minimax.sdd_context.minimax_client import MiniMaxResponse
from minimax.sdd_context.models import ModelChange, ModelPlan, TaskDefinition, sha256_text
from minimax.sdd_context.powercontext_bridge import RecalledMemory
from minimax.sdd_context.runner import SDDRunner
from minimax.sdd_context.store import ContextStore


class FakeMiniMax:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str) -> MiniMaxResponse:
        self.calls += 1
        self.last_prompt = prompt
        raw = {
            "summary": "Normalize the value to lowercase.",
            "changes": [{"path": "target.py", "content": "def normalize(value: str) -> str:\n    return value.lower()\n"}],
            "test_rationale": "The immutable command checks the required normalization.",
        }
        plan = ModelPlan(
            summary=raw["summary"],
            changes=(ModelChange(path="target.py", content=raw["changes"][0]["content"]),),
            test_rationale=raw["test_rationale"],
            raw=raw,
        )
        return MiniMaxResponse(plan=plan, request_id="request-test", usage={"total_tokens": 42}, response_sha256="f" * 64)


class BrokenMiniMax(FakeMiniMax):
    def generate(self, prompt: str) -> MiniMaxResponse:
        response = super().generate(prompt)
        broken = "def normalize(value: str) -> str:\n    return 'wrong'\n"
        plan = ModelPlan(
            summary="Return an invalid implementation.",
            changes=(ModelChange(path="target.py", content=broken),),
            test_rationale="This should fail verification.",
            raw={"summary": "invalid", "changes": [{"path": "target.py", "content": broken}]},
        )
        return MiniMaxResponse(
            plan=plan,
            request_id=response.request_id,
            usage=response.usage,
            response_sha256=response.response_sha256,
        )


class FakePowerContext:
    def __init__(self) -> None:
        self.remembered: list[str] = []

    async def recall(self, query: str) -> RecalledMemory:
        return RecalledMemory(scope_id="scope-test", texts=tuple(self.remembered), citations=())

    async def remember(self, *, scope_id: str, kind: str, text: str, reason: str):
        self.remembered.append(text)
        return {"entry_id": "entry-test", "scope_id": scope_id, "kind": kind, "reason": reason}


class RunnerTest(unittest.TestCase):
    def _fixture(self, root: Path, model: FakeMiniMax) -> tuple[SDDRunner, TaskDefinition, ContextStore]:
        (root / "spec.md").write_text("normalize to lowercase", encoding="utf-8")
        (root / "target.py").write_text("def normalize(value: str) -> str:\n    return value\n", encoding="utf-8")
        (root / "test_marker.txt").write_text("immutable", encoding="utf-8")
        database = root / "context.db"
        task = TaskDefinition(
            task_id="normalize",
            project_key="test-project",
            description="Normalize a value.",
            repository=root,
            specifications=("spec.md",),
            code_paths=("target.py",),
            test_paths=("test_marker.txt",),
            decision_paths=(),
            progress_paths=(),
            allowed_changes=("target.py",),
            verification_command=(sys.executable, "-c", "from target import normalize; assert normalize('A') == 'a'"),
        )
        settings = Settings(
            database_url=database.as_uri().replace("file:", "sqlite:"),
            minimax_api_key="test",
            minimax_base_url="https://example.invalid",
            minimax_model="MiniMax-test",
            powercontext_url="http://example.invalid",
            powercontext_token=None,
            evidence_dir=root / "evidence",
        )
        store = ContextStore(settings.database_url, allow_sqlite=True)
        return SDDRunner(settings=settings, store=store, model=model, powercontext=FakePowerContext()), task, store

    def test_verified_run_is_reused_without_second_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = FakeMiniMax()
            runner, task, _store = self._fixture(root, model)

            first = runner.run(task)
            second = runner.run(task)

            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "reused")
            self.assertEqual(second["reused_from_run_id"], first["run_id"])
            self.assertEqual(model.calls, 1)
            self.assertIn("<context kind=\"specification\"", model.last_prompt)
            self.assertEqual(sha256_text((root / "target.py").read_text(encoding="utf-8")), sha256_text(
                "def normalize(value: str) -> str:\n    return value.lower()\n"
            ))

    def test_failed_verification_restores_code_and_is_not_reusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = BrokenMiniMax()
            runner, task, store = self._fixture(root, model)
            original = (root / "target.py").read_text(encoding="utf-8")

            with self.assertRaisesRegex(Exception, "verification failed"):
                runner.run(task)

            self.assertEqual((root / "target.py").read_text(encoding="utf-8"), original)
            self.assertEqual(store.list_runs(task.task_id)[0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
