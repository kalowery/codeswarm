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
  CODESWARM_BOOTSTRAP_FORCE_NVM=1 Force nvm install/use path instead of system Node.
  CODESWARM_BOOTSTRAP_INSTALL_BEADS=ask|yes|no  Optional Beads CLI install policy (default: ask).
  CODESWARM_BEADS_VERSION=<version>             @beads/bd npm version to install (default: latest).
USAGE
      exit 0
      ;;
  esac
done

set -euo pipefail
# Clear inherited ERR traps/errtrace from parent shells or previously sourced scripts.
set +E
trap - ERR

echo "🚀 Bootstrapping Codeswarm..."
echo "[bootstrap] script version: 2026-03-17.3"

NODE_VERSION="24.13.0"
NODE_VERSION_TAG="v24.13.0"
NODE_MAJOR_REQUIRED=18
FORCE_NVM="${CODESWARM_BOOTSTRAP_FORCE_NVM:-0}"
INSTALL_BEADS_POLICY="${CODESWARM_BOOTSTRAP_INSTALL_BEADS:-ask}"
BEADS_VERSION="${CODESWARM_BEADS_VERSION:-latest}"
PYTHON_BIN=""

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

is_interactive_tty() {
  [ -t 0 ] && [ -t 1 ]
}

should_install_beads() {
  local policy="${1:-ask}"
  case "$policy" in
    yes|true|1|always)
      return 0
      ;;
    no|false|0|never)
      return 1
      ;;
    ask|prompt|"")
      if ! is_interactive_tty; then
        return 1
      fi
      printf "Optional dependency 'bd' (Beads CLI) is not installed. Install @beads/bd@%s now? [y/N] " "$BEADS_VERSION"
      read -r reply
      case "$reply" in
        y|Y|yes|YES)
          return 0
          ;;
        *)
          return 1
          ;;
      esac
      ;;
    *)
      echo "⚠️ Unknown CODESWARM_BOOTSTRAP_INSTALL_BEADS policy '$policy'. Expected ask|yes|no. Skipping beads install."
      return 1
      ;;
  esac
}

node_major() {
  node -p 'process.versions.node.split(".")[0]'
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

find_beads_cli() {
  if command -v bd >/dev/null 2>&1; then
    echo "bd"
    return 0
  fi
  if command -v beads >/dev/null 2>&1; then
    echo "beads"
    return 0
  fi
  return 1
}

has_compatible_system_node() {
  if ! command -v node >/dev/null 2>&1; then
    return 1
  fi
  local major
  major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  [ "$major" -ge "$NODE_MAJOR_REQUIRED" ]
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

if [ "$FORCE_NVM" != "1" ] && has_compatible_system_node; then
  echo "✅ Using system Node $(node -v) (nvm skipped)"
elif command -v nvm >/dev/null 2>&1; then
  # --- Ensure Node via nvm ---
  echo "📦 Ensuring Node $NODE_VERSION_TAG is installed in nvm..."
  set +e
  nvm install "$NODE_VERSION_TAG"
  NVM_INSTALL_RC=$?
  set -e
  if [ "$NVM_INSTALL_RC" -ne 0 ]; then
    echo "⚠️ nvm failed to install $NODE_VERSION_TAG. Will try system Node fallback."
  fi

  NVM_OK=0
  if [ "$NVM_INSTALL_RC" -eq 0 ]; then
    set +e
    nvm use "$NODE_VERSION_TAG"
    NVM_USE_RC=$?
    set -e

    if [ "$NVM_USE_RC" -ne 0 ]; then
      echo "⚠️ nvm use failed; retrying with --delete-prefix (common npm prefix conflict fix)..."
      set +e
      nvm use --delete-prefix "$NODE_VERSION_TAG"
      NVM_USE_DELETE_PREFIX_RC=$?
      set -e

      if [ "$NVM_USE_DELETE_PREFIX_RC" -eq 0 ]; then
        NVM_OK=1
      fi
    else
      NVM_OK=1
    fi
  fi

  if [ "$NVM_OK" -ne 1 ]; then
    if command -v node >/dev/null 2>&1; then
      echo "⚠️ Falling back to system Node $(node -v)"
    else
      echo "❌ nvm could not activate Node $NODE_VERSION_TAG and no system Node is available."
      echo "Try:"
      echo "  nvm cache clear"
      echo "  nvm install $NODE_VERSION_TAG"
      echo "  nvm use --delete-prefix $NODE_VERSION_TAG --silent"
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
  echo "   You can force nvm path with: CODESWARM_BOOTSTRAP_FORCE_NVM=1 ./bootstrap.sh"
  exit 1
fi

echo "✅ Using Node $(node -v)"
echo "✅ Using npm $(npm -v)"

# --- Ensure Python 3.10+ ---
PYTHON_BIN="$(pick_python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "❌ No Python 3.10+ interpreter found in PATH."
  echo "Install Python 3.10+ and rerun bootstrap."
  exit 1
fi

PY_VERSION=$("$PYTHON_BIN" - <<'PY'
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

echo "✅ Using Python $("$PYTHON_BIN" --version)"

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  echo "❌ Python pip is not available."
  echo "Install pip for your Python 3.10+ interpreter, then rerun bootstrap."
  exit 1
fi

echo "📦 Installing Codeswarm Python package..."
if [ -n "${VIRTUAL_ENV:-}" ]; then
  "$PYTHON_BIN" -m pip install -e .
else
  "$PYTHON_BIN" -m pip install --user --break-system-packages -e .
  PYTHON_USER_BIN="$("$PYTHON_BIN" -m site --user-base)/bin"
  case ":$PATH:" in
    *":$PYTHON_USER_BIN:"*) ;;
    *) export PATH="$PYTHON_USER_BIN:$PATH" ;;
  esac
  ensure_path_in_shell_startup "$PYTHON_USER_BIN"
fi


# --- Install dependencies ---
echo "📦 Installing root dependencies..."
npm install

echo "📦 Building frontend..."
npm --workspace=frontend run build

# --- Install CLI ---
echo "📦 Installing CLI..."
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

# --- Optional: Beads CLI ---
if BEADS_CLI="$(find_beads_cli)"; then
  echo "✅ $BEADS_CLI detected: $("$BEADS_CLI" --version 2>/dev/null || echo 'installed')"
else
  echo "ℹ️ bd/beads CLI not found."
  if should_install_beads "$INSTALL_BEADS_POLICY"; then
    echo "📦 Installing @beads/bd@$BEADS_VERSION (optional)..."
    set +e
    npm install -g "@beads/bd@$BEADS_VERSION"
    BEADS_INSTALL_RC=$?
    set -e
    if [ "$BEADS_INSTALL_RC" -ne 0 ]; then
      echo "⚠️ Beads CLI installation failed; continuing without bd/beads."
    fi
    hash -r || true
    if BEADS_CLI="$(find_beads_cli)"; then
      echo "✅ $BEADS_CLI installed: $("$BEADS_CLI" --version 2>/dev/null || echo 'installed')"
    else
      echo "⚠️ bd/beads still not available on PATH after install attempt."
    fi
  else
    echo "ℹ️ Skipping Beads CLI install."
  fi
fi

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
