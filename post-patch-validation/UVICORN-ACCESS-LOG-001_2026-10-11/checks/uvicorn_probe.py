from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


def request(path: str) -> tuple[int, str, str]:
    checkout = Path(os.environ["PPV_CHECKOUT"])
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--log-level",
        "info",
    ]
    # Use the no-raw-access-log option selected by the checked-in launcher.
    if (checkout / "scripts/dev/run-web.sh").is_file():
        command.append("--no-access-log")

    process = subprocess.Popen(
        command,
        cwd=checkout,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[str] = []
    try:
        deadline = time.monotonic() + 10
        port = None
        while port is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or process.poll() is not None:
                raise RuntimeError("isolated Uvicorn did not start")
            import select

            readable, _, _ = select.select([process.stdout], [], [], remaining)
            if not readable:
                raise TimeoutError("isolated Uvicorn startup timed out")
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("isolated Uvicorn closed its startup output")
            lines.append(line)
            match = re.search("http://127[.]0[.]0[.]1:([0-9]+)", line)
            if match:
                port = int(match.group(1))

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8", errors="replace")
        status = response.status
        connection.close()
        process.send_signal(signal.SIGTERM)
        remainder, _ = process.communicate(timeout=5)
        output = "".join(lines) + remainder
        return status, body, output
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=5)


def main(mode: str) -> None:
    if mode == "task-id":
        path = "/static/739201.pdf?token=synthetic-query-secret"
        status, body, output = request(path)
        print("PPV_REACHED", flush=True)
        assert status == 404
        assert path not in output, "Uvicorn retained the concrete path and query"
        assert "739201" not in output
        assert "synthetic-query-secret" not in output
        assert "HTTP request request_id=" in output
    elif mode == "unmatched":
        path = "/static/synthetic-person@example.invalid?case=A40-12345/2025"
        status, body, output = request(path)
        print("PPV_REACHED", flush=True)
        assert status == 404
        assert path not in output, "Uvicorn retained the user-controlled unmatched path"
        assert "synthetic-person@example.invalid" not in output
        assert "A40-12345/2025" not in output
    elif mode == "control":
        status, body, _output = request("/static/htmx.min.js")
        assert status == 200
        assert len(body) > 0
    elif mode == "behavior":
        status, body, _output = request("/static/synthetic-not-found")
        print(json.dumps({"status": status, "body": body}, sort_keys=True))
    elif mode == "security":
        status, body, _output = request("/static/private-query-check?value=synthetic@example.invalid")
        assert status == 404
        assert "synthetic@example.invalid" not in body
    else:
        raise SystemExit("unknown probe")


if __name__ == "__main__":
    main(sys.argv[1])
