import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    for key in list(__import__("os").environ):
        if any(part in key.upper() for part in ("API_KEY", "COOKIE", "CREATOR_REPORT", "DEEPSEEK", "GEMINI")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CREATOR_REPORT_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("BILIBILI_COOKIE_FILE", str(tmp_path / "absent-cookie.txt"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    original_connect = socket.socket.connect

    def no_external_connect(sock, address):
        # Asyncio may use a local socket pair on Windows.
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1"):
            raise AssertionError("External networking is forbidden in offline tests")
        return original_connect(sock, address)

    def no_process(*args, **kwargs):
        raise AssertionError("Native subprocesses are forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", no_external_connect)
    monkeypatch.setattr(subprocess, "Popen", no_process)
