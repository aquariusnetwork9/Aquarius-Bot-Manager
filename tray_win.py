"""
System tray icon for Aquarius Bot Manager (Windows only).

A small, separate process from the manager service itself -- it does not
supervise bots and does not need to be running for the service to work. It
exists purely so a logged-in user has:
  - an always-visible icon showing whether the manager service is up,
  - a quick way to open the native control panel (gui_win.py),
  - start/stop controls for the Windows SERVICE (not individual bots --
    that's what the control panel's per-instance "Instance" menu is for),
  - two login-item toggles (this tray app, and whether bots auto-start) that
    just flip Windows Startup-folder/registry entries, nothing server-side.

Deliberately NOT a live status dashboard: by design (confirmed with the
user), the tray menu stays plain text with no per-bot list, no counts, no
hover tooltip beyond the OS's own "Aquarius Bot Manager" name. All of that
detail lives in the control panel window instead.

Talks to the manager's existing local HTTP API via client_win.ApiClient --
same client gui_win.py uses -- never the service's own process directly.
Starting/stopping the WINDOWS SERVICE (as opposed to a bot instance) goes
through `sc.exe`/`nssm`, since that's an OS-level operation the HTTP API has
no endpoint for.
"""

import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image, ImageDraw

from client_win import ApiClient, ApiError

SERVICE_NAME = "AquariusBotManager"
POLL_INTERVAL_SEC = 5

STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")),
    "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
TRAY_SHORTCUT_NAME = "Aquarius Bot Manager Tray.lnk"


def _icon_image(color):
    """A small solid circle -- green/red/grey -- stdlib-only (Pillow, already
    a pystray dependency) rather than shipping an .ico asset."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = 6
    draw.ellipse([pad, pad, size - pad, size - pad], fill=color)
    return img


ICON_OK = _icon_image((61, 220, 151, 255))       # service reachable
ICON_DOWN = _icon_image((124, 140, 134, 255))     # service unreachable/stopped


def _service_running():
    """Best-effort check via `sc query` -- avoids needing pywin32 for this
    one query. Returns True/False/None (None = sc.exe itself failed, e.g.
    the service was never installed)."""
    try:
        r = subprocess.run(["sc", "query", SERVICE_NAME], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return "RUNNING" in r.stdout


def _service_control(action):
    """action: 'start' or 'stop'. Returns (ok, message)."""
    try:
        r = subprocess.run(["sc", action, SERVICE_NAME], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, f"service {action} requested"
    return False, (r.stdout + r.stderr).strip() or f"sc {action} failed ({r.returncode})"


def _open_control_panel():
    gui_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_win.py")
    subprocess.Popen([sys.executable, gui_path], close_fds=True)


def _login_item_enabled(name):
    return os.path.exists(os.path.join(STARTUP_DIR, name))


def _set_login_item(name, target_args, enabled):
    """Minimal .lnk writer via the Windows Script Host COM object (available
    on every Windows install, no extra dependency) -- avoids pywin32 just for
    a shortcut file."""
    path = os.path.join(STARTUP_DIR, name)
    if not enabled:
        try:
            os.remove(path)
        except OSError:
            pass
        return
    os.makedirs(STARTUP_DIR, exist_ok=True)
    vbs = f'''
Set oWS = WScript.CreateObject("WScript.Shell")
Set oLink = oWS.CreateShortcut("{path}")
oLink.TargetPath = "{sys.executable}"
oLink.Arguments = "{target_args}"
oLink.WorkingDirectory = "{os.path.dirname(os.path.abspath(__file__))}"
oLink.WindowStyle = 7
oLink.Save
'''
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".vbs", delete=False) as f:
        f.write(vbs)
        vbs_path = f.name
    try:
        subprocess.run(["cscript", "//nologo", vbs_path], capture_output=True, timeout=10)
    finally:
        try:
            os.remove(vbs_path)
        except OSError:
            pass


class TrayApp:
    def __init__(self):
        self.client = ApiClient()
        self._icon = pystray.Icon(
            "AquariusBotManager", ICON_DOWN, "Aquarius Bot Manager",
            menu=self._build_menu())
        self._stop_poll = threading.Event()

    # ---- menu ----

    def _build_menu(self):
        bots_autostart_on = self._bots_autostart_enabled()
        tray_login_on = _login_item_enabled(TRAY_SHORTCUT_NAME)
        return pystray.Menu(
            pystray.MenuItem("Open control panel", self._on_open_panel, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Start manager", self._on_start_service),
            pystray.MenuItem("Stop manager", self._on_stop_service),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"Start bots at login: {'On' if bots_autostart_on else 'Off'}",
                              self._toggle_handler(self._on_toggle_bots_autostart, bots_autostart_on)),
            pystray.MenuItem(f"Launch control panel at login: {'On' if tray_login_on else 'Off'}",
                              self._toggle_handler(self._on_toggle_tray_login, tray_login_on)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._on_quit),
        )

    @staticmethod
    def _toggle_handler(fn, current_state):
        """Binds the state this menu item was BUILT with (not re-derived from
        its rendered label text) into the click handler, so flipping it is
        unambiguous even if label wording changes later."""
        return lambda icon, item: fn(not current_state)

    def _refresh_menu(self):
        self._icon.menu = self._build_menu()

    # ---- bots-autostart is the manager's own "boot" concept, not a Windows
    # login item -- it governs whether `serve`'s startup path runs `boot()`
    # (launches every instance flagged autostart=true). Exposed here as a
    # single on/off toggle over the manager's own settings, not a per-login-
    # item shortcut like the tray/control-panel toggles below. ----

    def _bots_autostart_enabled(self):
        try:
            settings = self.client.get("/api/settings")
            return bool(settings.get("bots_autostart_enabled", True))
        except ApiError:
            return True

    def _on_toggle_bots_autostart(self, new_value):
        try:
            self.client.post("/api/settings", {"bots_autostart_enabled": new_value})
        except ApiError as e:
            self._notify(f"Could not change bots-at-login setting: {e}")
        self._refresh_menu()

    def _on_toggle_tray_login(self, new_value):
        _set_login_item(TRAY_SHORTCUT_NAME, "tray_win.py", new_value)
        self._refresh_menu()

    # ---- actions ----

    def _on_open_panel(self, icon, item):
        _open_control_panel()

    def _on_start_service(self, icon, item):
        ok, msg = _service_control("start")
        if not ok:
            self._notify(f"Could not start the manager service: {msg}")
        self._poll_once()

    def _on_stop_service(self, icon, item):
        ok, msg = _service_control("stop")
        if not ok:
            self._notify(f"Could not stop the manager service: {msg}")
        self._poll_once()

    def _on_quit(self, icon, item):
        self._stop_poll.set()
        icon.stop()

    def _notify(self, message):
        try:
            self._icon.notify(message, title="Aquarius Bot Manager")
        except Exception:
            pass  # notifications aren't available on every Windows config; non-fatal

    # ---- health polling: just updates the icon color, no menu content ----

    def _poll_once(self):
        running = _service_running()
        self._icon.icon = ICON_OK if running else ICON_DOWN
        self._icon.title = ("Aquarius Bot Manager — running" if running
                             else "Aquarius Bot Manager — stopped")

    def _poll_loop(self):
        while not self._stop_poll.is_set():
            self._poll_once()
            self._stop_poll.wait(POLL_INTERVAL_SEC)

    def run(self):
        threading.Thread(target=self._poll_loop, daemon=True).start()
        self._icon.run()


def main():
    if sys.platform != "win32":
        print("tray_win.py is Windows-only.", file=sys.stderr)
        sys.exit(1)
    TrayApp().run()


if __name__ == "__main__":
    main()
