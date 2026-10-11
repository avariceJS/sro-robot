from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, os.environ["PPV_CHECKOUT"])

from app.settings import Settings


field_names = Settings.__dataclass_fields__
settings_kwargs = {
    "database_url": "postgresql://runtime:synthetic-runtime-password@db.invalid/sro",
    "redis_url": "redis://:synthetic-redis-password@cache.invalid/0",
    "secret_key": "synthetic-session-secret-not-for-output",
    "upload_dir": Path("/tmp/synthetic-upload"),
    "debug": False,
}
if "migration_database_url" in field_names:
    settings_kwargs["migration_database_url"] = (
        "postgresql://migrator:synthetic-migration-password@db.invalid/sro"
    )

representation = repr(Settings(**settings_kwargs))
for secret in (
    "synthetic-runtime-password",
    "synthetic-redis-password",
    "synthetic-session-secret-not-for-output",
    "synthetic-migration-password",
):
    assert secret not in representation
print("configuration credential fields stay out of Settings repr")
