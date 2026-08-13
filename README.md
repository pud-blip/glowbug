# Glowbug

A little desk creature that shows your [Claude Code](https://claude.com/claude-code)
sessions — five OLED screens and ten RGB LEDs that tell you, from across the
room, which agent is thinking, which one needs you, and which one just finished.

- **dark** — session idle
- **ember-orange breathe** — thinking (with a little star-spinner on its screen)
- **deep blue ↔ violet fade + chime** — Claude asked you a question
- **pink pulse + chime** — Claude is waiting for permission to use a tool
- **green pulse + ding** — an agent just finished its turn
- **red blink** — error

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

Then plug in your Glowbug. New Claude Code sessions appear on the device.
(`glowbug status` for a health check; `glowbug.py uninstall` removes
everything, including the hook entries, with a backup of your settings.)

## If it ever seems dead

`glowbug rescue` reflashes a known-good firmware image over USB — it works
even if a bad update left the device unable to talk (hold the knob while
plugging in → the screen shows RESCUE MODE). Full walkthrough in
[TROUBLESHOOTING.md](TROUBLESHOOTING.md). Needs `brew install dfu-util`.

## Privacy — what Glowbug can and cannot see

The point of open-sourcing this is that you don't have to take our word:

- **No network code.** Search the repo for `http`, `urllib`, `requests` —
  there's nothing. Data flows from Claude Code's local files/hooks to a USB
  serial port. That's the entire graph.
- **The hook forwards exactly six fields** and nothing else — search
  [`glowbug.py`](glowbug.py) for `HOOK_SOURCE`, it's one screen of code:
  `hook_event_name`, `session_id`, `session_title`, `cwd`, `tool_name`,
  `error_type`. **Never prompt text, never tool arguments, never file
  contents.**
- The daemon also reads `~/.claude/sessions/*.json` (Claude Code's local
  session registry) for session names and busy/idle status.
- The device itself only ever receives a session's **name and a status word**.

## How it works

```
Claude Code ──hooks──▶ glowbug-hook ──unix socket──▶ glowbug.py (daemon)
Claude Code session registry (~/.claude/sessions) ──────▶      │
                                                       USB serial (115200-ish,
                                                        newline protocol)
                                                                ▼
                                                            Glowbug 🐛✨
```

The daemon assigns your first five sessions to the five slots (stable —
sessions keep their screen), merges hook events with the session registry,
and streams semantic states over a simple text protocol:

```
SLOT 3 STATE question NAME my-project DETAIL Bash
```

The device firmware owns all rendering — colors, animations, chimes, the
on-device settings menu (brightness, underglow, sound, chime choice).

## Requirements

- macOS (Apple Silicon or Intel), Python 3.9+ (the system one is fine)
- Claude Code with hooks support
- A Glowbug device (hardware docs coming later — glowbug.dev)

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```

Removes the daemon, LaunchAgent, and hook entries. Your
`~/.claude/settings.json` is backed up before every change.

## License

MIT — see [LICENSE](LICENSE). https://glowbug.dev
