const esbuild = require("esbuild");
const fs = require("node:fs");
const path = require("node:path");

const watch = process.argv.includes("--watch");

// Tiny .env loader — single source of truth for which API the extension
// talks to, so switching local ↔ deployed is "edit .env, rebuild" instead
// of editing source. See .env.example.
function loadEnvFile(filename) {
  const filepath = path.join(__dirname, filename);
  if (!fs.existsSync(filepath)) return;
  for (const line of fs.readFileSync(filepath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    let val = trimmed.slice(eq + 1).trim();
    if ((val.startsWith('"') && val.endsWith('"')) || (val.startsWith("'") && val.endsWith("'"))) {
      val = val.slice(1, -1);
    }
    if (!(key in process.env)) process.env[key] = val;
  }
}

loadEnvFile(".env");

const DEFAULT_API_URL = "https://podcast-commentary-api.fly.dev";
const API_URL = process.env.API_URL || DEFAULT_API_URL;
const usingProductionDefault = !process.env.API_URL;

// Surface the resolved API_URL before the build runs. Contributors who
// expected a local backend would otherwise discover the production
// default only when the side panel reports a connection failure — by
// then the bundle is already loaded and the cause isn't obvious.
if (usingProductionDefault) {
  console.log(`[build] API_URL=${API_URL} (production default — no chrome_extension/.env)`);
  console.log("[build] To target a local backend, create chrome_extension/.env containing:");
  console.log("[build]   API_URL=http://localhost:8080");
  console.log("[build] Or for a one-off build: API_URL=http://localhost:8080 npm run build");
} else {
  console.log(`[build] API_URL=${API_URL}`);
}

const buildOptions = {
  entryPoints: ["src/sidepanel.js"],
  bundle: true,
  outfile: "dist/sidepanel.js",
  format: "iife",
  target: "chrome120",
  minify: !watch,
  sourcemap: watch ? "inline" : false,
  define: {
    __API_URL__: JSON.stringify(API_URL),
  },
  logLevel: "info",
};

const summary = `API_URL=${API_URL}`;

if (watch) {
  esbuild.context(buildOptions).then((ctx) => {
    ctx.watch();
    console.log(`Watching for changes... (${summary})`);
  });
} else {
  esbuild.build(buildOptions).then(() => {
    console.log(`Build complete: dist/sidepanel.js (${summary})`);
  });
}
