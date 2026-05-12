// Ordo AI Stack: rewrite the upstream Hermes SPA so it lives under /hermes/.
//
// Upstream NousResearch/hermes-agent serves the dashboard at site root
// (/api/*, /assets, /ds-assets, /dashboard-plugins, plus four /api/*
// WebSocket endpoints). Caddy in this stack proxies /hermes/* to the
// hermes-dashboard container with prefix-strip (auth/caddy/Caddyfile),
// so without these patches every absolute URL the SPA emits 404s at the
// gateway.
//
// Each patch must match its anchor exactly once. A duplicate or missing
// match fails the build, so a future HERMES_PINNED_SHA bump cannot
// silently produce a half-broken bundle.

import fs from "node:fs";
import path from "node:path";

const ROOT = "/build/hermes-agent/web";

const patches = [
  {
    file: "vite.config.ts",
    find: "plugins: [react(), tailwindcss(), hermesDevToken()]",
    replace:
      "base: '/hermes/',\n  plugins: [react(), tailwindcss(), hermesDevToken()]",
  },
  {
    file: "src/main.tsx",
    find: "<BrowserRouter>",
    replace: '<BrowserRouter basename="/hermes">',
  },
  {
    file: "src/lib/api.ts",
    find: 'const BASE = "";',
    replace: 'const BASE = "/hermes";',
  },
  {
    file: "src/components/Backdrop.tsx",
    find: '"/ds-assets/filler-bg0.jpg"',
    replace: '"/hermes/ds-assets/filler-bg0.jpg"',
  },
  {
    file: "src/components/ChatSidebar.tsx",
    find: "`${proto}//${window.location.host}/api/events?${qs.toString()}`",
    replace:
      "`${proto}//${window.location.host}/hermes/api/events?${qs.toString()}`",
  },
  {
    file: "src/lib/gatewayClient.ts",
    find: "`${scheme}//${location.host}/api/ws?token=${encodeURIComponent(resolved)}`",
    replace:
      "`${scheme}//${location.host}/hermes/api/ws?token=${encodeURIComponent(resolved)}`",
  },
  {
    file: "src/pages/ChatPage.tsx",
    find: "`${proto}//${window.location.host}/api/pty?${qs.toString()}`",
    replace:
      "`${proto}//${window.location.host}/hermes/api/pty?${qs.toString()}`",
  },
  {
    file: "src/plugins/usePlugins.ts",
    find: "`/dashboard-plugins/${manifest.name}/${manifest.css}`",
    replace: "`/hermes/dashboard-plugins/${manifest.name}/${manifest.css}`",
  },
  {
    file: "src/plugins/usePlugins.ts",
    find: "`/dashboard-plugins/${manifest.name}/${manifest.entry}`",
    replace: "`/hermes/dashboard-plugins/${manifest.name}/${manifest.entry}`",
  },
];

let failed = 0;
for (const p of patches) {
  const full = path.join(ROOT, p.file);
  const text = fs.readFileSync(full, "utf8");
  const occurrences = text.split(p.find).length - 1;
  if (occurrences === 0) {
    console.error(
      `[fail] ${p.file}: anchor not found — upstream SHA likely shifted`,
    );
    console.error(`       expected: ${p.find}`);
    failed++;
    continue;
  }
  if (occurrences > 1) {
    console.error(
      `[fail] ${p.file}: anchor matches ${occurrences} times, expected exactly 1`,
    );
    failed++;
    continue;
  }
  fs.writeFileSync(full, text.replace(p.find, p.replace));
  console.log(`[ok]   ${p.file}`);
}
if (failed > 0) {
  console.error(`\n${failed} patch(es) failed. Aborting build.`);
  process.exit(1);
}
console.log("\nAll Hermes SPA patches applied for /hermes/ subpath hosting.");
