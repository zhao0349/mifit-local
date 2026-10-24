"""Synthetic Tk lifecycle checks; no credentials, health data, or network access."""

import asyncio
import gc
import threading
import time
import tkinter as tk
from contextlib import suppress
from pathlib import Path
from unittest.mock import Mock

import pytest

import desktop_app


@pytest.fixture
def app(monkeypatch, tmp_path):
    """Disable all real backend entry points before constructing the hidden UI."""
    # Finalize prior destroyed Tk applications on the Tk thread. Python 3.14
    # can otherwise collect their callback cycles in a newly started worker.
    gc.collect()
    backend = desktop_app.backend
    monkeypatch.setattr(backend, "ROOT", tmp_path)
    monkeypatch.setattr(backend, "account_scope", lambda: "synthetic-account")
    monkeypatch.setattr(backend, "EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(
        backend,
        "credentials_status",
        lambda: {
            "configured": False,
            "keyring_available": True,
            "account_masked": "",
        },
    )
    for name in ("save_credentials", "sync", "export", "smoke_demo"):
        monkeypatch.setattr(
            backend, name, Mock(side_effect=AssertionError(f"Unexpected backend {name}"))
        )
    monkeypatch.setattr(
        desktop_app, "login", Mock(side_effect=AssertionError("Unexpected browser login"))
    )
    monkeypatch.setattr(desktop_app.messagebox, "showerror", Mock())
    # Match the real launcher so geometry checks cover the current Windows DPI.
    if desktop_app.os.name == "nt":
        with suppress(AttributeError, OSError):
            desktop_app.ctypes.windll.shcore.SetProcessDpiAwareness(1)
    root = tk.Tk()
    root.withdraw()
    callbacks = []
    application = desktop_app.App(root)
    root.report_callback_exception = lambda *args: callbacks.append(args)
    application.callback_errors = callbacks
    original_destroy = root.destroy
    try:
        yield application
    finally:
        if application.busy and application.cancellable:
            application.cancel()
        if application.worker is not None:
            application.worker.join(timeout=3)
            assert not application.worker.is_alive(), "GUI worker did not shut down"
        try:
            for after_id in root.tk.call("after", "info"):
                root.after_cancel(after_id)
            original_destroy()
        except tk.TclError:
            pass
        variable_names = [name for name, value in vars(application).items()
                          if isinstance(value, tk.Variable)]
        for name in variable_names:
            setattr(application, name, None)
        gc.collect()
        assert not callbacks, "Tk callback raised an exception"


def pump_until(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("Timed out waiting for synthetic GUI operation")
        app.root.update()
        time.sleep(0.005)
    app.root.update_idletasks()


def assert_restored(app):
    assert not app.busy
    assert not app.cancellable
    assert app.stop_button.instate(["disabled"])
    assert all(button.instate(["!disabled"]) for button in app.buttons)
    assert all(
        entry.instate(["!disabled"])
        for entry in (app.user_input, app.token_input, app.start_input, app.end_input)
    )
    assert app.loop is None
    assert app.task is None


def output(app):
    return app.output.get("1.0", "end") + app.status.get()


def test_success_disables_controls_and_restores_after_worker(app, tmp_path):
    started = threading.Event()
    release = threading.Event()
    directory = tmp_path / "synthetic-export"

    async def operation():
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.005)
        app.emit("synthetic progress")
        return {"message": "synthetic complete", "directory": str(directory)}

    try:
        app.run("synthetic running", operation)
        pump_until(app, started.is_set)
        assert app.busy
        assert all(button.instate(["disabled"]) for button in app.buttons)
        assert app.token_input.instate(["disabled"])
        assert app.stop_button.instate(["!disabled"])
    finally:
        release.set()
    pump_until(app, lambda: not app.busy)
    assert_restored(app)
    assert app.last_export == directory
    assert "synthetic progress" in output(app)
    assert app.status.get() == "synthetic complete"


@pytest.mark.parametrize(
    "error, visible",
    [
        (RuntimeError("SYNTHETIC_SECRET_RUNTIME"), False),
        (desktop_app.backend.DesktopError("固定且安全的测试错误"), True),
        (desktop_app.OfficialLoginError("固定且安全的测试错误"), True),
    ],
)
def test_error_restores_controls_and_sanitizes_unexpected_exception(app, error, visible):
    async def operation():
        raise error

    app.run("synthetic operation", operation)
    pump_until(app, lambda: not app.busy)
    assert_restored(app)
    assert (str(error) in output(app)) is visible


def test_cancel_waits_for_async_cleanup_and_blocks_duplicate_operation(app):
    started = threading.Event()
    cleaned = threading.Event()
    duplicate = Mock()

    async def operation():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.03)
            cleaned.set()

    app.run("synthetic cancellable", operation)
    pump_until(app, started.is_set)
    app.run("duplicate", duplicate)
    app.cancel()
    assert app.busy
    assert app.stop_button.instate(["disabled"])
    pump_until(app, lambda: not app.busy)
    assert cleaned.is_set()
    duplicate.assert_not_called()
    assert_restored(app)


def test_cancel_requested_before_event_loop_is_started(app, monkeypatch):
    """Cover the race between clicking Stop and the worker acquiring its loop."""
    original_thread = threading.Thread
    targets = []
    operation = Mock()

    class DeferredThread:
        def __init__(self, target, **kwargs):
            targets.append(target)

        def start(self):
            pass

    monkeypatch.setattr(desktop_app.threading, "Thread", DeferredThread)
    app.run("synthetic immediate cancel", operation)
    app.cancel()
    monkeypatch.setattr(desktop_app.threading, "Thread", original_thread)
    app.worker = original_thread(target=targets[0], daemon=False)
    app.worker.start()
    pump_until(app, lambda: not app.busy)
    operation.assert_not_called()
    assert_restored(app)


def test_close_waits_for_cancellable_worker_cleanup(app, monkeypatch):
    started = threading.Event()
    cleaned = threading.Event()
    destroy = Mock()
    monkeypatch.setattr(app.root, "destroy", destroy)

    async def operation():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.03)
            cleaned.set()

    app.run("synthetic login", operation)
    pump_until(app, started.is_set)
    app.close()
    assert app.closing
    destroy.assert_not_called()
    pump_until(app, lambda: destroy.called)
    assert cleaned.is_set()
    assert_restored(app)
    destroy.assert_called_once()


def test_close_after_stop_does_not_interrupt_async_cleanup(app, monkeypatch):
    started = threading.Event()
    cleaning = threading.Event()
    cleaned = threading.Event()
    destroy = Mock()
    monkeypatch.setattr(app.root, "destroy", destroy)

    async def operation():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaning.set()
            await asyncio.sleep(0.2)
            cleaned.set()

    app.run("synthetic login", operation)
    pump_until(app, started.is_set)
    app.cancel()
    pump_until(app, cleaning.is_set)
    app.close()
    app.close()
    pump_until(app, lambda: destroy.called)
    assert cleaned.is_set(), "Repeated cancellation interrupted resource cleanup"
    assert_restored(app)
    destroy.assert_called_once()


def test_close_waits_for_non_cancellable_file_write(app, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    destroy = Mock()
    monkeypatch.setattr(app.root, "destroy", destroy)

    async def operation():
        started.set()
        release.wait(timeout=3)
        return {"message": "synthetic file written"}

    try:
        app.run("synthetic export", operation, cancellable=False)
        pump_until(app, started.is_set)
        app.close()
        assert app.closing
        assert not app.cancel_requested.is_set()
        destroy.assert_not_called()
    finally:
        release.set()
    pump_until(app, lambda: destroy.called)
    assert_restored(app)
    destroy.assert_called_once()


def test_manual_credentials_are_cleared_and_never_shown(app, monkeypatch):
    saved = Mock()
    monkeypatch.setattr(desktop_app.backend, "save_credentials", saved)
    app.user_id.set("123456789")
    app.pass_token.set("SYNTHETIC_SECRET_MANUAL")
    app.manual_save()
    assert app.user_id.get() == ""
    assert app.pass_token.get() == ""
    assert app.stop_button.instate(["disabled"])
    pump_until(app, lambda: not app.busy)
    saved.assert_called_once_with("123456789", "SYNTHETIC_SECRET_MANUAL", region="cn")
    assert "123456789" not in output(app)
    assert "SYNTHETIC_SECRET_MANUAL" not in output(app)
    assert_restored(app)


def test_official_login_saves_locally_without_exposing_pair(app, monkeypatch):
    saved = Mock()

    async def login(emit):
        emit("synthetic official login")
        return "123456789", "SYNTHETIC_SECRET_OFFICIAL"

    monkeypatch.setattr(desktop_app, "login", login)
    monkeypatch.setattr(desktop_app.backend, "save_credentials", saved)
    app.official_login()
    pump_until(app, lambda: not app.busy)
    saved.assert_called_once_with("123456789", "SYNTHETIC_SECRET_OFFICIAL", region="cn")
    assert "123456789" not in output(app)
    assert "SYNTHETIC_SECRET_OFFICIAL" not in output(app)
    assert_restored(app)


def test_cancelled_sync_does_not_start_export(app, monkeypatch):
    started = threading.Event()
    exported = Mock()

    async def sync(start, end, emit):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(desktop_app.backend, "sync", sync)
    monkeypatch.setattr(desktop_app.backend, "export", exported)
    app.sync_export()
    pump_until(app, started.is_set)
    app.cancel()
    pump_until(app, lambda: not app.busy)
    exported.assert_not_called()
    assert_restored(app)


def test_sync_export_marks_file_write_non_cancellable(app, monkeypatch, tmp_path):
    writing = threading.Event()
    release = threading.Event()

    async def sync(start, end, emit):
        return {"all_ok": True}

    def export(start, end, emit):
        writing.set()
        release.wait(timeout=3)
        return {"empty": False, "directory": str(tmp_path / "synthetic-export")}

    monkeypatch.setattr(desktop_app.backend, "sync", sync)
    monkeypatch.setattr(desktop_app.backend, "export", export)
    try:
        app.sync_export()
        pump_until(app, lambda: writing.is_set() and not app.cancellable)
        assert app.busy
        assert app.stop_button.instate(["disabled"])
    finally:
        release.set()
    pump_until(app, lambda: not app.busy)
    assert app.last_export == Path(tmp_path / "synthetic-export")
    assert_restored(app)


def test_invalid_dates_do_not_start_worker_or_backend(app):
    app.start.set("invalid")
    app.sync_export()
    assert not app.busy
    assert app.worker is None
    desktop_app.messagebox.showerror.assert_called_once()
    desktop_app.backend.sync.assert_not_called()
    desktop_app.backend.export.assert_not_called()


def test_callback_error_displays_fixed_message_without_raw_exception(app):
    app.callback_error(
        RuntimeError, RuntimeError("SYNTHETIC_SECRET_CALLBACK"), None
    )
    desktop_app.messagebox.showerror.assert_called_once()
    assert "SYNTHETIC_SECRET_CALLBACK" not in str(
        desktop_app.messagebox.showerror.call_args
    )


def test_cache_export_sets_last_directory_and_restores(app, monkeypatch, tmp_path):
    monkeypatch.setattr(
        desktop_app.backend,
        "export",
        lambda *args: {"empty": False, "directory": str(tmp_path / "synthetic-cache")},
    )
    app.export_cache()
    pump_until(app, lambda: not app.busy)
    assert app.last_export == tmp_path / "synthetic-cache"
    assert_restored(app)


def test_initial_width_fits_minimum_and_output_can_shrink(app):
    """Check requested geometry without mapping a window onto the real desktop."""
    app.root.update_idletasks()
    minimum_width, minimum_height = app.root.minsize()
    assert app.root.winfo_reqwidth() <= minimum_width
    # The output Text expands/shrinks, so keep at least two lines at minimum size.
    fixed_height = app.root.winfo_reqheight() - app.output.winfo_reqheight()
    assert fixed_height + 40 <= minimum_height


def test_web_operation_blocks_desktop_action_without_starting_worker(app):
    with desktop_app.backend.interface_lock:
        operation = Mock()
        app.run("blocked", operation)
        assert not app.busy
        assert app.worker is None
        operation.assert_not_called()


def test_dashboard_opens_only_its_owned_loopback_service(app, monkeypatch):
    import local_server
    service = Mock(url="http://127.0.0.1:17865/")
    service.start.return_value = service
    factory = Mock(return_value=service)
    monkeypatch.setattr(local_server, "LocalServer", factory)
    opened = Mock()
    monkeypatch.setattr(desktop_app.webbrowser, "open", opened)
    app.open_dashboard()
    app.open_dashboard()
    factory.assert_called_once()
    assert opened.call_count == 2
    opened.assert_called_with(service.url)


def test_replacement_requires_confirmation_and_resets_to_append(app, monkeypatch):
    confirm = Mock(return_value=False)
    monkeypatch.setattr(desktop_app.messagebox, "askyesno", confirm)
    app.replace_history.set(True)
    app.sync_export()
    assert not app.busy
    desktop_app.backend.sync.assert_not_called()
    confirm.return_value = True
    calls = []
    async def sync(*args, **kwargs):
        calls.append(kwargs)
        raise desktop_app.backend.DesktopError("synthetic stop")
    monkeypatch.setattr(desktop_app.backend, "sync", sync)
    app.sync_export()
    assert not app.replace_history.get()
    pump_until(app, lambda: not app.busy)
    assert calls == [{"mode": "replace", "expected_account": "synthetic-account"}]


def test_open_exports_follows_account_switch_not_previous_result(app, monkeypatch, tmp_path):
    old = tmp_path / "exports" / "synthetic-account" / "current"
    old.mkdir(parents=True)
    app.last_export = old
    monkeypatch.setattr(desktop_app.backend, "account_scope", lambda: "different-account")
    opened = Mock()
    monkeypatch.setattr(desktop_app.os, "startfile", opened)
    app.open_exports()
    opened.assert_called_once_with(str(tmp_path / "exports" / "different-account"))
    assert old.is_dir()
    assert app.last_export is None
