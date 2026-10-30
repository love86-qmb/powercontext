# ruff: noqa: S608
from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .errors import SDDContextError
from .models import CompletedRun, ContextItem


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class ContextStore:
    """Authoritative task context stored in OceanBase/seekdb-compatible SQL.

    SQLite exists only for deterministic unit tests. The competition demo refuses
    it unless ``allow_sqlite`` is explicitly enabled by the test harness.
    """

    def __init__(self, url: str, *, allow_sqlite: bool = False) -> None:
        self.url = url
        self.allow_sqlite = allow_sqlite
        self.engine = urlparse(url).scheme.lower()
        if self.engine == "sqlite" and not allow_sqlite:
            raise SDDContextError("SQLite is test-only; configure seekdb/OceanBase through a mysql:// URL")
        if self.engine not in {"mysql", "oceanbase", "seekdb", "sqlite"}:
            raise SDDContextError("SDD_DATABASE_URL must use mysql://, oceanbase://, seekdb://, or test-only sqlite://")
        self.audit: list[dict[str, Any]] = []

    @property
    def placeholder(self) -> str:
        return "?" if self.engine == "sqlite" else "%s"

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self.engine == "sqlite":
            parsed = urlparse(self.url)
            path = unquote(parsed.path)
            if parsed.netloc:
                path = f"//{parsed.netloc}{path}"
            if re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            connection = sqlite3.connect(path or ":memory:")
            connection.row_factory = sqlite3.Row
        else:
            import pymysql

            parsed = urlparse(self.url)
            query = parse_qs(parsed.query)
            ssl_mode = query.get("ssl", query.get("ssl-mode", ["false"]))[0].lower()
            ssl = {"check_hostname": True} if ssl_mode in {"1", "true", "required", "verify_identity"} else None
            connection = pymysql.connect(
                host=parsed.hostname or "127.0.0.1",
                port=parsed.port or 2881,
                user=unquote(parsed.username or "root"),
                password=unquote(parsed.password or ""),
                database=parsed.path.lstrip("/"),
                charset=query.get("charset", ["utf8mb4"])[0],
                autocommit=False,
                cursorclass=pymysql.cursors.DictCursor,
                ssl=ssl,
            )
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.engine == "sqlite":
            id_column = "INTEGER PRIMARY KEY AUTOINCREMENT"
            text_type = "TEXT"
        else:
            id_column = "BIGINT PRIMARY KEY AUTO_INCREMENT"
            text_type = "LONGTEXT"
        statements = (
            f"""
            CREATE TABLE IF NOT EXISTS sdd_context_items (
                id {id_column}, project_key VARCHAR(128) NOT NULL, task_id VARCHAR(128) NOT NULL,
                kind VARCHAR(32) NOT NULL, source_path VARCHAR(255) NOT NULL,
                source_digest CHAR(64) NOT NULL, content {text_type} NOT NULL,
                status VARCHAR(16) NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
                UNIQUE(project_key, task_id, kind, source_path, source_digest)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS sdd_task_runs (
                run_id VARCHAR(36) PRIMARY KEY, project_key VARCHAR(128) NOT NULL, task_id VARCHAR(128) NOT NULL,
                definition_digest CHAR(64) NOT NULL, context_digest CHAR(64) NOT NULL,
                input_code_digest CHAR(64) NOT NULL, result_code_digest CHAR(64),
                status VARCHAR(20) NOT NULL, model VARCHAR(128) NOT NULL,
                prompt_sha256 CHAR(64), response_sha256 CHAR(64), patch_sha256 CHAR(64),
                verification_command {text_type} NOT NULL, verification_output {text_type},
                reused_from_run_id VARCHAR(36), started_at VARCHAR(40) NOT NULL, completed_at VARCHAR(40)
            )
            """,
            f"""
            CREATE TABLE IF NOT EXISTS sdd_powercontext_events (
                id {id_column}, run_id VARCHAR(36) NOT NULL, operation VARCHAR(32) NOT NULL,
                scope_id VARCHAR(191), citation_json {text_type}, created_at VARCHAR(40) NOT NULL
            )
            """,
        )
        with self.connect() as connection:
            cursor = connection.cursor()
            for statement in statements:
                cursor.execute(statement)
        self._audit("initialize", rows=0)

    def upsert_context(self, project_key: str, task_id: str, item: ContextItem) -> None:
        p = self.placeholder
        now = _utc_now()
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"UPDATE sdd_context_items SET status={p}, updated_at={p} "
                f"WHERE project_key={p} AND task_id={p} AND kind={p} AND source_path={p} AND status={p}",
                ("superseded", now, project_key, task_id, item.kind, item.source_path, "active"),
            )
            cursor.execute(
                f"SELECT id FROM sdd_context_items WHERE project_key={p} AND task_id={p} "
                f"AND kind={p} AND source_path={p} AND source_digest={p}",
                (project_key, task_id, item.kind, item.source_path, item.source_digest),
            )
            existing = cursor.fetchone()
            if existing is None:
                cursor.execute(
                    f"INSERT INTO sdd_context_items "
                    f"(project_key,task_id,kind,source_path,source_digest,content,status,created_at,updated_at) "
                    f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p})",
                    (
                        project_key,
                        task_id,
                        item.kind,
                        item.source_path,
                        item.source_digest,
                        item.content,
                        "active",
                        now,
                        now,
                    ),
                )
            else:
                cursor.execute(
                    f"UPDATE sdd_context_items SET content={p}, status={p}, updated_at={p} "
                    f"WHERE project_key={p} AND task_id={p} AND kind={p} AND source_path={p} AND source_digest={p}",
                    (item.content, "active", now, project_key, task_id, item.kind, item.source_path, item.source_digest),
                )
        self._audit("upsert_context", rows=1, kind=item.kind, source_path=item.source_path)

    def retrieve_active(self, project_key: str, task_id: str) -> list[ContextItem]:
        p = self.placeholder
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT kind,source_path,source_digest,content,status FROM sdd_context_items "
                f"WHERE project_key={p} AND task_id={p} AND status={p} ORDER BY kind,source_path",
                (project_key, task_id, "active"),
            )
            rows = cursor.fetchall()
        items = [ContextItem(**dict(row)) for row in rows]
        self._audit("retrieve_active", rows=len(items), task_id=task_id)
        return items

    def find_reusable_run(
        self,
        *,
        project_key: str,
        task_id: str,
        definition_digest: str,
        context_digest: str,
        code_digest: str,
    ) -> CompletedRun | None:
        p = self.placeholder
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT run_id,task_id,result_code_digest,verification_output,model FROM sdd_task_runs "
                f"WHERE project_key={p} AND task_id={p} AND definition_digest={p} AND context_digest={p} "
                f"AND result_code_digest={p} AND status={p} ORDER BY completed_at DESC LIMIT 1",
                (project_key, task_id, definition_digest, context_digest, code_digest, "completed"),
            )
            row = cursor.fetchone()
        self._audit("find_reusable_run", rows=1 if row else 0, task_id=task_id)
        return CompletedRun(**dict(row)) if row else None

    def start_run(self, values: dict[str, Any]) -> None:
        p = self.placeholder
        columns = tuple(values)
        with self.connect() as connection:
            connection.cursor().execute(
                f"INSERT INTO sdd_task_runs ({','.join(columns)}) VALUES ({','.join(p for _ in columns)})",
                tuple(values[column] for column in columns),
            )
        self._audit("start_run", rows=1, run_id=values["run_id"])

    def finish_run(self, run_id: str, values: dict[str, Any]) -> None:
        p = self.placeholder
        assignments = ",".join(f"{column}={p}" for column in values)
        with self.connect() as connection:
            connection.cursor().execute(
                f"UPDATE sdd_task_runs SET {assignments} WHERE run_id={p}",
                (*tuple(values.values()), run_id),
            )
        self._audit("finish_run", rows=1, run_id=run_id, status=values.get("status"))

    def record_powercontext_event(
        self, run_id: str, operation: str, scope_id: str | None, citation: dict[str, Any] | None
    ) -> None:
        p = self.placeholder
        with self.connect() as connection:
            connection.cursor().execute(
                f"INSERT INTO sdd_powercontext_events (run_id,operation,scope_id,citation_json,created_at) "
                f"VALUES ({p},{p},{p},{p},{p})",
                (run_id, operation, scope_id, json.dumps(citation, ensure_ascii=False), _utc_now()),
            )
        self._audit("powercontext_event", rows=1, powercontext_operation=operation)

    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        p = self.placeholder
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute(
                f"SELECT run_id,task_id,status,model,reused_from_run_id,started_at,completed_at "
                f"FROM sdd_task_runs WHERE task_id={p} ORDER BY started_at",
                (task_id,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        self._audit("list_runs", rows=len(rows), task_id=task_id)
        return rows

    def evidence_snapshot(self, project_key: str, task_id: str) -> dict[str, Any]:
        """Return a credential-free snapshot proving storage and reuse state."""
        p = self.placeholder
        with self.connect() as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT sqlite_version() AS version" if self.engine == "sqlite" else "SELECT VERSION() AS version")
            server = dict(cursor.fetchone())
            cursor.execute(
                f"SELECT kind,source_path,source_digest,status,created_at,updated_at "
                f"FROM sdd_context_items WHERE project_key={p} AND task_id={p} "
                f"ORDER BY kind,source_path,created_at",
                (project_key, task_id),
            )
            contexts = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT run_id,status,model,reused_from_run_id,prompt_sha256,response_sha256,patch_sha256,"
                f"input_code_digest,result_code_digest,started_at,completed_at FROM sdd_task_runs "
                f"WHERE project_key={p} AND task_id={p} ORDER BY started_at",
                (project_key, task_id),
            )
            runs = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                f"SELECT e.run_id,e.operation,e.scope_id,e.citation_json,e.created_at "
                f"FROM sdd_powercontext_events e JOIN sdd_task_runs r ON r.run_id=e.run_id "
                f"WHERE r.project_key={p} AND r.task_id={p} ORDER BY e.id",
                (project_key, task_id),
            )
            events = [dict(row) for row in cursor.fetchall()]
        self._audit("evidence_snapshot", rows=len(contexts) + len(runs) + len(events), task_id=task_id)
        return {
            "database_engine": self.engine,
            "server": server,
            "contexts": contexts,
            "runs": runs,
            "events": events,
        }

    def _audit(self, operation: str, *, rows: int, **details: Any) -> None:
        self.audit.append({"operation": operation, "rows": rows, **details})
