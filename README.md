# Glowbug

A little desk creature that shows your coding-agent sessions — five OLED
screens and ten RGB LEDs that tell you, from across the room, which agent is
thinking, which one needs you, and which one just finished.

Works with [Claude Code](https://claude.com/claude-code), [Cursor](https://cursor.com),
[Codex](https://developers.openai.com/codex), and [Antigravity](https://antigravity.google)
— mix and match, one screen each.

- **dark** — session idle
- **magenta-violet breathe** — thinking (with a little star-spinner on its screen)
- **ember-orange fade + chime** — the agent asked you a question
- **pink pulse + chime** — the agent is waiting for permission to use a tool
- **green pulse + ding** — an agent just finished its turn
- **red blink** — error

Automated subagents (`claude -p` one-shots, background verification runs)
are deliberately **not shown** — they aren't sessions you control, so a
screen for them is just noise.

Every pulse runs on its own clock — agents that start thinking at different
moments breathe out of phase, so the board reads as separate creatures
rather than one synchronized blob.

The underglow acts as one ambient lamp echoing the most important thing
happening on the board, so you don't even need to look directly at it.

This repository is the **host software**: everything that runs on your Mac.
It's deliberately tiny — **one Python file, standard library only, zero
network code** — so you can read every line before trusting it.

## Install

Pick a door (they all do the same thing):

**Tell Claude Code** (easiest — you already have it):

```text
Install glowbug from github.com/pud-blip/glowbug
```

**One-liner:**

```sh
curl -fsSL https://glowbug.dev/install | sh
```

**Homebrew:**

```sh
brew install pud-blip/tap/glowbug
glowbug install
```

**pipx:**

```sh
pipx install glowbug && glowbug install
```

**uv:**

```sh
uvx glowbug install
```

**By hand** (the fully-auditable path):

```sh
git clone https://github.com/pud-blip/glowbug
cd glowbug && python3 glowbug.py install
```

Then plug in your Glowbug. New sessions appear on the device.
(`glowbug status` for a health check, `glowbug doctor` when something's not
showing up; `glowbug.py uninstall` removes everything, including the hook
entries, with a backup of every config it touched.)

Install finds whichever coding agents you already have and connects to each.
**Install a new one later and it connects itself** — the daemon checks every
few minutes. Hooks only ever attach to *new* sessions, so restart any that
are already open.

## Which agents, and what you'll see

| | thinking | question | permission | done | error | closed |
|---|---|---|---|---|---|---|
| **Claude Code** | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Cursor** | ✓ | — | — | ✓ | ✓ | ✓ |
| **Codex** | ✓ | — | ✓ | ✓ | — | ✓ |
| **Antigravity** | ✓ | — | — | ✓ | ✓ | after a while |

The dashes are honest gaps, not bugs: those tools don't expose an
*observational* event for that moment. Glowbug only ever subscribes to events
it can watch without being able to interfere — it will never sit in the path
of a shell command or a tool call, and never gets a vote on whether your agent
is allowed to do something. Two consequences worth knowing:

- Cursor has no watch-only signal for its approval dialog, so no pink light
  there. And a freshly-opened chat pane doesn't get a screen until the agent
  first does something — Cursor announces empty panes as sessions, and
  Glowbug won't show a screen for a conversation that doesn't exist yet.
  To *close* a Cursor session on the device, **archive the chat** in Cursor
  (Cursor has no close/end event, but the archive flag is visible) — the
  screen plays its red farewell and frees up within a couple of seconds.
- Antigravity has no session-start or session-end event, so its screen appears
  on the first tool call and clears a while after the session goes quiet.

One thing that is never a gap: **ghost agents.** Quit (or force-quit) any of
these apps and their screens clear within seconds — the daemon checks that a
session's app is still running, so Glowbug never shows an agent that no
longer exists.

**Codex needs one setting** turned on for hooks to fire — add to `~/.codex/config.toml`:

```toml
[features]
hooks = true
```

Glowbug prints this during install if it's missing. It doesn't edit that file
— it's yours.

## If it ever seems dead

`glowbug rescue` reflashes a known-good firmware image over USB — it works
even if a bad update left the device unable to talk (hold the knob while
plugging in → the screen shows RESCUE MODE). Full walkthrough in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). Needs `brew install dfu-util`.

## Privacy — what Glowbug can and cannot see

The point of open-sourcing this is that you don't have to take our word:

- **No network code.** Search the repo for `http`, `urllib`, `requests` —
  there's nothing. Data flows from your agents' local files/hooks to a USB
  serial port. That's the entire graph.
- **The hook forwards eight fields** and nothing else — search
  [`glowbug.py`](glowbug.py) for `FORWARDER_SOURCE`, it's one screen of code:
  `hook_event_name`, `session_id`, `session_title`, `cwd`, `tool_name`,
  `error_type`, plus `source` (which tool it came from) and `idle` (a
  true/false). **Never prompt text, never tool arguments, never file
  contents.**
- **Things the tools offer that we deliberately drop:** transcript paths,
  model names, your email, turn ids, free-text error messages, file diffs,
  shell commands. The whitelist is in the forwarder as an `ALIASES` table —
  anything not named there never leaves that process.
- The daemon also reads `~/.claude/sessions/*.json` (Claude Code's local
  session registry) for session names and busy/idle status. Cursor hooks
  never include a chat title, so the daemon looks up **names and the
  archived flag only** from
  Cursor's local `composerHeaders` table (`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`)
  for sessions it already learned about from hooks — it does not scan your
  chat history. Codex and Antigravity have no such store, so for them the
  hooks are all Glowbug knows.
- The device itself only ever receives a session's **name and a status word**.

## How it works

```
Claude Code ──┐
Cursor ───────┤ hooks ──▶ glowbug-hook ──unix socket──▶ glowbug.py (daemon)
Codex ────────┤                                             │
Antigravity ──┘                                             │
Claude Code session registry (~/.claude/sessions) ──────────▶│
Cursor chat titles + archived flag (local composerHeaders) ─▶│
                                                       USB serial (newline
                                                         text protocol)
                                                                ▼
                                                            Glowbug 🐛✨
```

The daemon gives each live session a screen (oldest on the left, newest on the
right), merges hook events with Claude Code's session registry (and Cursor
chat titles from its local DB), and streams
semantic states over a simple text protocol:

```
SLOT 3 STATE question NAME my-project DETAIL Bash SUB 0 SID 8aada7ae
```

The device firmware owns all rendering — colors, animations, chimes, the
on-device settings menu (brightness, underglow, sound, chime choice).

## Requirements

- macOS (Apple Silicon or Intel), Python 3.9+ (the system one is fine)
- At least one of: Claude Code, Cursor 1.7+, Codex (with `features.hooks`),
  Antigravity 2.0+
- A Glowbug device (hardware docs coming later — glowbug.dev)

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, LaunchAgent, and hook entries. Your
`~/.claude/settings.json` is backed up before every change.

## License

MIT — see [LICENSE](LICENSE). https://glowbug.dev
