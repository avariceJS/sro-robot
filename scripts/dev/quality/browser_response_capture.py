"""Private original-response capture in one explicitly owned research Chrome."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CONFIG_NAME = "browser-response-capture.json"
MAX_BYTES = 25 * 1024 * 1024
REPO = Path(__file__).resolve().parents[3]


class BrowserCaptureError(Exception):
    def __init__(self, code, *, disable=False, cooldown_seconds=60):
        super().__init__(code)
        self.code, self.disable, self.cooldown_seconds = code, disable, cooldown_seconds


def _private(path, *, directory=False):
    path = Path(path)
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path.absolute()
            or not (path.is_dir() if directory else path.is_file())
            or path.stat().st_mode & 0o077 or path == REPO
            or path.is_relative_to(REPO) or REPO.is_relative_to(path)):
        raise BrowserCaptureError("browser_response_configuration_rejected", disable=True)


def _argv_from_procargs(data):
    """Decode only argc arguments, never the following process environment."""
    import struct
    if len(data) < 5:
        raise ValueError()
    argc = struct.unpack_from("=i", data)[0]
    if not 1 <= argc <= 4096:
        raise ValueError()
    offset = data.index(b"\0", 4) + 1  # Skip the executable path and padding.
    while offset < len(data) and data[offset] == 0:
        offset += 1
    arguments = []
    for _ in range(argc):
        end = data.index(b"\0", offset)
        arguments.append(os.fsdecode(data[offset:end]))
        offset = end + 1
    return arguments


def _process_argv(pid):
    if sys.platform == "darwin":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN / KERN_PROCARGS2.
        size = ctypes.c_size_t()
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not 4 < size.value <= 4 * 1024 * 1024:
            raise ValueError()
        buffer = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            raise ValueError()
        return _argv_from_procargs(buffer.raw[:size.value])
    if sys.platform.startswith("linux"):
        return [os.fsdecode(value) for value in Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    raise ValueError()


def _owned_argv(arguments, profile):
    expected = {"--user-data-dir": "--user-data-dir=" + str(profile),
                "--remote-debugging-port": "--remote-debugging-port=0",
                "--remote-debugging-address": "--remote-debugging-address=127.0.0.1"}
    return (isinstance(arguments, list) and arguments and
            all([arg for arg in arguments if arg == key or arg.startswith(key + "=")] == [value]
                for key, value in expected.items()) and
            "--remote-debugging-pipe" not in arguments)


def _loopback_listener(pid, port):
    lsof = shutil.which("lsof")
    if not lsof:
        raise ValueError()
    result = subprocess.run([lsof, "-nP", "-a", "-p", str(pid), "-iTCP:" + str(port),
                             "-sTCP:LISTEN", "-Fpn"], capture_output=True, text=True, timeout=5)
    if result.returncode or len(result.stdout) > 16384:
        raise ValueError()
    owner, names = None, []
    for line in result.stdout.splitlines():
        if line.startswith("p"):
            owner = int(line[1:])
            if owner != pid:
                raise ValueError()
        elif line.startswith("n"):
            if owner != pid:
                raise ValueError()
            names.append(line[1:])
        elif re.fullmatch(r"f[0-9]+[A-Za-z]*", line):
            if owner != pid:
                raise ValueError()
        elif line:
            raise ValueError()
    if not names or f"127.0.0.1:{port}" not in names or any(
            name not in {f"127.0.0.1:{port}", f"[::1]:{port}"} for name in names):
        raise ValueError()


def configured(root):
    path = root / "discovery" / CONFIG_NAME
    return path.exists() or path.is_symlink()


def load_config(root):
    """Verify configuration belongs to the sole qualified owned process/profile."""
    root = Path(root)
    _private(root, directory=True)
    discovery = root / "discovery"
    _private(discovery, directory=True)
    config_path = discovery / CONFIG_NAME
    _private(config_path)
    try:
        if config_path.stat().st_size > 16384:
            raise ValueError()
        config = json.loads(config_path.read_text())
        custody_path = Path(config["custody_path"])
        _private(custody_path)
        if custody_path.stat().st_size > 16384:
            raise ValueError()
        custody = json.loads(custody_path.read_text())
        profile = Path(custody["profile"])
        _private(profile, directory=True)
        if (custody["phase"] != "qualified" or custody["endpoint"] != config["endpoint"]
                or type(custody["pid"]) is not int or custody["pid"] <= 0):
            raise ValueError()
        qualification = custody["qualification"]
        if (not all(qualification.get(k) is True for k in ("origin_ok", "root_path", "root_ready", "status_ok"))
                or any(qualification.get(k) is not False for k in ("challenge", "auth_required", "access_block", "rate_block"))):
            raise ValueError()
        os.kill(custody["pid"], 0)
        if not _owned_argv(_process_argv(custody["pid"]), profile):
            raise ValueError()
        active = profile / "DevToolsActivePort"
        if active.is_symlink() or not active.is_file() or active.stat().st_size > 1024:
            raise ValueError()
        port, token = active.read_text().splitlines()[:2]
        if config["endpoint"] != f"ws://127.0.0.1:{int(port)}{token}":
            raise ValueError()
        _loopback_listener(custody["pid"], int(port))
        return config
    except BrowserCaptureError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        raise BrowserCaptureError("browser_response_configuration_rejected", disable=True) from None


def _failure(code, cooldown_seconds=None):
    if code == "rate-limited":
        delay = cooldown_seconds if type(cooldown_seconds) in {int, float} else 60
        if not 0 <= delay < float("inf"):
            delay = 60
        return BrowserCaptureError("browser_rate_limited", cooldown_seconds=max(60, delay))
    if code in {"challenge", "pdf_html_challenge"}:
        return BrowserCaptureError("browser_access_challenge", disable=True)
    if code in {"pdf_redirect", "pdf_navigation_mismatch", "pdf_handoff_mismatch", "pdf_post_redirect", "invalid_pdf_url"}:
        return BrowserCaptureError("browser_navigation_rejected", disable=True)
    if code in {"private_path", "invalid_endpoint", "browser_context_unverified"}:
        return BrowserCaptureError("browser_response_configuration_rejected", disable=True)
    if code == "browser_target_unresolved":
        return BrowserCaptureError("browser_target_unresolved", disable=True)
    if code in {"pdf_too_large", "pdf_handoff_too_large"}:
        return BrowserCaptureError("document_size_limit", disable=True)
    return BrowserCaptureError("browser_response_capture_failed")


def capture(url, root):
    """Observe one ordinary browser navigation; return (original bytes, final URL)."""
    root = Path(root)
    config = load_config(root)
    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.origin:
        raise BrowserCaptureError("ordinary_browser_adapter_unavailable")
    driver = Path(spec.origin).parent / "driver"
    staging = root / "staging"
    _private(staging, directory=True)
    with tempfile.TemporaryDirectory(prefix="response-", dir=staging) as temporary:
        directory = Path(temporary)
        os.chmod(directory, 0o700)
        output, job = directory / "original.pdf", directory / "job.json"
        fd = os.open(job, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"endpoint": config["endpoint"], "url": url, "output_path": str(output),
                       "journal_path": str(root / "discovery/browser-response-target.json")}, stream)
        try:
            result = subprocess.run([str(driver / "node"), str(Path(__file__).with_suffix(".cjs")),
                                     str(job), str(driver / "package")], capture_output=True, timeout=85)
        except (OSError, subprocess.SubprocessError):
            raise BrowserCaptureError("browser_response_capture_failed") from None
        try:
            if len(result.stdout) > 4096:
                raise ValueError()
            status = json.loads(result.stdout)
            if not isinstance(status, dict):
                raise ValueError()
            if result.returncode or status.get("ok") is not True:
                raise _failure(status.get("code"), status.get("cooldown_seconds"))
            receipt_path = Path(str(output) + ".json")
            _private(output)
            _private(receipt_path)
            if output.stat().st_size > MAX_BYTES or receipt_path.stat().st_size > 16384:
                raise ValueError()
            receipt, data = json.loads(receipt_path.read_text()), output.read_bytes()
            final_url = receipt["final_url"]
            if (not data.startswith(b"%PDF-") or len(data) != receipt["size"]
                    or len(data) != status["bytes"] or not isinstance(final_url, str)
                    or hashlib.sha256(data).hexdigest() != receipt["sha256"]):
                raise ValueError()
            from scripts.dev.quality.download_current_kad import DownloadError, _validate_pdf_snapshot
            try:
                _validate_pdf_snapshot(data, directory)
            except DownloadError:
                raise BrowserCaptureError("incomplete_pdf") from None
            return data, final_url
        except BrowserCaptureError:
            raise
        except (ValueError, KeyError, TypeError, OSError):
            raise BrowserCaptureError("browser_response_readback_mismatch", disable=True) from None
