const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const openclawPath = process.argv[2];

if (!openclawPath) {
  console.error("Usage: node install.js <openclaw-path>");
  process.exit(1);
}

const skillDir = path.join(__dirname, "skill");
const dest = path.join(openclawPath, "skills", "codeswarm");

const skillsDir = path.join(openclawPath, "skills");
const packageJsonPath = path.join(openclawPath, "package.json");
const workspaceMarker = path.join(openclawPath, "AGENTS.md");

if (fs.existsSync(workspaceMarker) && !fs.existsSync(skillsDir)) {
  console.error("❌ The provided path appears to be an OpenClaw workspace, not the OpenClaw runtime root.");
  console.error("   Do NOT install into ~/.openclaw/workspace.");
  console.error("   Instead provide the root directory where OpenClaw is installed (contains skills/ and package.json).");
  process.exit(1);
}

if (!fs.existsSync(skillsDir)) {
  console.error("❌ Invalid OpenClaw path: skills/ directory not found.");
  console.error("   Expected structure:");
  console.error("     <openclaw-root>/skills/");
  console.error("     <openclaw-root>/package.json");
  process.exit(1);
}

if (!fs.existsSync(packageJsonPath)) {
  console.error("❌ Invalid OpenClaw root: package.json not found.");
  console.error("   Ensure you are pointing to the OpenClaw installation root.");
  process.exit(1);
}

console.log("✅ OpenClaw root validated.");
console.log("🔧 Installing skill dependencies...");
execSync("npm install", { cwd: skillDir, stdio: "inherit" });

console.log("🔨 Building skill...");
execSync("npm run build", { cwd: skillDir, stdio: "inherit" });

console.log("📦 Copying skill into OpenClaw...");
fs.rmSync(dest, { recursive: true, force: true });
fs.cpSync(skillDir, dest, { recursive: true });

console.log("✅ Codeswarm skill installed into OpenClaw.");
