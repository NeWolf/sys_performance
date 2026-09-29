#!/usr/bin/env node
/**
 * read_joyspace_sheet.mjs
 *
 * Read a JoySpace online sheet (/sheets/<id>) and return its cells as
 * structured JSON. Works for both old (Shimo) and new (WPS) engines —
 * they share the same export API which yields a real .xlsx file.
 *
 * Flow: POST /v2/sm/export -> poll /v2/sm/getExportProcess -> GET signed
 * S3 url -> parse with exceljs. Auth: me_token via shared-auth.
 *
 * Usage:
 *   node read_joyspace_sheet.mjs --url https://joyspace.jd.com/sheets/<id>
 *   node read_joyspace_sheet.mjs --page-id <id>
 *   node read_joyspace_sheet.mjs --url ... --sheet "Sheet1"      # one sheet only
 *   node read_joyspace_sheet.mjs --url ... --save-xlsx /tmp/x.xlsx  # keep raw file
 *   node read_joyspace_sheet.mjs --url ... --max-rows 200 --max-cols 40
 *
 * Output (JSON to stdout):
 *   {
 *     pageId, title, teamId, folderId, sheetsUrl,
 *     sheets: [
 *       { name, rowCount, colCount, rows: [ [v11,v12,...], [v21,...], ... ] }
 *     ],
 *     xlsxPath?          // present when --save-xlsx
 *   }
 *
 * Cell value rules:
 *   - number -> number
 *   - string -> string
 *   - date   -> ISO string
 *   - formula -> evaluated result (exceljs.value.result), formula preserved in .formula field only when --keep-formulas
 *   - hyperlink -> string ("text" only)
 *   - null/empty -> null
 *
 * Exit codes:
 *   0  ok
 *   1  runtime error (auth/api/parse)
 *   2  bad arguments
 */
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { getMeToken, JOYSPACE_API_BASE, TEAM_HEADER_ID } from "../../shared-auth.mjs";
import ExcelJS from "exceljs";

// ---------- args ----------
const rawArgs = process.argv.slice(2);
function arg(name) {
  const i = rawArgs.indexOf("--" + name);
  return i < 0 ? undefined : rawArgs[i + 1];
}
function flag(name) { return rawArgs.includes("--" + name); }

const urlArg = arg("url");
const pageIdArg = arg("page-id");
const sheetFilter = arg("sheet");
const savePath = arg("save-xlsx");
const maxRows = parseInt(arg("max-rows") || "1000", 10);
const maxCols = parseInt(arg("max-cols") || "100", 10);
const keepFormulas = flag("keep-formulas");
const pollTimeoutMs = parseInt(arg("timeout-ms") || "30000", 10);

function extractPageId(u) {
  if (!u) return null;
  const m = u.match(/joyspace\.jd\.com\/(?:sheets|pages|table)\/([A-Za-z0-9_-]+)/i);
  return m ? m[1] : null;
}

const pageId = pageIdArg || extractPageId(urlArg);
if (!pageId) {
  console.error("--url or --page-id is required");
  process.exit(2);
}

// ---------- HTTP helpers ----------
const { meToken } = getMeToken();

function apiHeaders() {
  return {
    "Cookie": `me_token=${meToken}`,
    "focus-team-id": TEAM_HEADER_ID,
    "focus-client": "WEB",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": `https://joyspace.jd.com/sheets/${pageId}`,
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

// ---------- 1. basic info (title / team / folder) ----------
async function fetchBasic() {
  const j = await jsonGet(`${JOYSPACE_API_BASE}/v3/pages/${pageId}/basic?sendRecent=0`);
  if (j.status !== "success" || !j.data) throw new Error(`basic returned ${JSON.stringify(j).slice(0, 300)}`);
  return {
    title: j.data.title || "",
    teamId: j.data.team_id || "",
    folderId: j.data.folder_id || "",
    pageType: j.data.page_type,
  };
}

// ---------- 2. trigger export, poll, download ----------
async function exportXlsx(title) {
  const trig = await jsonPost(`${JOYSPACE_API_BASE}/v2/sm/export`, {
    fileId: pageId,
    title: title || pageId,
    exportType: "",
  });
  if (trig.status !== "success" || !trig.data?.taskId) {
    // errCode 40302 = server-side rate limit on export triggers. Surface it clearly.
    if (trig.errCode === "40302") {
      throw new Error(`JoySpace export is rate-limited (errCode 40302). Wait ~30s and retry.`);
    }
    // errCode 50001 = sheet engine not yet initialized (freshly uploaded sheet
    // that no browser has opened yet). Cannot be resolved from a headless script.
    if (trig.errCode === "50001") {
      throw new Error(`Sheet engine not ready (errCode 50001 "关联操作未完成"). This means JoySpace's WPS backend hasn't finished associating this sheet with its render engine yet. Freshly-uploaded sheets can stay in this state for minutes to hours — it cannot be forced from outside JoySpace. Wait and retry later, or upload the xlsx in a way that lets it "warm up" first.`);
    }
    throw new Error(`export trigger returned ${JSON.stringify(trig).slice(0, 300)}`);
  }
  const taskId = trig.data.taskId;

  const deadline = Date.now() + pollTimeoutMs;
  let downloadUrl = "";
  let lastProgress = "";
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 800));
    const p = await jsonPost(`${JOYSPACE_API_BASE}/v2/sm/getExportProcess`, { fileId: pageId, taskId });
    if (p.status !== "success") throw new Error(`getExportProcess returned ${JSON.stringify(p).slice(0, 300)}`);
    lastProgress = String(p.data?.progress ?? "");
    if (p.data?.downloadUrl) { downloadUrl = p.data.downloadUrl; break; }
  }
  if (!downloadUrl) throw new Error(`export timed out after ${pollTimeoutMs}ms (last progress: ${lastProgress})`);

  const r = await fetch(downloadUrl, { method: "GET" });
  if (!r.ok) throw new Error(`downloadUrl GET ${r.status}`);
  const buf = Buffer.from(await r.arrayBuffer());
  if (buf.slice(0, 4).toString("hex") !== "504b0304") {
    throw new Error("downloaded bytes are not a ZIP/XLSX (magic mismatch)");
  }
  return buf;
}

// ---------- 3. parse XLSX with exceljs ----------
function cellToJson(cell) {
  if (cell == null || cell.value == null) return null;
  const v = cell.value;
  if (v instanceof Date) return v.toISOString();
  if (typeof v === "number" || typeof v === "boolean" || typeof v === "string") return v;
  // Formula cell: { formula, result }
  if (typeof v === "object" && "result" in v) {
    if (keepFormulas) return { formula: v.formula, result: v.result ?? null };
    return v.result ?? null;
  }
  // Rich text: { richText: [{text}] }
  if (v && Array.isArray(v.richText)) return v.richText.map((r) => r.text || "").join("");
  // Hyperlink: { text, hyperlink }
  if (v && typeof v.text === "string") return v.text;
  // Shared string / error / other
  if (v && typeof v === "object" && "error" in v) return `#${v.error}`;
  return String(v);
}

async function parseWorkbook(buf) {
  const wb = new ExcelJS.Workbook();
  await wb.xlsx.load(buf);

  const sheets = [];
  wb.eachSheet((ws) => {
    if (sheetFilter && ws.name !== sheetFilter) return;
    const rowCount = Math.min(ws.rowCount || ws.actualRowCount || 0, maxRows);
    const colCount = Math.min(ws.columnCount || ws.actualColumnCount || 0, maxCols);
    const rows = [];
    for (let r = 1; r <= rowCount; r++) {
      const row = ws.getRow(r);
      const arr = [];
      for (let c = 1; c <= colCount; c++) {
        arr.push(cellToJson(row.getCell(c)));
      }
      rows.push(arr);
    }
    sheets.push({ name: ws.name, rowCount, colCount, rows });
  });
  return sheets;
}

// ---------- main ----------
(async () => {
  const basic = await fetchBasic();
  const buf = await exportXlsx(basic.title);

  let xlsxPath;
  if (savePath) {
    xlsxPath = path.resolve(savePath);
    await fs.writeFile(xlsxPath, buf);
  }

  const sheets = await parseWorkbook(buf);

  const result = {
    pageId,
    title: basic.title,
    teamId: basic.teamId,
    folderId: basic.folderId,
    sheetsUrl: `https://joyspace.jd.com/sheets/${pageId}`,
    sheets,
  };
  if (xlsxPath) result.xlsxPath = xlsxPath;

  console.log(JSON.stringify(result, null, 2));
})().catch((e) => {
  console.error("ERROR:", e.message);
  process.exit(1);
});
