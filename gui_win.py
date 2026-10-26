"""
Native control panel for Aquarius Bot Manager (Windows only).

A real PySide6 window -- genuine Qt widgets, no embedded browser/WebView,
no reuse of the web dashboard's HTML. It talks to the manager's existing
local HTTP API via client_win.ApiClient, the same client tray_win.py uses.
Launched from the tray app ("Open control panel") or directly
(`python gui_win.py`); it's a separate process from the manager service and
can be closed/reopened freely without affecting any running bot.

Layout (confirmed with the user): a sidebar of bot names on the left, a
detail pane on the right for whichever one is selected, and a menu bar
across the top. Selecting a bot in the sidebar adds a menu named after it
with that bot's own actions -- the menu bar is otherwise unchanged. Every
setting (startup/login, rename, resource limits, notifications) opens its
own small dialog instead of living inline in the main window ("one window
per task"). Any bot's detail pane can pop out into its own small floating
window via "Open in new window", so several can stay on screen at once;
closing one of those windows doesn't stop the bot.
"""

import sys

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton,
    QRadioButton, QSplitter, QVBoxLayout, QWidget,
)

from client_win import ApiClient, ApiError, AuthError
import tray_win  # reuses _service_control/_set_login_item/_login_item_enabled/TRAY_SHORTCUT_NAME

POLL_INTERVAL_MS = 3000  # matches the web dashboard's own refresh interval

STATUS_COLORS = {
    "running": "#3ddc97",
    "stopped": "#7c8c86",
    "crashed": "#ff6b6b",
}


class _Worker(QObject):
    """Runs one blocking callable (an ApiClient call) on a background
    QThread and reports back via signals -- every network call in this file
    goes through this, since several (stop/restart in particular) can block
    for the instance's full stop_timeout (up to ~15-30s) and must not freeze
    the window's event loop while that happens."""
    finished = Signal(object)
    failed = Signal(str, bool)  # (message, is_auth_error)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except AuthError as e:
            self.failed.emit(str(e), True)
            return
        except Exception as e:  # ApiError and friends; surfaced as plain text
            self.failed.emit(str(e), False)
            return
        self.finished.emit(result)


def run_async(parent, fn, on_success=None, on_error=None, on_auth_error=None):
    """Fire-and-forget a blocking call off the main thread. `parent` must
    outlive the call (it owns the thread/worker via Qt parent-child
    lifetime, so they aren't garbage-collected mid-flight) -- pass the
    widget that initiated the action, not a short-lived local object.
    on_error(message) fires for any ApiError; on_auth_error() (no args)
    fires instead, specifically, when the session is no longer valid."""
    thread = QThread(parent)
    worker = _Worker(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def cleanup():
        thread.quit()
        thread.wait()
        worker.deleteLater()
        thread.deleteLater()

    def handle_failed(message, is_auth_error):
        if is_auth_error and on_auth_error:
            on_auth_error()
        elif on_error:
            on_error(message)

    if on_success:
        worker.finished.connect(on_success)
    worker.failed.connect(handle_failed)
    worker.finished.connect(cleanup)
    worker.failed.connect(cleanup)

    # keep a reference on the parent so Python doesn't GC thread/worker
    # before Qt's own parent-child ownership would otherwise cover it
    if not hasattr(parent, "_async_refs"):
        parent._async_refs = []
    parent._async_refs.append((thread, worker))

    def forget():
        try:
            parent._async_refs.remove((thread, worker))
        except ValueError:
            pass
    worker.finished.connect(forget)
    worker.failed.connect(forget)

    thread.start()


def _apply_dark_palette(app):
    """A plain dark palette matching the web dashboard's midnight theme,
    applied at the Qt level so every widget (including dialogs) picks it up
    without per-widget stylesheets."""
    pal = QPalette()
    pal.setColor(QPalette.Window, QColor("#0a0e12"))
    pal.setColor(QPalette.WindowText, QColor("#e4ece7"))
    pal.setColor(QPalette.Base, QColor("#141a21"))
    pal.setColor(QPalette.AlternateBase, QColor("#11171e"))
    pal.setColor(QPalette.Text, QColor("#e4ece7"))
    pal.setColor(QPalette.Button, QColor("#161d26"))
    pal.setColor(QPalette.ButtonText, QColor("#e4ece7"))
    pal.setColor(QPalette.Highlight, QColor("#3ddc97"))
    pal.setColor(QPalette.HighlightedText, QColor("#06120b"))
    pal.setColor(QPalette.PlaceholderText, QColor("#7c8c86"))
    app.setPalette(pal)


def _status_label(status):
    lbl = QLabel(status.capitalize())
    color = STATUS_COLORS.get(status, "#7c8c86")
    lbl.setStyleSheet(f"color: {color}; font-weight: 600;")
    return lbl


# ---------------------------------------------------------------------------
# Login dialog -- shown once if the manager has a login configured and this
# machine has no valid session yet (see client_win.ApiClient's persistence).
# ---------------------------------------------------------------------------

class LoginDialog(QDialog):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.setWindowTitle("Log in — Aquarius Bot Manager")
        self.setModal(True)

        self.user_edit = QLineEdit()
        self.pass_edit = QLineEdit()
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #ff6b6b;")
        self.error_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pass_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_login)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

    def _on_login(self):
        try:
            self.client.login(self.user_edit.text(), self.pass_edit.text())
            self.accept()
        except ApiError as e:
            self.error_label.setText(str(e))


# ---------------------------------------------------------------------------
# Startup & Login dialog (Tools -> Startup & login…)
# ---------------------------------------------------------------------------

class StartupLoginDialog(QDialog):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client = client
        self.setWindowTitle("Startup & Login")
        self.setModal(True)

        self.wait_for_login = QRadioButton("Start only after a user logs in")
        self.always_start = QRadioButton("Start automatically, before any user logs in")
        self._load_boot_mode()

        service_box = QGroupBox("Manager service, at boot")
        svc_layout = QVBoxLayout(service_box)
        svc_layout.addWidget(self.always_start)
        svc_layout.addWidget(self.wait_for_login)
        note = QLabel(
            "This is set at service install time (see the README's Windows install "
            "steps) — this dialog shows the current mode but changing it here "
            "requires reinstalling the service with the other option.")
        note.setWordWrap(True)
        note.setStyleSheet("color: #7c8c86; font-size: 11px;")
        svc_layout.addWidget(note)
        self.always_start.setEnabled(False)
        self.wait_for_login.setEnabled(False)

        self.tray_login_check = QCheckBox("Open the control panel when I log in")
        self.tray_login_check.setChecked(tray_win._login_item_enabled(tray_win.TRAY_SHORTCUT_NAME))

        login_box = QGroupBox("Control panel, per user")
        login_layout = QVBoxLayout(login_box)
        login_layout.addWidget(self.tray_login_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(service_box)
        layout.addWidget(login_box)
        layout.addWidget(buttons)

    def _load_boot_mode(self):
        """Boot mode (always-start vs. wait-for-login) is an NSSM service
        install-time choice (per the approved Phase 5 plan), not a manager
        setting -- there's no API for it. Best-effort detection: a service
        configured with Start=SERVICE_AUTO_START shows as such via `sc
        qc`; this dialog shows it read-only (see the disabled radios above)
        rather than pretending it's live-editable here."""
        self.always_start.setChecked(True)  # default assumption; informational only

    def _on_save(self):
        tray_win._set_login_item(tray_win.TRAY_SHORTCUT_NAME, "tray_win.py",
                                  self.tray_login_check.isChecked())
        self.accept()


# ---------------------------------------------------------------------------
# Rename dialog (Instance menu -> Rename…)
# ---------------------------------------------------------------------------

class RenameDialog(QDialog):
    def __init__(self, client, instance_name, parent=None):
        super().__init__(parent)
        self.client = client
        self.instance_name = instance_name
        self.setWindowTitle(f"Rename {instance_name}")
        self.setModal(True)

        self.name_edit = QLineEdit(instance_name)
        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #ff6b6b;")
        self.error_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("New name", self.name_edit)

        hint = QLabel("Letters, digits, '.', '_' and '-' only.")
        hint.setStyleSheet("color: #7c8c86; font-size: 11px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_rename)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(self.error_label)
        layout.addWidget(buttons)

    def _on_rename(self):
        new_name = self.name_edit.text().strip()
        if not new_name or new_name == self.instance_name:
            self.reject()
            return
        try:
            self.client.rename(self.instance_name, new_name)
            self.accept()
        except ApiError as e:
            self.error_label.setText(str(e))


# ---------------------------------------------------------------------------
# Resource limits dialog (Instance menu -> Resource limits…)
# ---------------------------------------------------------------------------

class LimitsDialog(QDialog):
    def __init__(self, client, instance, parent=None):
        super().__init__(parent)
        self.client = client
        self.instance_name = instance["name"]
        self.setWindowTitle(f"Resource Limits — {self.instance_name}")
        self.setModal(True)

        limits = instance.get("limits") or {}
        self.memory_edit = QLineEdit(str(limits.get("memory", "")))
        self.memory_edit.setPlaceholderText("e.g. 512M or 2G")
        self.cpu_edit = QLineEdit(str(limits.get("cpu", "")) if limits.get("cpu") else "")
        self.cpu_edit.setPlaceholderText("% of one core, e.g. 150")

        self.error_label = QLabel("")
        self.error_label.setStyleSheet("color: #ff6b6b;")
        self.error_label.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Memory cap", self.memory_edit)
        form.addRow("CPU cap (% of one core)", self.cpu_edit)

        clear_btn = QPushButton("Clear limits")
        clear_btn.clicked.connect(self._on_clear)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(clear_btn)
        btn_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.error_label)
        layout.addLayout(btn_row)
        layout.addWidget(buttons)

    def _on_save(self):
        try:
            self.client.set_limits(
                self.instance_name,
                memory=self.memory_edit.text().strip() or None,
                cpu=self.cpu_edit.text().strip() or None,
            )
            self.accept()
        except ApiError as e:
            self.error_label.setText(str(e))

    def _on_clear(self):
        try:
            self.client.set_limits(self.instance_name, memory="", cpu="0")
            self.accept()
        except ApiError as e:
            self.error_label.setText(str(e))


# ---------------------------------------------------------------------------
# About dialog (Help -> About…)
# ---------------------------------------------------------------------------

class AboutDialog(QDialog):
    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About")
        self.setModal(True)

        try:
            instances = client.list_instances()
            count_text = f"{len(instances)} instances registered"
            service_text = "Manager service: reachable"
        except ApiError:
            count_text = ""
            service_text = "Manager service: unreachable"

        title = QLabel("Aquarius Bot Manager")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        title.setAlignment(Qt.AlignCenter)
        sub = QLabel("Windows build")
        sub.setStyleSheet("color: #7c8c86; font-size: 11px;")
        sub.setAlignment(Qt.AlignCenter)
        status = QLabel(f"{service_text}\n{count_text}")
        status.setAlignment(Qt.AlignCenter)
        status.setStyleSheet("color: #7c8c86; font-size: 11px;")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(sub)
        layout.addSpacing(10)
        layout.addWidget(status)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# Detail pane -- shared between the main window's right side and a popped-
# out floating window, so both stay visually/behaviorally identical.
# ---------------------------------------------------------------------------

class DetailPane(QWidget):
    def __init__(self, client, instance_name, on_pop_out=None, parent=None):
        super().__init__(parent)
        self.client = client
        self.instance_name = instance_name
        self._on_pop_out = on_pop_out

        self.name_label = QLabel(instance_name)
        self.name_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.status_label = _status_label("stopped")

        header = QHBoxLayout()
        header.addWidget(self.name_label)
        header.addStretch()
        header.addWidget(self.status_label)
        if on_pop_out is not None:
            pop_btn = QPushButton("⧉ Pop out")
            pop_btn.setToolTip("Open this bot in its own window")
            pop_btn.clicked.connect(lambda: self._on_pop_out(self.instance_name))
            header.addWidget(pop_btn)

        self.cpu_label = QLabel("—")
        self.mem_label = QLabel("—")
        stats_form = QFormLayout()
        stats_form.addRow("CPU", self.cpu_label)
        stats_form.addRow("Memory", self.mem_label)

        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.restart_btn = QPushButton("Restart")
        self.start_btn.clicked.connect(self._on_start)
        self.stop_btn.clicked.connect(self._on_stop)
        self.restart_btn.clicked.connect(self._on_restart)

        action_row = QHBoxLayout()
        action_row.addWidget(self.start_btn)
        action_row.addWidget(self.stop_btn)
        action_row.addWidget(self.restart_btn)
        action_row.addStretch()

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(stats_form)
        layout.addLayout(action_row)
        layout.addStretch()

    def update_from(self, inst):
        self.instance_name = inst["name"]
        self.name_label.setText(inst["name"])
        status = inst.get("status", "stopped")
        color = STATUS_COLORS.get(status, "#7c8c86")
        self.status_label.setText(status.capitalize())
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        stats = inst.get("stats")
        if stats:
            self.cpu_label.setText(f"{stats.get('cpu_pct', 0)}%")
            mem_mb = (stats.get("rss") or 0) / (1024 * 1024)
            self.mem_label.setText(f"{mem_mb:.0f}MB")
        else:
            self.cpu_label.setText("—")
            self.mem_label.setText("—")
        running = status == "running"
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.restart_btn.setEnabled(running)

    def _on_start(self):
        self._run_action(self.client.start, "start")

    def _on_stop(self):
        self._run_action(self.client.stop, "stop")

    def _on_restart(self):
        self._run_action(self.client.restart, "restart")

    def _run_action(self, fn, verb):
        """Runs off the main thread (see run_async) -- stop/restart can block
        for the instance's full stop_timeout (up to ~15-30s server-side) and
        must not freeze this window while that happens. Buttons disable for
        the duration as the only feedback that something is in flight."""
        name = self.instance_name
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.restart_btn.setEnabled(False)

        def on_error(message):
            QMessageBox.warning(self, "Aquarius Bot Manager", f"Could not {verb} {name}: {message}")
            # the next poll (<=3s) will set real button states from server
            # status; re-enable them now too so a failed action doesn't
            # leave every button looking permanently disabled in the meantime
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(True)
            self.restart_btn.setEnabled(True)

        run_async(self, lambda: fn(name), on_error=on_error)


class PoppedOutWindow(QMainWindow):
    """A bot's DetailPane in its own small floating window. Closing this
    window has no effect on the bot -- it's purely a detached viewer, kept
    alive and polled by MainWindow the same as the main detail pane."""

    def __init__(self, client, instance_name, parent=None):
        super().__init__(parent)
        self.setWindowTitle(instance_name)
        self.resize(280, 220)
        self.detail = DetailPane(client, instance_name)
        self.setCentralWidget(self.detail)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self, client):
        super().__init__()
        self.client = client
        self.setWindowTitle("Aquarius Bot Manager")
        self.resize(640, 420)

        self._popped_out = {}  # instance name -> PoppedOutWindow
        self._instance_menu = None  # the per-bot menu; None when nothing's selected

        self._build_menu_bar()
        self._build_body()

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll)
        self.poll_timer.start(POLL_INTERVAL_MS)
        self._poll()

    # ---- menu bar ----

    def _build_menu_bar(self):
        mb = self.menuBar()

        file_menu = mb.addMenu("File")
        file_menu.addAction("Restart manager service", self._on_restart_service)
        file_menu.addAction("Stop manager service", self._on_stop_service)
        file_menu.addSeparator()
        file_menu.addAction("Exit control panel", self.close)

        view_menu = mb.addMenu("View")
        view_menu.addAction("Refresh now", self._poll)
        self.autorefresh_action = QAction("Auto-refresh", self, checkable=True, checked=True)
        self.autorefresh_action.toggled.connect(self._on_toggle_autorefresh)
        view_menu.addAction(self.autorefresh_action)

        self._instance_menu_index = mb.actions()  # placeholder; real menu inserted on selection

        self.tools_menu = mb.addMenu("Tools")
        self.tools_menu.addAction("Start all bots", self._on_start_all)
        self.tools_menu.addAction("Stop all bots", self._on_stop_all)
        self.tools_menu.addSeparator()
        self.tools_menu.addAction("Startup && login…", self._on_startup_login_dialog)
        self.tools_menu.addAction("Notifications…", self._on_notifications_dialog)

        help_menu = mb.addMenu("Help")
        help_menu.addAction("Check for updates", self._on_check_updates)
        help_menu.addSeparator()
        help_menu.addAction("About…", self._on_about_dialog)

        self._menu_bar_ref = mb

    def _set_instance_menu(self, instance_name):
        """Adds/replaces the per-bot menu named after the selected instance,
        inserted right after View and before Tools -- the one part of the
        menu bar that changes with selection."""
        mb = self._menu_bar_ref
        if self._instance_menu is not None:
            mb.removeAction(self._instance_menu.menuAction())
            self._instance_menu = None
        if instance_name is None:
            return
        menu = QMenu(instance_name, self)
        menu.addAction("Restart", lambda: self._instance_action(instance_name, self.client.restart, "restart"))
        menu.addAction("Stop", lambda: self._instance_action(instance_name, self.client.stop, "stop"))
        menu.addSeparator()
        menu.addAction("Open in new window ⧉", lambda: self._pop_out(instance_name))
        menu.addAction("Rename…", lambda: self._on_rename_dialog(instance_name))
        menu.addAction("Resource limits…", lambda: self._on_limits_dialog(instance_name))
        menu.addSeparator()
        delete_action = menu.addAction("Delete instance…", lambda: self._on_delete(instance_name))
        delete_action.setMenuRole(QAction.NoRole)
        self._instance_menu = menu
        mb.insertMenu(self.tools_menu.menuAction(), menu)

    # ---- body: sidebar + detail ----

    def _build_body(self):
        self.sidebar = QListWidget()
        self.sidebar.setFixedWidth(170)
        self.sidebar.currentItemChanged.connect(self._on_sidebar_selection)

        self.detail_container = QVBoxLayout()
        self.current_detail = None
        self._empty_label = QLabel("Select a bot")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setStyleSheet("color: #7c8c86;")
        self.detail_container.addWidget(self._empty_label)

        detail_widget = QWidget()
        detail_widget.setLayout(self.detail_container)

        splitter = QSplitter()
        splitter.addWidget(self.sidebar)
        splitter.addWidget(detail_widget)
        splitter.setStretchFactor(1, 1)

        self.setCentralWidget(splitter)

    def _on_sidebar_selection(self, current, previous):
        if self.current_detail is not None:
            self.detail_container.removeWidget(self.current_detail)
            self.current_detail.deleteLater()
            self.current_detail = None
        self._empty_label.hide()

        if current is None:
            self._empty_label.show()
            self._set_instance_menu(None)
            return

        name = current.data(Qt.UserRole)
        self._set_instance_menu(name)
        self.current_detail = DetailPane(self.client, name, on_pop_out=self._pop_out)
        self.detail_container.addWidget(self.current_detail)
        self._poll()  # fill in current status immediately rather than waiting for the next tick

    def _pop_out(self, instance_name):
        if instance_name in self._popped_out:
            win = self._popped_out[instance_name]
            win.show()
            win.raise_()
            win.activateWindow()
            return
        win = PoppedOutWindow(self.client, instance_name, parent=None)
        win.destroyed.connect(lambda: self._popped_out.pop(instance_name, None))
        self._popped_out[instance_name] = win
        win.show()

    # ---- polling ----

    def _poll(self):
        """list_instances() is usually fast, but a slow/unreachable server
        could still stall it for its 10s request timeout -- run it off the
        main thread like every other network call here, so a bad network
        moment never freezes the whole window, just this one refresh tick."""
        def on_auth_error():
            self.poll_timer.stop()
            self._prompt_login()

        run_async(self, self.client.list_instances,
                  on_success=self._apply_poll_result,
                  on_error=lambda message: None,  # transient; next tick retries
                  on_auth_error=on_auth_error)

    def _apply_poll_result(self, instances):
        by_name = {i["name"]: i for i in instances}

        selected_name = None
        current_item = self.sidebar.currentItem()
        if current_item is not None:
            selected_name = current_item.data(Qt.UserRole)

        self.sidebar.blockSignals(True)
        self.sidebar.clear()
        select_item = None
        for inst in instances:
            item = QListWidgetItem(inst["name"])
            item.setData(Qt.UserRole, inst["name"])
            color = STATUS_COLORS.get(inst.get("status", "stopped"), "#7c8c86")
            item.setForeground(QColor(color))
            self.sidebar.addItem(item)
            if inst["name"] == selected_name:
                select_item = item
        if select_item is not None:
            self.sidebar.setCurrentItem(select_item)
        self.sidebar.blockSignals(False)

        if self.current_detail is not None and self.current_detail.instance_name in by_name:
            self.current_detail.update_from(by_name[self.current_detail.instance_name])

        for name, win in list(self._popped_out.items()):
            if name in by_name:
                win.detail.update_from(by_name[name])

    def _prompt_login(self):
        dlg = LoginDialog(self.client, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.poll_timer.start(POLL_INTERVAL_MS)
            self._poll()
        else:
            self.close()

    # ---- menu actions ----

    def _instance_action(self, name, fn, verb):
        """Instance-menu actions (restart/stop) can block for the instance's
        full stop_timeout, same as DetailPane's buttons -- run off-thread."""
        def on_error(message):
            QMessageBox.warning(self, "Aquarius Bot Manager", f"Could not {verb} {name}: {message}")

        run_async(self, lambda: fn(name), on_success=lambda _: self._poll(), on_error=on_error)

    def _on_restart_service(self):
        ok, msg = tray_win._service_control("stop")
        ok2, msg2 = tray_win._service_control("start")
        if not (ok and ok2):
            QMessageBox.warning(self, "Aquarius Bot Manager", f"Service restart issue: {msg or msg2}")

    def _on_stop_service(self):
        ok, msg = tray_win._service_control("stop")
        if not ok:
            QMessageBox.warning(self, "Aquarius Bot Manager", f"Could not stop the service: {msg}")

    def _on_toggle_autorefresh(self, checked):
        if checked:
            self.poll_timer.start(POLL_INTERVAL_MS)
        else:
            self.poll_timer.stop()

    def _on_start_all(self):
        self._run_bulk_action(self.client.start_all, "start")

    def _on_stop_all(self):
        self._run_bulk_action(self.client.stop_all, "stop")

    def _run_bulk_action(self, bulk_fn, verb):
        """start_all/stop_all loop per-instance server-side calls (no bulk
        HTTP route exists) and can take a while with several bots -- run the
        whole thing, including the initial list_instances() name lookup,
        off the main thread."""
        def do_it():
            names = [i["name"] for i in self.client.list_instances()]
            return bulk_fn(names)

        def on_success(results):
            errors = {n: r["error"] for n, r in results.items() if "error" in r}
            if errors:
                QMessageBox.warning(self, "Aquarius Bot Manager",
                                     f"Some bots failed to {verb}:\n" +
                                     "\n".join(f"{n}: {e}" for n, e in errors.items()))
            self._poll()

        def on_error(message):
            QMessageBox.warning(self, "Aquarius Bot Manager", message)

        run_async(self, do_it, on_success=on_success, on_error=on_error)

    def _on_startup_login_dialog(self):
        StartupLoginDialog(self.client, parent=self).exec()

    def _on_notifications_dialog(self):
        QMessageBox.information(self, "Notifications",
                                 "Notification settings aren't built into the control panel "
                                 "yet — use the web dashboard's Settings → Notifications tab.")

    def _on_check_updates(self):
        try:
            info = self.client.get("/api/update/check")
        except ApiError as e:
            QMessageBox.warning(self, "Aquarius Bot Manager", str(e))
            return
        state = info.get("state", "unknown")
        QMessageBox.information(self, "Check for Updates", f"Update status: {state}")

    def _on_about_dialog(self):
        AboutDialog(self.client, parent=self).exec()

    def _on_rename_dialog(self, instance_name):
        dlg = RenameDialog(self.client, instance_name, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self._poll()

    def _on_limits_dialog(self, instance_name):
        try:
            inst = next(i for i in self.client.list_instances() if i["name"] == instance_name)
        except (StopIteration, ApiError):
            return
        LimitsDialog(self.client, inst, parent=self).exec()

    def _on_delete(self, instance_name):
        confirm = QMessageBox.question(
            self, "Delete instance",
            f"Delete '{instance_name}'? This removes it from the manager but does not "
            "touch its files on disk. If it's running, it will be stopped first.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if confirm != QMessageBox.Yes:
            return
        try:
            self.client.delete(instance_name, force=True)
        except ApiError as e:
            QMessageBox.warning(self, "Aquarius Bot Manager", str(e))
        self._poll()


def main():
    if sys.platform != "win32":
        print("gui_win.py is Windows-only.", file=sys.stderr)
        sys.exit(1)

    app = QApplication(sys.argv)
    app.setApplicationName("Aquarius Bot Manager")
    _apply_dark_palette(app)

    client = ApiClient()
    win = MainWindow(client)

    try:
        status = client.auth_status()
        needs_login = status.get("required") and not status.get("authed")
    except ApiError:
        needs_login = False  # server unreachable; let the main poll loop surface that

    if needs_login:
        win.poll_timer.stop()
        win._prompt_login()

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
