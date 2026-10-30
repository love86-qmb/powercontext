from __future__ import annotations

import unittest

from minimax.sdd_context.task_identity import canonical_repository_key


class RepositoryIdentityTest(unittest.TestCase):
    def test_windows_and_posix_spellings_share_one_key(self) -> None:
        self.assertEqual(canonical_repository_key("C:\\Work\\PowerContext\\"), "c:/work/powercontext")
        self.assertEqual(canonical_repository_key("c:/work//powercontext"), "c:/work/powercontext")

    def test_unc_prefix_is_preserved(self) -> None:
        self.assertEqual(canonical_repository_key("\\\\SERVER\\Share\\Repo\\"), "//server/share/repo")

    def test_posix_path_is_normalized_without_filesystem_access(self) -> None:
        self.assertEqual(canonical_repository_key("/srv/PowerContext/"), "/srv/powercontext")
        self.assertEqual(canonical_repository_key("/"), "/")

    def test_empty_value_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_repository_key("  ")


if __name__ == "__main__":
    unittest.main()
