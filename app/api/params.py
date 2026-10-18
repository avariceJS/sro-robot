"""Validated route parameter types shared by internal database-backed APIs."""

from typing import Annotated

from fastapi import Path

DatabaseId = Annotated[int, Path(gt=0, le=2_147_483_647)]
