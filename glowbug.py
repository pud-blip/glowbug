#!/usr/bin/env python3
"""Glowbug — a little desk creature that shows your coding-agent sessions.

    glowbug.py              run the daemon (LaunchAgent does this for you)
    glowbug.py install      set everything up (daemon, hooks, autostart)
    glowbug.py uninstall    remove everything cleanly
    glowbug.py rescue       reflash firmware (works even on a "bricked" board)
    glowbug.py status       one-line health check
    glowbug.py doctor       verbose health check (paths, per-tool wiring)
    glowbug.py --version

Everything Glowbug knows stays on this Mac. There is no network code in this
file — it reads your coding agents' local hook events (and Claude Code's
session registry), and writes to the Glowbug device over USB serial. That's
it. Read it and see: it's one file, standard library only.

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

VERSION = "1.4.9"
NUM_SLOTS = 5
SESSION_STALE_S = 12 * 3600          # silent sessions free their slot
PING_INTERVAL_S = 1.0
REGISTRY_POLL_S = 1.5

# Hook-only sources (everything except Claude Code, which has a live session
# registry to read) can't be polled — we only know what their hooks tell us.
# A killed IDE never sends its "stop"/"session end" event, so these two
# timeouts are what stop a dead session from owning a screen forever.
WORK_STALE_S = 300                   # silent "working" session -> idle
HOOK_SESSION_TTL_S = 2 * 3600        # silent session -> closed
DONE_S = 30.0                        # green "just finished" celebration window.
                                     # Host-owned (2026-08-16) so it SHIFTS with
                                     # the agent when the ticker compacts —
                                     # firmware used to synthesize it per-slot
                                     # and the green got left behind / cleared.
                                     # Matches firmware DONE_FLASH_MS (30s).
AUTOWIRE_POLL_S = 300                # look for newly-installed coding agents

# Ghost-buster (user directive 2026-08-16: NEVER show agents that don't
# exist). A hard-closed IDE never sends its sessionEnd hook, and before this
# its sessions squatted a screen until the 2h TTL. So we ask the OS: if a
# source's application has no running process AT ALL, every session it owns
# is provably dead — reaped within seconds instead of hours. (An app that's
# still open with a quietly-abandoned session inside is a different case;
# that one still falls to the idle/TTL timers above.)
APP_PROBE_S = 3.0                    # how often to scan the process table
APP_GONE_S = 10.0                    # app unseen this long -> sessions dead

HOME = os.path.expanduser("~")
APP_DIR = os.path.join(HOME, ".glowbug")
SOCK_PATH = os.path.join(HOME, "Library", "Application Support", "Glowbug", "daemon.sock")
SESS_STATE = os.path.join(HOME, "Library", "Application Support", "Glowbug", "sessions.json")
LOG_PATH = os.path.join(HOME, "Library", "Logs", "glowbug.log")
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", "dev.glowbug.daemon.plist")
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
SESSIONS_DIR = os.path.join(HOME, ".claude", "sessions")

HOOK_EVENTS = ["SessionStart", "UserPromptSubmit", "PermissionRequest",
               "PostToolUse", "Stop", "StopFailure", "SessionEnd"]

# Where each tool keeps its config. We only ever write into a directory that
# already exists — Glowbug never creates config for a tool you don't have.
CURSOR_HOOKS = os.path.join(HOME, ".cursor", "hooks.json")
CODEX_HOME = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
CODEX_HOOKS = os.path.join(CODEX_HOME, "hooks.json")
CODEX_CONFIG = os.path.join(CODEX_HOME, "config.toml")
# Antigravity's global config dir has moved around; we write into whichever
# one already exists and never create one.
ANTIGRAVITY_DIRS = [os.path.join(HOME, ".gemini", "config"),
                    os.path.join(HOME, ".gemini", "antigravity-cli"),
                    os.path.join(HOME, ".gemini", "antigravity")]

# Which of each tool's events we subscribe to. ONLY observational ones: several
# tools let a hook veto the action it is reporting, and Glowbug must never be
# able to block your agent, so the "before/pre" families are deliberately
# absent. The cost is that "thinking" starts at the first tool call rather
# than at prompt submit.
CURSOR_EVENTS = ["sessionStart", "afterAgentThought", "postToolUse",
                 "afterShellExecution", "afterFileEdit", "afterAgentResponse",
                 "stop", "sessionEnd"]
# Codex runs these in the background (async), so they never sit in the way of
# a tool call. PermissionRequest is what lights the "waiting on you" screen.
CODEX_EVENTS = ["SessionStart", "UserPromptSubmit", "PostToolUse",
                "PermissionRequest", "Stop", "SessionEnd"]
# Antigravity: only its "after the fact" events. PreToolUse/PreInvocation are
# decision points that can deny a tool call — we don't go near them.
ANTIGRAVITY_EVENTS = ["PostToolUse", "PostInvocation", "Stop"]

# Every source drives the same tiny state machine; only the event names differ.
# An event that isn't listed here still counts as activity (keeps the session
# "thinking"), so a tool adding new events can't break us.
EVENT_MAPS = {
    "cursor": {
        "permission": (),                    # no observational approval event
        "stop": ("stop",),
        "end": ("sessionEnd",),
        # A bare sessionStart may NOT create a session: Cursor fires one for
        # a freshly-opened chat pane that has no conversation yet (bench
        # 2026-08-16: sid "empty-state…", no further events, no sessionEnd —
        # a permanent ghost screen). A real session births on its first
        # actual agent event, seconds later.
        "no_birth": ("sessionStart",),
    },
    "codex": {
        "permission": ("PermissionRequest",),
        "stop": ("Stop", "agent-turn-complete"),   # hooks, and legacy notify
        "end": ("SessionEnd",),
    },
    "antigravity": {
        "permission": (),                    # no local approval event
        "stop": ("Stop",),                   # carries fullyIdle -> our "idle"
        "end": (),                           # no session-end event: TTL only
    },
}

ERRORISH = ("error", "failed", "failure", "crash")

# What proves each hook-only source's app is actually running, for the
# ghost-buster. Matched against `ps -axo args=` two ways: an .app-bundle
# substring (the IDE) or the exact basename of the command (its CLI).
# A source with NO entry here cannot be ghost-reaped (only the TTL saves
# it) — always add a probe when adding a source.
SOURCE_PROBES = {
    "cursor":      {"substr": ("Cursor.app",),      "basenames": ("cursor", "cursor-agent")},
    "codex":       {"substr": (),                    "basenames": ("codex",)},
    "antigravity": {"substr": ("Antigravity.app",),  "basenames": ("antigravity",)},
}


def running_sources():
    """The set of hook-only sources whose app/CLI is running right now.
    Returns None when the probe itself failed — callers must treat that as
    'no evidence either way' and reap nothing (fail open, never fabricate
    a death)."""
    try:
        out = subprocess.run(["ps", "-axo", "args="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    seen = set()
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        first = os.path.basename(line.split()[0])
        for src, probe in SOURCE_PROBES.items():
            if src not in seen and (first in probe["basenames"]
                                    or any(sub in line for sub in probe["substr"])):
                seen.add(src)
    return seen

# The hook script your coding agents run on session events — written to
# ~/.glowbug/glowbug-hook.py by `install`. Embedded here so the whole host
# software is genuinely ONE file (and pipx/uvx installs work). Read it: it is
# the entire trust surface, and it is one screen of code.
FORWARDER_SOURCE = '''#!/usr/bin/env python3
# glowbug-hook -- the entire trust surface.
#
# Your coding agent runs this on session events. It reads the tool's JSON from
# stdin, keeps ONLY the handful of fields Glowbug uses -- never prompt text,
# never tool arguments, never file contents -- and forwards them to the local
# Glowbug daemon over a unix socket. There is no network code here.
#
# It can never interfere with your agent: every failure path is swallowed, it
# gives up on the socket after 250ms, and it always exits 0 (some tools treat
# a non-zero exit as "block this action").
#
# Usage: glowbug-hook.py [--source NAME] [--event NAME] [--argv-json]
#        no arguments  ==  --source claude   (keeps older installs working)
import json
import os
import socket
import sys

SOCK_PATH = os.path.expanduser(
    "~/Library/Application Support/Glowbug/daemon.sock")

# canonical field  ->  the names the different tools use for it.
# Anything not listed here NEVER leaves this process: prompt text, tool
# arguments, file contents, transcript paths, model names, free-text errors.
ALIASES = (
    ("session_id",      ("session_id", "conversation_id", "conversationId",
                         "sessionId", "thread-id", "threadId")),
    ("session_title",   ("session_title", "title", "conversationTitle")),
    ("cwd",             ("cwd", "workspace_roots", "workspacePaths", "workspace_root")),
    ("tool_name",       ("tool_name", "toolName", "tool")),
    ("error_type",      ("error_type", "status", "terminationReason", "termination_reason")),
    ("hook_event_name", ("hook_event_name", "hookEventName", "eventName")),
)


def clean(v, limit):
    """One short, single-line string, or nothing at all."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else ""
    if not isinstance(v, str):
        return ""
    return " ".join(v.split())[:limit]


def normalize(ev, source, event):
    slim = {"source": source}
    for canon, keys in ALIASES:
        for k in keys:
            if k in ev:
                val = clean(ev[k], 64 if canon == "error_type" else 256)
                if val:
                    slim[canon] = val
                break
    if not slim.get("hook_event_name") and event:
        slim["hook_event_name"] = event
    if isinstance(ev.get("fullyIdle"), bool):
        slim["idle"] = ev["fullyIdle"]     # Antigravity's turn-is-over flag
    return slim


ARGS = sys.argv[1:]


def arg(flag, default=""):
    for i, a in enumerate(ARGS):
        if a == flag and i + 1 < len(ARGS):
            return ARGS[i + 1]
    return default


SOURCE = arg("--source", "claude")


def main():
    args = ARGS
    source, event = SOURCE, arg("--event")
    argv_json = "--argv-json" in args
    raw = b""
    if not argv_json:                      # --argv-json: stdin may be a tty
        raw = sys.stdin.buffer.read(65536)
    if not raw and args and args[-1].lstrip().startswith("{"):
        raw = args[-1].encode()            # some tools pass JSON as an argument
    if not raw:
        return
    ev = json.loads(raw.decode(errors="replace"))
    if not isinstance(ev, dict):
        return
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.25)
    s.connect(SOCK_PATH)
    s.sendall(json.dumps(normalize(ev, source, event)).encode())
    s.close()


try:
    main()
except BaseException:
    pass          # never, for any reason, interfere with the agent
if SOURCE != "claude":
    # An empty object means "no opinion" to the tools that read hook output.
    # Written even if main() blew up, so a bug in here can't look like a
    # malformed response. Claude Code gets silence, exactly as it always has.
    sys.stdout.write("{}")
sys.exit(0)
'''

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
    def __init__(self, sid, source="claude"):
        self.sid = sid
        self.source = source         # which coding agent this session belongs to
        self.key = (source, sid)     # session ids are only unique per source
        self.activity_at = time.time()   # last hook event of any kind
        self.created = time.time()   # stable order key (registry startedAt wins)
        self.name = ""
        self.cwd = ""
        self.is_subagent = False     # entrypoint == "sdk-cli" (Agent tool /
                                      # `claude -p`), not a session the user
                                      # opened themselves — labeled "(sub)"
                                      # on the device (user report 2026-08-16)
        self.busy = False            # registry status == "busy"
        self.reg_waiting = False     # registry status == "waiting" (dialog open)
        self.hook_state = "idle"     # idle | working | waiting | error
        self.waiting_at = 0.0        # when a hook last raised "waiting"
        self.done_at = 0.0           # when the agent last finished a turn —
                                     # drives the green DONE_S celebration
        self.detail = ""
        self.last_seen = time.time()
        self.alive = True
        self.born_at = time.time()   # arrival flutter window
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
        # "Claude is waiting on you" — the REGISTRY is authoritative here.
        # No hook fires when the user dismisses a dialog or interrupts
        # (docs: Stop "doesn't fire on user interrupts"), but Claude Code's
        # session file flips status busy -> waiting -> idle, so escaping
        # clears within one poll. The hook still gives instant onset (and the
        # tool name) before the registry catches up. Bench-proven 2026-08-12.
        # question (AskUserQuestion dialog) vs permission (a gated tool) —
        # the PermissionRequest hook's tool_name is the discriminator.
        if self.reg_waiting or (self.hook_state == "waiting"
                                and time.time() - self.waiting_at < 3.0):
            if self.detail and self.detail != "AskUserQuestion":
                return "permission"
            return "question"
        if time.time() - self.born_at < 1.5:
            return "arriving"        # device: firefly flutter-in + hello chirp
        if self.busy:
            return "thinking"
        # Sources without a session registry (everything but Claude Code) have
        # only their hooks to go on: a turn is "working" from the first event
        # until the tool says it stopped. reap_stale() is the safety net for a
        # session that dies without ever sending that stop event.
        if self.source != "claude" and self.hook_state == "working" \
                and time.time() - self.activity_at < WORK_STALE_S:
            return "thinking"
        if time.time() - self.done_at < DONE_S:
            return "done"            # green celebration — travels with the
                                     # session across ticker shifts
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
                # entrypoint "sdk-cli" = launched via the Agent tool /
                # `claude -p`, not a session the user opened by hand.
                # "cli" (or absent) = a real interactive session.
                "is_subagent": d.get("entrypoint") == "sdk-cli",
                "busy": d.get("status") == "busy",
                "waiting": d.get("status") == "waiting",
                "created": d.get("startedAt", 0) / 1000.0,   # ms epoch -> s
            }
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return out


class Daemon:
    def __init__(self):
        self.sessions = {}                 # (source, sid) -> Session
        self.slots = [None] * NUM_SLOTS    # first-come, stable (user spec)
        self.lock = threading.Lock()
        self.dirty = True
        self.last_event_at = {}            # source -> when we last heard from it
        self.app_seen_at = {}              # source -> ghost-buster last saw its app
        self.last_probe = 0.0              # last process-table scan
        self.last_wire_check = 0.0         # auto-wire timer (see maybe_wire)
        self._persist_cache = None         # last sessions.json blob written
        self.load_sessions()               # reattach to agents that were live
                                           # when the previous daemon exited

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
        live.sort(key=lambda s: (s.created, s.source, s.sid))
        self.slots = [s.key for s in live[-NUM_SLOTS:]]
        while len(self.slots) < NUM_SLOTS:
            self.slots.append(None)

    # ---- called on every poll tick: refresh Claude, retire the stale ----
    def tick(self):
        self.poll_claude_registry()
        self.reap_stale()
        self.maybe_wire()
        self.persist_sessions()

    # ---- hook-only sessions survive daemon restarts ----
    # Claude Code rebuilds from its live registry, but Cursor/Codex/
    # Antigravity exist only in this process's memory — so a daemon restart
    # (an UPDATE!) made a mid-turn agent invisible until its next hook event,
    # possibly forever if its turn ended during the restart window (user
    # report 2026-08-16). The session table is mirrored to disk and reloaded
    # at startup; the ghost-buster and idle/TTL timers then re-verify
    # everything restored, so a stale restore self-corrects in seconds.
    def persist_sessions(self):
        with self.lock:
            data = [{"source": s.source, "sid": s.sid, "name": s.name,
                     "cwd": s.cwd, "hook_state": s.hook_state,
                     "waiting_at": s.waiting_at, "detail": s.detail,
                     "created": s.created, "activity_at": s.activity_at,
                     "done_at": s.done_at}
                    for s in self.sessions.values()
                    if s.alive and s.source != "claude"]
        blob = json.dumps(data, sort_keys=True)
        if blob == self._persist_cache:
            return
        self._persist_cache = blob
        try:
            os.makedirs(os.path.dirname(SESS_STATE), exist_ok=True)
            tmp = SESS_STATE + ".tmp"
            with open(tmp, "w") as f:
                f.write(blob)
            os.replace(tmp, SESS_STATE)
        except OSError:
            pass                        # persistence is best-effort

    def load_sessions(self):
        try:
            saved = json.load(open(SESS_STATE))
        except (OSError, ValueError):
            return
        now = time.time()
        n = 0
        for d in saved:
            try:
                if d["source"] == "claude" or \
                        now - d["activity_at"] > HOOK_SESSION_TTL_S:
                    continue
                s = Session(d["sid"], d["source"])
                s.name = d.get("name") or d["sid"][:8]
                s.cwd = d.get("cwd", "")
                s.hook_state = d.get("hook_state", "idle")
                s.waiting_at = d.get("waiting_at", 0.0)
                s.done_at = d.get("done_at", 0.0)
                s.detail = d.get("detail", "")
                s.created = d.get("created", now)
                s.activity_at = s.last_seen = d["activity_at"]
                s.born_at = 0.0         # no arrival-flutter replay on restore
                self.sessions[s.key] = s
                n += 1
            except (KeyError, TypeError):
                continue
        if n:
            log("restored %d hook session(s) from %s" % (n, SESS_STATE))
            self.assign_slots()
            self.dirty = True

    def maybe_wire(self):
        """Install a coding agent after Glowbug and it connects itself.
        Only ever adds hooks to a tool that is actually installed, only once
        per tool (delete our hook and it stays deleted), and can never be
        fatal to the daemon."""
        now = time.time()
        if now - self.last_wire_check < AUTOWIRE_POLL_S:
            return
        self.last_wire_check = now
        try:
            for name, label, ok, added, note in wire_sources():
                if ok and added:
                    log("wired %s (%s) — new sessions will appear"
                        % (label, ", ".join(added)))
        except Exception as e:
            log("auto-wire skipped: %s" % e)

    # ---- Claude Code: live session registry ----
    def poll_claude_registry(self):
        reg = read_registry()
        with self.lock:
            changed = False
            for sid, info in reg.items():
                s = self.sessions.get(("claude", sid))
                if s is None:
                    s = self.sessions[("claude", sid)] = Session(sid, "claude")
                    changed = True
                if (s.name, s.cwd, s.busy, s.reg_waiting, s.alive, s.is_subagent) != (
                        info["name"], info["cwd"], info["busy"], info["waiting"], True,
                        info["is_subagent"]):
                    changed = True
                # "agent got back": busy (or waiting-on-you) -> plain idle
                # starts the green celebration window
                if (s.busy or s.reg_waiting) and \
                        not info["busy"] and not info["waiting"]:
                    s.done_at = time.time()
                s.name, s.cwd, s.busy, s.alive = info["name"], info["cwd"], info["busy"], True
                s.reg_waiting = info["waiting"]
                s.is_subagent = info["is_subagent"]
                if s.hook_state == "waiting" and not info["waiting"] and \
                        time.time() - s.waiting_at >= 3.0:
                    s.hook_state = "idle"        # dialog gone: dismissed or answered
                    s.detail = ""
                    changed = True
                if info["created"]:
                    s.created = info["created"]
                s.last_seen = time.time()
            now2 = time.time()
            purge = []
            for key, s in self.sessions.items():
                # "gone from the registry" only means dead for Claude Code —
                # every other source is hook-driven and owns its own liveness
                # (reap_stale below). Without this guard, sessions from other
                # tools would be killed 1.5s after they appeared.
                if s.source == "claude" and s.sid not in reg and s.alive:
                    s.alive = False
                    s.died_at = now2
                    changed = True
                # keep pushing while any farewell/arrival window is open (so
                # transient states resolve without another event). This window
                # must comfortably outlast the 2.2s farewell slot-hold in
                # assign_slots AND the 1.5s poll spacing — at the old 3.0s,
                # a poll only landed in the (2.2, 3.0) gap about half the
                # time, so the ticker often didn't compact until some
                # unrelated state change forced a push (user report
                # 2026-08-16: agents stayed put after a middle one closed).
                if not s.alive and now2 - s.died_at < 6.0:
                    changed = True
                # and eventually forget the dead entirely
                if not s.alive and now2 - s.died_at > 30.0:
                    purge.append(key)
            for key in purge:
                del self.sessions[key]
                if s.alive and now2 - s.born_at < 2.5:
                    changed = True
                if s.hook_state == "waiting" and now2 - s.waiting_at < 4.0:
                    changed = True        # keep pushing across the handoff
                if s.done_at and now2 - s.done_at < DONE_S + 3.0:
                    changed = True        # keep pushing through the green
                                          # window AND its expiry back to idle
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- hook-only sources: retire what has gone quiet ----
    def reap_stale(self):
        """A killed IDE never sends its stop event. Without this a dead
        session would hold a screen forever, thinking away. Two layers:
        the ghost-buster (app process gone -> sessions dead in ~10s) and
        the idle/TTL timers (app open but the session went quiet)."""
        now = time.time()
        changed = False
        if now - self.last_probe >= APP_PROBE_S:
            self.last_probe = now
            seen = running_sources()
            if seen is not None:
                for src in seen:
                    self.app_seen_at[src] = now
        with self.lock:
            for s in self.sessions.values():
                if s.source == "claude" or not s.alive:
                    continue           # Claude's liveness comes from the registry
                if s.source in SOURCE_PROBES:
                    # freshest proof-of-life: the probe saw the app, or a
                    # hook arrived (a hook can only come from a live app)
                    evidence = max(self.app_seen_at.get(s.source, 0.0),
                                   self.last_event_at.get(s.source, 0.0))
                    if evidence and now - evidence > APP_GONE_S:
                        log("reap: %s app gone — closing '%s'" % (s.source, s.name))
                        s.alive = False
                        s.died_at = now
                        changed = True
                        continue
                if s.hook_state == "working" and now - s.activity_at > WORK_STALE_S:
                    s.hook_state = "idle"
                    s.detail = ""
                    changed = True
                if now - s.activity_at > HOOK_SESSION_TTL_S:
                    s.alive = False
                    s.died_at = now
                    changed = True
            if changed:
                self.assign_slots()
                self.dirty = True

    # ---- hook events, from any source ----
    def handle_hook(self, ev):
        source = ev.get("source") or "claude"
        name = ev.get("hook_event_name", "")
        sid = ev.get("session_id", "")
        log("hook[%s]: %s sid=%s tool=%s" % (
            source, name, sid[:8], ev.get("tool_name", "-")))
        self.last_event_at[source] = time.time()
        if not sid:
            return
        if source == "claude":
            self._hook_claude(ev, name, sid)
        elif source in EVENT_MAPS:
            self._hook_generic(ev, source, name, sid)

    def _hook_generic(self, ev, source, name, sid):
        """Hook-only sources: no registry to consult, so the events ARE the
        state. Anything unrecognised counts as activity, never as an error."""
        m = EVENT_MAPS[source]
        # Placeholder ids are not sessions (ghost rule, user directive
        # 2026-08-16: never show agents that don't exist). Cursor's empty
        # chat pane announces itself as sid "empty-state…"; treat any
        # obviously-non-conversation id the same way, from any source.
        low = sid.lower()
        if low.startswith("empty") or low in ("unknown", "none", "null",
                                              "undefined", "new"):
            return
        now = time.time()
        with self.lock:
            s = self.sessions.get((source, sid))
            if s is None:
                if name in m.get("no_birth", ()):
                    return           # birth only on real agent activity
                s = self.sessions[(source, sid)] = Session(sid, source)
                s.name = (ev.get("session_title")
                          or os.path.basename(ev.get("cwd", "")) or sid[:8])
                s.cwd = ev.get("cwd", "")
            elif not s.alive and name not in m["end"]:
                s.alive = True          # it's back: flutter in again
                s.born_at = now
            if ev.get("session_title"):
                s.name = ev["session_title"]
            elif ev.get("cwd") and (not s.name or s.name == s.sid[:8]):
                # Cursor never sends a chat title (its hook payloads have no
                # title field at all — docs 2026-08-16), and workspace_roots
                # only rides along on SOME events; upgrade a hex-id name to
                # the project folder as soon as any event carries it.
                s.name = os.path.basename(ev["cwd"]) or s.name
            if ev.get("cwd") and not s.cwd:
                s.cwd = ev["cwd"]
            s.last_seen = s.activity_at = now

            err = ev.get("error_type", "")
            if name in m["end"]:
                s.alive = False
                s.died_at = now
            elif name in m["permission"]:
                s.hook_state = "waiting"
                s.waiting_at = now
                s.detail = ev.get("tool_name", "")
            elif name in m["stop"]:
                if ev.get("idle") is False:
                    s.hook_state = "working"        # turn isn't over yet
                elif any(w in err.lower() for w in ERRORISH):
                    s.hook_state = "error"
                    s.detail = err
                else:
                    if s.hook_state in ("working", "waiting"):
                        s.done_at = now         # turn over -> green celebration
                    s.hook_state = "idle"
                    s.detail = ""
            else:
                s.hook_state = "working"            # any activity = a live turn
                s.detail = ""
            self.assign_slots()
            self.dirty = True

    def _hook_claude(self, ev, name, sid):
        with self.lock:
            s = self.sessions.get(("claude", sid))
            if s is None:
                s = self.sessions[("claude", sid)] = Session(sid, "claude")
                s.name = ev.get("session_title") or os.path.basename(ev.get("cwd", "")) or sid[:8]
                s.cwd = ev.get("cwd", "")
            s.last_seen = s.activity_at = time.time()
            if name == "UserPromptSubmit":
                s.hook_state = "working"
            elif name == "PermissionRequest":
                s.hook_state = "waiting"
                s.waiting_at = time.time()
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
        debug = os.environ.get("GLOWBUG_DEBUG_SLOTS")
        with self.lock:
            for i in range(NUM_SLOTS):
                key = self.slots[i]
                s = self.sessions.get(key) if key else None
                if s:
                    st = s.display_state()
                    detail = s.detail if st in ("permission", "error") else ""
                    # SID = which session occupies the slot, so firmware can
                    # tell a ticker shift (different agent moved in) from a
                    # state change of the same agent and skip transition
                    # effects (Done! ding, chimes) on shifts.
                    sid8 = (s.sid or "-").replace(" ", "")[:8] or "-"
                    line = "SLOT %d STATE %s NAME %s DETAIL %s SUB %d SID %s" % (
                        i + 1, st, s.name[:21], detail[:21],
                        1 if s.is_subagent else 0, sid8)
                else:
                    line = "SLOT %d STATE idle NAME - DETAIL  SUB 0 SID -" % (i + 1)
                if debug:
                    log("slot: %s" % line)   # bench testing without a device
                if fd is not None:
                    os.write(fd, (line + "\n").encode())
            self.dirty = False

    def report(self):
        """What `glowbug status` / `doctor` ask the daemon over the socket."""
        now = time.time()
        with self.lock:
            sess = [{"source": s.source, "name": s.name,
                     "state": s.display_state()}
                    for k in self.slots if k for s in [self.sessions.get(k)] if s]
        return {"version": VERSION, "sessions": sess,
                "last_event_at": {k: round(now - v, 1)
                                  for k, v in self.last_event_at.items()}}

    def handle_board_line(self, line):
        parts = line.strip().split()
        if parts[:2] == ["EVT", "HELLO"]:
            log("board: hello %s" % " ".join(parts[2:]))
            self.dirty = True

    def serial_loop(self):
        # macOS sleep/wake gotcha (found 2026-08-16): after a lid-close the
        # CDC port re-enumerates while the OLD fd is left silently dead —
        # os.write()/os.read() on it don't raise, they just go nowhere, so
        # the `except OSError` reconnect below never fires and the board
        # sits in "Offline" until a manual replug. Worse (bench-observed
        # same day): the port usually comes back under the SAME /dev path,
        # so comparing paths alone doesn't catch it either. Three
        # detections, all needed:
        #   1. path change  — find_port() re-run every PORT_RECHECK_S,
        #      reconnect when it disagrees with the fd we hold;
        #   2. node identity — same path, but the device node was torn down
        #      and recreated: its inode changes, so os.stat(port) vs
        #      os.fstat(fd) disagree;
        #   3. time jump    — this loop runs every 50ms; an iteration gap
        #      >5s means the Mac slept. Reconnect unconditionally — after
        #      any sleep the fd is suspect, and a spurious reopen is
        #      harmless (one log line + a state re-push).
        PORT_RECHECK_S = 2.0
        SLEEP_GAP_S = 5.0
        buf = b""
        fd = None
        port = None
        last_ping = 0.0
        last_poll = 0.0
        last_recheck = 0.0
        last_loop = 0.0

        def close_fd():
            nonlocal fd, buf
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            fd = None
            buf = b""

        while True:
            now = time.time()
            if last_loop and now - last_loop > SLEEP_GAP_S:
                log("serial: %.0fs time jump (slept?), reconnecting" % (now - last_loop))
                close_fd()
                last_recheck = 0.0          # re-scan the port immediately
            last_loop = now
            if now - last_recheck >= PORT_RECHECK_S:
                last_recheck = now
                current = find_port()
                if fd is not None and current != port:
                    log("serial: port changed (%s -> %s), reconnecting" % (port, current))
                    close_fd()
                elif fd is not None and current is not None:
                    try:                    # same path — but same NODE?
                        if os.stat(current).st_ino != os.fstat(fd).st_ino:
                            log("serial: device node recreated, reconnecting")
                            close_fd()
                    except OSError:
                        close_fd()
                port = current

            if fd is None:
                if port is None:
                    time.sleep(0.2)
                    # keep session state fresh even while unplugged
                    if time.time() - last_poll >= REGISTRY_POLL_S:
                        self.tick()
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
                    fd = None
                    time.sleep(0.2)
                    continue
            try:
                if now - last_poll >= REGISTRY_POLL_S:
                    self.tick()
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
                close_fd()

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
                    msg = json.loads(data.decode(errors="replace"))
                    if isinstance(msg, dict) and "cmd" in msg:
                        conn.sendall(json.dumps(self.report()).encode())
                    else:
                        self.handle_hook(msg)
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


# ------------------------------------------------- wiring up the other tools
# Rules, in order of importance:
#   1. only ever ADD entries; never rewrite or reorder what's already there
#   2. back up before every write
#   3. if a config file doesn't parse, leave it alone and say so — never guess
#   4. never create a config directory for a tool that isn't installed
#   5. wire each tool once; if you delete our hook, it stays deleted

def hook_cmd(source, event=""):
    """How a tool should invoke our forwarder. Explicit interpreter: apps
    launched from the Finder have a minimal PATH, so a shebang is a coin flip."""
    py = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
    cmd = '%s -S -E "%s" --source %s' % (
        py, os.path.join(APP_DIR, "glowbug-hook.py"), source)
    return cmd + (" --event %s" % event if event else "")


def read_json_config(path):
    """{} if absent, the parsed config if readable, None if we must not touch it."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_config(path, data):
    if os.path.exists(path):
        shutil.copy(path, path + ".glowbug-backup")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_state():
    try:
        with open(os.path.join(APP_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        with open(os.path.join(APP_DIR, "state.json"), "w") as f:
            json.dump(st, f, indent=2)
    except OSError:
        pass


# ---- Claude Code ----
def has_claude():
    return True          # always wired: settings.json is created if absent,
                         # so installing Claude Code later just works


def install_claude():
    added = merge_hooks(CLAUDE_SETTINGS, os.path.join(APP_DIR, "glowbug-hook.py"))
    return True, added, ""


def uninstall_claude():
    return unmerge_hooks(CLAUDE_SETTINGS, "glowbug")


# ---- Cursor ----
def has_cursor():
    return (os.path.isdir(os.path.join(HOME, ".cursor"))
            or os.path.exists("/Applications/Cursor.app")
            or bool(shutil.which("cursor-agent")))


def install_cursor():
    if not os.path.isdir(os.path.dirname(CURSOR_HOOKS)):
        return False, [], "no ~/.cursor directory — is Cursor installed?"
    cfg = read_json_config(CURSOR_HOOKS)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % CURSOR_HOOKS
    cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    added = []
    for ev in CURSOR_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any("glowbug" in json.dumps(e) for e in entries):
            continue
        entries.append({"command": hook_cmd("cursor", ev), "timeout": 5})
        added.append(ev)
    if added:
        write_json_config(CURSOR_HOOKS, cfg)
    return True, added, ""


def uninstall_cursor():
    cfg = read_json_config(CURSOR_HOOKS)
    if not cfg:
        return 0
    hooks = cfg.get("hooks", {})
    removed = 0
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if "glowbug" not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    if removed:
        write_json_config(CURSOR_HOOKS, cfg)
    return removed


# ---- Codex ----
CODEX_TOML_NOTE = (
    "Codex needs one setting turned on. Add these two lines to %s:\n"
    "        [features]\n"
    "        hooks = true\n"
    "      (Glowbug doesn't edit that file — it's yours.)" % CODEX_CONFIG)


def has_codex():
    return os.path.isdir(CODEX_HOME) or bool(shutil.which("codex"))


def codex_hooks_enabled():
    """Read-only peek at config.toml — we never write to it."""
    try:
        with open(CODEX_CONFIG) as f:
            txt = f.read()
    except OSError:
        return False
    return bool(re.search(r"^\s*hooks\s*=\s*true", txt, re.M) or
                re.search(r"^\s*features\s*\.\s*hooks\s*=\s*true", txt, re.M))


def install_codex():
    if not os.path.isdir(CODEX_HOME):
        return False, [], "no %s directory — is Codex installed?" % CODEX_HOME
    cfg = read_json_config(CODEX_HOOKS)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % CODEX_HOOKS
    hooks = cfg.setdefault("hooks", {})
    added = []
    for ev in CODEX_EVENTS:
        entries = hooks.setdefault(ev, [])
        if any("glowbug" in json.dumps(e) for e in entries):
            continue
        entries.append({"hooks": [{"type": "command",
                                   "command": hook_cmd("codex", ev),
                                   "timeout": 5, "async": True}]})
        added.append(ev)
    if added:
        write_json_config(CODEX_HOOKS, cfg)
    return True, added, ("" if codex_hooks_enabled() else CODEX_TOML_NOTE)


def uninstall_codex():
    cfg = read_json_config(CODEX_HOOKS)
    if not cfg:
        return 0
    hooks = cfg.get("hooks", {})
    removed = 0
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if "glowbug" not in json.dumps(e)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]
    if removed:
        write_json_config(CODEX_HOOKS, cfg)
    return removed


# ---- Antigravity ----
def antigravity_dir():
    for d in ANTIGRAVITY_DIRS:
        if os.path.isdir(d):
            return d
    return None


def has_antigravity():
    return (antigravity_dir() is not None
            or os.path.exists("/Applications/Antigravity.app")
            or bool(shutil.which("agy")))


def install_antigravity():
    d = antigravity_dir()
    if d is None:
        return False, [], ("no Antigravity config directory found (looked in %s)"
                           % ", ".join(ANTIGRAVITY_DIRS))
    path = os.path.join(d, "hooks.json")
    cfg = read_json_config(path)
    if cfg is None:
        return False, [], "%s isn't valid JSON — left untouched" % path
    entry = {"enabled": True}
    for ev in ANTIGRAVITY_EVENTS:
        entry[ev] = [{"matcher": "*",
                      "hooks": [{"type": "command",
                                 "command": hook_cmd("antigravity", ev),
                                 "timeout": 5}]}]
    if cfg.get("glowbug") == entry:
        return True, [], ""
    cfg["glowbug"] = entry             # one key of ours; everything else untouched
    write_json_config(path, cfg)
    return True, list(ANTIGRAVITY_EVENTS), ""


def uninstall_antigravity():
    removed = 0
    for d in ANTIGRAVITY_DIRS:
        path = os.path.join(d, "hooks.json")
        cfg = read_json_config(path)
        if cfg and "glowbug" in cfg:
            del cfg["glowbug"]
            write_json_config(path, cfg)
            removed += 1
    return removed


SOURCES = [
    ("claude",      "Claude Code", has_claude,      install_claude,      uninstall_claude),
    ("cursor",      "Cursor",      has_cursor,      install_cursor,      uninstall_cursor),
    ("codex",       "Codex",       has_codex,       install_codex,       uninstall_codex),
    ("antigravity", "Antigravity", has_antigravity, install_antigravity, uninstall_antigravity),
]


def wire_sources(explicit=False):
    """Register hooks with every detected tool. Returns a list of
    (name, label, ok, added, note) for the caller to print or log."""
    out = []
    st = load_state()
    wired = st.setdefault("wired", {})
    for name, label, detect, do_install, _ in SOURCES:
        try:
            if not detect():
                out.append((name, label, None, [], "not detected"))
                continue
            if not explicit and name in wired:
                continue          # already done once; respect manual removal
            ok, added, note = do_install()
            if ok:
                wired[name] = {"at": int(time.time()), "events": added or wired.get(name, {}).get("events", [])}
            out.append((name, label, ok, added, note))
        except Exception as e:            # a broken tool config is never fatal
            out.append((name, label, False, [], str(e)))
    save_state(st)
    return out


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
    dst = os.path.join(APP_DIR, "glowbug.py")
    # re-running install FROM the installed copy is a supported way to
    # refresh hooks/forwarder — skip the self-copy instead of crashing
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copy(src, dst)
    fw_src = os.path.join(os.path.dirname(src), "firmware")
    if os.path.exists(os.path.join(fw_src, "glowbug.bin")):
        shutil.copy(os.path.join(fw_src, "glowbug.bin"),
                    os.path.join(APP_DIR, "firmware.bin"))
        for extra in ("VERSION", "SHA256SUMS"):
            if os.path.exists(os.path.join(fw_src, extra)):
                shutil.copy(os.path.join(fw_src, extra),
                            os.path.join(APP_DIR, extra))
        print("    rescue firmware image installed")
    with open(os.path.join(APP_DIR, "glowbug-hook.py"), "w") as f:
        f.write(FORWARDER_SOURCE)
    for name in ("glowbug.py", "glowbug-hook.py"):
        os.chmod(os.path.join(APP_DIR, name), 0o755)

    print("==> Connecting your coding agents")
    results = wire_sources(explicit=True)
    for name, label, ok, added, note in results:
        if ok is None:
            print("    %-14s not installed — skipped (it'll connect itself"
                  " if you install it later)" % label)
        elif ok:
            print("    %-14s %s" % (label, "connected (%d events)" % len(added)
                                    if added else "already connected"))
            if note:
                print("      ! %s" % note)
        else:
            print("    %-14s COULD NOT CONNECT: %s" % (label, note))
    print("    note: hooks apply to NEW sessions only — restart any that are open")

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
    print()
    print("  %s daemon %s" % ("✓" if daemon_ok else "✗", "running" if daemon_ok else "NOT RUNNING"))
    print("  %s board %s" % ("✓" if port else "✗", ("connected (%s)" % port) if port else "not found — is it plugged in?"))
    for name, label, ok, added, note in results:
        mark = "✓" if ok else ("—" if ok is None else "✗")
        print("  %s %-14s %s" % (mark, label,
                                 "connected" if ok else
                                 ("not installed" if ok is None else note)))
    print()
    print("Glowbug is %s. New sessions will appear on the device."
          % ("ready" if (daemon_ok and port) else "partially set up"))


def uninstall():
    print("==> Stopping daemon")
    launchctl("unload", PLIST_PATH)
    for p in (PLIST_PATH,):
        try: os.unlink(p)
        except OSError: pass
    print("==> Disconnecting from your coding agents")
    for name, label, _detect, _install, do_uninstall in SOURCES:
        try:
            n = do_uninstall()
            if n:
                print("    %-14s removed %d hook entries (backup kept)" % (label, n))
        except Exception as e:
            print("    %-14s could not clean up: %s" % (label, e))
    print("==> Removing %s" % APP_DIR)
    shutil.rmtree(APP_DIR, ignore_errors=True)
    try:
        os.unlink(SOCK_PATH)
    except OSError:
        pass
    print("Glowbug uninstalled. (Log kept at %s)" % LOG_PATH)


def ask_daemon(timeout=1.0):
    """Ask the running daemon what it sees. None if it isn't listening."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps({"cmd": "report"}).encode())
        s.shutdown(socket.SHUT_WR)          # server reads to EOF
        data = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        s.close()
        return json.loads(data.decode(errors="replace"))
    except (OSError, ValueError):
        return None


def status():
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    print("glowbug %s · daemon %s · board %s" % (
        VERSION,
        "running" if daemon_ok else "stopped",
        port or "not found"))


def doctor():
    """Everything you need to answer 'why isn't X showing up?'"""
    daemon_ok = launchctl("list", "dev.glowbug.daemon").returncode == 0
    port = find_port()
    print("glowbug %s" % VERSION)
    print("  daemon    %s" % ("running" if daemon_ok else "STOPPED"))
    print("  board     %s" % (port or "not found — is it plugged in?"))
    print("  app dir   %s" % APP_DIR)
    print("  socket    %s" % SOCK_PATH)
    print("  log       %s" % LOG_PATH)
    print()
    rep = ask_daemon()
    if rep is None:
        print("  The daemon isn't answering — start it with: glowbug install")
        return
    print("  Sessions on the device:")
    if rep.get("sessions"):
        for s in rep["sessions"]:
            print("    %-12s %-21s %s" % (s["source"], s["name"], s["state"]))
    else:
        print("    (none — start a session, the device wakes within a second)")
    print()
    print("  Your coding agents:")
    seen = rep.get("last_event_at") or {}
    wired = (load_state().get("wired") or {})
    for name, label, detect, _i, _u in SOURCES:
        ago = seen.get(name)
        if ago is not None:
            note = "last event %.0fs ago" % ago
        elif name in wired:
            note = "connected — hooks only attach to NEW sessions, start one"
        elif detect():
            note = "installed but not connected yet — run: glowbug install"
        else:
            note = "not installed"
        print("    %-14s %s" % (label, note))



# -------------------------------------------------------------------- rescue
DFU_ID = "0483:df11"          # STM32 ROM bootloader, all families
FW_RAW_URL = "https://raw.githubusercontent.com/pud-blip/glowbug/main/firmware/glowbug.bin"


def _dfu_present():
    try:
        out = subprocess.run(["dfu-util", "-l"], capture_output=True,
                             text=True, timeout=10).stdout
        return DFU_ID in out
    except (OSError, subprocess.SubprocessError):
        return False


def _find_firmware():
    """Locate the bundled known-good image; verify sha256 when a manifest
    sits beside it. Returns (path, version) or (None, None)."""
    import hashlib
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (APP_DIR, os.path.join(here, "firmware")):
        bin_path = os.path.join(base, "firmware.bin")
        if not os.path.exists(bin_path):
            bin_path = os.path.join(base, "glowbug.bin")
        if not os.path.exists(bin_path):
            continue
        sums = os.path.join(base, "SHA256SUMS")
        if os.path.exists(sums):
            want = open(sums).read().split()[0]
            got = hashlib.sha256(open(bin_path, "rb").read()).hexdigest()
            if got != want:
                print("!! %s fails its integrity check — ignoring it" % bin_path)
                continue
        ver = "unknown"
        vp = os.path.join(base, "VERSION")
        if os.path.exists(vp):
            ver = open(vp).read().strip()
        return bin_path, ver
    return None, None


def rescue():
    """Reflash the known-good firmware. Handles a running board (sends the
    in-band DFU command) AND a 'bricked' one (user holds the knob at plug-in
    -> ROM bootloader). Never touches the network — if the image is missing,
    prints the fetch command for the USER to run."""
    if shutil.which("dfu-util") is None:
        sys.exit("dfu-util is required for rescue. Install it with:\n\n"
                 "    brew install dfu-util\n\nthen re-run: glowbug rescue")
    fw, fw_ver = _find_firmware()
    if fw is None:
        sys.exit("No firmware image found. Fetch the known-good image with:\n\n"
                 "    mkdir -p %s && curl -fsSL -o %s/firmware.bin \\\n"
                 "        %s\n\nthen re-run: glowbug rescue"
                 % (APP_DIR, APP_DIR, FW_RAW_URL))
    print("==> Firmware image: %s (fw %s)" % (fw, fw_ver))

    daemon_was_loaded = os.path.exists(PLIST_PATH)
    if daemon_was_loaded:
        subprocess.run(["launchctl", "unload", PLIST_PATH],
                       capture_output=True)
    try:
        if not _dfu_present():
            port = find_port()
            if port:
                print("==> Glowbug found on %s — asking it to enter update mode"
                      % port)
                try:
                    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                    attrs = termios.tcgetattr(fd)
                    attrs[0] = attrs[1] = attrs[3] = 0
                    attrs[2] = termios.CREAD | termios.CLOCAL | termios.CS8
                    termios.tcsetattr(fd, termios.TCSANOW, attrs)
                    os.write(fd, b"DFU\n")
                    os.close(fd)
                except OSError:
                    pass
                deadline = time.time() + 20
            else:
                print("""==> No Glowbug detected. Put it in Rescue Mode:

    1. Unplug the Glowbug.
    2. Press and hold the knob (push straight down) — keep holding.
    3. While holding, plug the USB-C cable back in.
    4. Keep holding two more seconds, then let go.

The middle screen will read RESCUE MODE. Waiting up to 60s...""")
                deadline = time.time() + 60
            while not _dfu_present():
                if time.time() > deadline:
                    sys.exit("Never saw the device in rescue mode. Check the\n"
                             "cable (charge-only cables are common!) and see\n"
                             "TROUBLESHOOTING.md.")
                time.sleep(1.5)
        print("==> Rescue mode detected — writing firmware (~10s)...")
        try:
            r = subprocess.run(
                ["dfu-util", "-a", "0", "-s", "0x08000000:leave", "-D", fw],
                capture_output=True, text=True, timeout=90)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired as e:
            # dfu-util can hang on the final :leave handshake after the
            # device has already reset into the app — judge by the output.
            out = ((e.stdout or b"").decode(errors="replace") +
                   (e.stderr or b"").decode(errors="replace"))
        if "File downloaded successfully" not in out:
            tail = "\n".join(out.strip().splitlines()[-6:])
            sys.exit("Flash FAILED:\n%s" % tail)
        print("==> Firmware written — waiting for the Glowbug to wake up...")
        deadline = time.time() + 20
        while time.time() < deadline:
            if find_port():
                print("\n✓ Glowbug restored (fw %s). Enjoy the welcome show."
                      % fw_ver)
                return
            time.sleep(1.5)
        print("\nFirmware written OK, but the device hasn't re-appeared —\n"
              "unplug it and plug it back in.")
    finally:
        if daemon_was_loaded:
            subprocess.run(["launchctl", "load", PLIST_PATH],
                           capture_output=True)


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "install":
        install()
    elif arg == "uninstall":
        uninstall()
    elif arg == "status":
        status()
    elif arg == "doctor":
        doctor()
    elif arg == "rescue":
        rescue()
    elif arg in ("--version", "version"):
        print("glowbug %s" % VERSION)
    elif arg == "":
        run_daemon()
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main()
