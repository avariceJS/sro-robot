from __future__ import annotations

from pathlib import Path
import unittest

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.db.schema import EXPECTED_SCHEMA_REVISION


class SchemaRevisionPolicyTests(unittest.TestCase):
    def test_runtime_readiness_revision_matches_the_single_alembic_head(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        config = Config(str(repository_root / "alembic.ini"))
        heads = ScriptDirectory.from_config(config).get_heads()

        self.assertEqual(heads, [EXPECTED_SCHEMA_REVISION])


if __name__ == "__main__":
    unittest.main()
