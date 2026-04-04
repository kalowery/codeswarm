#!/usr/bin/env bash
set -euo pipefail

# Curl-able installer for Codeswarm release bundles.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/kalowery/codeswarm/main/install-codeswarm.sh | bash
#
# Optional env vars:
#   CODESWARM_INSTALL_DIR=$HOME/.codeswarm
#   CODESWARM_RELEASE_URL=https://github.com/kalowery/codeswarm/releases/latest/download/codeswarm-full.tar.gz
#   CODESWARM_RELEASE_ARCHIVE=/path/to/codeswarm-full.tar.gz
#   CODESWARM_INSTALL_MODE=release|source
#   CODESWARM_REPO_URL=https://github.com/kalowery/codeswarm.git
#   CODESWARM_BRANCH=main

INSTALL_DIR="${CODESWARM_INSTALL_DIR:-$HOME/.codeswarm}"
INSTALL_MODE="${CODESWARM_INSTALL_MODE:-release}"
RELEASE_URL="${CODESWARM_RELEASE_URL:-https://github.com/kalowery/codeswarm/releases/latest/download/codeswarm-full.tar.gz}"
RELEASE_ARCHIVE="${CODESWARM_RELEASE_ARCHIVE:-}"
REPO_URL="${CODESWARM_REPO_URL:-https://github.com/kalowery/codeswarm.git}"
BRANCH="${CODESWARM_BRANCH:-main}"
TMP_DIR=""

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

pick_python() {
  local candidate
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if ! command -v "$candidate" >/dev/null 2>&1; then
      continue
    fi
    if "$candidate" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
    then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_path_in_shell_startup() {
  local bin_path="$1"
  local line="export PATH=\"$bin_path:\$PATH\""
  local profile
  for profile in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -f "$profile" ] || touch "$profile"
    if ! grep -F "$line" "$profile" >/dev/null 2>&1; then
      printf "\n# Added by Codeswarm installer\n%s\n" "$line" >> "$profile"
      log "Added PATH update to $profile"
    fi
  done
}

ensure_bash_profile_sources_bashrc() {
  local profile="$HOME/.bash_profile"
  local marker="# Added by Codeswarm installer: load ~/.bashrc for login shells"
  [ -f "$profile" ] || touch "$profile"
  if grep -F "$marker" "$profile" >/dev/null 2>&1; then
    return 0
  fi
  cat >>"$profile" <<'EOF'

# Added by Codeswarm installer: load ~/.bashrc for login shells
if [ -f "$HOME/.bashrc" ]; then
  . "$HOME/.bashrc"
fi
EOF
  log "Updated $profile to source ~/.bashrc"
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}

install_from_source() {
  require_cmd git
  require_cmd bash

  log "Installing Codeswarm from source checkout"
  log "Repository: $REPO_URL (branch: $BRANCH)"
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

  local bootstrap_script="$INSTALL_DIR/bootstrap.sh"
  [ -f "$bootstrap_script" ] || fail "bootstrap.sh not found in $INSTALL_DIR"
  log "Running bootstrap script..."
  bash "$bootstrap_script"
}

download_release_archive() {
  local archive_path="$1"
  if [ -n "$RELEASE_ARCHIVE" ]; then
    [ -f "$RELEASE_ARCHIVE" ] || fail "Release archive not found: $RELEASE_ARCHIVE"
    cp "$RELEASE_ARCHIVE" "$archive_path"
    return 0
  fi

  require_cmd curl
  log "Downloading Codeswarm release bundle..."
  curl -fsSL "$RELEASE_URL" -o "$archive_path"
}

write_launcher() {
  local launcher_path="$INSTALL_DIR/bin/codeswarm"
  cat >"$launcher_path" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
export CODESWARM_HOME="$INSTALL_DIR"
export CODESWARM_PYTHON="$INSTALL_DIR/venv/bin/python"
exec node "$INSTALL_DIR/cli/dist/index.js" "$@"
EOF
  chmod +x "$launcher_path"
}

install_from_release() {
  require_cmd bash
  require_cmd tar
  require_cmd node
  require_cmd npm

  local python_bin
  python_bin="$(pick_python || true)"
  [ -n "$python_bin" ] || fail "No Python 3.10+ interpreter found in PATH."

  TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/codeswarm-install.XXXXXX")"
  trap cleanup EXIT

  local archive_path="$TMP_DIR/codeswarm-full.tar.gz"
  download_release_archive "$archive_path"

  log "Extracting release bundle..."
  tar -xzf "$archive_path" -C "$TMP_DIR"
  local extracted_root
  extracted_root="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d -name 'codeswarm-*' | head -n 1)"
  [ -n "$extracted_root" ] || fail "Release archive did not contain a codeswarm bundle root."

  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/bin" "$INSTALL_DIR/cli" "$INSTALL_DIR/web" "$INSTALL_DIR/python" \
    "$INSTALL_DIR/agent" "$INSTALL_DIR/common" "$INSTALL_DIR/router" "$INSTALL_DIR/slurm" \
    "$INSTALL_DIR/ssh" "$INSTALL_DIR/release-manifest.json" \
    "$INSTALL_DIR/README.md" "$INSTALL_DIR/LICENSE" "$INSTALL_DIR/install-codeswarm.sh" \
    "$INSTALL_DIR/pyproject.toml" "$INSTALL_DIR/bootstrap.sh"
  cp -R "$extracted_root"/. "$INSTALL_DIR"/

  log "Creating Python virtual environment..."
  "$python_bin" -m venv "$INSTALL_DIR/venv"
  local wheel_path
  wheel_path="$(find "$INSTALL_DIR/python/dist" -maxdepth 1 -name 'codeswarm-*.whl' | head -n 1)"
  [ -n "$wheel_path" ] || fail "No Codeswarm wheel found in release bundle."
  "$INSTALL_DIR/venv/bin/python" -m pip install --no-deps "$wheel_path"

  log "Installing CLI runtime dependencies..."
  NPM_CONFIG_CACHE="$TMP_DIR/npm-cache" npm ci --omit=dev --ignore-scripts --prefix "$INSTALL_DIR/cli"

  log "Installing backend runtime dependencies..."
  NPM_CONFIG_CACHE="$TMP_DIR/npm-cache" npm ci --omit=dev --ignore-scripts --prefix "$INSTALL_DIR/web/backend"

  mkdir -p "$INSTALL_DIR/bin"
  write_launcher
  ensure_bash_profile_sources_bashrc
  ensure_path_in_shell_startup "$INSTALL_DIR/bin"

  log "Install complete."
  log "Codeswarm home: $INSTALL_DIR"
  if command -v codeswarm >/dev/null 2>&1; then
    log "codeswarm is available at: $(command -v codeswarm)"
  else
    log "Open a new terminal session and run: codeswarm --help"
  fi
}

case "$INSTALL_MODE" in
  source)
    install_from_source
    ;;
  release)
    install_from_release
    ;;
  *)
    fail "Unknown CODESWARM_INSTALL_MODE: $INSTALL_MODE (expected release or source)"
    ;;
esac
