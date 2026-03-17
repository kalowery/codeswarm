#!/usr/bin/env bash
if [ -z "${BASH_VERSION:-}" ]; then
  echo "❌ bootstrap.sh must be run with bash (not sh)." >&2
  exit 1
fi

VERBOSE="${CODESWARM_BOOTSTRAP_VERBOSE:-0}"
for arg in "$@"; do
  case "$arg" in
    --verbose|-v)
      VERBOSE=1
      ;;
    --help|-h)
      cat <<'USAGE'
Usage: ./bootstrap.sh [--verbose]

Options:
  -v, --verbose   Enable debug tracing and extra diagnostics.
  -h, --help      Show this help text.

Environment:
  CODESWARM_BOOTSTRAP_VERBOSE=1   Enable verbose mode.
USAGE
      exit 0
      ;;
  esac
done

set -euo pipefail

echo "🚀 Bootstrapping Codeswarm..."

NODE_VERSION="24.13.0"
NODE_MAJOR_REQUIRED=24

log() {
  echo "[bootstrap] $*"
}

if [ "$VERBOSE" = "1" ]; then
  export PS4='+ [${BASH_SOURCE##*/}:${LINENO}] '
  set -x
  log "Verbose mode enabled"
fi

need_cmd() {
  local cmd="$1"
  local hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "❌ Required command not found: $cmd"
    echo "   $hint"
    exit 1
  fi
}

node_major() {
  node -p 'process.versions.node.split(".")[0]'
}

ensure_path_in_shell_startup() {
  local bin_path="$1"
  local line="export PATH=\"$bin_path:\$PATH\""
  local profile
  for profile in "$HOME/.bashrc" "$HOME/.bash_profile" "$HOME/.profile"; do
    [ -f "$profile" ] || touch "$profile"
    if ! grep -F "$line" "$profile" >/dev/null 2>&1; then
      printf "\n# Added by Codeswarm bootstrap\n%s\n" "$line" >> "$profile"
      log "Added PATH update to $profile"
    fi
  done
}

log "Starting environment checks"

# --- Ensure nvm ---
if [ -z "${HOME:-}" ]; then
  echo "❌ HOME is not set in this shell environment."
  exit 1
fi

export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  # Load nvm in non-interactive shells.
  # shellcheck source=/dev/null
  set +u
  . "$NVM_DIR/nvm.sh"
  set -u
fi

if ! command -v nvm >/dev/null 2>&1; then
  echo "📦 nvm not found. Installing nvm..."
  need_cmd curl "Install it first (RHEL/Fedora: sudo dnf install -y curl)."
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash

  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    set +u
    . "$NVM_DIR/nvm.sh"
    set -u
  fi
fi

if command -v nvm >/dev/null 2>&1; then
  # --- Ensure Node via nvm ---
  if ! command -v node >/dev/null 2>&1; then
    echo "📦 Installing Node $NODE_VERSION..."
    nvm install "$NODE_VERSION"
  fi

  set +e
  nvm use "$NODE_VERSION"
  NVM_USE_RC=$?
  set -e

  if [ "$NVM_USE_RC" -ne 0 ]; then
    echo "⚠️ nvm use failed; retrying with --delete-prefix (common npm prefix conflict fix)..."
    set +e
    nvm use --delete-prefix "$NODE_VERSION"
    NVM_USE_DELETE_PREFIX_RC=$?
    set -e

    if [ "$NVM_USE_DELETE_PREFIX_RC" -ne 0 ]; then
      echo "❌ nvm could not activate Node $NODE_VERSION."
      echo "If you have npm prefix settings, remove them from ~/.npmrc and retry."
      echo "Typical fix:"
      echo "  nvm use --delete-prefix v$NODE_VERSION --silent"
      exit 1
    fi
  fi
else
  # --- Fallback: system Node ---
  if ! command -v node >/dev/null 2>&1; then
    echo "❌ Node.js is not installed and nvm is unavailable."
    echo "Install one of:"
    echo "  1) nvm (recommended), then rerun bootstrap"
    echo "  2) Node.js $NODE_MAJOR_REQUIRED+ via system package manager"
    exit 1
  fi
  echo "⚠️ nvm unavailable; using system Node $(node -v)"
fi

if [ "$(node_major)" -lt "$NODE_MAJOR_REQUIRED" ]; then
  echo "❌ Node.js $NODE_MAJOR_REQUIRED+ required. Found $(node -v)."
  exit 1
fi

echo "✅ Using Node $(node -v)"
echo "✅ Using npm $(npm -v)"

# --- Ensure Python 3.10+ ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 not found in PATH. Install Python 3.10+."
  echo "   RHEL/Fedora example: sudo dnf install -y python3"
  exit 1
fi

PY_VERSION=$(python3 - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)

PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  echo "❌ Python 3.10+ required. Found $PY_VERSION."
  echo "Install a modern Python and ensure it is first in PATH."
  exit 1
fi

echo "✅ Using Python $(python3 --version)"


# --- Install dependencies ---
echo "📦 Installing root dependencies..."
npm install

echo "📦 Installing backend..."
cd web/backend
npm install
cd ../..

echo "📦 Installing frontend..."
cd web/frontend
npm install
npm run build
cd ../..

# --- Install CLI ---
echo "📦 Installing CLI..."
npm --workspace=cli install
npm --workspace=cli run build
npm --workspace=cli link

# Ensure global npm bin is on PATH for this shell.
NPM_GLOBAL_PREFIX="$(npm prefix -g)"
NPM_GLOBAL_BIN="$NPM_GLOBAL_PREFIX/bin"
case ":$PATH:" in
  *":$NPM_GLOBAL_BIN:"*) ;;
  *) export PATH="$NPM_GLOBAL_BIN:$PATH" ;;
esac
ensure_path_in_shell_startup "$NPM_GLOBAL_BIN"

# Verify link actually produced a runnable codeswarm binary.
if ! command -v codeswarm >/dev/null 2>&1; then
  # Fallback shim for environments where npm global bin is not respected.
  mkdir -p "$HOME/.local/bin"
  ln -sf "$(pwd)/cli/dist/index.js" "$HOME/.local/bin/codeswarm"
  chmod +x "$(pwd)/cli/dist/index.js" || true
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
  esac
  ensure_path_in_shell_startup "$HOME/.local/bin"
fi

if ! command -v codeswarm >/dev/null 2>&1; then
  echo ""
  echo "❌ codeswarm CLI was linked but is not on PATH."
  echo "npm global prefix: $NPM_GLOBAL_PREFIX"
  echo "expected binary path: $NPM_GLOBAL_BIN/codeswarm"
  echo "Current PATH: $PATH"
  echo ""
  echo "Add this to your shell profile and open a new shell:"
  echo "  export PATH=\"$NPM_GLOBAL_BIN:\$PATH\""
  echo ""
  exit 1
fi

echo "✅ codeswarm linked at: $(command -v codeswarm)"

# --- Ensure Homebrew paths (macOS) ---
if [ -d "/opt/homebrew/bin" ]; then
  export PATH="/opt/homebrew/bin:$PATH"
fi

if [ -d "/usr/local/bin" ]; then
  export PATH="/usr/local/bin:$PATH"
fi

# --- Check Codex ---
if ! command -v codex >/dev/null 2>&1; then
  echo ""
  echo "❌ Codex CLI not found in PATH."
  echo "Current PATH: $PATH"
  echo ""
  echo "If installed via Homebrew, ensure brew is configured correctly."
  echo ""
  exit 1
fi

echo "Using codex at: $(command -v codex)"

# Check Codex authentication (non-interactive)
if ! codex login status >/dev/null 2>&1; then
  echo ""
  echo "❌ Codex CLI is not logged in."
  echo "Run:"
  echo "  codex login"
  echo ""
  exit 1
fi

echo "✅ Codex CLI detected and authenticated."

echo ""
echo "✅ Bootstrap complete."
echo ""
echo "You can now run:"
echo "  codeswarm <command>"
