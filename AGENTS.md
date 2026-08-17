# Installing Glowbug (instructions for AI coding agents)

A user has asked you to install Glowbug — the desk device that shows their
Claude Code sessions. This takes about a minute.

## Steps

1. Clone and install:
   ```sh
   git clone https://github.com/pud/glowbug /tmp/glowbug-install
   python3 /tmp/glowbug-install/glowbug.py install
   ```
2. The installer prints a self-check: the daemon, the board, and one line per
   coding agent it found. Success looks like:
   ```
   ✓ daemon running
   ✓ board connected (/dev/cu.usbmodemXXXX)
   ✓ Claude Code    connected
   ✓ Cursor         connected
   — Codex          not installed
   ```
3. Report the result to the user, including:
   - If the board line shows ✗: ask them to plug the Glowbug in (any USB-C
     port) — the daemon will find it automatically within seconds, no rerun
     needed.
   - **Hooks only attach to NEW sessions** — sessions already running
     (including yours) show thinking/idle only until restarted. For Claude
     Code, idle/thinking works immediately either way (it has a session
     registry the daemon can read); the other tools are hook-only.
   - If a line shows `! Codex needs one setting turned on`, relay those two
     TOML lines to the user. Glowbug never edits `config.toml` itself.
   - Tools installed *later* connect themselves — the daemon re-checks every
     few minutes. Nothing to re-run.

## What the installer does (so you can explain it)

- Copies itself to `~/.glowbug/glowbug.py` and writes the hook
  (`~/.glowbug/glowbug-hook.py`, embedded in the source as `FORWARDER_SOURCE`)
- Adds hook entries to whichever of these exist — always an additive JSON
  merge, always with a `.glowbug-backup` alongside, idempotent on re-run, and
  it refuses to touch a config file that doesn't parse:
  `~/.claude/settings.json`, `~/.cursor/hooks.json`, `$CODEX_HOME/hooks.json`,
  and Antigravity's `hooks.json` (one top-level `"glowbug"` key)
- Never creates a config directory for a tool that isn't installed, and never
  writes TOML (Codex's one `features.hooks` line is printed, not written)
- Installs + starts a LaunchAgent (`dev.glowbug.daemon`) so the daemon runs
  at login, and records what it wired in `~/.glowbug/state.json` — delete a
  Glowbug hook by hand and it stays deleted

## Privacy notes you can relay if asked

Local-only: no network code anywhere in the file. The hook forwards only
eight metadata fields (event name, session id, title, cwd, tool name, error
type, which tool it came from, and an idle flag) — never prompt text, tool
arguments, or file contents. Transcript paths, model names, and free-text
error strings are deliberately dropped. The device receives only session
names + status words.

Glowbug also subscribes only to events it can *watch*: it never registers a
hook that could deny or delay a tool call, and the forwarder always exits 0.

## Rescue / reflash

`python3 glowbug.py rescue` (or `glowbug rescue`) reflashes the bundled
known-good image in `firmware/` (glowbug.bin + VERSION + SHA256SUMS,
sha256-verified). Works from a running board (sends the in-band DFU command)
or a bricked one (user holds the knob while plugging in → ROM bootloader →
"RESCUE MODE" on the middle screen). Requires dfu-util (`brew install
dfu-util`). The command never touches the network — if the image is missing
it prints a curl command for the user to run.

**When firmware is updated:** rebuild in the (private) firmware tree, then
refresh all three files here — `cp firmware.bin firmware/glowbug.bin`, update
`firmware/VERSION`, regenerate `firmware/SHA256SUMS` (`shasum -a 256
glowbug.bin > SHA256SUMS` from inside `firmware/`). They are the rescue image.

## Uninstall

```sh
python3 ~/.glowbug/glowbug.py uninstall
```
