#!/usr/bin/env bash
set -euo pipefail

# Curl-able installer for Codeswarm.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kalowery/codeswarm/main/install-codeswarm.sh | bash
# Optional env vars:
#   CODESWARM_REPO_URL=https://github.com/kalowery/codeswarm.git
#   CODESWARM_BRANCH=main
#   CODESWARM_INSTALL_DIR=$HOME/.codeswarm

REPO_URL="${CODESWARM_REPO_URL:-https://github.com/kalowery/codeswarm.git}"
BRANCH="${CODESWARM_BRANCH:-main}"
INSTALL_DIR="${CODESWARM_INSTALL_DIR:-$HOME/.codeswarm}"

log() {
  printf "[codeswarm-install] %s\n" "$*"
}

fail() {
  printf "[codeswarm-install] ERROR: %s\n" "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

require_cmd git
require_cmd bash

log "Installing Codeswarm from $REPO_URL (branch: $BRANCH)"
log "Install directory: $INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  log "Existing Codeswarm checkout detected. Updating..."
  git -C "$INSTALL_DIR" fetch origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout "$BRANCH"
  git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
elif [ -e "$INSTALL_DIR" ]; then
  fail "Install directory exists but is not a git checkout: $INSTALL_DIR"
else
  log "Cloning repository..."
  git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

BOOTSTRAP_SCRIPT="$INSTALL_DIR/bootstrap.sh"
[ -f "$BOOTSTRAP_SCRIPT" ] || fail "bootstrap.sh not found in $INSTALL_DIR"

log "Running bootstrap script..."
bash "$BOOTSTRAP_SCRIPT"

if command -v codeswarm >/dev/null 2>&1; then
  log "Install complete. codeswarm is available at: $(command -v codeswarm)"
else
  log "Install finished, but 'codeswarm' is not on PATH in this shell."
  log "Open a new terminal session and run: codeswarm --help"
fi

