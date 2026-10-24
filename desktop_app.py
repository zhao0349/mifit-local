"""Local Windows desktop entry point. Never logs credentials or health records."""

from __future__ import annotations

import asyncio
import ctypes
import os
import queue
import threading
import tkinter as tk
import webbrowser
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk

import desktop_backend as backend
from official_login import OfficialLoginError, login

ROOT = Path(__file__).resolve().parent


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.task: asyncio.Task | None = None
        self.cancel_requested = threading.Event()
        self.closing = False
        self.busy = False
        self.cancellable = False
        self.buttons: list[ttk.Button] = []
        self.last_export: Path | None = None
        self.active_account_scope: str | None = None
        self.local_server = None
        root.title("小米运动健康 · 本地数据导出")
        root.geometry("920x800")
        root.minsize(840, 740)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        style = ttk.Style(root)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure(".", font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("Sub.TLabel", foreground="#52616b")
        style.configure("Action.TButton", padding=(12, 8))
        main = ttk.Frame(root, padding=24)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text="把运动健康数据留在自己电脑", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            main, text="小米运动健康 Mi Fitness  ·  中国大陆  ·  CSV / JSON / SQLite",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(6, 18))

        account = ttk.LabelFrame(main, text="1  连接自己的小米账号", padding=14)
        account.pack(fill="x")
        row = ttk.Frame(account)
        row.pack(fill="x")
        self.account_status = tk.StringVar(value="尚未登录")
        ttk.Label(row, textvariable=self.account_status).pack(side="left")
        self.button(row, "小米官网登录", self.official_login).pack(side="right")
        self.button(row, "登录帮助", self.open_help).pack(side="right", padx=8)
        ttk.Label(
            account,
            text="在独立 Edge 窗口完成官网验证或扫码。凭证保存在 Windows 凭据管理器。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(10, 0))
        manual = ttk.Frame(account)
        manual.pack(fill="x", pady=(12, 0))
        self.user_id = tk.StringVar()
        self.pass_token = tk.StringVar()
        ttk.Label(manual, text="手动配置（备用）  userId").pack(side="left")
        self.user_input = ttk.Entry(manual, textvariable=self.user_id, width=15)
        self.user_input.pack(side="left", padx=(6, 12))
        ttk.Label(manual, text="passToken").pack(side="left")
        self.token_input = ttk.Entry(manual, textvariable=self.pass_token, show="●", width=23)
        self.token_input.pack(side="left", padx=6)
        self.button(manual, "保存", self.manual_save).pack(side="right")

        export = ttk.LabelFrame(main, text="2  选择日期并导出", padding=14)
        export.pack(fill="x", pady=16)
        dates = ttk.Frame(export)
        dates.pack(fill="x")
        self.start = tk.StringVar()
        self.end = tk.StringVar()
        self.set_days(30)
        ttk.Label(dates, text="开始").pack(side="left")
        self.start_input = ttk.Entry(dates, textvariable=self.start, width=13)
        self.start_input.pack(side="left", padx=(8, 16))
        ttk.Label(dates, text="结束").pack(side="left")
        self.end_input = ttk.Entry(dates, textvariable=self.end, width=13)
        self.end_input.pack(side="left", padx=(8, 16))
        self.button(dates, "近 30 天", lambda: self.set_days(30)).pack(side="left", padx=4)
        self.button(dates, "近一年", lambda: self.set_days(365)).pack(side="left", padx=4)
        ttk.Label(
            export, text="日期用于云端同步或另存快照。默认更新当前账号的全部累计档案。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(9, 12))
        self.replace_history = tk.BooleanVar(value=False)
        replace_toggle = ttk.Checkbutton(
            export, text="覆盖当前账号历史（仅保留本次日期；需再次确认）",
            variable=self.replace_history,
        )
        replace_toggle.pack(anchor="w", pady=(0, 10))
        self.buttons.append(replace_toggle)
        actions = ttk.Frame(export)
        actions.pack(fill="x")
        self.button(actions, "同步并导出", self.sync_export).pack(side="left")
        self.button(actions, "仅导出已有缓存", self.export_cache).pack(side="left", padx=8)
        self.button(actions, "打开导出文件夹", self.open_exports).pack(side="left")
        self.button(actions, "打开本机仪表盘", self.open_dashboard).pack(side="left", padx=8)
        self.stop_button = ttk.Button(actions, text="停止", command=self.cancel, state="disabled")
        self.stop_button.pack(side="right")
        ttk.Label(
            export,
            text="累计 JSON/CSV 固定保存在当前账号的 current 文件夹；重复记录更新，不叠加。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(12, 0))
        self.button(export, "按日期另存独立快照", lambda: self.export_cache(snapshot=True)).pack(anchor="w", pady=(8, 0))

        status_row = ttk.Frame(main)
        status_row.pack(fill="x")
        self.status = tk.StringVar(value="就绪。首次使用请点击“小米官网登录”。")
        ttk.Label(status_row, textvariable=self.status).pack(side="left")
        self.button(status_row, "离线自检", self.demo).pack(side="right")
        self.progress = ttk.Progressbar(main, mode="indeterminate")
        self.progress.pack(fill="x", pady=(10, 8))
        self.output = tk.Text(
            main, height=10, wrap="word", font=("Microsoft YaHei UI", 10),
            relief="flat", bg="#f4f7fa", padx=12, pady=10, state="disabled",
        )
        self.output.pack(fill="both", expand=True)
        self.append(f"数据保存在本机 {backend.ROOT}。同步前请先在手机 App 中完成设备同步。")
        self.append("首次建议同步近 30 天。历史数据较多时可分段选择日期。")
        ttk.Label(
            main,
            text="基于 Mi Fitness Data Bridge 的非官方实验性工具。可用数据取决于小米接口、账号和设备。",
            style="Sub.TLabel", wraplength=820,
        ).pack(anchor="w", pady=(12, 0))
        root.update_idletasks()
        min_width = max(920, root.winfo_reqwidth() + 16)
        min_height = root.winfo_reqheight() - self.output.winfo_reqheight() + 100
        available_height = root.winfo_screenheight()
        if os.name == "nt":
            from ctypes import wintypes
            work_area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(work_area), 0):
                available_height = work_area.bottom - work_area.top
        root.minsize(min_width, min_height)
        root.geometry(
            f"{max(940, min_width)}x"
            f"{max(min_height, min(available_height - 60, max(900, min_height + 60)))}"
        )
        self.refresh_account()
        root.after(100, self.poll)

    def button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command, style="Action.TButton")
        self.buttons.append(button)
        return button

    def append(self, message: str):
        self.output.configure(state="normal")
        self.output.insert("end", message + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def refresh_account(self):
        if not backend.interface_lock.acquire(blocking=False):
            return
        try:
            state = backend.credentials_status()
            scope = backend.account_scope()
        finally:
            backend.interface_lock.release()
        if scope != self.active_account_scope:
            self.last_export = None
            self.active_account_scope = scope
        if not state["keyring_available"]:
            self.account_status.set("Windows 凭据管理器暂不可用")
        elif state["configured"]:
            self.account_status.set("已配置账号 " + (state["account_masked"] or ""))
        else:
            self.account_status.set("尚未登录")

    def set_days(self, days):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        self.end.set(today.isoformat())
        self.start.set((today - timedelta(days=days - 1)).isoformat())

    def dates(self):
        try:
            return backend.validate_dates(self.start.get().strip(), self.end.get().strip())
        except ValueError as exc:
            messagebox.showerror("请检查日期", str(exc), parent=self.root)
            return None

    def emit(self, message):
        self.events.put(("message", message))

    def run(self, label, operation, *, cancellable=True):
        if self.busy:
            return
        if not backend.interface_lock.acquire(blocking=False):
            self.status.set("网页中已有任务正在运行，请完成或取消后再操作。")
            return
        self.busy = True
        self.cancellable = cancellable
        self.cancel_requested.clear()
        self.status.set(label)
        self.append(label)
        for button in self.buttons:
            button.configure(state="disabled")
        for entry in (self.user_input, self.token_input, self.start_input, self.end_input):
            entry.configure(state="disabled")
        self.stop_button.configure(state="normal" if cancellable else "disabled")
        self.progress.start(12)

        async def execute():
            self.loop = asyncio.get_running_loop()
            self.task = asyncio.current_task()
            if self.cancel_requested.is_set():
                raise asyncio.CancelledError
            return await operation()

        def work():
            result = None
            try:
                result = asyncio.run(execute())
            except asyncio.CancelledError:
                self.emit("已停止。已同步到本机的数据会保留，可以选择相同日期再次同步。")
                outcome = "已停止"
            except (backend.DesktopError, OfficialLoginError, ValueError) as exc:
                self.emit(str(exc))
                outcome = "操作未完成，请查看上面的提示"
            except Exception:
                self.emit("操作遇到未预期错误。请重启程序后重试；未输出原始响应或凭证。")
                outcome = "操作未完成"
            else:
                outcome = result.get("message", "操作完成") if result else "操作完成"
            finally:
                self.loop = None
                self.task = None
                backend.interface_lock.release()
            self.events.put(("done", (outcome, result)))

        self.worker = threading.Thread(target=work, name="MiFit-worker", daemon=False)
        try:
            self.worker.start()
        except Exception:
            backend.interface_lock.release()
            self.events.put(("done", ("后台任务未能启动，请重试。", None)))

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.append(value)
                elif kind == "exporting":
                    self.cancellable = False
                    self.stop_button.configure(state="disabled")
                    self.status.set("正在写入导出文件…")
                elif kind == "done":
                    outcome, result = value
                    self.busy = False
                    self.cancellable = False
                    self.progress.stop()
                    for button in self.buttons:
                        button.configure(state="normal")
                    for entry in (
                        self.user_input, self.token_input, self.start_input, self.end_input,
                    ):
                        entry.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.status.set(outcome)
                    self.append(outcome)
                    if result and result.get("directory"):
                        self.last_export = Path(result["directory"])
                    self.refresh_account()
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def official_login(self):
        async def action():
            user_id, token = await login(self.emit)
            try:
                backend.save_credentials(user_id, token, region="cn")
            finally:
                user_id = token = ""
            return {"message": "官网登录凭证已保存。现在可以选择日期并同步导出。"}
        self.run("等待你在小米官网完成登录…", action)

    def manual_save(self):
        user_id = self.user_id.get().strip()
        token = self.pass_token.get().strip()
        self.pass_token.set("")
        self.user_id.set("")

        async def action():
            backend.save_credentials(user_id, token, region="cn")
            return {"message": "凭证已保存。请点击“同步并导出”验证云端连接。"}
        self.run("正在保存到 Windows 凭据管理器…", action, cancellable=False)

    def sync_export(self):
        dates = self.dates()
        if not dates:
            return
        replace = self.replace_history.get()
        expected_account = None
        if replace:
            if not backend.interface_lock.acquire(blocking=False):
                self.status.set("已有网页任务正在运行，请完成后再确认覆盖。")
                return
            try:
                expected_account = backend.account_scope()
                account = backend.credentials_status()
            except backend.DesktopError:
                expected_account = None
            finally:
                backend.interface_lock.release()
            if not expected_account:
                messagebox.showerror("无法覆盖", "请先配置当前账号。", parent=self.root)
                return
            if not messagebox.askyesno(
                "确认覆盖当前账号历史",
                f"账号 {account.get('account_masked', '')}：仅保留 {dates[0]} 至 {dates[1]} 的新记录。\n"
                "全部同步成功后才替换；失败或取消保留旧历史。其他账号和已另存快照不受影响。",
                parent=self.root,
            ):
                return
            self.replace_history.set(False)

        async def action():
            options = {"mode": "replace", "expected_account": expected_account} if replace else {}
            synced = await backend.sync(*dates, self.emit, **options)
            if self.cancel_requested.is_set():
                raise asyncio.CancelledError
            self.events.put(("exporting", None))
            exported = backend.export(*dates, self.emit)
            if synced["all_ok"] and not exported["empty"]:
                exported["message"] = "同步及导出完成，点击“打开导出文件夹”查看。"
            elif replace and not synced.get("committed"):
                exported["message"] = "覆盖未完成，原有历史保持不变；已更新原缓存的累计导出。"
            elif synced["all_ok"]:
                exported["message"] = "请求已完成，但所选日期没有记录。请核对账号、日期和手机同步情况。"
            else:
                exported["message"] = "已导出本地缓存；本次同步有失败或不完整数据，请查看提示后重试。"
            return exported
        self.run("正在连接小米并同步数据…", action)

    def export_cache(self, *, snapshot=False):
        dates = self.dates()
        if not dates:
            return

        async def action():
            result = backend.export(*dates, self.emit, **({"mode": "snapshot"} if snapshot else {}))
            result["message"] = (
                "缓存导出完成，点击“打开导出文件夹”查看。"
                if not result["empty"] else "没有可导出的缓存记录，请先同步或修改快照日期。"
            )
            return result
        self.run("正在导出本机缓存…", action, cancellable=False)

    def demo(self):
        async def action():
            result = backend.smoke_demo()
            self.emit("已用虚构数据验证 SQLite、CSV 和 JSON 导出。演示文件与个人数据分开保存。")
            result["message"] = "离线自检通过；真实账号同步仍需登录后验证。"
            return result
        self.run("正在运行离线自检（不连接小米）…", action, cancellable=False)

    def cancel(self):
        if not self.busy or not self.cancellable or self.cancel_requested.is_set():
            return
        self.cancel_requested.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止，请稍候…")
        loop, task = self.loop, self.task
        if loop and task:
            with suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)

    def callback_error(self, *_):
        messagebox.showerror(
            "操作未完成",
            "操作遇到错误，请检查目录权限后重试，或重新打开程序。",
            parent=self.root,
        )

    def close(self):
        if self.busy:
            self.closing = True
            self.cancel()
            self.status.set("正在收尾，完成后自动关闭…")
        else:
            self.root.destroy()

    def open_help(self):
        webbrowser.open((ROOT / "使用说明.html").as_uri())

    def start_local_server(self):
        from local_server import LocalServer
        if self.local_server is None:
            self.local_server = LocalServer().start()

    def open_dashboard(self):
        try:
            self.start_local_server()
            webbrowser.open(self.local_server.url)
        except OSError:
            self.append("本机仪表盘未能启动，端口 17865 可能被占用；手动导入与桌面导出仍可使用。")

    def open_exports(self):
        scope = backend.account_scope()
        if scope != self.active_account_scope:
            self.last_export = None
            self.refresh_account()
        base = backend._account_directory(backend.EXPORTS_DIR, scope) if scope else backend.EXPORTS_DIR
        current = base / "current"
        path = self.last_export or (current if current.is_dir() else base)
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))


def main():
    # One desktop process per deployment prevents concurrent token rotation/sync.
    mutex = None
    if os.name == "nt":
        import hashlib
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        name = "Local\\MiFitDesktop-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
        mutex = kernel.CreateMutexW(None, False, name)
        duplicate = ctypes.get_last_error() == 183
        if duplicate:
            ctypes.windll.user32.MessageBoxW(None, "导出工具已在运行，请切换到现有窗口。", "小米数据导出", 0)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(mutex)
            return
        with suppress(AttributeError, OSError):
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
    try:
        root = tk.Tk()
        app = App(root)
        try:
            try:
                app.start_local_server()
            except OSError:
                app.append("本机网页服务未能启动，请检查端口 17865；桌面导出仍可使用。")
            root.mainloop()
        finally:
            if app.local_server:
                app.local_server.close()
    finally:
        if mutex:
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
