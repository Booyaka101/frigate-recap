"""Shared pytest plumbing: tool paths on PATH, a serving contextmanager."""
from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = PROJECT_ROOT / ".tools"

# Before any module-level skipif evaluates, so a dev box with only .tools/ffmpeg
# still runs the end-to-end tests.
if TOOLS_DIR.is_dir():
    os.environ["PATH"] = f"{TOOLS_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


@pytest.fixture(scope="session", autouse=True)
def _tools_on_path():
    """Dev boxes without system ffmpeg can drop binaries into .tools/."""
    if TOOLS_DIR.is_dir():
        os.environ["PATH"] = f"{TOOLS_DIR}{os.pathsep}{os.environ.get('PATH', '')}"


def ffmpeg_available() -> bool:
    import shutil

    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg/ffprobe not available"
)


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Run cli.main in-process, capturing stdout and stderr."""
    import contextlib
    import io

    from frigate_recap import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv)
    return code, out.getvalue(), err.getvalue()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def serve(app, trust_env: bool = False):
    """Run a FastAPI app on an ephemeral localhost port in a daemon thread."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/api/health", timeout=1, trust_env=trust_env).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("mock Frigate did not become ready")
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
