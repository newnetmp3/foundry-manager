#!/usr/bin/env python3
"""Foundry Server Manager: run several Foundry VTT servers on one Linux machine.

Lists the Foundry servers pm2 runs for the Foundry user (status, address,
port, people connected), starts/stops/restarts them, shows their logs, and adds
new ones: install folder and data folder, the Foundry Node.js build (timed
download link or a zip), options.json, a pm2 process, a Cloudflare DNS record,
a Let's Encrypt certificate, and an nginx site.

Runs as your desktop user. Commands for the Foundry user go through
`sudo -u <user>`; nginx and certbot through `sudo`. Your sudo password is
asked once and kept only in memory for this session.

Needs: python-pyside6, nginx, certbot (+ certbot-dns-cloudflare to get
certificates through Cloudflare DNS), and pm2 for the Foundry user.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

APP = "Foundry Server Manager"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "foundry-manager"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
CF_CREDENTIALS = CONFIG_DIR / "cloudflare.ini"  # certbot-dns-cloudflare format, mode 600
MARKER = "# Written by Foundry Server Manager"

DEFAULTS = {
    "foundry_user": "",      # asked on first run (or --user NAME)
    "existing_domain": "",   # found from that user's running server
    "cloudflare_zone": "",   # the existing address's domain
    "letsencrypt_email": "",
    "nginx_dir": "",  # found from the existing site when empty
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save_settings(s: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(s, indent=2))


def cloudflare_token() -> str:
    try:
        for line in CF_CREDENTIALS.read_text().splitlines():
            if line.strip().startswith("dns_cloudflare_api_token"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def save_cloudflare_token(token: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CF_CREDENTIALS.touch(mode=0o600, exist_ok=True)
    os.chmod(CF_CREDENTIALS, 0o600)
    CF_CREDENTIALS.write_text(f"dns_cloudflare_api_token = {token.strip()}\n")
    os.chmod(CF_CREDENTIALS, 0o600)


def zone_of(fqdn: str) -> str:
    """'play.example.com' -> 'example.com' (good for most domains; editable)."""
    parts = [p for p in fqdn.strip().lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else ""


def detect_foundry_users() -> list[str]:
    """Users running Foundry (node … main.js) or a pm2 daemon, most likely first."""
    try:
        out = subprocess.run(["ps", "-eo", "user:64=,args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    foundry, pm2 = [], []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        user, args = parts
        if user == "root":
            continue
        if re.search(r"node\S*\s+\S*main\.m?js", args) and re.search(r"foundry|--dataPath", args, re.I):
            if user not in foundry:
                foundry.append(user)
        elif re.search(r"PM2 v\d|pm2\b.*God Daemon", args):
            if user not in pm2:
                pm2.append(user)
    return foundry + [u for u in pm2 if u not in foundry]


# ---------------------------------------------------------------------------
# Running commands (as you, as the Foundry user, or as root)
# ---------------------------------------------------------------------------
class Runner:
    """Runs commands as you, as the Foundry user (sudo -u) or as root (sudo).

    The sudo password is only ever given to `sudo -v` (which just checks it
    and refreshes sudo's timestamp); the real command then runs with
    `sudo -n`, so the password can never end up in a command's input.
    It's kept in memory for this session only.
    """

    def __init__(self, ask_password):
        self._ask = ask_password
        self._password: str | None = None

    def ensure(self) -> bool:
        """Make sure sudo will work. Asks for the password on the GUI thread only."""
        if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
            return True
        if self._password is not None and self._refresh():
            return True
        app = QtWidgets.QApplication.instance()
        if app is None or QtCore.QThread.currentThread() is not app.thread():
            return False  # can't show a dialog from a background task
        for _ in range(3):
            pw = self._ask()
            if pw is None:
                return False
            self._password = pw
            if self._refresh():
                return True
            self._password = None
        return False

    def _refresh(self) -> bool:
        return subprocess.run(["sudo", "-S", "-p", "", "-v"], input=(self._password or "") + "\n",
                              text=True, capture_output=True).returncode == 0

    def run(self, cmd: list[str], *, root=False, user: str | None = None, stdin: str | None = None, timeout=900):
        """Returns (exit code, output). `user`: run as that user with a login shell."""
        if user or root:
            if not self.ensure():
                return 1, "sudo isn't available (password not given or wrong)"
            if user:
                line = " ".join(shlex.quote(c) for c in cmd)
                cmd = ["sudo", "-n", "-u", user, "-H", "bash", "-lc", line]
            else:
                cmd = ["sudo", "-n"] + cmd
        try:
            p = subprocess.run(cmd, input=stdin, text=True, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, "timed out: " + " ".join(cmd)
        except FileNotFoundError as e:
            return 127, str(e)
        return p.returncode, (p.stdout + p.stderr).strip()

    def write_file(self, path: str, text: str, *, root=False, user: str | None = None, mode: str = "644"):
        """Write a file as root or as `user` (through tee)."""
        code, out = self.run(["tee", path], root=root, user=user, stdin=text)
        if code == 0:
            self.run(["chmod", mode, path], root=root, user=user)
        return code, ("" if code == 0 else out)


# ---------------------------------------------------------------------------
# Foundry servers
# ---------------------------------------------------------------------------
@dataclass
class Server:
    name: str
    status: str = "unknown"
    port: int = 30000
    data_path: str = ""
    app_path: str = ""
    hostname: str = ""
    pm_id: int | None = None
    users: int | None = None
    world: str = ""
    version: str = ""
    extra: dict = field(default_factory=dict)


def arg_value(args: list[str], key: str) -> str:
    for i, a in enumerate(args):
        if a.startswith(key + "="):
            return a.split("=", 1)[1]
        if a == key and i + 1 < len(args):
            return args[i + 1]
    return ""


def parse_pm2(jlist: str, home: str) -> list[Server]:
    """Foundry servers among pm2's processes (`pm2 jlist`)."""
    try:
        procs = json.loads(jlist[jlist.find("["):]) if "[" in jlist else []
    except ValueError:
        return []
    out = []
    for p in procs:
        env = p.get("pm2_env", {}) or {}
        script = str(env.get("pm_exec_path", ""))
        args = [str(a) for a in (env.get("args") or [])]
        if isinstance(env.get("args"), str):
            args = shlex.split(env["args"])
        looks = "--dataPath" in " ".join(args) or re.search(r"foundry", script, re.I) or script.endswith("main.js")
        if not looks:
            continue
        port = arg_value(args, "--port")
        out.append(Server(
            name=str(p.get("name", "")), status=str(env.get("status", "unknown")),
            port=int(port) if port.isdigit() else 0,
            data_path=arg_value(args, "--dataPath") or f"{home}/.local/share/FoundryVTT",
            app_path=script, pm_id=p.get("pm_id"),
        ))
    return out


def fetch_status(port: int, timeout=3) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except (OSError, ValueError, urllib.error.URLError):
        return None


def port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def nginx_site(name: str, fqdn: str, port: int) -> str:
    return f"""{MARKER}: {name}
# Foundry VTT at https://{fqdn} -> 127.0.0.1:{port}
server {{
    listen 80;
    listen [::]:80;
    server_name {fqdn};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name {fqdn};

    ssl_certificate /etc/letsencrypt/live/{fqdn}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{fqdn}/privkey.pem;

    client_max_body_size 300M;

    location / {{
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Foundry needs WebSockets.
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "Upgrade";
        proxy_read_timeout 90s;

        proxy_pass http://127.0.0.1:{port};
    }}
}}
"""


def foundry_options(fqdn: str, port: int, existing: dict | None = None) -> dict:
    opts = dict(existing or {})
    opts.update({"port": port, "hostname": fqdn, "proxySSL": True, "proxyPort": 443, "upnp": False})
    return opts


def cloudflare(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.cloudflare.com/client/v4" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except ValueError:
            return {"success": False, "errors": [{"message": f"HTTP {e.code}"}]}


# ---------------------------------------------------------------------------
# Background work with a log
# ---------------------------------------------------------------------------
class Worker(QtCore.QThread):
    line = QtCore.Signal(str)
    finished_ok = QtCore.Signal(bool)

    def __init__(self, steps):
        super().__init__()
        self.steps = steps  # list of (label, callable(log) -> bool)

    def run(self):
        ok = True
        for label, fn in self.steps:
            self.line.emit(f"▶ {label}")
            try:
                if not fn(self.line.emit):
                    self.line.emit(f"✗ {label} failed.")
                    ok = False
                    break
            except Exception as e:  # keep the GUI alive whatever happens
                self.line.emit(f"✗ {label}: {e}")
                ok = False
                break
            self.line.emit(f"✓ {label}")
        self.finished_ok.emit(ok)


class LogDialog(QtWidgets.QDialog):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 460)
        lay = QtWidgets.QVBoxLayout(self)
        self.text = QtWidgets.QPlainTextEdit(readOnly=True)
        self.text.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay.addWidget(self.text)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        lay.addWidget(self.close_btn, alignment=QtCore.Qt.AlignRight)

    def add(self, s: str):
        self.text.appendPlainText(s)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, settings: dict, first_run: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Set up " + APP if first_run else "Settings")
        form = QtWidgets.QFormLayout(self)
        if first_run:
            intro = QtWidgets.QLabel(
                "Which system user runs your Foundry servers (with pm2)? The tool manages that user's servers "
                "and installs new ones in its home folder. Users already running Foundry are suggested.")
            intro.setWordWrap(True)
            form.addRow(intro)
        # The Foundry user: an editable list of users seen running Foundry or pm2.
        self.user = QtWidgets.QComboBox(editable=True)
        found = detect_foundry_users()
        current = settings["foundry_user"]
        for u in ([current] if current else []) + [u for u in found if u != current]:
            self.user.addItem(u)
        self.user.setCurrentText(current or (found[0] if found else ""))
        self.user.lineEdit().setPlaceholderText("e.g. foundry")
        self.domain = QtWidgets.QLineEdit(settings["existing_domain"])
        self.domain.setPlaceholderText("filled in from your running server, if it has one")
        self.zone = QtWidgets.QLineEdit(settings["cloudflare_zone"])
        self.zone.setPlaceholderText("e.g. example.com")
        self.domain.textEdited.connect(lambda t: self.zone.setText(zone_of(t)) if not self._zone_edited else None)
        self._zone_edited = bool(settings["cloudflare_zone"])
        self.zone.textEdited.connect(lambda _t: setattr(self, "_zone_edited", True))
        self.email = QtWidgets.QLineEdit(settings["letsencrypt_email"])
        self.nginx = QtWidgets.QLineEdit(settings["nginx_dir"])
        self.nginx.setPlaceholderText("found from your existing site")
        self.token = QtWidgets.QLineEdit(cloudflare_token())
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText("API token with Zone › DNS › Edit")
        form.addRow("Foundry runs as user:", self.user)
        form.addRow("Existing server address:", self.domain)
        form.addRow("Cloudflare zone (domain):", self.zone)
        form.addRow("Cloudflare API token:", self.token)
        form.addRow("Let's Encrypt email:", self.email)
        form.addRow("nginx sites folder:", self.nginx)
        hint = QtWidgets.QLabel(
            "The token is stored in ~/.config/foundry-manager/cloudflare.ini (only you can read it).\n"
            "Create one at dash.cloudflare.com → My Profile → API Tokens → Edit zone DNS.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: gray")
        form.addRow(hint)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._check)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _check(self):
        user = self.user.currentText().strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
            QtWidgets.QMessageBox.warning(self, APP, "Enter the system user that runs Foundry (e.g. foundry).")
            return
        try:
            import pwd
            pwd.getpwnam(user)
        except KeyError:
            QtWidgets.QMessageBox.warning(self, APP, f"There's no user called {user} on this machine.")
            return
        except ImportError:
            pass
        self.accept()

    def values(self) -> tuple[dict, str]:
        return ({
            "foundry_user": self.user.currentText().strip(), "existing_domain": self.domain.text().strip(),
            "cloudflare_zone": self.zone.text().strip(), "letsencrypt_email": self.email.text().strip(),
            "nginx_dir": self.nginx.text().strip(),
        }, self.token.text().strip())


class AddDialog(QtWidgets.QDialog):
    def __init__(self, parent, settings: dict, used_ports: set[int], home: str):
        super().__init__(parent)
        self.setWindowTitle("Add a Foundry server")
        self.resize(560, 0)
        form = QtWidgets.QFormLayout(self)
        port = 30001
        while port in used_ports or not port_free(port):
            port += 1
        n = 2
        self.name = QtWidgets.QLineEdit(f"foundry{n}")
        zone = settings["cloudflare_zone"]
        self.fqdn = QtWidgets.QLineEdit(f"second.{zone}" if zone else "")
        self.port = QtWidgets.QSpinBox(minimum=1024, maximum=65535, value=port)
        self.app_dir = QtWidgets.QLineEdit(f"{home}/foundry{n}")
        self.data_dir = QtWidgets.QLineEdit(f"{home}/foundrydata{n}")
        self.name.textEdited.connect(self._names)
        self.source = QtWidgets.QLineEdit()
        self.source.setPlaceholderText("timed Node.js download link from foundryvtt.com, or a .zip file")
        pick = QtWidgets.QPushButton("Zip file…")
        pick.clicked.connect(self._pick)
        src = QtWidgets.QHBoxLayout()
        src.addWidget(self.source)
        src.addWidget(pick)
        target = settings["existing_domain"]
        self.dns = QtWidgets.QCheckBox(f"Create the DNS record in Cloudflare (CNAME to {target})" if target
                                       else "Create the DNS record in Cloudflare (set the existing server address in Settings first)")
        self.dns.setChecked(bool(cloudflare_token()) and bool(target))
        self.dns.setEnabled(bool(target))
        self.proxied = QtWidgets.QCheckBox("Proxied through Cloudflare (orange cloud), like most sites")
        self.proxied.setChecked(True)
        self.cert = QtWidgets.QComboBox()
        self.cert.addItems(["Cloudflare DNS (certbot-dns-cloudflare)", "nginx (HTTP check)", "Skip (I'll add a certificate myself)"])
        if not cloudflare_token():
            self.cert.setCurrentIndex(1)
        form.addRow("Name (pm2):", self.name)
        form.addRow("Address:", self.fqdn)
        form.addRow("Port:", self.port)
        form.addRow("Install folder:", self.app_dir)
        form.addRow("Data folder:", self.data_dir)
        form.addRow("Foundry download:", src)
        form.addRow("", self.dns)
        form.addRow("", self.proxied)
        form.addRow("Certificate:", self.cert)
        note = QtWidgets.QLabel("The licence key is entered in the browser on first visit: use your second key.")
        note.setStyleSheet("color: gray")
        form.addRow(note)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.button(QtWidgets.QDialogButtonBox.Ok).setText("Create server")
        btns.accepted.connect(self._check)
        btns.rejected.connect(self.reject)
        form.addRow(btns)
        self.home = home

    def _names(self, text):
        n = re.sub(r"[^A-Za-z0-9_-]", "", text) or "foundry2"
        self.app_dir.setText(f"{self.home}/{n}")
        self.data_dir.setText(f"{self.home}/{n.replace('foundry', 'foundrydata', 1) if n.startswith('foundry') else n + '-data'}")

    def _pick(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Foundry Node.js zip", str(Path.home() / "Downloads"), "Zip files (*.zip)")
        if f:
            self.source.setText(f)

    def _check(self):
        problems = []
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name.text()):
            problems.append("Name: letters, digits, - and _ only.")
        if not re.fullmatch(r"(?=.{1,253}$)([a-z0-9-]+\.)+[a-z]{2,}", self.fqdn.text().strip().lower()):
            problems.append("Address: a full domain name such as second.example.com.")
        if not self.source.text().strip():
            problems.append("Foundry download: paste the timed link or pick the zip.")
        if not port_free(self.port.value()):
            problems.append(f"Port {self.port.value()} is in use.")
        if problems:
            QtWidgets.QMessageBox.warning(self, APP, "\n".join(problems))
            return
        self.accept()

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(), "fqdn": self.fqdn.text().strip().lower(), "port": self.port.value(),
            "app_dir": self.app_dir.text().strip(), "data_dir": self.data_dir.text().strip(),
            "source": self.source.text().strip(), "dns": self.dns.isChecked(), "proxied": self.proxied.isChecked(),
            "cert": ["cloudflare", "nginx", "skip"][self.cert.currentIndex()],
        }


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
COLUMNS = ["Name", "Status", "Address", "Port", "Players", "World", "Version", "Data folder"]


class Main(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(1080, 420)
        self.settings = load_settings()
        self.runner = Runner(self._ask_password)
        self.servers: list[Server] = []
        self.workers = []

        self.table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self.open_browser)
        self.setCentralWidget(self.table)

        tb = self.addToolBar("Actions")
        tb.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        def act(text, icon, fn, tip=""):
            a = QtGui.QAction(QtGui.QIcon.fromTheme(icon), text, self)
            a.triggered.connect(fn)
            a.setToolTip(tip or text)
            tb.addAction(a)
            return a
        act("Refresh", "view-refresh", self.refresh)
        tb.addSeparator()
        act("Start", "media-playback-start", lambda: self.pm2_action("start"))
        act("Stop", "media-playback-stop", lambda: self.pm2_action("stop"))
        act("Restart", "view-refresh", lambda: self.pm2_action("restart"))
        act("Log", "text-x-log", self.show_log, "Last 200 lines of the server's log")
        act("Open", "internet-web-browser", self.open_browser)
        tb.addSeparator()
        act("Add server…", "list-add", self.add_server)
        act("Remove…", "list-remove", self.remove_server)
        tb.addSeparator()
        act("Settings…", "configure", self.edit_settings)

        self.status = self.statusBar()
        self.timer = QtCore.QTimer(self, interval=15000, timeout=self.update_status)
        self.timer.start()
        QtCore.QTimer.singleShot(100, self.refresh)

    # ---- helpers ------------------------------------------------------------
    @property
    def user(self) -> str:
        return self.settings["foundry_user"]

    def home(self) -> str:
        try:
            import pwd
            return pwd.getpwnam(self.user).pw_dir
        except (KeyError, ImportError):
            return f"/home/{self.user}"

    def _ask_password(self):
        pw, ok = QtWidgets.QInputDialog.getText(self, APP, "sudo password (kept in memory for this session):",
                                                QtWidgets.QLineEdit.Password)
        return pw if ok else None

    def selected(self) -> Server | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QtWidgets.QMessageBox.information(self, APP, "Pick a server in the list first.")
            return None
        return self.servers[rows[0].row()]

    def run_steps(self, title, steps, done=None):
        # Get sudo working here, on the GUI thread, before any background step needs it.
        if not self.runner.ensure():
            QtWidgets.QMessageBox.warning(self, APP, "This needs sudo, and the password wasn't given or was wrong.")
            return
        dlg = LogDialog(self, title)
        w = Worker(steps)
        w.line.connect(dlg.add)
        def finished(ok):
            dlg.add("\nDone." if ok else "\nStopped: see above.")
            self.refresh()
            if done:
                done(ok)
        w.finished_ok.connect(finished)
        self.workers.append(w)
        w.start()
        dlg.show()

    # ---- listing ---------------------------------------------------------------
    def refresh(self):
        code, out = self.runner.run(["pm2", "jlist"], user=self.user, timeout=60)
        if code != 0:
            self.status.showMessage(f"Couldn't ask pm2 as {self.user}: {out[:200]}")
            self.servers = []
        else:
            self.servers = parse_pm2(out, self.home())
            for s in self.servers:
                c, opts = self.runner.run(["cat", f"{s.data_path}/Config/options.json"], user=self.user, timeout=30)
                try:
                    o = json.loads(opts) if c == 0 else {}
                except ValueError:
                    o = {}
                s.extra = o
                s.port = s.port or int(o.get("port") or 30000)
                s.hostname = o.get("hostname") or ""
            # First servers seen: remember the existing address (and its zone) for new subdomains.
            if not self.settings["existing_domain"]:
                host = next((s.hostname for s in self.servers if s.hostname), "")
                if host:
                    self.settings["existing_domain"] = host
                    self.settings["cloudflare_zone"] = self.settings["cloudflare_zone"] or zone_of(host)
                    save_settings(self.settings)
        self.update_status()

    def update_status(self):
        for s in self.servers:
            st = fetch_status(s.port) if s.port else None
            s.users = st.get("users") if st and isinstance(st.get("users"), int) else None
            s.world = (st or {}).get("world") or ("(no world)" if st else "")
            s.version = (st or {}).get("version") or ""
        self.table.setRowCount(len(self.servers))
        for r, s in enumerate(self.servers):
            vals = [s.name, s.status, s.hostname or "–", str(s.port or "–"),
                    "–" if s.users is None else str(s.users), s.world, s.version, s.data_path]
            for c, v in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(v)
                if c == 1:
                    it.setForeground(QtGui.QColor("#2e9d4f" if s.status == "online" else "#c0392b"))
                self.table.setItem(r, c, it)
        self.table.resizeColumnsToContents()
        n = sum(s.users or 0 for s in self.servers)
        self.status.showMessage(f"{len(self.servers)} server(s), {n} player(s) connected · Foundry user: {self.user}")

    # ---- actions ---------------------------------------------------------------
    def pm2_action(self, what):
        s = self.selected()
        if not s:
            return
        code, out = self.runner.run(["pm2", what, s.name], user=self.user, timeout=120)
        if code != 0:
            QtWidgets.QMessageBox.warning(self, APP, out[-2000:])
        self.refresh()

    def show_log(self):
        s = self.selected()
        if not s:
            return
        code, out = self.runner.run(["pm2", "logs", s.name, "--lines", "200", "--nostream", "--raw"], user=self.user, timeout=60)
        dlg = LogDialog(self, f"Log: {s.name}")
        dlg.add(out or "(empty)")
        dlg.show()

    def open_browser(self, *_):
        s = self.selected()
        if s:
            url = f"https://{s.hostname}" if s.hostname else f"http://localhost:{s.port}"
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def edit_settings(self):
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            self.settings, token = dlg.values()
            save_settings(self.settings)
            if token != cloudflare_token():
                save_cloudflare_token(token)
            self.refresh()

    # ---- nginx folder ------------------------------------------------------------
    def nginx_dir(self, log) -> str | None:
        if self.settings.get("nginx_dir"):
            return self.settings["nginx_dir"]
        files = []
        code = 1
        if self.settings["existing_domain"]:
            dom = re.escape(self.settings["existing_domain"])
            code, out = self.runner.run(["grep", "-rlE", f"server_name[^;]*{dom}", "/etc/nginx"], root=True)
            files = [f for f in out.splitlines() if f.strip() and not f.endswith("nginx.conf")]
        if code == 0 and files:
            d = str(Path(files[0]).parent)
            # A file in sites-available is enabled by a link in sites-enabled.
            log(f"Your existing site is in {files[0]}; the new one goes next to it.")
            return d
        code, inc = self.runner.run(["grep", "-hE", r"^\s*include\s", "/etc/nginx/nginx.conf"], root=True)
        for line in inc.splitlines():
            m = re.search(r"include\s+(\S+)/\*(\.conf)?;", line)
            if m and "mime" not in m.group(1):
                return m.group(1)
        log("Couldn't find where nginx keeps sites: set the nginx sites folder in Settings.")
        return None

    # ---- add a server ------------------------------------------------------------
    def add_server(self):
        dlg = AddDialog(self, self.settings, {s.port for s in self.servers}, self.home())
        if not dlg.exec():
            return
        v = dlg.values()
        u = self.user
        R = self.runner
        state = {}

        def mkdirs(log):
            code, out = R.run(["mkdir", "-p", v["app_dir"], v["data_dir"] + "/Config"], user=u)
            log(out)
            return code == 0

        def install(log):
            z = v["app_dir"] + "/foundryvtt.zip"
            if re.match(r"https?://", v["source"]):
                code, out = R.run(["curl", "-fL", "--retry", "3", "-o", z, v["source"]], user=u, timeout=1800)
            else:
                code, out = R.run(["install", "-m", "644", "-o", u, v["source"], z], root=True)
            log(out[-500:])
            if code != 0:
                return False
            code, out = R.run(["unzip", "-q", "-o", z, "-d", v["app_dir"]], user=u, timeout=600)
            log(out[-500:])
            R.run(["rm", "-f", z], user=u)
            for entry in ("main.js", "resources/app/main.js", "main.mjs"):
                if R.run(["test", "-f", f"{v['app_dir']}/{entry}"], user=u)[0] == 0:
                    state["entry"] = f"{v['app_dir']}/{entry}"
                    log(f"Foundry's start file: {state['entry']}")
                    return code == 0
            log("No main.js found: is this the Node.js build of Foundry?")
            return False

        def options(log):
            path = f"{v['data_dir']}/Config/options.json"
            code, cur = R.run(["cat", path], user=u)
            try:
                existing = json.loads(cur) if code == 0 else {}
            except ValueError:
                existing = {}
            text = json.dumps(foundry_options(v["fqdn"], v["port"], existing), indent=2)
            code, out = R.write_file(path, text + "\n", user=u)
            log(text)
            return code == 0

        def pm2(log):
            code, out = R.run(["pm2", "start", state["entry"], "--name", v["name"], "--",
                               f"--dataPath={v['data_dir']}", f"--port={v['port']}"], user=u, timeout=120)
            log(out[-800:])
            if code != 0:
                return False
            code, out = R.run(["pm2", "save"], user=u)
            log(out[-300:])
            return code == 0

        def dns(log):
            token = cloudflare_token()
            if not token:
                log("No Cloudflare token (Settings): add the DNS record yourself.")
                return True
            z = cloudflare("GET", "/zones?name=" + self.settings["cloudflare_zone"], token)
            if not z.get("success") or not z.get("result"):
                log(f"Cloudflare: zone {self.settings['cloudflare_zone']} not found: {z.get('errors')}")
                return False
            zid = z["result"][0]["id"]
            have = cloudflare("GET", f"/zones/{zid}/dns_records?name={v['fqdn']}", token)
            if have.get("result"):
                log(f"Cloudflare already has a record for {v['fqdn']}: leaving it.")
                return True
            r = cloudflare("POST", f"/zones/{zid}/dns_records", token, {
                "type": "CNAME", "name": v["fqdn"], "content": self.settings["existing_domain"],
                "proxied": v["proxied"], "ttl": 1, "comment": "Foundry VTT (Foundry Server Manager)",
            })
            log(("Created " if r.get("success") else "Cloudflare refused: ") + json.dumps(r.get("result") or r.get("errors"))[:400])
            return bool(r.get("success"))

        def cert(log):
            if v["cert"] == "skip":
                log("Skipped: the nginx site expects /etc/letsencrypt/live/%s/." % v["fqdn"])
                return True
            email = self.settings.get("letsencrypt_email")
            common = ["certbot", "certonly", "--non-interactive", "--agree-tos", "-d", v["fqdn"]]
            common += ["-m", email] if email else ["--register-unsafely-without-email"]
            if v["cert"] == "cloudflare":
                cmd = common + ["--dns-cloudflare", "--dns-cloudflare-credentials", str(CF_CREDENTIALS),
                                "--dns-cloudflare-propagation-seconds", "30"]
            else:
                cmd = common + ["--nginx"]
            code, out = R.run(cmd, root=True, timeout=600)
            log(out[-1500:])
            return code == 0

        def nginx(log):
            d = self.nginx_dir(log)
            if not d:
                return False
            # Always .conf: many nginx setups only load *.conf files.
            path = f"{d}/{v['name']}.conf"
            code, out = R.write_file(path, nginx_site(v["name"], v["fqdn"], v["port"]), root=True)
            if code != 0:
                log(out)
                return False
            log(f"Wrote {path}")
            if d.endswith("sites-available"):
                R.run(["ln", "-sf", path, path.replace("sites-available", "sites-enabled")], root=True)
            code, out = R.run(["nginx", "-t"], root=True)
            log(out)
            if code != 0:
                log("nginx doesn't accept it: the site file was left in place for you to look at; nginx wasn't reloaded.")
                return False
            code, out = R.run(["systemctl", "reload", "nginx"], root=True)
            log(out)
            return code == 0

        steps = [
            ("Create folders", mkdirs),
            ("Install Foundry", install),
            ("Write options.json", options),
            ("Start under pm2", pm2),
        ]
        if v["dns"]:
            steps.append(("Cloudflare DNS record", dns))
        steps += [("Certificate", cert), ("nginx site", nginx)]

        def done(ok):
            if ok:
                QtWidgets.QMessageBox.information(
                    self, APP,
                    f"https://{v['fqdn']} is set up.\n\nOpen it, enter your second licence key and accept the "
                    "licence agreement, then set an admin password."
                    + ("\n\nDNS can take a minute to reach everyone." if v["dns"] else ""))
        self.run_steps(f"Adding {v['name']}", steps, done)

    # ---- remove ------------------------------------------------------------------------
    def remove_server(self):
        s = self.selected()
        if not s:
            return
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(APP)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText(f"Remove {s.name} from pm2 and nginx?")
        box.setInformativeText("Its install and data folders (worlds, licence) are kept unless you tick the box.")
        files = QtWidgets.QCheckBox(f"Also delete {s.data_path} and the install folder")
        box.setCheckBox(files)
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel)
        if box.exec() != QtWidgets.QMessageBox.Yes:
            return
        if files.isChecked():
            name, ok = QtWidgets.QInputDialog.getText(self, APP, f"Type {s.name} to delete its folders for good:")
            if not ok or name != s.name:
                return
        R, u = self.runner, self.user

        def pm2(log):
            log(R.run(["pm2", "delete", s.name], user=u)[1][-400:])
            log(R.run(["pm2", "save"], user=u)[1][-200:])
            return True

        def nginx(log):
            if not s.hostname:
                log("No address in its options.json: nginx left as is.")
                return True
            code, out = R.run(["grep", "-rlF", f"{MARKER}: {s.name}", "/etc/nginx"], root=True)
            mine = [f for f in out.splitlines() if f.strip()]
            if not mine:
                log("Its nginx site wasn't written by this tool: left as is (remove it yourself if you want).")
                return True
            for f in mine:
                R.run(["rm", "-f", f], root=True)
                R.run(["rm", "-f", f.replace("sites-available", "sites-enabled")], root=True)
                log(f"Removed {f}")
            code, out = R.run(["nginx", "-t"], root=True)
            log(out)
            return code == 0 and R.run(["systemctl", "reload", "nginx"], root=True)[0] == 0

        def folders(log):
            app = str(Path(s.app_path).parent) if s.app_path else ""
            if app.endswith("resources/app"):
                app = str(Path(app).parent.parent)
            for d in [s.data_path, app]:
                if d and d.startswith(self.home() + "/") and d.rstrip("/") != self.home():
                    log(R.run(["rm", "-rf", d], user=u)[1] or f"Deleted {d}")
            return True

        steps = [("pm2", pm2), ("nginx", nginx)]
        if files.isChecked():
            steps.append(("Folders", folders))
        self.run_steps(f"Removing {s.name}", steps)


def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP)
    ap.add_argument("--user", help="the system user that runs Foundry (saved for next time)")
    args, qt_args = ap.parse_known_args()
    app = QtWidgets.QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName(APP)
    app.setWindowIcon(QtGui.QIcon.fromTheme("applications-games"))
    settings = load_settings()
    if args.user:
        settings["foundry_user"] = args.user.strip()
        save_settings(settings)
    # First run: ask which user runs Foundry (suggesting the ones running it).
    if not settings["foundry_user"]:
        dlg = SettingsDialog(None, settings, first_run=True)
        if not dlg.exec():
            return
        settings, token = dlg.values()
        save_settings(settings)
        if token:
            save_cloudflare_token(token)
    w = Main()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
