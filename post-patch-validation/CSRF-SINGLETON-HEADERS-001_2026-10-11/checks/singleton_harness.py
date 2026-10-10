from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from app.security import _same_host_origin

mode = sys.argv[1]
if mode == "origin-duplicate":
    headers = [
        (b"host", b"robot.example"),
        (b"origin", b"https://attacker.example"),
        (b"origin", b"https://robot.example"),
    ]
    expected = False
elif mode == "host-duplicate":
    headers = [
        (b"host", b"attacker.example"),
        (b"host", b"robot.example"),
        (b"origin", b"https://robot.example"),
    ]
    expected = False
elif mode == "control":
    headers = [(b"host", b"robot.example"), (b"origin", b"https://robot.example")]
    expected = True
elif mode == "behavior":
    headers = [(b"host", b"robot.example"), (b"referer", b"https://robot.example/login?next=%2F")]
    print(json.dumps({"allowed": _same_host_origin(headers)}, sort_keys=True))
    raise SystemExit(0)
elif mode == "security":
    headers = [(b"host", b"robot.example,attacker.example"), (b"origin", b"https://robot.example")]
    expected = False
else:
    raise SystemExit(f"unknown mode: {mode}")

if mode in {"origin-duplicate", "host-duplicate"}:
    print("PPV_REACHED", flush=True)
assert _same_host_origin(headers) is expected
