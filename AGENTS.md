# Installing Glowbug (instructions for AI coding agents)

A user has asked you to install Glowbug — the desk device that shows their
Claude Code sessions. This takes about a minute.

## Steps

1. Clone and install:
   ```sh
   git clone https://github.com/pud-blip/glowbug /tmp/glowbug-install
   python3 /tmp/glowbug-install/glowbug.py install
   ```
2. The installer prints a three-line self-check. Success looks like:
   ```
   ✓ daemon running
   ✓ board connected (/dev/cu.usbmodemXXXX)
   ✓ hooks installed
   ```
3. Report the result to the user, including:
   - If the board line shows ✗: ask them to plug the Glowbug in (any USB-C
     port) — the daemon will find it automatically within seconds, no rerun
     needed.
   - **Hooks only attach to NEW Claude Code sessions** — sessions already
     running (including yours) show thinking/idle only until restarted.
     Idle/thinking states work immediately for all sessions either way.

## What the installer does (so you can explain it)

- Copies itself to `~/.glowbug/glowbug.py` and writes the hook
  (`~/.glowbug/glowbug-hook.py`, embedded in the source as `HOOK_SOURCE`)
- Adds hook entries to `~/.claude/settings.json` (JSON-safe merge, backup
  saved as `settings.json.glowbug-backup`, idempotent on re-run)
- Installs + starts a LaunchAgent (`dev.glowbug.daemon`) so the daemon runs
  at login

## Privacy notes you can relay if asked

Local-only: no network code anywhere in the file. The hook forwards only
six metadata fields (event name, session id, title, cwd, tool name, error
type) — never prompt text or file contents. The device receives only session
names + status words.

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```
