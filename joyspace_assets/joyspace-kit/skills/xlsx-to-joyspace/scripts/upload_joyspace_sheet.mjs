#!/usr/bin/env node
/**
 * upload_joyspace_sheet.mjs
 *
 * Upload a local .xlsx / .xls / .csv to JoySpace as an ONLINE SHEET
 * (page_type=18), mirroring what the web editor's "import" button does.
 *
 * Four-step flow (verified 2026-07-16, works with me_token cookie):
 *   1. POST /v3/web-files/init          -> uploadId, partInfos[]
 *   2. PUT part(s) to signed S3 URLs    -> capture X-Wps3-Info-Token
 *   3. POST /v3/web-files/complete      -> fileId (relateFileId)
 *   4. POST /v2/pages (pageType=18)     -> new pageId
 *
 * Usage:
 *   node upload_joyspace_sheet.mjs --file /abs/path/data.xlsx
 *   node upload_joyspace_sheet.mjs --file ./data.xlsx --title "月度报表"
 *   node upload_joyspace_sheet.mjs --file ./data.csv \
 *     --page-url https://joyspace.jd.com/teams/<teamId>/<folderId>
 *
 * Placement:
 *   Without --page-url -> personal space root (teamId=root, folderId=root)
 *   With --page-url    -> resolve teamId + folderId from the referenced page,
 *                         drop the new sheet as a sibling in the same folder.
 *
 *   Sub-page mounting (categoryId) is NOT supported here because the /v2/pages
 *   endpoint used for sheet creation does not accept categoryId the same way
 *   /v1/pages does for markdown docs. If you need it, verify empirically first.
 *
 * Output (JSON to stdout):
 *   { pageId, title, url, teamId, folderId, sourceFile, sizeBytes, parts }
 */

import fs from "node:fs/promises";
import path from "node:path";
import { getMeToken, JOYSPACE_API_BASE, TEAM_HEADER_ID } from "../../shared-auth.mjs";

// ---------- args ----------
const rawArgs = process.argv.slice(2);
function arg(name) {
  const i = rawArgs.indexOf("--" + name);
  return i < 0 ? undefined : rawArgs[i + 1];
}

const filePath = arg("file");
const titleArg = arg("title");
const pageUrl = arg("page-url");

if (!filePath) { console.error("--file is required"); process.exit(2); }

const SPREADSHEET_MIMES = {
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".xls": "application/vnd.ms-excel",
  ".csv": "text/csv",
};

const absPath = path.resolve(filePath);
const ext = path.extname(absPath).toLowerCase();
if (!(ext in SPREADSHEET_MIMES)) {
  console.error(`unsupported extension: ${ext} (want .xlsx / .xls / .csv)`);
  process.exit(2);
}
const mimeType = SPREADSHEET_MIMES[ext];

// ---------- auth + headers ----------
const { meToken } = getMeToken();

function apiHeaders() {
  return {
    "Cookie": `me_token=${meToken}`,
    "focus-team-id": TEAM_HEADER_ID,
    "focus-client": "WEB",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://joyspace.jd.com/",
    "User-Agent": "Mozilla/5.0 (joyspace-kit)",
  };
}

async function jsonPost(url, body) {
  const r = await fetch(url, { method: "POST", headers: apiHeaders(), body: JSON.stringify(body) });
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch {}
  if (!r.ok || !j) throw new Error(`POST ${url} HTTP ${r.status}: ${t.slice(0, 400)}`);
  return j;
}

async function jsonGet(url) {
  const r = await fetch(url, { method: "GET", headers: apiHeaders() });
  const t = await r.text();
  let j = null;
  try { j = JSON.parse(t); } catch {}
  if (!r.ok || !j) throw new Error(`GET ${url} HTTP ${r.status}: ${t.slice(0, 400)}`);
  return j;
}

// ---------- placement ----------
function extractPageId(u) {
  if (!u) return null;
  const m = u.match(/joyspace\.jd\.com\/(?:pages|doc|sheets?|table|ppt|board|mind|meeting|teams\/[^/]+)\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : null;
}
function extractTeamFolderFromTeamsUrl(u) {
  // e.g. https://joyspace.jd.com/teams/<teamId>/<folderId>
  const m = u.match(/joyspace\.jd\.com\/teams\/([A-Za-z0-9_-]+)\/([A-Za-z0-9_-]+)/i);
  return m ? { teamId: m[1], folderId: m[2] } : null;
}

async function resolvePlacement() {
  if (!pageUrl) return { teamId: "root", folderId: "root", source: "personal-space-root" };

  // Fast path: /teams/<teamId>/<folderId>
  const teams = extractTeamFolderFromTeamsUrl(pageUrl);
  if (teams) return { ...teams, source: pageUrl };

  // Resolve via basic info of the referenced page.
  const refId = extractPageId(pageUrl);
  if (!refId) throw new Error(`cannot extract page id from --page-url: ${pageUrl}`);
  const j = await jsonGet(`${JOYSPACE_API_BASE}/v3/pages/${refId}/basic?sendRecent=0`);
  if (j.status !== "success" || !j.data) throw new Error(`basic returned ${JSON.stringify(j).slice(0, 300)}`);
  return {
    teamId: j.data.team_id?.trim() || "root",
    folderId: j.data.folder_id?.trim() || "root",
    source: pageUrl,
  };
}

// ---------- main ----------
(async () => {
  const stat = await fs.stat(absPath);
  const buf = await fs.readFile(absPath);
  const fileName = titleArg || path.basename(absPath);

  const placement = await resolvePlacement();

  // 1. init
  const initJson = await jsonPost(`${JOYSPACE_API_BASE}/v3/web-files/init`, {
    fileSize: stat.size,
    fileName,
    mimeType,
  });
  if (initJson.status !== "success" || !initJson.data?.uploadId) {
    throw new Error(`init returned ${JSON.stringify(initJson).slice(0, 300)}`);
  }
  const { uploadId, partSize, partInfos, storeResponseHeaderKeys } = initJson.data;

  // 2. PUT parts to S3-like storage.
  // Note: current backend always returns partInfos.length === 1 for files
  // under partSize (16 MB by default). For larger files we'd need chunked
  // uploads with per-part byte ranges. That path is not verified here — the
  // script errors out early if it happens.
  if (partInfos.length > 1) {
    throw new Error(`multi-part uploads (>${Math.round(partSize / 1024 / 1024)}MB) not yet supported. Split the file first.`);
  }
  const parts = [];
  for (const part of partInfos) {
    const putResp = await fetch(part.uploadUrl, { method: "PUT", headers: part.headers, body: buf });
    if (!putResp.ok) throw new Error(`S3 PUT part ${part.partNumber} HTTP ${putResp.status}: ${(await putResp.text()).slice(0, 400)}`);
    const storeResponseInfos = (storeResponseHeaderKeys || []).map((n) => ({ name: n, value: putResp.headers.get(n) || "" }));
    parts.push({ partNumber: part.partNumber, storeResponseInfos });
  }

  // 3. complete
  const completeJson = await jsonPost(`${JOYSPACE_API_BASE}/v3/web-files/complete`, { uploadId, parts });
  if (completeJson.status !== "success" || !completeJson.data?.fileId) {
    throw new Error(`complete returned ${JSON.stringify(completeJson).slice(0, 300)}`);
  }
  const relateFileId = String(completeJson.data.fileId);

  // 4. create sheet page
  const createJson = await jsonPost(`${JOYSPACE_API_BASE}/v2/pages`, {
    title: fileName,
    pageType: 18,
    partCount: partInfos.length,
    uploadId,
    partSize,
    partInfos,
    storeResponseHeaderKeys: storeResponseHeaderKeys || [],
    folderId: placement.folderId,
    teamId: placement.teamId,
    pageId: "",
    relateFileId,
  });
  if (createJson.status !== "success" || !createJson.data?.id) {
    throw new Error(`create page returned ${JSON.stringify(createJson).slice(0, 400)}`);
  }

  const created = createJson.data;
  const result = {
    pageId: created.id,
    title: created.title,
    url: `https://joyspace.jd.com/sheets/${created.id}`,
    teamId: created.team_id || placement.teamId,
    folderId: created.folder_id || placement.folderId,
    pageType: created.page_type,
    sourceFile: absPath,
    sizeBytes: stat.size,
    parts: partInfos.length,
    placement: placement.source,
    // The upload/create succeeds but JoySpace's WPS backend defers file-to-engine
    // association for a while — minutes to hours. During that window
    // /v2/sm/export returns errCode 50001 "关联操作未完成", so joyspace-read-sheet
    // cannot immediately round-trip this sheet. Verified 2026-07-17: this
    // cannot be forced from outside JoySpace (neither joyspace-kit nor o2's
    // webcli:joyspace upload can trigger it, even with headless/real-browser
    // handshakes). The sheet is fully functional for viewing/sharing right
    // away — only the export API path is temporarily blocked.
    engineReadyHint: "Freshly-uploaded sheets can't be read back via joyspace-read-sheet for a while (JoySpace-side lazy init). Design flows to not round-trip immediately.",
  };

  console.log(JSON.stringify(result, null, 2));
})().catch((e) => {
  console.error("ERROR:", e.message);
  process.exit(1);
});
