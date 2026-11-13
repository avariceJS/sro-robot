#!/usr/bin/env python3
"""Serve the existing private queue behind one HTTPS reverse proxy; no workers."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))


def private_token(path):
    """Read an existing secret by descriptor, without following symlinks or logging it."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid()
                or not 32 <= info.st_size <= 257):
            raise ValueError('invalid_private_token_file')
        with os.fdopen(descriptor, 'r', encoding='ascii') as stream:
            descriptor = None
            token = stream.read(258).strip()
        if not 32 <= len(token) <= 256 or any(c.isspace() for c in token):
            raise ValueError('invalid_private_token_file')
        return token
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--public-origin', required=True)
    parser.add_argument('--expert', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8767)
    parser.add_argument('--session-seconds', type=int, default=43200)
    args = parser.parse_args(argv)
    try:
        from sro_robot.research.review_api import create_app, validate_remote_configuration
        from sro_robot.research.review_store import ReviewStore
        import uvicorn

        token_path = args.token_file.expanduser()
        if token_path.is_symlink() or token_path.resolve().is_relative_to(PROJECT):
            raise ValueError('invalid_private_token_file')
        # Configuration validation precedes opening or creating the private store.
        token = private_token(token_path)
        validate_remote_configuration(args.public_origin, args.expert, args.session_seconds)
        if not 1024 <= args.port <= 65535:
            raise ValueError('invalid_remote_port')
        store = ReviewStore(args.root)
        app = create_app(store, launch_token=token, port=args.port, public_origin=args.public_origin,
                         expert_identity=args.expert, session_seconds=args.session_seconds)
        uvicorn.run(app, host='127.0.0.1', port=args.port, proxy_headers=False,
                    access_log=False, log_level='critical')
        return 0
    except (OSError, ValueError, UnicodeError):
        print('Remote review could not start; check private configuration and storage.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
