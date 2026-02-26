#!/usr/bin/env bash

OPENCLAW_ROOT="$1"

if [ -z "$OPENCLAW_ROOT" ]; then
  echo "Usage: ./install.sh <openclaw-root>"
  exit 1
fi

if [ ! -d "$OPENCLAW_ROOT/skills" ]; then
  echo "Invalid OpenClaw root. Expected skills/ directory."
  exit 1
fi

mkdir -p "$OPENCLAW_ROOT/skills/codeswarm"
cp "$(dirname "$0")/SKILL.md" "$OPENCLAW_ROOT/skills/codeswarm/SKILL.md"

echo "✅ Codeswarm SKILL.md installed into OpenClaw."
