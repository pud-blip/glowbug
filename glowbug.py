#!/usr/bin/env python3
"""Glowbug — a little desk creature that shows your Claude Code sessions.

    glowbug.py              run the daemon (LaunchAgent does this for you)
    glowbug.py install      set everything up (daemon, hooks, autostart)
    glowbug.py uninstall    remove everything cleanly
    glowbug.py status       one-line health check
    glowbug.py --version

Everything Glowbug knows stays on this Mac. There is no network code in this
file — it reads Claude Code's local session registry and hook events, and
writes to the Glowbug device over USB serial. That's it. Read it and see:
it's one file, standard library only.

https://glowbug.dev · https://github.com/pud-blip/glowbug · MIT license
"""

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import termios
import threading
import time

VERSION = "1.0.0"
NUM_SLOTS = 5
SESSION_STALE_S = 12 * 3600          # silent sessions free their slot
PING_INTERVAL_S = 1.0
REGISTRY_POLL_S = 1.5

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".glowbug")
SOCK_PATH = os.path.join(HOME, "Library", "Application Support", "Glowbug", "daemon.sock")
LOG_PATH = os.path.join(HOME, "Library", "Logs", "glowbug.log")
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", "dev.glowbug.daemon.plist")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest",
               "PostToolUse", "Stop", "StopFailure", "SessionEnd"]

# The hook script Claude Code runs on session events — written to
# ~/.glowbug/glowbug-hook.py by `install`. Embedded here so the whole
# host software is genuinely ONE file (and pipx/uvx installs work).
HOOK_SOURCE = '#!/usr/bin/env python3\n"""glowbug-hook — Claude Code hook forwarder (the entire trust surface).\n\nClaude Code runs this (async) on session events. It reads the hook\'s JSON\nfrom stdin, keeps ONLY the six fields Glowbug uses — never prompt text, never\ntool arguments, never file contents — and forwards them to the local Glowbug\ndaemon over a unix socket. No network. Fails silently in <250ms if the daemon\nisn\'t running, so it can never slow Claude Code down.\n"""\nimport json\nimport os\nimport socket\nimport sys\n\nSOCK_PATH = os.path.expanduser(\n    "~/Library/Application Support/Glowbug/daemon.sock")\n\nFIELDS = ("hook_event_name", "session_id", "session_title",\n          "cwd", "tool_name", "error_type")\n\n\ndef main():\n    raw = sys.stdin.buffer.read(65536)\n    if not raw:\n        return\n    try:\n        ev = json.loads(raw.decode(errors="replace"))\n        slim = {k: ev[k] for k in FIELDS if k in ev}\n        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n        s.settimeout(0.25)\n        s.connect(SOCK_PATH)\n        s.sendall(json.dumps(slim).encode())\n        s.close()\n    except (OSError, ValueError):\n        pass   # daemon not running / bad payload — never block Claude\n\n\nif __name__ == "__main__":\n    main()\n'

# Old CoderDong install locations (pre-rename) — migrated away by `install`.
OLD_DIR = os.path.join(HOME, ".coderdong")
OLD_PLIST = os.path.join(HOME, "Library", "LaunchAgents", "com.pudtronics.coderdong.plist")


def log(msg):
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


# ---------------------------------------------------------------- serial port
def find_port():
    """Find the Glowbug's serial port by USB product name via ioreg.

    Never 'the first /dev/cu.usbmodem*' — debug probes (ST-LINK & friends)
    also enumerate modem ports and the names are not distinguishable.
    """
    try:
        out = subprocess.run(
            ["ioreg", "-c", "IOSerialBSDClient", "-r", "-t", "-l", "-w0"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    product = None
    for line in out.splitlines():
        m = re.search(r'"USB Product Name" = "([^"]+)"', line)
        if m:
            product = m.group(1)
            continue
        m = re.search(r'"IOCalloutDevice" = "([^"]+)"', line)
        if m and product in ("Glowbug", "CoderDong"):
            return m.group(1)
    return None


# ------------------------------------------------------------------- sessions
class Session:
    def __init__(self, sid):
        self.sid = sid
        self.created = time.time()   # stable order key (registry startedAt wins)
        self.name = ""
        self.cwd = ""
        self.busy = False            # registry status
        self.hook_state = "idle"     # idle | working | waiting | unread | error
        self.detail = ""
        self.last_seen = time.time()
        self.alive = True
        self.died_at = 0.0           # set when alive flips False

    def display_state(self):
        """Merge hook state machine + registry into a protocol-v2 state.
        Registry-only sessions (hooks not installed) still get thinking/idle.
        (No unread state — user simplification 2026-08-12: a finished session
        just goes dark.)"""
        if not self.alive:
            return "closing"         # device: red 1s, fade 1s, then off
        if self.hook_state == "error":
            return "error"
        if self.hook_state == "waiting":
            return "question"
        if self.busy:
            return "thinking"
        return "idle"


def read_registry():
    """~/.claude/sessions/*.json — Claude Code's live session registry:
    names (incl. renames), busy/idle, cwd, pid liveness."""
    out = {}
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            d = json.load(open(p))
            pid = d.get("pid")
            os.kill(pid, 0)                       # process alive?
            sid = d["sessionId"]
            out[sid] = {
                "name": d.get("name") or os.path.basename(d.get("cwd", "")) or sid[:8],
                "cwd": d.get("cwd", ""),
                "busy": d.get("status") == "busy",
                "created": d.get("startedAt", 0) / 1000.0,   # ms epoch -> s
            }
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


class Daemon:
    def __init__(self):
        self.sessions = {}                 # sid -> Session
        self.slots = [None] * NUM_SLOTS    # first-come, stable (user spec)
        self.lock = threading.Lock()
        self.dirty = True

    # ---- slot policy (user spec 2026-08-12): a chronological ticker.
    # Sessions line up left->right by start time; a NEW session appears at
    # the RIGHT; with more than 5, the row shifts LEFT (oldest falls off).
    def assign_slots(self):
        now = time.time()
        # dead sessions hold their slot ~2.2s so the device can play the
        # red "Closing..." farewell before the ticker compacts
        live = [s for s in self.sessions.values()
                if (s.alive or now - s.died_at < 2.2)
                and now - s.last_seen <= SESSION_STALE_S]
        live.sort(key=lambda s: (s.created, s.sid))
        self.slots = [s.sid for s in live[-NUM_SLOTS:]]
        while len(self.slots) < NUM_SLOTS:
            self.slots.append(None)

    # ---- source 1: registry poll ----
    def poll_registry(self):
        reg = read_registry()
        with self.lock:
            changed = False
            for sid, info in reg.items():
                s = self.sessions.get(sid)
                if s is None:
                    s = self.sessions[sid] = Session(sid)
                    changed = True
                if (s.name, s.cwd, s.busy, s.alive) != (info["name"], info["cwd"], info["busy"], True):
                    changed = True
                s.name, s.cwd, s.busy, s.alive = info["name"], info["cwd"], info["busy"], True
                if info["created"]:
                    s.created = info["created"]
                s.last_seen = time.time()
            now2 = time.time()
            for sid, s in self.sessions.items():
                if sid not in reg and s.alive:
                    s.alive = False
                    s.died_at = now2
                    changed = True
                # keep pushing while any farewell window is open (so the
                # slot actually drops when the 2.2s expires)
                if not s.alive and now2 - s.died_at < 3.0:
                    changed = True
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- source 2: hook events (question/unread/error) ----
    def handle_hook(self, ev):
        name = ev.get("hook_event_name", "")
        sid = ev.get("session_id", "")
        if not sid:
            return
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                s = self.sessions[sid] = Session(sid)
                s.name = ev.get("session_title") or os.path.basename(ev.get("cwd", "")) or sid[:8]
                s.cwd = ev.get("cwd", "")
            s.last_seen = time.time()
            if name == "UserPromptSubmit":
                s.hook_state = "working"
            elif name == "PermissionRequest":
                s.hook_state = "waiting"
                s.detail = ev.get("tool_name", "")
            elif name == "PostToolUse":
                if s.hook_state == "waiting":
                    s.hook_state = "working"
            elif name == "Stop":
                s.hook_state = "idle"
                s.detail = ""
            elif name == "StopFailure":
                s.hook_state = "error"
                s.detail = ev.get("error_type", "")
            elif name == "SessionEnd":
                s.alive = False
                s.died_at = time.time()
            self.assign_slots()
            self.dirty = True

    # ---- board serial ----
    def push_state(self, fd):
        with self.lock:
            for i in range(NUM_SLOTS):
                sid = self.slots[i]
                s = self.sessions.get(sid) if sid else None
                if s:
                    st = s.display_state()
                    detail = s.detail if st in ("question", "error") else ""
                    line = "SLOT %d STATE %s NAME %s DETAIL %s" % (
                        i + 1, st, s.name[:21], detail[:21])
                else:
                    line = "SLOT %d STATE idle NAME - DETAIL " % (i + 1)
                os.write(fd, (line + "\n").encode())
            self.dirty = False

    def handle_board_line(self, line):
        parts = line.strip().split()
        if parts[:2] == ["EVT", "HELLO"]:
            log("board: hello %s" % " ".join(parts[2:]))
            self.dirty = True

    def serial_loop(self):
        buf = b""
        fd = None
        last_ping = 0.0
        last_poll = 0.0
        while True:
            if fd is None:
                port = find_port()
                if port is None:
                    time.sleep(2)
                    # keep session state fresh even while unplugged
                    if time.time() - last_poll >= REGISTRY_POLL_S:
                        self.poll_registry()
                        last_poll = time.time()
                    continue
                try:
                    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                    attrs = termios.tcgetattr(fd)
                    attrs[0] = attrs[1] = attrs[3] = 0          # raw
                    attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
                    termios.tcsetattr(fd, termios.TCSANOW, attrs)
                    log("serial: opened %s" % port)
                    self.dirty = True
                except OSError:
                    time.sleep(2)
                    continue
            try:
                now = time.time()
                if now - last_poll >= REGISTRY_POLL_S:
                    self.poll_registry()
                    last_poll = now
                if now - last_ping >= PING_INTERVAL_S:
                    os.write(fd, b"PING\n")
                    last_ping = now
                if self.dirty:
                    self.push_state(fd)
                try:
                    chunk = os.read(fd, 256)
                    if chunk:
                        buf += chunk
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            self.handle_board_line(line.decode(errors="replace"))
                except BlockingIOError:
                    pass
                time.sleep(0.05)
            except OSError:
                log("serial: lost connection, rescanning")
                try:
                    os.close(fd)
                except OSError:
                    pass
                fd = None
                buf = b""

    # ---- hook socket ----
    def socket_loop(self):
        os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
        try:
            os.unlink(SOCK_PATH)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK_PATH)
        os.chmod(SOCK_PATH, 0o600)
        srv.listen(16)
        log("glowbug %s listening on %s" % (VERSION, SOCK_PATH))
        while True:
            conn, _ = srv.accept()
            try:
                conn.settimeout(2)
                data = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    self.handle_hook(json.loads(data.decode(errors="replace")))
            except (json.JSONDecodeError, socket.timeout, ValueError) as e:
                log("bad hook payload: %s" % e)
            finally:
                conn.close()


def run_daemon():
    d = Daemon()
    t = threading.Thread(target=d.serial_loop, daemon=True)
    t.start()
    d.socket_loop()


# ------------------------------------------------------------ install / admin
PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.glowbug.daemon</string>
  <key>ProgramArguments</key>
  <array><string>/usr/bin/python3</string><string>{app}/glowbug.py</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""


def merge_hooks(settings_path, hook_cmd):
    """Idempotently add Glowbug's hook entries to Claude Code settings.
    Backup kept; aborts loudly on malformed JSON (never guesses)."""
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    settings = {}
    if os.path.exists(settings_path):
        shutil.copy(settings_path, settings_path + ".glowbug-backup")
        with open(settings_path) as f:
            settings = json.load(f)          # malformed JSON = loud abort
    hooks = settings.setdefault("hooks", {})
    added = []
    for ev in HOOK_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any(hook_cmd in json.dumps(e) for e in entries):
            continue
        entries.append({
            "matcher": "*",
            "hooks": [{"type": "command", "command": hook_cmd, "async": True}],
        })
        added.append(ev)
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    return added


def unmerge_hooks(settings_path, needle):
    """Remove any hook entry whose command mentions `needle`."""
    if not os.path.exists(settings_path):
        return 0
    with open(settings_path) as f:
        settings = json.load(f)
    removed = 0
    hooks = settings.get("hooks", {})
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if needle not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
    return removed


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install():
    src = os.path.abspath(__file__)

    # migrate an old CoderDong install if present
    if os.path.exists(OLD_PLIST) or os.path.isdir(OLD_DIR):
        print("==> Migrating old CoderDong install")
        launchctl("unload", OLD_PLIST)
        for p in (OLD_PLIST,):
            try: os.unlink(p)
            except OSError: pass
        shutil.rmtree(OLD_DIR, ignore_errors=True)
        unmerge_hooks(CLAUDE_SETTINGS, "coderdong")

    print("==> Installing Glowbug to %s" % APP_DIR)
    os.makedirs(APP_DIR, exist_ok=True)
    shutil.copy(src, os.path.join(APP_DIR, "glowbug.py"))
    with open(os.path.join(APP_DIR, "glowbug-hook.py"), "w") as f:
        f.write(HOOK_SOURCE)
    for name in ("glowbug.py", "glowbug-hook.py"):
        os.chmod(os.path.join(APP_DIR, name), 0o755)

    print("==> Registering Claude Code hooks in %s" % CLAUDE_SETTINGS)
    added = merge_hooks(CLAUDE_SETTINGS, os.path.join(APP_DIR, "glowbug-hook.py"))
    print("    added: %s" % (", ".join(added) if added else "none (already installed)"))
    print("    note: hooks apply to NEW Claude Code sessions only")

    print("==> Installing LaunchAgent")
    os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
    with open(PLIST_PATH, "w") as f:
        f.write(PLIST_TEMPLATE.format(app=APP_DIR, log=LOG_PATH))
    launchctl("unload", PLIST_PATH)
    r = launchctl("load", PLIST_PATH)
    if r.returncode != 0:
        sys.exit("glowbug: launchctl load failed: %s" % r.stderr.strip())

    # self-check
    time.sleep(1.5)
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    hooks_ok = bool(added) or os.path.getsize(CLAUDE_SETTINGS) > 0
    print()
    print("  %s daemon %s" % ("✓" if daemon_ok else "✗", "running" if daemon_ok else "NOT RUNNING"))
    print("  %s board %s" % ("✓" if port else "✗", ("connected (%s)" % port) if port else "not found — is it plugged in?"))
    print("  %s hooks installed" % ("✓" if hooks_ok else "✗"))
    print()
    print("Glowbug is %s. New Claude Code sessions will appear on the device."
          % ("ready" if (daemon_ok and port) else "partially set up"))


def uninstall():
    print("==> Stopping daemon")
    launchctl("unload", PLIST_PATH)
    for p in (PLIST_PATH,):
        try: os.unlink(p)
        except OSError: pass
    print("==> Removing Claude Code hooks")
    n = unmerge_hooks(CLAUDE_SETTINGS, "glowbug")
    print("    removed %d entries (backup: %s.glowbug-backup)" % (n, CLAUDE_SETTINGS))
    print("==> Removing %s" % APP_DIR)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    print("Glowbug uninstalled. (Log kept at %s)" % LOG_PATH)


def status():
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    print("glowbug %s · daemon %s · board %s" % (
        VERSION,
        "running" if daemon_ok else "stopped",
        port or "not found"))


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "install":
        install()
    elif arg == "uninstall":
        uninstall()
    elif arg == "status":
        status()
    elif arg in ("--version", "version"):
        print("glowbug %s" % VERSION)
    elif arg == "":
        run_daemon()
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main()
