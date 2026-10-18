"""Validated route parameter types shared by internal database-backed APIs."""

from typing import Annotated

from fastapi import Path

MAX_DATABASE_ID = 2_147_483_647
DatabaseId = Annotated[int, Path(gt=0, le=MAX_DATABASE_ID)]
