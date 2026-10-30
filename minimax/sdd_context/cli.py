from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .models import TaskDefinition
from .runner import SDDRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MiniMax x OceanBase SDD context manager")
    parser.add_argument("--task", type=Path, default=Path("minimax/tasks/task-identity.json"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("minimax/evidence"))
    parser.add_argument("--env-file", type=Path, default=Path("minimax/.env"))
    parser.add_argument("command", choices=("init-db", "ingest", "run", "show-runs"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.env_file.is_file():
        load_dotenv(args.env_file, override=False)
    task = TaskDefinition.load(args.task.resolve())
    settings = Settings.from_environment(evidence_dir=args.evidence_dir.resolve())
    runner = SDDRunner(settings=settings)
    if args.command == "init-db":
        runner.initialize()
        result = {"status": "initialized", "database_engine": runner.store.engine}
    elif args.command == "ingest":
        runner.initialize()
        context = runner.ingest(task)
        result = {
            "status": "ingested",
            "items": len(context.items),
            "context_digest": context.context_digest,
            "sources": [item.source_path for item in context.items],
        }
    elif args.command == "run":
        result = runner.run(task)
    else:
        runner.initialize()
        result = {"runs": runner.store.list_runs(task.task_id)}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0
