#!/usr/bin/env bash
set -e

echo "🚀 Bootstrapping Codeswarm..."

NODE_VERSION="24.13.0"

# --- Ensure nvm ---
export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -s "$NVM_DIR/nvm.sh" ]; then
  # Load nvm in non-interactive shells.
  # shellcheck source=/dev/null
  . "$NVM_DIR/nvm.sh"
fi

if ! command -v nvm >/dev/null 2>&1; then
  echo "📦 nvm not found. Installing nvm..."
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash

  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
fi

# --- Ensure Node ---
if ! command -v node >/dev/null 2>&1; then
  echo "📦 Installing Node $NODE_VERSION..."
  nvm install "$NODE_VERSION"
fi

nvm use "$NODE_VERSION"

echo "✅ Using Node $(node -v)"
echo "✅ Using npm $(npm -v)"

# --- Ensure Python 3.10+ ---
if ! command -v python3 >/dev/null 2>&1; then
  echo "❌ python3 not found in PATH. Install Python 3.10+ (e.g., brew install python@3.11)."
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
  echo "Install a modern Python (e.g., brew install python@3.11) and ensure it is first in PATH."
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

# Verify link actually produced a runnable codeswarm binary.
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
