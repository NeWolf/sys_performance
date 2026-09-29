#!/usr/bin/env node
// List recent JoySpace pages the current user has accessed.
// Uses /v1/pages/recent (verified via CDP probing 2026-05-28).
//
// Usage:
//   node list_joyspace.mjs                 # pretty table, last 20
//   node list_joyspace.mjs --limit 50      # last 50
//   node list_joyspace.mjs --json          # raw JSON
//   node list_joyspace.mjs --grep keyword  # filter by title/preview substring (case-insensitive)
//
// Auth: uses shared-auth to get me_token via HiOffice client.

import { getMeToken, JOYSPACE_API_BASE, joyHeaders } from "../../shared-auth.mjs";

const args = process.argv.slice(2);
function arg(name, def) {
  const i = args.indexOf("--" + name);
  if (i < 0) return def;
  return args[i + 1];
}
const wantJson = args.includes("--json");
const limit = parseInt(arg("limit", "20"), 10);
const grep = arg("grep", null);

// ---- get me_token ----
const { meToken } = getMeToken();

// ---- call /v1/pages/recent ----
const url = `${JOYSPACE_API_BASE}/v1/pages/recent`;
const resp = await fetch(url, { headers: joyHeaders(meToken) });
if (!resp.ok) {
  console.error("HTTP", resp.status, await resp.text());
  process.exit(1);
}
const body = await resp.json();
if (body.status !== "success") {
  console.error("API error:", JSON.stringify(body).slice(0, 400));
  process.exit(1);
}
let pages = body.data?.pages || [];

if (grep) {
  const g = grep.toLowerCase();
  pages = pages.filter(p =>
    (p.title || "").toLowerCase().includes(g) ||
    (p.preview_text || "").toLowerCase().includes(g)
  );
}
pages = pages.slice(0, limit);

if (wantJson) {
  console.log(JSON.stringify(pages, null, 2));
  process.exit(0);
}

// pretty print
const TYPE_NAMES = {
  13: "md", 11: "doc", 17: "sheet", 18: "sheet", 19: "slide",
  20: "form", 21: "mind", 22: "flow", 23: "wiki",
};
function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const pad = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function trunc(s, n) {
  if (!s) return "";
  s = s.replace(/\s+/g, " ");
  return s.length <= n ? s : s.slice(0, n-1) + "…";
}

console.log(`Recent ${pages.length} JoySpace pages\n`);
for (const p of pages) {
  const type = TYPE_NAMES[p.page_type] || `t${p.page_type}`;
  const author = p.author?.name || "?";
  console.log(`[${type}] ${p.title}`);
  console.log(`    ${p.link}`);
  console.log(`    by ${author}  updated ${fmtDate(p.updated_at)}`);
  if (p.preview_text) console.log(`    ${trunc(p.preview_text, 100)}`);
  console.log();
}
