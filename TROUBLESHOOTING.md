# Glowbug — Troubleshooting

## One of my coding agents isn't showing up

Run `glowbug doctor` — it prints a line per agent, and that line tells you
which of these you've hit:

**"not installed"** — Glowbug can't find it. It looks for `~/.claude`,
`~/.cursor`, `$CODEX_HOME` (default `~/.codex`), and Antigravity's config
under `~/.gemini`. If the tool keeps its config somewhere else, that's the
gap — open an issue with the path and we'll add it.

**"installed but not connected yet"** — run `glowbug install`. (Normally the
daemon does this for you within a few minutes of you installing a new tool.
It won't, on purpose, if you previously deleted Glowbug's hook by hand.)

**"connected — hooks only attach to NEW sessions"** — the wiring is in place
but that tool hasn't sent an event yet. **Start a new session.** Hooks never
apply retroactively to a session that was already open. Then watch the line
change to "last event Ns ago".

**Still nothing after a new session:**

- **Codex** needs hooks switched on — add to `~/.codex/config.toml`:
  ```toml
  [features]
  hooks = true
  ```
  Glowbug never edits that file, so this one is always yours to do.
- **Cursor CLI** (`cursor-agent`) fires fewer events than the Cursor app, and
  older versions may not read the global `~/.cursor/hooks.json` at all. The
  app is the reliable one today.
- **Antigravity** has no session-start event, so nothing appears until the
  agent's *first tool call* — a pure-chat reply may never light a screen.
- **Cursor showing a hex id** (`8aada7ae`) instead of the chat name — Cursor's
  hooks don't include a title. Glowbug 1.4.11+ reads the name from Cursor's
  local DB for sessions it already knows about. `glowbug install` from a
  current tree, then wait ~1.5s (renames follow the same way).
- Check the agent's own hook config actually points at
  `~/.glowbug/glowbug-hook.py`, and that the file is executable.
- `tail -f ~/Library/Logs/glowbug.log` shows every event as it arrives.

**A state I expected never lights up** — some are genuinely unavailable; see
the support matrix in the README. Cursor has no watch-only approval event
(no pink light), and Codex has no failure event (no red).

---

## My Glowbug is dark and my Mac doesn't see it

**First, the boring checks:**

- Try a different USB-C cable. Charge-only cables are extremely common and carry
  no data — the Glowbug will look completely dead on one.
- Try a different port, and plug directly into the Mac rather than through a hub.
- Run `glowbug status`. If it says `board not found`, the Mac genuinely isn't
  seeing the device.

If none of that helps — and **especially if it worked fine until you updated the
firmware** — use Rescue Mode below.

---

## Rescue Mode

Your Glowbug has a built-in escape hatch. It doesn't matter how badly the
firmware is broken: as long as the device gets power, this works.

**1.** Unplug the Glowbug.

**2.** Press and hold the knob (push straight down, like clicking it) —
   and keep holding.

**3.** While still holding the knob, plug the USB-C cable back in.

**4.** Keep holding for two more seconds, then let go.

The middle screen will read:

```
   RESCUE MODE
   Ready for update
```

No welcome animation, no lights — just that. It means the Glowbug is waiting
for new firmware. (If the screens stay completely blank, see the last section.)

**5.** With it still plugged in, run:

```sh
glowbug rescue
```

That reinstalls the last known-good firmware. About ten seconds later, the
welcome animation plays and you're back to normal.

---

## Why does this exist?

Firmware updates normally happen over the USB cable, with the Glowbug's own
software cooperating (that's the "UPDATING / do not unplug" screen you see
during a normal update): your Mac says "time to update," the Glowbug steps aside,
and new firmware is written.

That works perfectly — **as long as the firmware on the device is healthy enough
to listen.** If an update ever goes wrong in the specific way that leaves the
device unable to talk over USB, your Mac can't reach it anymore, so the normal
update can't rescue it either. Without an escape hatch, a $0.02 software mistake
would mean opening the case.

Rescue Mode skips the firmware entirely. Holding the knob at power-up talks to a
tiny program burned into the processor at the factory that cannot be erased,
overwritten, or broken by anything we ship. It's the same idea as holding a
button while powering on a phone to reach its recovery screen.

You will probably never need it. It's a seatbelt.

---

## Rescue Mode didn't work either

If you don't get the RESCUE MODE screen, the device isn't reaching that code at
all — which usually means it isn't getting power (bad cable/port) rather than a
firmware problem. Recheck the cable first.

If you've confirmed a known-good data cable and it's still unreachable, get in
touch — that one needs the case opened, and we'd rather do it than have you
do it.
