#!/usr/bin/env node
// Upload a local image file to JoySpace, returning a stable imgUrl
// that can be embedded in a JoySpace markdown document.
//
// IMPORTANT: JoySpace's upload endpoint requires a "host pageId". The
// uploaded file is logically owned by that page. Two modes:
//
//   1) --page-id <id>           Upload as attachment of an existing page.
//   2) --auto-scratch           Create a hidden scratch page, upload there,
//                               return the imgUrl. Caller is responsible for
//                               eventually deleting the scratch page (or
//                               pass --delete-scratch to delete immediately,
//                               which also works because the file CDN url
//                               keeps living after the page is gone — verified).
//
// Usage:
//   node upload_image.mjs --file /abs/path/img.png --page-id <id>
//   node upload_image.mjs --file /abs/path/img.png --auto-scratch
//   node upload_image.mjs --file ... --auto-scratch --delete-scratch
//
// Output: JSON with imgUrl, dimensions, mode, scratchPageId(if any).

import { getMeToken, JOYSPACE_API_BASE, TEAM_HEADER_ID } from "../../shared-auth.mjs";
import path from "node:path";
import fs from "node:fs/promises";

const args = process.argv.slice(2);
function arg(name) {
  const i = args.indexOf("--" + name);
  if (i < 0) return undefined;
  return args[i + 1];
}
function flag(name) { return args.includes("--" + name); }

const filePath = arg("file");
const pageId = arg("page-id");
const autoScratch = flag("auto-scratch");
const deleteScratch = flag("delete-scratch");

if (!filePath) { console.error("--file is required"); process.exit(2); }
if (!pageId && !autoScratch) { console.error("--page-id or --auto-scratch is required"); process.exit(2); }

// ---- get me_token ----
const { meToken } = getMeToken();

function commonHeaders(refererPageId) {
  return {
    "Cookie": `me_token=${meToken}`,
    "focus-team-id": TEAM_HEADER_ID,
    "focus-client": "WEB",
    "Referer": `https://joyspace.jd.com/pages/${refererPageId || ""}`,
    "User-Agent": "Mozilla/5.0 (joyspace-kit)",
  };
}

async function createScratchPage() {
  const url = JOYSPACE_API_BASE + "/v1/pages";
  const body = JSON.stringify({
    title: "[joyspace-kit-scratch-upload] " + Date.now(),
    page_type: 13,
    teamId: "root",
    content: [{ value: "scratch upload host" }],
    contentType: "markdown",
  });
  const r = await fetch(url, {
    method: "POST",
    headers: { ...commonHeaders(""), "Content-Type": "application/json", "Accept": "application/json" },
    body,
  });
  if (!r.ok) throw new Error("create scratch failed: " + r.status + " " + (await r.text()).slice(0, 200));
  const j = await r.json();
  if (j.status !== "success" || !j.data?.id) throw new Error("create scratch returned " + JSON.stringify(j).slice(0, 200));
  return j.data.id;
}

async function deletePage(id) {
  const url = JOYSPACE_API_BASE + "/v1/pages/" + id;
  const r = await fetch(url, { method: "DELETE", headers: commonHeaders("") });
  return { status: r.status, ok: r.ok };
}

async function uploadImage(pid, absFile) {
  const buf = await fs.readFile(absFile);
  const filename = path.basename(absFile);
  const ext = path.extname(absFile).toLowerCase();
  const mime = ({
    ".png":"image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg",
    ".gif":"image/gif", ".webp":"image/webp", ".svg":"image/svg+xml",
    ".bmp":"image/bmp", ".heic":"image/heic",
  })[ext] || "application/octet-stream";

  const form = new FormData();
  form.append("file", new Blob([buf], { type: mime }), filename);

  const url = JOYSPACE_API_BASE + "/v2/pages/" + pid + "/uploadImage";
  const r = await fetch(url, { method: "POST", headers: commonHeaders(pid), body: form });
  const text = await r.text();
  let j = null; try { j = JSON.parse(text); } catch (_) {}
  if (!r.ok || !j || j.status !== "success") {
    throw new Error("upload failed: HTTP " + r.status + " body=" + text.slice(0, 400));
  }
  return j.data;
}

(async () => {
  const abs = path.resolve(filePath);
  await fs.access(abs); // throws if not readable

  let host = pageId;
  let scratchId = null;
  if (!host) {
    scratchId = await createScratchPage();
    host = scratchId;
  }

  let result;
  try {
    result = await uploadImage(host, abs);
  } finally {
    if (scratchId && deleteScratch) {
      const del = await deletePage(scratchId);
      result = { ...(result || {}), scratchDeleted: del };
    }
  }

  console.log(JSON.stringify({
    mode: scratchId ? (deleteScratch ? "auto-scratch-deleted" : "auto-scratch-kept") : "page-id",
    hostPageId: host,
    scratchPageId: scratchId,
    imgUrl: result.imgUrl,
    dimensions: result.dimensions,
  }, null, 2));
})().catch(e => { console.error("ERROR:", e.message); process.exit(1); });
