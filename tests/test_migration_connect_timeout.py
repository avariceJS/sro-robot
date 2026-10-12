from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class MigrationConnectTimeoutTests(unittest.TestCase):
    def test_alembic_connection_times_out_when_loopback_peer_stalls(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        stop = threading.Event()
        accepted = threading.Event()

        def stall_peer() -> None:
            try:
                connection, _address = listener.accept()
            except OSError:
                return
            accepted.set()
            with connection:
                connection.settimeout(0.1)
                while not stop.is_set():
                    try:
                        if not connection.recv(4096):
                            return
                    except socket.timeout:
                        continue
                    except OSError:
                        return

        peer = threading.Thread(target=stall_peer, daemon=True)
        peer.start()
        target = f"postgresql+psycopg://synthetic:synthetic@127.0.0.1:{port}/synthetic"
        try:
            with tempfile.TemporaryDirectory(prefix="sro-alembic-timeout-") as upload_dir:
                environment = {
                    **os.environ,
                    "APP_DEBUG": "1",
                    "DATABASE_URL": target,
                    "MIGRATION_DATABASE_URL": target,
                    "REDIS_URL": "redis://synthetic:synthetic@127.0.0.1:1/0",
                    "SRO_UPLOAD_DIR": upload_dir,
                }
                started = time.monotonic()
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "alembic",
                        "-c",
                        str(REPO_ROOT / "alembic.ini"),
                        "upgrade",
                        "head",
                    ],
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    timeout=9,
                    check=False,
                )
                elapsed = time.monotonic() - started
        finally:
            stop.set()
            listener.close()
            peer.join(timeout=1)

        self.assertTrue(accepted.is_set(), "Alembic did not reach the loopback TCP stub")
        self.assertNotEqual(result.returncode, 0)
        self.assertGreaterEqual(elapsed, 4)
        self.assertLess(elapsed, 8)


if __name__ == "__main__":
    unittest.main()
