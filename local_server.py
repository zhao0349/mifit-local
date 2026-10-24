"""Loopback dashboard control, using the pinned bridge's local backend services.

The upstream MCP is stdio-only. This HTTP adapter shares its sync/status/cancel
workflow, not its transport or credential store. No generic file/tool endpoint,
CORS, request logging, cloud probe, or automatic synchronization is provided.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import socket
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import desktop_backend as backend
from database_manager import DatabaseManager
from dashboard_builder import render_html
from dashboard_data import DashboardDataError, build_summary, resolve_current_database
from official_login import OfficialLoginError, login

DEFAULT_PORT = 17865
BUSY = "已有任务正在运行，请等待完成。"


def installation_id():
    """Non-secret identity used only to reopen this installation's own service."""
    return hashlib.sha256(str(backend.ROOT.resolve()).encode()).hexdigest()


class LocalControl:
    """One bounded process-local job shared by all browser tabs.

The interface lock also covers desktop login/export actions. Cancellation is
idempotent and completion is reported only after async resource cleanup.
"""

    def __init__(self):
        self.lock = threading.Lock()
        self.job = None
        self.worker = None
        self.loop = self.task = None
        self.cancel_requested = False
        self.closed = False
        self.last_export = None
        self.export_account = None

    def status(self):
        account = None
        with self.lock:
            # A stopped backend exposes only its lifecycle state. The small
            # launcher stays available without reopening the credential vault.
            if not self.closed and backend.interface_lock.acquire(blocking=False):
                try:
                    account = backend.credentials_status()
                    account["scope"] = backend.account_scope()
                finally:
                    backend.interface_lock.release()
            return {"service": "mifit-local", "version": 1, "account": account,
                    "busy": backend.interface_lock.locked(), "closed": self.closed,
                    "job": copy.deepcopy(self.job)}

    def emit(self, message):
        with self.lock:
            self.job["messages"] = (self.job["messages"] + [message])[-60:]

    def start(self, kind, dates=None, *, mode="merge", expected_account=None):
        if kind not in {"sync", "sync_export", "export", "selftest", "login"}:
            raise ValueError("任务类型无效。")
        with self.lock:
            if self.closed or not backend.interface_lock.acquire(blocking=False):
                raise backend.DesktopError(BUSY)
            try:
                if ((mode == "replace" and not expected_account)
                        or (expected_account is not None and expected_account != backend.account_scope())):
                    raise backend.DesktopError("账号已变化，请刷新并重新确认覆盖操作。")
            except Exception:
                backend.interface_lock.release()
                raise
            self.cancel_requested = False
            self.job = {"id": secrets.token_hex(16), "kind": kind,
                        "status": "running", "messages": [], "result": None,
                        "cancellable": kind in {"sync", "sync_export", "login"}}
            initial = copy.deepcopy(self.job)
            self.worker = threading.Thread(
                target=self._work, args=(kind, dates, mode, expected_account), name="MiFit-web-job", daemon=False,
            )
            try:
                self.worker.start()
            except Exception:
                backend.interface_lock.release()
                self.job["status"] = "error"
                raise
            return initial

    async def _execute(self, kind, dates, mode, expected_account):
        with self.lock:
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            cancelled = self.cancel_requested
        if cancelled:
            raise asyncio.CancelledError
        if kind in {"sync", "sync_export"}:
            options = {"mode": mode, "expected_account": expected_account} if mode == "replace" else {}
            result = await backend.sync(*dates, self.emit, **options)
            if kind == "sync":
                return result
            # Crossing into file publication is atomic with respect to Cancel.
            # Once publication starts, shutdown waits for it to finish.
            with self.lock:
                if self.cancel_requested:
                    raise asyncio.CancelledError
                self.job["cancellable"] = False
            exported = backend.export(*dates, self.emit)
            self._remember_export(exported)
            return {**result, "export": exported}
        if kind == "export":
            result = backend.export(*dates, self.emit, mode=mode)
            self._remember_export(result)
            return {**result, "status": "ok"}
        if kind == "selftest":
            result = backend.smoke_demo()
            self.emit("离线自检通过：已用虚构数据验证 SQLite、CSV 和 JSON；未连接小米。")
            return {**result, "status": "ok"}
        user_id, token = await login(self.emit)
        try:
            backend.save_credentials(user_id, token, region="cn")
        finally:
            user_id = token = ""
        return {"status": "ok"}

    def _remember_export(self, result):
        self.last_export = result.get("directory")
        self.export_account = backend.account_scope()

    def idle_action(self, action):
        """Serialize short credential/file actions with jobs and database edits."""
        with self.lock:
            if self.closed or not backend.interface_lock.acquire(blocking=False):
                raise backend.DesktopError(BUSY)
        try:
            return action()
        finally:
            backend.interface_lock.release()

    def open_exports(self):
        def action():
            scope = backend.account_scope()
            if not scope:
                raise backend.DesktopError("尚未配置账号，请先登录。")
            base = backend._account_directory(backend.EXPORTS_DIR, scope)
            current = backend._managed_child(base, "current")
            target = current if current.is_dir() else base
            if self.export_account == scope and self.last_export:
                candidate = backend._inside(Path(self.last_export))
                if candidate.is_relative_to(base) and candidate.is_dir():
                    target = candidate
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
            return {"status": "ok"}
        return self.idle_action(action)

    def _work(self, kind, dates, mode, expected_account):
        result = None
        try:
            result = asyncio.run(self._execute(kind, dates, mode, expected_account))
            status = result["status"]
        except asyncio.CancelledError:
            status = "cancelled"
            self.emit("已取消，已写入的缓存保留在本机。")
        except (backend.DesktopError, OfficialLoginError) as exc:
            status = "error"
            self.emit(str(exc))
        except Exception:
            status = "error"
            self.emit("操作未完成，请检查本机配置后重试。")
        finally:
            with self.lock:
                self.loop = self.task = None
                self.job.update(status=status, result=result)
                backend.interface_lock.release()

    def cancel(self, job_id):
        with self.lock:
            if not self.job or self.job["id"] != job_id:
                raise ValueError("找不到该任务，请刷新连接状态。")
            if (self.job["status"] == "running" and self.job["cancellable"]
                    and not self.cancel_requested):
                self.cancel_requested = True
                self.job["messages"].append("正在停止，请等待资源清理完成。")
                if self.loop and self.task:
                    with suppress(RuntimeError):
                        self.loop.call_soon_threadsafe(self.task.cancel)

    def close(self):
        with self.lock:
            self.closed = True
            job_id = self.job["id"] if self.job else None
        if job_id:
            self.cancel(job_id)
        if self.worker:
            self.worker.join()

    def summary(self, dates, *, scope="all", expected_account=None):
        with self.lock:
            if self.closed or not backend.interface_lock.acquire(blocking=False):
                raise backend.DesktopError(BUSY)
        try:
            if expected_account is not None and expected_account != backend.account_scope():
                raise backend.DesktopError("账号已变化，请刷新账号状态后重新读取；当前档案未改动。")
            options = {"start": dates[0], "end": dates[1]} if scope == "range" else {}
            return build_summary(resolve_current_database(), **options)
        finally:
            backend.interface_lock.release()


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # Broken browser connections must not print data-bearing tracebacks.
        pass


class LocalServer:
    def __init__(self, port=DEFAULT_PORT):
        self.control = LocalControl()
        self.databases = DatabaseManager()
        self.closed = threading.Event()
        self.backend_stopped = threading.Event()
        self._close_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopping = False
        self._terminating = False
        self._shutdown_thread = None
        self.token = secrets.token_urlsafe(32)
        self.httpd = _HTTPServer(("127.0.0.1", port), _Handler)
        self.httpd.owner = self
        self.origin = f"http://127.0.0.1:{self.httpd.server_port}"
        self.url = self.origin + "/"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="MiFit-local-http", daemon=True,
        )
        # Pin the page assets to this process's Python code. Reading new assets
        # on each request can mix an old running backend with an updated UI.
        empty = {"schema_version": 1, "days": [], "workouts": [],
                 "meta": {"timezone": "Asia/Shanghai", "notes": []}}
        try:
            self.page = render_html(empty, local_token=self.token)
        except Exception:
            self.httpd.server_close()
            raise

    def start(self):
        self.thread.start()
        return self

    def close(self):
        """Terminate the launcher itself, including any backend still draining."""
        with self._close_lock:
            if self.closed.is_set():
                return
            try:
                with self._lifecycle_lock:
                    self._terminating = True
                    shutdown_thread = self._shutdown_thread
                    with self.control.lock:
                        self.control.closed = True
                if shutdown_thread:
                    shutdown_thread.join()
                else:
                    self.control.close()
                with backend.interface_lock:
                    pass  # Drain an already admitted cache/file operation.
                self.httpd.shutdown()
                self.httpd.server_close()
                self.thread.join()
            finally:
                self.closed.set()

    def request_shutdown(self):
        """Stop data operations while retaining the local page's start endpoint."""
        with self._lifecycle_lock:
            if self._terminating:
                raise backend.DesktopError("本机启动器正在退出，请重新运行启动程序。")
            if self.control.closed:
                return
            with self.control.lock:
                self.control.closed = True
            self._stopping = True
            self.backend_stopped.clear()
            self._shutdown_thread = threading.Thread(
                target=self._stop_backend, args=(self.control,), name="MiFit-stop-backend", daemon=False,
            )
            self._shutdown_thread.start()

    def _stop_backend(self, control):
        try:
            control.close()
            with backend.interface_lock:
                pass
        finally:
            with self._lifecycle_lock:
                self._stopping = False
                self.backend_stopped.set()

    def activate(self):
        """Explicitly enable local operations; never log in or sync on its own."""
        with self._lifecycle_lock:
            if self._terminating or self.closed.is_set():
                raise backend.DesktopError("本机启动器正在退出，请重新运行启动程序。")
            if self._stopping:
                raise backend.DesktopError("后端正在收尾，请稍后再开启。")
            if self.control.closed:
                self.control = LocalControl()
                self.databases = DatabaseManager()
                self._shutdown_thread = None
                self.backend_stopped.clear()
            return {"status": "ok"}

    def status(self):
        state = self.control.status()
        state["stopping"] = self._stopping
        return state

    def database_action(self, method, *args, **kwargs):
        # Database methods own the operation locks; keep admission and shutdown
        # ordered without acquiring those locks for a second time here.
        with self._lifecycle_lock:
            if self.control.closed or self._terminating:
                raise backend.DesktopError("后端暂未运行，请先开启后端。")
            return getattr(self.databases, method)(*args, **kwargs)


class _Handler(BaseHTTPRequestHandler):
    server_version = "MiFitLocal"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_):
        pass

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        # On Windows, closing with an unread POST body can reset the connection
        # before the browser receives our error response. Drain only small,
        # fixed-length bodies, including requests rejected before authentication.
        if self.command == "POST" and not getattr(self, "_body_read", False):
            self._body_read = True
            with suppress(ValueError, OSError):
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 12288 and not self.headers.get("Transfer-Encoding"):
                    self.rfile.read(length)
        body = (json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if content_type.startswith("application/json") else payload.encode("utf-8"))
        self.send_response(status)
        for key, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "frame-ancestors 'none'",
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, *, api=False):
        owner = self.server.owner
        # Reject DNS rebinding, cross-site form/image/fetch and opaque origins.
        if (self.headers.get_all("Host") != [owner.origin.removeprefix("http://")]
                or self.headers.get("Sec-Fetch-Site") == "cross-site" and api
                or self.headers.get("Origin") not in (None, owner.origin)):
            self._send(403, {"error": "仅允许本机仪表盘访问。"})
            return False
        if api and not secrets.compare_digest(
            self.headers.get("X-MiFit-Token", ""), owner.token,
        ):
            self._send(403, {"error": "连接已失效，请重新打开本机仪表盘。"})
            return False
        return True

    def do_GET(self):
        if not self._allowed(api=self.path not in {"/", "/health", "/help"}):
            return
        if self.path == "/":
            # Never embed private data in the initial navigation response.
            self._send(200, self.server.owner.page,
                       "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, self.server.owner.status())
        elif self.path == "/health":
            self._send(200, {"service": "mifit-local", "version": 2,
                             "installation": installation_id()})
        elif self.path == "/help":
            self._send(200, (backend.ROOT / "使用说明.html").read_text(encoding="utf-8"),
                       "text/html; charset=utf-8")
        elif self.path == "/api/accounts":
            try:
                self._send(200, self.server.owner.database_action("inventory"))
            except backend.DesktopError as exc:
                self._send(409, {"error": str(exc)})
            except Exception:
                self._send(500, {"error": "无法读取本机数据库列表。"})
        else:
            self._send(404, {"error": "接口不存在。"})

    def do_POST(self):
        if not self._allowed(api=True):
            return
        try:
            if (self.headers.get("Content-Type") != "application/json"
                    or self.headers.get("Transfer-Encoding")
                    or len(self.headers.get_all("Content-Length", [])) != 1):
                raise ValueError("请求格式不正确。")
            length = int(self.headers.get("Content-Length", "0"))
            if not 2 <= length <= (12288 if self.path == "/api/credentials" else 2048):
                raise ValueError("请求大小不正确。")
            self._body_read = True
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("请求格式不正确。")
            if self.path == "/api/start" and not payload:
                self._send(200, self.server.owner.activate())
                return
            if self.path == "/api/shutdown" and not payload:
                self.server.owner.request_shutdown()
                self._send(200, {"status": "closed"})
                return
            control = self.server.owner.control
            if control.closed:
                raise backend.DesktopError("后端暂未运行，请先开启后端。")
            if self.path in ("/api/cache", "/api/sync", "/api/export"):
                allowed = ({"start", "end", "scope", "account_scope", "account_key"} if self.path == "/api/cache"
                           else {"start", "end", "mode", "account_scope", "export"}
                           if self.path == "/api/sync" else {"start", "end", "mode", "account_scope"})
                if not {"start", "end"} <= set(payload) <= allowed:
                    raise ValueError("请选择起止日期。")
                dates = backend.validate_dates(payload["start"], payload["end"])
                if self.path == "/api/cache":
                    scope = payload.get("scope", "all")
                    if scope not in ("all", "range"):
                        raise ValueError("读取范围无效。")
                    if "account_key" in payload:
                        options = {"start": dates[0], "end": dates[1]} if scope == "range" else {}
                        self._send(200, self.server.owner.database_action(
                            "summary", payload["account_key"], **options))
                    else:
                        self._send(200, control.summary(dates, scope=scope,
                                                       expected_account=payload.get("account_scope")))
                elif self.path == "/api/sync":
                    mode = payload.get("mode", "merge")
                    if mode not in ("merge", "replace") or not isinstance(payload.get("export", False), bool):
                        raise ValueError("同步模式无效。")
                    kind = "sync_export" if payload.get("export") else "sync"
                    self._send(202, control.start(kind, dates, mode=mode,
                                                 expected_account=payload.get("account_scope")))
                else:
                    mode = payload.get("mode", "cumulative")
                    if mode not in ("cumulative", "snapshot"):
                        raise ValueError("导出模式无效。")
                    self._send(202, control.start("export", dates, mode=mode,
                                                 expected_account=payload.get("account_scope")))
            elif self.path == "/api/login" and not payload:
                self._send(202, control.start("login"))
            elif self.path == "/api/credentials" and set(payload) == {"user_id", "pass_token"}:
                try:
                    self._send(200, control.idle_action(lambda: backend.save_credentials(
                        payload["user_id"], payload["pass_token"], region="cn")))
                finally:
                    payload.clear()
            elif self.path == "/api/selftest" and not payload:
                self._send(202, control.start("selftest"))
            elif self.path == "/api/open-exports" and not payload:
                self._send(200, control.open_exports())
            elif self.path == "/api/deletion-preview":
                if not {"account_key"} <= set(payload) <= {"account_key", "start", "end", "categories"}:
                    raise ValueError("请明确选择一个账号。")
                self._send(200, self.server.owner.database_action("preview", **payload))
            elif self.path == "/api/delete" and set(payload) == {"token"}:
                self._send(200, self.server.owner.database_action("delete", payload["token"]))
            elif self.path == "/api/repair":
                if set(payload) != {"account_key"}:
                    raise ValueError("请明确选择一个需要修复的账号。")
                self._send(200, self.server.owner.database_action("repair", payload["account_key"]))
            elif self.path in ("/api/open-database", "/api/damaged-deletion-preview"):
                if set(payload) != {"account_key"}:
                    raise ValueError("请明确选择一个账号。")
                method = "open_location" if self.path == "/api/open-database" else "preview_damaged"
                self._send(200, self.server.owner.database_action(method, payload["account_key"]))
            elif self.path == "/api/cancel" and set(payload) == {"id"}:
                control.cancel(payload["id"])
                self._send(200, {"status": "cancelling"})
            else:
                self._send(404, {"error": "接口不存在或参数不正确。"})
        except (ValueError, UnicodeError):
            self._send(400, {"error": "请求格式或日期不正确。"})
        except DashboardDataError as exc:
            self._send(409, {"error": str(exc) + " 可尝试缩小读取日期范围后重试；当前档案保留。"})
        except backend.DesktopError as exc:
            details = {"diagnostics": exc.diagnostics} if hasattr(exc, "diagnostics") else {}
            self._send(409, {"error": str(exc), **details})
        except Exception:
            self._send(500, {"error": "本机操作未完成，请检查配置或缩小日期范围后重试。"})

    def do_OPTIONS(self):
        self._send(403, {"error": "不允许跨站访问。"})
