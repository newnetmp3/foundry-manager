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
import shutil
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


def site_dirs(conf: str) -> list[str]:
    """Folders that nginx.conf includes *directly inside http { }* with a wildcard (where site files go).

    Folders included elsewhere (top-level modules.d for load_module, snippets inside server blocks)
    don't take site files. Relative paths are relative to /etc/nginx.
    """
    text = re.sub(r"#[^\n]*", "", conf)
    stack, buf, found = [], "", []
    for ch in text:
        if ch not in "{};":
            buf += ch
            continue
        stmt = buf.strip()
        buf = ""
        if ch == "{":
            stack.append(stmt.split()[0] if stmt else "")
        elif ch == "}":
            if stack:
                stack.pop()
        elif stack == ["http"] and re.match(r"include\s", stmt):
            path = stmt.split(None, 1)[1].strip().strip("'\"")
            if "*" not in Path(path).name or "mime" in path:
                continue
            d = str((Path("/etc/nginx") / path).parent)
            if d not in found:
                found.append(d)
    # Prefer the usual site folders.
    rank = lambda d: 0 if d.endswith("sites-enabled") else 1 if d.endswith("conf.d") else 2
    return sorted(found, key=rank)


def add_sites_include(conf: str) -> str | None:
    """nginx.conf with `include sites-enabled/*.conf;` added at the start of the http block."""
    m = re.search(r"^(\s*)http\s*\{[^\n]*\n", conf, re.M)
    if not m:
        return None
    line = f"{m.group(1)}    include sites-enabled/*.conf;  {MARKER}\n"
    return conf[: m.end()] + line + conf[m.end():]


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

    def __init__(self, steps, start=0):
        super().__init__()
        self.steps = steps  # list of (label, callable(log) -> bool)
        self.start_at = start
        self.failed_at = None  # index of the step that failed, for Retry

    def run(self):
        ok = True
        for i in range(self.start_at, len(self.steps)):
            label, fn = self.steps[i]
            self.line.emit(f"▶ {label}")
            try:
                if not fn(self.line.emit):
                    self.line.emit(f"✗ {label} failed.")
                    ok = False
            except Exception as e:  # keep the GUI alive whatever happens
                self.line.emit(f"✗ {label}: {e}")
                ok = False
            if not ok:
                self.failed_at = i
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
        btns = QtWidgets.QHBoxLayout()
        self.hint = QtWidgets.QLabel("")
        self.hint.setWordWrap(True)
        btns.addWidget(self.hint, 1)
        # Shown when a step fails: runs again from that step (the steps before it stay done).
        self.retry_btn = QtWidgets.QPushButton(QtGui.QIcon.fromTheme("view-refresh"), "Retry")
        self.retry_btn.hide()
        btns.addWidget(self.retry_btn)
        self.close_btn = QtWidgets.QPushButton("Close")
        self.close_btn.clicked.connect(self.accept)
        btns.addWidget(self.close_btn)
        lay.addLayout(btns)

    def add(self, s: str):
        s = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)  # pm2 and others colour their output
        self.text.appendPlainText(s)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, settings: dict):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        form = QtWidgets.QFormLayout(self)
        intro = note("Quick edit of the setup answers. <b>Setup guide…</b> on the toolbar explains each one.")
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


# ---------------------------------------------------------------------------
# Step-by-step wizards
# ---------------------------------------------------------------------------
def explain(html: str) -> QtWidgets.QLabel:
    """A paragraph of explanation on a wizard page."""
    lab = QtWidgets.QLabel(html)
    lab.setWordWrap(True)
    lab.setTextFormat(QtCore.Qt.RichText)
    lab.setOpenExternalLinks(True)
    lab.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
    return lab


def note(text: str) -> QtWidgets.QLabel:
    """A smaller grey hint under a field."""
    lab = explain(text)
    lab.setStyleSheet("color: gray")
    return lab


def page(title: str, subtitle: str = "") -> tuple[QtWidgets.QWizardPage, QtWidgets.QVBoxLayout]:
    p = QtWidgets.QWizardPage()
    p.setTitle(title)
    if subtitle:
        p.setSubTitle(subtitle)
    lay = QtWidgets.QVBoxLayout(p)
    lay.setSpacing(8)
    return p, lay


class CheckedPage(QtWidgets.QWizardPage):
    """A page whose Next button runs a check first (a message explains what's wrong)."""
    def __init__(self, title, subtitle, check):
        super().__init__()
        self.setTitle(title)
        self.setSubTitle(subtitle)
        self.check = check
        self.lay = QtWidgets.QVBoxLayout(self)
        self.lay.setSpacing(8)

    def validatePage(self):
        problem = self.check()
        if problem:
            QtWidgets.QMessageBox.warning(self, APP, problem)
            return False
        return True


def user_exists(user: str) -> bool:
    try:
        import pwd
        pwd.getpwnam(user)
        return True
    except KeyError:
        return False
    except ImportError:
        return True


def detect_existing_domain(user: str) -> str:
    """The hostname in the options.json of a server this user runs, if we can read it."""
    try:
        out = subprocess.run(["ps", "-u", user, "-o", "args="], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        m = re.search(r"--dataPath[= ](\S+)", line)
        if not m:
            continue
        try:
            with open(Path(m.group(1)) / "Config" / "options.json", encoding="utf-8") as f:
                host = json.load(f).get("hostname") or ""
        except (OSError, ValueError):
            continue
        if host:
            return host
    return ""


def wizard(parent, title: str) -> QtWidgets.QWizard:
    w = QtWidgets.QWizard(parent)
    w.setWindowTitle(title)
    w.setWizardStyle(QtWidgets.QWizard.ModernStyle)
    w.setOption(QtWidgets.QWizard.NoBackButtonOnStartPage, True)
    w.resize(680, 560)
    return w


class SetupWizard:
    """First-run setup (and re-runnable from the toolbar): one question per page, with the reason for it."""

    def __init__(self, parent, settings: dict):
        self.settings = dict(settings)
        self.w = wz = wizard(parent, "Set up " + APP)

        # 1. Welcome
        p, lay = page("Welcome", "A few questions so this tool can find and manage your Foundry servers.")
        lay.addWidget(explain(
            "<p>This tool runs and adds <b>Foundry VTT</b> servers (the Node.js version) on this machine. "
            "Each server gets its own web address, port, install folder and data folder, and is kept running by "
            "<b>pm2</b>. nginx puts them on the web with HTTPS.</p>"
            "<p>The next pages ask:</p><ol>"
            "<li>Which system user runs Foundry.</li>"
            "<li>The address of the server you already have (if any).</li>"
            "<li>Your Cloudflare details, if your DNS is on Cloudflare (optional).</li>"
            "<li>An email for certificate notices (optional).</li></ol>"
            "<p>Nothing on the system changes during setup: answers are only saved to "
            f"<code>{SETTINGS_FILE}</code>. You can change them later under <b>Settings</b>.</p>"
            "<p>Later, when the tool needs to change something (start a server, write an nginx site), it asks for "
            "your <b>sudo password</b> once and keeps it in memory only until you close it.</p>"))
        lay.addStretch()
        wz.addPage(p)

        # 2. Foundry user
        self.user = QtWidgets.QComboBox(editable=True)
        found = detect_foundry_users()
        current = settings["foundry_user"]
        for u in ([current] if current else []) + [u for u in found if u != current]:
            self.user.addItem(u)
        self.user.setCurrentText(current or (found[0] if found else ""))
        self.user.lineEdit().setPlaceholderText("e.g. foundry")

        def check_user():
            u = self.user.currentText().strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", u):
                return "Enter the name of the system user that runs Foundry, such as foundry."
            if not user_exists(u):
                return (f"There's no user called {u} on this machine.\n\nCreate one first, for example:\n"
                        f"sudo useradd -m {u}\n\nthen install pm2 for it (npm install -g pm2) and come back.")
            return ""
        p = CheckedPage("Which user runs Foundry?", "Foundry runs as its own system user, not as you.", check_user)
        p.lay.addWidget(explain(
            "<p>Running Foundry under a separate user keeps it away from your own files: if a module or the "
            "server misbehaves, it can only touch that user's folders.</p>"
            "<p>This tool asks that user's <b>pm2</b> which servers it runs, and installs new servers in that "
            "user's home folder.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Foundry runs as user:", self.user)
        p.lay.addLayout(form)
        p.lay.addWidget(note(
            ("Suggested: " + ", ".join(found) + " (seen running Foundry or pm2 right now).") if found else
            "No user is running Foundry or pm2 right now, so there's nothing to suggest. Type the user's name."))
        p.lay.addStretch()
        wz.addPage(p)

        # 3. Existing server address
        self.domain = QtWidgets.QLineEdit(settings["existing_domain"])
        self.domain.setPlaceholderText("e.g. play.example.com (or leave empty)")
        self.domain_note = note("")

        def check_domain():
            d = self.domain.text().strip().lower()
            if d and not re.fullmatch(r"(?=.{1,253}$)([a-z0-9-]+\.)+[a-z]{2,}", d):
                return "Enter just the address, like play.example.com (no https:// or /join)."
            return ""
        p = CheckedPage("Your existing server's address",
                        "The web address players use for the server you already run.", check_domain)
        p.lay.addWidget(explain(
            "<p>It's used in two ways when you add a server:</p><ul>"
            "<li><b>DNS:</b> the new address (say <code>second.example.com</code>) is pointed at this one "
            "with a CNAME record, so it reaches the same machine without you looking up its IP.</li>"
            "<li><b>nginx:</b> the new site file goes in the same folder as this address's site file, "
            "so nginx loads it the same way.</li></ul>"
            "<p>No server yet? Leave it empty: new servers still install, and you add their DNS record "
            "yourself.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Existing address:", self.domain)
        p.lay.addLayout(form)
        p.lay.addWidget(self.domain_note)
        p.lay.addStretch()
        p.initializePage = self._fill_domain
        wz.addPage(p)

        # 4. Cloudflare
        self.zone = QtWidgets.QLineEdit(settings["cloudflare_zone"])
        self.zone.setPlaceholderText("e.g. example.com")
        self.token = QtWidgets.QLineEdit(cloudflare_token())
        self.token.setEchoMode(QtWidgets.QLineEdit.Password)
        self.token.setPlaceholderText("leave empty to skip Cloudflare")
        test = QtWidgets.QPushButton("Test token")
        test.clicked.connect(self._test_token)
        self.token_result = note("")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.token)
        row.addWidget(test)

        def check_cf():
            if self.token.text().strip() and not self.zone.text().strip():
                return "With a token, also enter the Cloudflare zone (your domain, like example.com)."
            return ""
        p = CheckedPage("Cloudflare (optional)", "Lets the tool create DNS records and certificates for you.", check_cf)
        p.lay.addWidget(explain(
            "<p>If your domain's DNS is on Cloudflare, an <b>API token</b> lets this tool:</p><ul>"
            "<li>add the DNS record for each new server's address, and</li>"
            "<li>get its HTTPS certificate from Let's Encrypt by proving you own the domain through DNS, "
            "which works even with Cloudflare's proxy (orange cloud) on.</li></ul>"
            "<p><b>Zone</b> is the domain as Cloudflare lists it, for example <code>example.com</code>.</p>"
            "<p><b>To make a token:</b> <a href='https://dash.cloudflare.com/profile/api-tokens'>"
            "dash.cloudflare.com → My Profile → API Tokens</a> → Create Token → use the "
            "<b>Edit zone DNS</b> template → under Zone Resources pick your domain → Create. "
            "That token can only change DNS for that one domain.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Cloudflare zone:", self.zone)
        form.addRow("API token:", row)
        p.lay.addLayout(form)
        p.lay.addWidget(self.token_result)
        p.lay.addWidget(note(
            f"The token is saved in {CF_CREDENTIALS}, readable only by you. Not on Cloudflare? Leave both empty "
            "and Next: you'll add DNS records yourself, and certificates are checked over HTTP instead."))
        p.lay.addStretch()
        p.initializePage = lambda: self.zone.text() or self.zone.setText(zone_of(self.domain.text()))
        wz.addPage(p)

        # 5. Certificates and nginx
        self.email = QtWidgets.QLineEdit(settings["letsencrypt_email"])
        self.email.setPlaceholderText("you@example.com (optional)")
        self.nginx = QtWidgets.QLineEdit(settings["nginx_dir"])
        self.nginx.setPlaceholderText("leave empty to find it automatically")

        def check_email():
            e = self.email.text().strip()
            if e and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
                return "That doesn't look like an email address (or leave it empty)."
            return ""
        p = CheckedPage("Certificates and nginx", "Both optional: the defaults work for most setups.", check_email)
        p.lay.addWidget(explain(
            "<p><b>Let's Encrypt email.</b> Each server's address gets a free HTTPS certificate from Let's "
            "Encrypt. They renew on their own; this email only gets a warning if a renewal keeps failing. "
            "It isn't shared or used for anything else. Empty is fine.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Let's Encrypt email:", self.email)
        p.lay.addLayout(form)
        p.lay.addWidget(explain(
            "<p><b>nginx sites folder.</b> Where new site files go. Left empty, the tool uses the folder of your "
            "existing server's site, or the folder your <code>nginx.conf</code> includes "
            "(such as <code>/etc/nginx/sites-available</code>). Only fill this in if that guesses wrong.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("nginx sites folder:", self.nginx)
        p.lay.addLayout(form)
        p.lay.addStretch()
        wz.addPage(p)

        # 6. Summary
        self.summary = explain("")
        p, lay = page("Ready", "Check your answers. Back changes them; Finish saves them.")
        lay.addWidget(self.summary)
        lay.addStretch()
        p.initializePage = self._fill_summary
        wz.addPage(p)
        wz.setButtonText(QtWidgets.QWizard.FinishButton, "Save")

    # ---- page helpers ----------------------------------------------------------------
    def _fill_domain(self):
        if self.domain.text().strip():
            self.domain_note.setText("")
            return
        found = detect_existing_domain(self.user.currentText().strip())
        if found:
            self.domain.setText(found)
            self.domain_note.setText(f"Found in your running server's options.json: {found}. Change it if that's wrong.")
        else:
            self.domain_note.setText("Couldn't read it from the running server (it may belong to another user). "
                                     "It's filled in automatically the first time the server list loads, "
                                     "or type it here.")

    def _test_token(self):
        token, zone = self.token.text().strip(), self.zone.text().strip()
        if not token:
            self.token_result.setText("Enter a token first.")
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            v = cloudflare("GET", "/user/tokens/verify", token)
            z = cloudflare("GET", "/zones?name=" + zone, token) if v.get("success") and zone else {}
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        if not v.get("success"):
            msg = "; ".join(e.get("message", "") for e in v.get("errors") or []) or "no answer"
            self.token_result.setText(f"✗ Cloudflare didn't accept the token ({msg}).")
        elif not zone:
            self.token_result.setText("✓ The token works. Enter the zone too.")
        elif z.get("result"):
            self.token_result.setText(f"✓ The token works and can see {zone}.")
        else:
            self.token_result.setText(f"✗ The token works but can't see the zone {zone}: check the zone name, "
                                      "or the token's Zone Resources.")

    def _fill_summary(self):
        v, token = self.values()
        def row(k, val, empty):
            return f"<tr><td><b>{k}</b></td><td>{val if val else '<i>' + empty + '</i>'}</td></tr>"
        self.summary.setText(
            "<table cellspacing='6'>"
            + row("Foundry user", v["foundry_user"], "")
            + row("Existing address", v["existing_domain"], "none: you'll add DNS records yourself")
            + row("Cloudflare zone", v["cloudflare_zone"], "none")
            + row("Cloudflare token", "saved (hidden)" if token else "", "none: no automatic DNS")
            + row("Let's Encrypt email", v["letsencrypt_email"], "none")
            + row("nginx sites folder", v["nginx_dir"], "found automatically")
            + "</table><p>After saving, the main window lists the servers pm2 runs for "
            f"<b>{v['foundry_user']}</b>. Use <b>Add server…</b> to set up another one; it walks you through it "
            "the same way.</p>")

    def exec(self) -> bool:
        return bool(self.w.exec())

    def values(self) -> tuple[dict, str]:
        return ({
            "foundry_user": self.user.currentText().strip(),
            "existing_domain": self.domain.text().strip().lower(),
            "cloudflare_zone": self.zone.text().strip().lower(),
            "letsencrypt_email": self.email.text().strip(),
            "nginx_dir": self.nginx.text().strip(),
        }, self.token.text().strip())


class AddWizard:
    """Adding a server, one decision per page, each with the reason it's asked."""

    def __init__(self, parent, settings: dict, used_ports: set[int], home: str):
        self.settings = settings
        self.home = home
        self.w = wz = wizard(parent, "Add a Foundry server")
        has_token = bool(cloudflare_token())
        target = settings["existing_domain"]
        zone = settings["cloudflare_zone"]

        # 1. Before you start
        p, lay = page("Before you start", "What a new server needs, and what this wizard will do.")
        lay.addWidget(explain(
            "<p>You'll need:</p><ul>"
            "<li><b>A Foundry licence key that isn't in use.</b> Each server running at the same time needs its "
            "own key. You enter it in the browser at the end, not here.</li>"
            "<li><b>A web address</b> for the new server, such as <code>second.example.com</code>.</li>"
            "<li><b>The Foundry download</b> (Node.js version): a timed link from foundryvtt.com, or the zip.</li>"
            "<li><b>Your sudo password</b>, asked once when the work starts.</li></ul>"
            "<p>When you click <b>Create server</b> on the last page, the tool:</p><ol>"
            "<li>makes the install and data folders,</li>"
            "<li>downloads and unpacks Foundry,</li>"
            "<li>writes Foundry's <code>options.json</code> (port and address),</li>"
            "<li>starts it under pm2 so it runs after reboots,</li>"
            "<li>adds the DNS record (if you choose),</li>"
            "<li>gets an HTTPS certificate, and</li>"
            "<li>adds an nginx site that forwards the address to the server.</li></ol>"
            "<p>Nothing changes until that last click, and your existing servers aren't touched.</p>"))
        lay.addStretch()
        wz.addPage(p)

        # 2. Name and address
        n = 2
        self.name = QtWidgets.QLineEdit(f"foundry{n}")
        self.fqdn = QtWidgets.QLineEdit(f"second.{zone}" if zone else "")
        self.fqdn.setPlaceholderText("e.g. second.example.com")

        def check_name():
            if not re.fullmatch(r"[A-Za-z0-9_-]+", self.name.text()):
                return "Name: letters, digits, - and _ only (no spaces)."
            if not re.fullmatch(r"(?=.{1,253}$)([a-z0-9-]+\.)+[a-z]{2,}", self.fqdn.text().strip().lower()):
                return "Address: a full domain name such as second.example.com (no https://)."
            if self.fqdn.text().strip().lower() == target:
                return f"{target} is your existing server's address: pick a new one."
            return ""
        p = CheckedPage("Name and address", "How you and your players will tell this server apart.", check_name)
        p.lay.addWidget(explain(
            "<p><b>Name</b> is what pm2 calls the server (you'll see it in the list and in <code>pm2 list</code>). "
            "It also names its folders and its nginx file. Short and without spaces, like <code>foundry2</code>.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Name:", self.name)
        p.lay.addLayout(form)
        p.lay.addWidget(explain(
            "<p><b>Address</b> is the web address players open, so it must be one you own: a new subdomain "
            "of your domain is easiest. It has to differ from your existing server's address, because nginx "
            "uses the address to tell which server a visitor wants.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Address:", self.fqdn)
        p.lay.addLayout(form)
        p.lay.addStretch()
        wz.addPage(p)

        # 3. DNS
        self.dns = QtWidgets.QCheckBox(f"Create the DNS record in Cloudflare (CNAME to {target or '…'})")
        self.dns.setChecked(has_token and bool(target))
        self.dns.setEnabled(has_token and bool(target))
        self.proxied = QtWidgets.QCheckBox("Proxied through Cloudflare (orange cloud)")
        self.proxied.setChecked(True)
        self.dns.toggled.connect(self.proxied.setEnabled)
        self.proxied.setEnabled(self.dns.isChecked())
        self.dns_note = note("")
        p, lay = page("DNS record", "So the new address leads to this machine.")
        lay.addWidget(explain(
            "<p>A browser finds a server through a <b>DNS record</b>. The new address gets a <b>CNAME</b> record "
            f"pointing at <code>{target or 'your existing address'}</code>: “this name lives wherever that one "
            "does”. If this machine's IP ever changes, only the existing record needs updating.</p>"
            "<p><b>Proxied</b> (Cloudflare's orange cloud) sends visitors through Cloudflare, which hides "
            "this machine's IP and filters attacks. Foundry works fine through it. Turn it off only if you "
            "want players to connect directly.</p>"))
        lay.addWidget(self.dns)
        lay.addWidget(self.proxied)
        lay.addWidget(self.dns_note)
        lay.addStretch()
        def init_dns():
            fq = self.fqdn.text().strip().lower()
            if not has_token:
                self.dns_note.setText("No Cloudflare token is saved (Settings), so add this record yourself in your "
                                      f"DNS provider: CNAME <b>{fq}</b> → <b>{target or 'this machine'}</b> "
                                      "(or an A record with this machine's IP). Do it before Create, so the "
                                      "certificate step can find the address.")
            elif not target:
                self.dns_note.setText("There's no existing server address in Settings to point at, so add the "
                                      f"record for <b>{fq}</b> yourself (an A record with this machine's IP).")
            elif zone and not fq.endswith("." + zone) and fq != zone:
                self.dns.setChecked(False)
                self.dns_note.setText(f"{fq} isn't in your Cloudflare zone {zone}, so the tool can't add its record: "
                                      "add it yourself where that domain's DNS is.")
            else:
                self.dns_note.setText("If Cloudflare already has a record for this address, it's left as it is.")
        p.initializePage = init_dns
        wz.addPage(p)

        # 4. Port and folders
        port = 30001
        while port in used_ports or not port_free(port):
            port += 1
        self.port = QtWidgets.QSpinBox(minimum=1024, maximum=65535, value=port)
        self.app_dir = QtWidgets.QLineEdit(f"{home}/foundry{n}")
        self.data_dir = QtWidgets.QLineEdit(f"{home}/foundrydata{n}")
        self.name.textEdited.connect(self._names)

        def check_where():
            if self.port.value() in used_ports:
                return f"Port {self.port.value()} belongs to another of your servers."
            if not port_free(self.port.value()):
                return f"Something on this machine already uses port {self.port.value()}: pick another."
            a, d = self.app_dir.text().strip(), self.data_dir.text().strip()
            if not a.startswith("/") or not d.startswith("/"):
                return "Folders: full paths, starting with /."
            if a == d or d.startswith(a.rstrip("/") + "/") or a.startswith(d.rstrip("/") + "/"):
                return "Keep the install folder and the data folder separate (neither inside the other)."
            return ""
        p = CheckedPage("Port and folders", "Where the server listens, and where its files go.", check_where)
        p.lay.addWidget(explain(
            "<p><b>Port.</b> Every Foundry server on the machine needs its own port; your first one usually has "
            "30000. Players never see it: nginx takes visits to the address on the normal HTTPS port and "
            "forwards them here. The first free one is filled in.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Port:", self.port)
        p.lay.addLayout(form)
        p.lay.addWidget(explain(
            "<p><b>Install folder</b> holds Foundry's program files, replaced when you update Foundry. "
            "<b>Data folder</b> holds everything that's yours: worlds, modules, systems, uploaded art and the "
            "licence. Keeping them apart means an update can't touch your worlds. Each server needs its own "
            f"data folder. Both go in {settings['foundry_user']}'s home folder so that user owns them.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Install folder:", self.app_dir)
        form.addRow("Data folder:", self.data_dir)
        p.lay.addLayout(form)
        p.lay.addStretch()
        wz.addPage(p)

        # 5. HTTPS certificate
        self.cert = QtWidgets.QButtonGroup(wz)
        opts = [
            ("cloudflare", "Through Cloudflare DNS (recommended with Cloudflare)",
             "certbot proves you own the domain by adding a temporary DNS record with your token. Works with the "
             "orange cloud on." + ("" if has_token else " <i>Needs a Cloudflare token in Settings.</i>")),
            ("nginx", "Through nginx (HTTP check)",
             "Let's Encrypt visits the address over plain HTTP to check it reaches this machine. Needs the DNS "
             "record in place and port 80 open from the internet."),
            ("skip", "Skip: I'll add a certificate myself",
             "The nginx site expects one at /etc/letsencrypt/live/&lt;address&gt;/; nginx won't load the site "
             "until it's there."),
        ]
        p, lay = page("HTTPS certificate", "So browsers connect securely and show the padlock.")
        lay.addWidget(explain(
            "<p>Browsers need a certificate for the new address before they'll connect over HTTPS. "
            "<b>Let's Encrypt</b> gives them free, and certbot renews them automatically. First it has to "
            "check that you control the address. Choose how:</p>"))
        for i, (key, label, why) in enumerate(opts):
            rb = QtWidgets.QRadioButton(label)
            self.cert.addButton(rb, i)
            lay.addWidget(rb)
            hint = note(why)
            hint.setContentsMargins(24, 0, 0, 6)
            lay.addWidget(hint)
            if key == "cloudflare" and not has_token:
                rb.setEnabled(False)
        self.cert.button(0 if has_token else 1).setChecked(True)
        self._cert_keys = [o[0] for o in opts]
        lay.addStretch()
        wz.addPage(p)

        # 6. Foundry download (last, because the timed link expires)
        self.source = QtWidgets.QLineEdit()
        self.source.setPlaceholderText("https://… timed link, or pick a .zip file")
        pick = QtWidgets.QPushButton("Zip file…")
        pick.clicked.connect(self._pick)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.source)
        row.addWidget(pick)

        def check_source():
            s = self.source.text().strip()
            if not s:
                return "Paste the timed download link, or pick the zip."
            if not re.match(r"https?://", s) and not Path(s).is_file():
                return f"Can't find the file {s}."
            return ""
        p = CheckedPage("Foundry download", "The Node.js version of Foundry, from your account.", check_source)
        p.lay.addWidget(explain(
            "<p>Foundry is only downloadable from your account, so the tool needs a link or the file:</p><ol>"
            "<li>Sign in at <a href='https://foundryvtt.com'>foundryvtt.com</a> and open "
            "<b>Purchased Licenses</b>.</li>"
            "<li>Pick the version you want and the <b>Node.js</b> operating system.</li>"
            "<li>Click <b>Timed URL</b> to copy a download link, and paste it below. "
            "Or click <b>Download</b> and pick the zip with the button.</li></ol>"
            "<p>The timed link stops working after a few minutes, which is why this is the last question. "
            "Use the same Foundry version as your other server if your worlds should move between them.</p>"))
        form = QtWidgets.QFormLayout()
        form.addRow("Download:", row)
        p.lay.addLayout(form)
        p.lay.addStretch()
        wz.addPage(p)

        # 7. Summary
        self.summary = explain("")
        p, lay = page("Ready to create", "Check the details. Back changes them.")
        lay.addWidget(self.summary)
        lay.addStretch()
        p.initializePage = self._fill_summary
        wz.addPage(p)
        wz.setButtonText(QtWidgets.QWizard.FinishButton, "Create server")

    def _names(self, text):
        n = re.sub(r"[^A-Za-z0-9_-]", "", text) or "foundry2"
        self.app_dir.setText(f"{self.home}/{n}")
        self.data_dir.setText(f"{self.home}/{n.replace('foundry', 'foundrydata', 1) if n.startswith('foundry') else n + '-data'}")

    def _pick(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self.w, "Foundry Node.js zip", str(Path.home() / "Downloads"), "Zip files (*.zip)")
        if f:
            self.source.setText(f)

    def _fill_summary(self):
        v = self.values()
        cert = {"cloudflare": "Let's Encrypt, checked through Cloudflare DNS",
                "nginx": "Let's Encrypt, checked over HTTP", "skip": "skipped: you'll add one"}[v["cert"]]
        dns = (f"CNAME to {self.settings['existing_domain']}" + (", proxied" if v["proxied"] else ", DNS only")
               if v["dns"] else "you add it yourself")
        src = "timed link" if re.match(r"https?://", v["source"]) else v["source"]
        rows = [("Name", v["name"]), ("Address", f"https://{v['fqdn']}"), ("DNS record", dns),
                ("Port", v["port"]), ("Install folder", v["app_dir"]), ("Data folder", v["data_dir"]),
                ("Certificate", cert), ("Foundry from", src), ("Runs as", self.settings["foundry_user"])]
        self.summary.setText(
            "<table cellspacing='6'>" + "".join(f"<tr><td><b>{k}</b></td><td>{val}</td></tr>" for k, val in rows)
            + "</table><p>Create server asks for your sudo password (if it hasn't yet), then shows each step "
            "as it runs. If a step fails, it stops there and says why; the steps before it stay done.</p>"
            f"<p>Afterwards, open https://{v['fqdn']}, enter your licence key and set an admin password.</p>")

    def exec(self) -> bool:
        return bool(self.w.exec())

    def values(self) -> dict:
        return {
            "name": self.name.text().strip(), "fqdn": self.fqdn.text().strip().lower(), "port": self.port.value(),
            "app_dir": self.app_dir.text().strip(), "data_dir": self.data_dir.text().strip(),
            "source": self.source.text().strip(), "dns": self.dns.isChecked() and self.dns.isEnabled(),
            "proxied": self.proxied.isChecked(),
            "cert": self._cert_keys[self.cert.checkedId()],
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
        act("Set up web access…", "network-server", self.finish_web,
            "Certificate and nginx site for the selected server (if adding it stopped before those)")
        act("Remove…", "list-remove", self.remove_server)
        tb.addSeparator()
        act("Settings…", "configure", self.edit_settings, "Change the answers from setup")
        act("Setup guide…", "help-contents", self.run_setup, "Go through setup again, step by step")

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

        def start(at):
            dlg.retry_btn.hide()
            dlg.hint.setText("")
            w = Worker(steps, at)
            w.line.connect(dlg.add)

            def finished(ok):
                self.refresh()
                if ok:
                    dlg.add("\nDone.")
                    if done:
                        done(ok)
                    return
                label = steps[w.failed_at][0]
                dlg.add(f"\nStopped at “{label}”: see above. The steps before it are done.")
                dlg.hint.setText(f"Fix what's above, then Retry to carry on from “{label}”.")
                dlg.retry_btn.setText(f"Retry from “{label}”")
                dlg.retry_btn.show()
                dlg._retry_at = w.failed_at
            w.finished_ok.connect(finished)
            self.workers.append(w)
            w.start()

        def retry():
            if not self.runner.ensure():
                QtWidgets.QMessageBox.warning(self, APP, "This needs sudo, and the password wasn't given or was wrong.")
                return
            dlg.add("")
            start(dlg._retry_at)
        dlg.retry_btn.clicked.connect(retry)
        start(0)
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

    def run_setup(self):
        wz = SetupWizard(self, self.settings)
        if wz.exec():
            self.settings, token = wz.values()
            save_settings(self.settings)
            if token != cloudflare_token():
                save_cloudflare_token(token)
            self.refresh()

    # ---- nginx folder ------------------------------------------------------------
    def nginx_dir(self, log) -> str | None:
        """The folder nginx loads site files from (inside its http block)."""
        if self.settings.get("nginx_dir"):
            return str(Path("/etc/nginx") / self.settings["nginx_dir"])
        R = self.runner
        code, conf = R.run(["cat", "/etc/nginx/nginx.conf"], root=True)
        if code != 0:
            log("Couldn't read /etc/nginx/nginx.conf: is nginx installed?")
            return None
        dirs = site_dirs(conf)
        def available(d):
            # Sites in sites-enabled are usually links to files in sites-available.
            if d.endswith("/sites-enabled"):
                alt = d[: -len("enabled")] + "available"
                if R.run(["test", "-d", alt], root=True)[0] == 0:
                    return alt
            return d
        # Next to the existing site, if it's in one of those folders.
        if self.settings["existing_domain"] and dirs:
            dom = re.escape(self.settings["existing_domain"])
            code, out = R.run(["grep", "-rlE", f"server_name[^;]*{dom}", "/etc/nginx"], root=True)
            for f in [x.strip() for x in out.splitlines() if x.strip()]:
                parent = str(Path(f).parent)
                if parent in dirs or parent in [available(d) for d in dirs]:
                    log(f"Your existing site is in {f}; the new one goes next to it.")
                    return parent
        if dirs:
            d = available(dirs[0])
            log(f"nginx loads sites from {dirs[0]}; the new site goes in {d}.")
            return d
        # nginx.conf includes no folder of sites: add the usual one.
        log("nginx.conf doesn't load a folder of site files, so the tool adds the usual one: "
            "/etc/nginx/sites-enabled (with sites-available). A backup of nginx.conf is kept as "
            "/etc/nginx/nginx.conf.foundry-manager.bak.")
        patched = add_sites_include(conf)
        if patched is None:
            log("Couldn't find the http { block in nginx.conf: set the nginx sites folder in Settings.")
            return None
        R.run(["mkdir", "-p", "/etc/nginx/sites-available", "/etc/nginx/sites-enabled"], root=True)
        R.run(["cp", "-n", "/etc/nginx/nginx.conf", "/etc/nginx/nginx.conf.foundry-manager.bak"], root=True)
        code, out = R.write_file("/etc/nginx/nginx.conf", patched, root=True)
        if code != 0:
            log(out)
            return None
        return "/etc/nginx/sites-available"

    # ---- add a server ------------------------------------------------------------
    def add_server(self):
        dlg = AddWizard(self, self.settings, {s.port for s in self.servers}, self.home())
        if not dlg.exec():
            return
        v = dlg.values()
        if not self.check_programs(v):
            return
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
                code, out = R.run(["curl", "-fsSL", "--retry", "3", "-o", z, v["source"]], user=u, timeout=1800)
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

        steps = [
            ("Create folders", mkdirs),
            ("Install Foundry", install),
            ("Write options.json", options),
            ("Start under pm2", pm2),
        ]
        if v["dns"]:
            steps.append(("Cloudflare DNS record", dns))
        steps += self.web_steps(v)

        def done(ok):
            if ok:
                QtWidgets.QMessageBox.information(
                    self, APP,
                    f"https://{v['fqdn']} is set up.\n\nOpen it, enter your second licence key and accept the "
                    "licence agreement, then set an admin password."
                    + ("\n\nDNS can take a minute to reach everyone." if v["dns"] else ""))
        self.run_steps(f"Adding {v['name']}", steps, done)

    def check_programs(self, v) -> bool:
        """Before changing anything: are the programs the steps use installed?"""
        need = [("curl", "curl"), ("unzip", "unzip"), ("nginx", "nginx")]
        if v["cert"] != "skip":
            need.append(("certbot", "certbot"))
        missing = sorted({pkg for cmd, pkg in need if not shutil.which(cmd)})
        import importlib.util
        plugin = {"cloudflare": ("certbot_dns_cloudflare", "certbot-dns-cloudflare"),
                  "nginx": ("certbot_nginx", "certbot-nginx")}.get(v["cert"])
        if plugin and not importlib.util.find_spec(plugin[0]):
            missing.append(plugin[1])
        if not missing:
            return True
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle(APP)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText("Some programs the new server needs aren't installed:\n\n    " + ", ".join(missing)
                    + "\n\nInstall them first (in a terminal):\n\n    sudo pacman -S --needed " + " ".join(missing)
                    + "\n\nNothing has been changed yet.")
        box.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        box.setStandardButtons(QtWidgets.QMessageBox.Cancel | QtWidgets.QMessageBox.Ignore)
        box.button(QtWidgets.QMessageBox.Ignore).setText("Continue anyway")
        return box.exec() == QtWidgets.QMessageBox.Ignore

    def web_steps(self, v) -> list:
        """Certificate and nginx site for v = {name, fqdn, port, cert}."""
        R = self.runner

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
            for attempt in range(4):
                code, out = R.run(cmd, root=True, timeout=600)
                if code == 0 or "Another instance of Certbot" not in out or attempt == 3:
                    break
                log("Another certbot is running (often the automatic renewal); trying again in 30 seconds…")
                import time
                time.sleep(30)
            log(out[-1500:])
            return code == 0

        def nginx(log):
            d = self.nginx_dir(log)
            if not d:
                return False
            # Always .conf: many nginx setups only load *.conf files.
            path = f"{d}/{v['name']}.conf"
            # Never replace a site someone wrote by hand: only files with our marker.
            code, out = R.run(["grep", "-rlE", f"server_name[^;]*[[:space:]]{re.escape(v['fqdn'])}[[:space:];]",
                               "/etc/nginx"], root=True)
            for f in [x.strip() for x in out.splitlines() if x.strip()] + [path]:
                exists = R.run(["test", "-e", f], root=True)[0] == 0
                if exists and R.run(["grep", "-qF", MARKER, f], root=True)[0] != 0:
                    log(f"{f} already exists and wasn't written by this tool"
                        + (f" (it serves {v['fqdn']})" if f != path else "") + ", so it's left alone. "
                        "If that site works, there's nothing to do; otherwise move that file away and Retry.")
                    return False
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

        return [("Certificate", cert), ("nginx site", nginx)]

    def finish_web(self):
        """For a server that runs but isn't on the web yet (e.g. an add that stopped at the certificate)."""
        s = self.selected()
        if not s:
            return
        if not s.hostname:
            QtWidgets.QMessageBox.information(
                self, APP, f"{s.name} has no address in its options.json (hostname), so there's nothing to set up.")
            return
        choices = ["Through Cloudflare DNS (needs the Cloudflare token)", "Through nginx (HTTP check)",
                   "Skip the certificate (one is already in /etc/letsencrypt/live/)"]
        pick, ok = QtWidgets.QInputDialog.getItem(
            self, "Set up web access",
            f"This gets an HTTPS certificate for {s.hostname} and adds the nginx site that sends it to port "
            f"{s.port}.\nThe server itself and its DNS record aren't touched.\n\nHow should Let's Encrypt check "
            "that you own the address?", choices, 0 if cloudflare_token() else 1, False)
        if not ok:
            return
        v = {"name": s.name, "fqdn": s.hostname, "port": s.port,
             "cert": ["cloudflare", "nginx", "skip"][choices.index(pick)]}
        if not self.check_programs(v):
            return
        self.run_steps(f"Web access for {s.name}", self.web_steps(v), lambda ok: ok and QtWidgets.QMessageBox.information(
            self, APP, f"https://{s.hostname} is set up. Open it to enter the licence key."))

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
        dlg = SetupWizard(None, settings)
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
