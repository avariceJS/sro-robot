#!/usr/bin/env bash
set -euo pipefail

python_bin="${SRO_ROBOT_PYTHON:-.venv/bin/python}"
exec "$python_bin" -m uvicorn app.main:app --reload --no-access-log "$@"
