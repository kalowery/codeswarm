#!/usr/bin/env bash

set -e

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_DIR="$ROOT_DIR/cli"
SKILL_SRC="$ROOT_DIR/integrations/openclaw/SKILL.md"

OPENCLAW_DIR="${OPENCLAW_DIR:-$HOME/openclaw}"
SKILL_DEST="$OPENCLAW_DIR/skills/codeswarm"

echo "==> Building Codeswarm CLI"
cd "$CLI_DIR"
npm install
npm run build
npm link
cd "$ROOT_DIR"

echo "==> Installing / Updating OpenClaw skill"

if [ -d "$SKILL_DEST" ]; then
  cp "$SKILL_SRC" "$SKILL_DEST/SKILL.md"
  echo "Updated skill at $SKILL_DEST"
else
  echo "OpenClaw skill directory not found at $SKILL_DEST"
  echo "Skipping skill installation."
fi

echo "\n✅ Installation complete."
echo "If OpenClaw is running, restart the gateway:"
echo "  openclaw gateway restart"