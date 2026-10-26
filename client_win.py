"""
HTTP client for the manager's local API, shared by tray_win.py and gui_win.py.

Both the tray app and the control-panel window are separate processes from
the manager service itself (see tray_win.py / gui_win.py module docstrings
for why) -- they talk to it purely as HTTP clients of the same JSON API the
web dashboard already uses (GET /api/instances, POST /api/instances/<name>/
start|stop|restart, etc.), never embedding or opening a browser.

Auth: reuses the manager's own login (username/password -> a session cookie,
matching the web dashboard) rather than a separate credential. The session
token is persisted to a small local file so it survives across tray/GUI
restarts; it's invalidated the same way a browser session would be (password
change, `abm logout-all`, or its own 7-day expiry), at which point
`ApiClient` raises AuthError and the caller (tray/GUI) re-prompts for login.
"""

import http.client
import json
import os
import urllib.parse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SESSION_FILE = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                             "AquariusBotManager", "session.json")


class ApiError(Exception):
    """Any non-2xx response, or a request that failed outright (connection refused)."""
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class AuthError(ApiError):
    """401/403 -- caller should discard the stored session and re-prompt for login."""


class ApiClient:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT):
        self.host = host
        self.port = port
        self._token = self._load_session()

    # ---- session persistence ----

    def _load_session(self):
        try:
            with open(SESSION_FILE) as f:
                data = json.load(f)
            return data.get("token")
        except (OSError, ValueError):
            return None

    def _save_session(self, token):
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        with open(SESSION_FILE, "w") as f:
            json.dump({"token": token}, f)

    def _clear_session(self):
        self._token = None
        try:
            os.remove(SESSION_FILE)
        except OSError:
            pass

    @property
    def logged_in(self):
        return self._token is not None

    # ---- low-level request ----

    def _request(self, method, path, body=None, timeout=10):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers["Cookie"] = f"abm_session={self._token}"
            payload = json.dumps(body).encode() if body is not None else b""
            if body is not None:
                headers["Content-Length"] = str(len(payload))
            try:
                conn.request(method, path, body=payload if body is not None else None, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
            except (ConnectionRefusedError, OSError) as e:
                raise ApiError(f"could not reach the manager at {self.host}:{self.port}: {e}")

            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                data = {"error": raw.decode("utf-8", errors="replace")}

            if resp.status in (401, 403):
                raise AuthError(data.get("error", "not authenticated"), status=resp.status)
            if resp.status >= 400:
                raise ApiError(data.get("error", f"HTTP {resp.status}"), status=resp.status)
            return data
        finally:
            conn.close()

    def get(self, path, timeout=10):
        return self._request("GET", path, timeout=timeout)

    def post(self, path, body=None, timeout=10):
        return self._request("POST", path, body=body or {}, timeout=timeout)

    # ---- auth ----

    def auth_status(self):
        """{"required","authed","needs_setup","principal",...} -- call before
        prompting for login, to skip the prompt entirely if auth is off."""
        return self.get("/api/authstatus")

    def login(self, username, password):
        """Raises ApiError on bad credentials. On success, persists the
        session cookie so future ApiClient instances (a later tray/GUI
        launch) start already logged in."""
        conn = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            payload = json.dumps({"username": username, "password": password}).encode()
            headers = {"Content-Type": "application/json", "Content-Length": str(len(payload))}
            conn.request("POST", "/api/login", body=payload, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            data = json.loads(raw) if raw else {}
            if resp.status >= 400:
                raise ApiError(data.get("error", f"HTTP {resp.status}"), status=resp.status)
            set_cookie = resp.getheader("Set-Cookie", "")
            token = None
            for part in set_cookie.split(";"):
                part = part.strip()
                if part.startswith("abm_session="):
                    token = part[len("abm_session="):]
                    break
            if not token:
                raise ApiError("login succeeded but no session cookie was returned")
            self._token = token
            self._save_session(token)
            return data
        finally:
            conn.close()

    def logout(self):
        self._clear_session()

    # ---- instances ----

    def list_instances(self):
        return self.get("/api/instances").get("instances", [])

    def start(self, name):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/start")

    # stop (and restart, which stops first) can legitimately take as long as
    # the instance's own stop_timeout (default 15s) before the manager force-
    # kills it -- give these calls real headroom instead of the default 10s,
    # which a real stop can and does exceed (confirmed against a live test
    # instance whose process doesn't respond to the graceful-stop signal).
    STOP_TIMEOUT_SEC = 30

    def stop(self, name):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/stop",
                          timeout=self.STOP_TIMEOUT_SEC)

    def restart(self, name):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/restart",
                          timeout=self.STOP_TIMEOUT_SEC)

    def logs(self, name, lines=300):
        return self.get(f"/api/instances/{urllib.parse.quote(name)}/logs?lines={int(lines)}").get("logs", [])

    def delete(self, name, force=False):
        """Raises ApiError (409) if running and force=False -- caller should
        either confirm with the user and retry with force=True, or stop the
        instance first."""
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/delete", {"force": bool(force)})

    def send_command(self, name, command):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/command", {"command": command})

    def rename(self, name, new_name, move_dir=True):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/rename",
                          {"new": new_name, "move_dir": move_dir})

    def set_limits(self, name, memory=None, cpu=None):
        return self.post(f"/api/instances/{urllib.parse.quote(name)}/limits",
                          {"memory": memory, "cpu": cpu})

    def start_all(self, names):
        """No bulk route exists server-side -- loop per-instance, collecting
        per-name results/errors instead of raising on the first failure."""
        results = {}
        for name in names:
            try:
                results[name] = self.start(name)
            except ApiError as e:
                results[name] = {"error": str(e)}
        return results

    def stop_all(self, names):
        results = {}
        for name in names:
            try:
                results[name] = self.stop(name)
            except ApiError as e:
                results[name] = {"error": str(e)}
        return results

    # ---- host ----

    def system_info(self):
        return self.get("/api/system/info")
