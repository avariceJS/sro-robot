#!/usr/bin/env python3
"""Start an RQ worker using the private application Redis URL."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from redis import Redis
from rq import Queue, Worker

from app.settings import settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("queues", nargs="+", help="RQ queue names to serve")
    args = parser.parse_args(argv)

    connection = Redis.from_url(settings.redis_url)
    try:
        queues = [Queue(name, connection=connection) for name in args.queues]
        Worker(queues, connection=connection).work()
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
