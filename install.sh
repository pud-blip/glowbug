#!/bin/sh
# Glowbug one-line installer — the target of `curl -fsSL https://glowbug.dev/install | sh`
#
# Nothing hidden: this downloads the repo tarball from GitHub, then runs
# `glowbug.py install` (which prints everything it does and keeps a backup
# of your Claude Code settings). Prefer to audit first? Do it by hand:
#   git clone https://github.com/pud-blip/glowbug && cd glowbug
#   python3 glowbug.py install
set -eu

REPO="pud-blip/glowbug"
TMP="$(mktemp -d /tmp/glowbug.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

echo "==> Downloading Glowbug (github.com/$REPO)"
curl -fsSL "https://github.com/$REPO/archive/refs/heads/main.tar.gz" \
    | tar -xz -C "$TMP" --strip-components 1

python3 "$TMP/glowbug.py" install
