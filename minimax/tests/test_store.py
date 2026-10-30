from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from minimax.sdd_context.errors import SDDContextError
from minimax.sdd_context.models import ContextItem
from minimax.sdd_context.store import ContextStore


class ContextStoreTest(unittest.TestCase):
    def test_changed_source_supersedes_old_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "context.db"
            store = ContextStore(database.as_uri().replace("file:", "sqlite:"), allow_sqlite=True)
            store.initialize()
            store.upsert_context("project", "task", ContextItem("specification", "spec.md", "a" * 64, "old"))
            store.upsert_context("project", "task", ContextItem("specification", "spec.md", "b" * 64, "new"))

            current = store.retrieve_active("project", "task")

            self.assertEqual([(item.source_digest, item.content) for item in current], [("b" * 64, "new")])

    def test_sqlite_requires_explicit_test_opt_in(self) -> None:
        with self.assertRaises(SDDContextError):
            ContextStore("sqlite:///context.db")


if __name__ == "__main__":
    unittest.main()
