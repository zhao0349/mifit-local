"""Headless Windows launcher; all user controls live in the local web page."""

from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
import webbrowser
from pathlib import Path

from local_server import DEFAULT_PORT, LocalServer, installation_id

ROOT = Path(__file__).resolve().parent


def reopen_running():
    """Open only a matching service; never follow redirects to an unknown host."""
    connection = http.client.HTTPConnection("127.0.0.1", DEFAULT_PORT, timeout=2)
    try:
        connection.request("GET", "/health")
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(1025)
        if len(body) > 1024:
            return False
        identity = json.loads(body)
        if identity != {"service": "mifit-local", "version": 2,
                        "installation": installation_id()}:
            return False
        return bool(webbrowser.open(f"http://127.0.0.1:{DEFAULT_PORT}/"))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def run_service(*, server_factory=LocalServer, open_browser=None):
    """Keep the backend alive until the page explicitly shuts it down."""
    server = server_factory().start()
    try:
        if not (open_browser or webbrowser.open)(server.url):
            raise RuntimeError("Browser did not open")
        server.closed.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.close()


def main():
    # Retain the old mutex name to exclude a still-running legacy console.
    mutex = None
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\MiFitDesktop-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
        mutex = kernel.CreateMutexW(None, False, name)
        if not mutex:
            raise OSError("Cannot acquire installation mutex")
        if ctypes.get_last_error() == 183:
            try:
                if not reopen_running():
                    ctypes.windll.user32.MessageBoxW(
                        None, "后端已在运行，但网页尚未就绪。若旧版控制台仍开着，请先关闭旧版后重新启动。",
                        "MiFit 本地后端", 0x30,
                    )
            finally:
                kernel.CloseHandle(mutex)
            return
    try:
        run_service()
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()