#!/usr/bin/env bash
set -e

echo "🚀 Bootstrapping Codeswarm..."

NODE_VERSION="24.13.0"

# --- Ensure nvm ---
if ! command -v nvm >/dev/null 2>&1; then
  echo "📦 nvm not found. Installing nvm..."
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash

  export NVM_DIR="$HOME/.nvm"
  [ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
fi

# --- Ensure Node ---
if ! command -v node >/dev/null 2>&1; then
  echo "📦 Installing Node $NODE_VERSION..."
  nvm install "$NODE_VERSION"
fi

nvm use "$NODE_VERSION"

echo "✅ Using Node $(node -v)"

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

# --- Check Codex ---
if ! command -v codex >/dev/null 2>&1; then
  echo ""
  echo "❌ Codex CLI is not installed."
  echo "Codeswarm currently requires Codex."
  echo ""
  echo "Install Codex:"
  echo "  npm install -g @openai/codex"
  echo ""
  exit 1
fi

if ! codex whoami >/dev/null 2>&1; then
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
echo "  npx codeswarm <command>"
