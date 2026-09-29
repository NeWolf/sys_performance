#!/usr/bin/env node
/**
 * read_joyspace_doc.js
 *
 * Read a JoySpace document by URL or pageId.
 *
 * Outputs JSON:
 *   {
 *     pageId, title, teamId, folderId, fullName, pageType,
 *     creator, updater, updatedAt,
 *     authMode, link,
 *     content: "<markdown-ish text reconstructed from blocks>",
 *     raw: { basic, content }   // when --raw
 *   }
 *
 * Usage:
 *   node read_joyspace_doc.js --url https://joyspace.jd.com/pages/<id>
 *   node read_joyspace_doc.js --page-id <id>
 *   node read_joyspace_doc.js --url ... --save /tmp/out.md
 *   node read_joyspace_doc.js --url ... --raw
 *
 * Auth resolution order (same as markdown-to-joyspace):
 *   ME_TOKEN / SSO_TOKEN env
 *   ~/.joyclaw/openclaw.json cookies
 *   JMECHAT_token + tenantCode + deviceId via tokenGrant
 *   Local HiOffice client (8988-9006) legacy exchange
 */

import fs from "node:fs/promises";
import { realpathSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_JOYSPACE_API_BASE = "https://apijoyspace.jd.com";
const DEFAULT_COLOR_GATEWAY_BASE = "https://api.m.jd.com";
const DEFAULT_TENANT_CODE = "CN.JD.GROUP";
const DEFAULT_APP_ID = "JDME_DESKTOP";
const DEFAULT_HIOFFICE_PORTS = Object.freeze(
  Array.from({ length: 10 }, (_, index) => 8988 + index * 2),
);

const TENANT_CONFIG = Object.freeze({
  "CN.JD.GROUP": { teamHeaderId: "00046419", ddAppId: "ee" },
  "TH.JD.GROUP": { teamHeaderId: "00046420", ddAppId: "th.ee" },
  "ID.JD.GROUP": { teamHeaderId: "00046421", ddAppId: "id.ee" },
  "SF.JD.GROUP": { teamHeaderId: "00046422", ddAppId: "sf.ee" },
});

function requireTenantConfig(tenantCode) {
  const config = TENANT_CONFIG[tenantCode];
  if (!config) {
    throw new Error(
      `Unsupported tenantCode "${tenantCode}". Expected one of ${Object.keys(TENANT_CONFIG).join(", ")}`,
    );
  }
  return config;
}

function extractCookieValue(cookie, name) {
  if (!cookie) return "";
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = cookie.match(new RegExp(`${escapedName}=([^;]+)`));
  return match?.[1]?.trim() || "";
}

function readTokensFromConfigText(configText) {
  if (!configText?.trim()) return { meToken: "", ssoToken: "" };
  try {
    const parsed = JSON.parse(configText);
    const cookie =
      parsed?.models?.providers?.jdcloud?.headers?.Cookie ||
      parsed?.models?.providers?.jdcloud?.headers?.cookie ||
      "";
    return {
      meToken: extractCookieValue(cookie, "me_token"),
      ssoToken: extractCookieValue(cookie, "sso.jd.com"),
    };
  } catch {
    return { meToken: "", ssoToken: "" };
  }
}

function buildCookieHeader({ meToken = "", ssoToken = "" }) {
  const cookies = [];
  if (meToken) cookies.push(`me_token=${meToken}`);
  if (ssoToken) cookies.push(`sso.jd.com=${ssoToken}`);
  return cookies.join("; ");
}

async function readConfigTokens(configPath) {
  try {
    const text = await fs.readFile(configPath, "utf8");
    return readTokensFromConfigText(text);
  } catch {
    return { meToken: "", ssoToken: "" };
  }
}

async function callColorGateway(functionId, body) {
  const url = `${DEFAULT_COLOR_GATEWAY_BASE}?functionId=${encodeURIComponent(functionId)}&appid=${DEFAULT_APP_ID}`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ functionId, body, appid: DEFAULT_APP_ID }),
  });
  if (!response.ok) {
    throw new Error(`${functionId} HTTP ${response.status} ${response.statusText}`);
  }
  return response.json();
}

async function exchangeMeTokenFromStartupToken({ startupToken, tenantCode, deviceId }) {
  const { ddAppId } = requireTenantConfig(tenantCode);
  const response = await callColorGateway("desk.agent.auth.tokenGrant", {
    tenantCode,
    deviceUuid: deviceId,
    jdmeAppId: ddAppId,
    token: startupToken,
    appCode: process.platform === "darwin" ? "hio_plugin_joydesk_Mac" : "hio_plugin_joydesk",
  });
  if (response?.code !== 0 || !response?.data?.accessToken) {
    throw new Error(response?.msg || "desk.agent.auth.tokenGrant failed");
  }
  return response.data.accessToken.trim();
}

async function getLegacyEncryptPayload(ddAppId) {
  const timestamp = Math.floor(Date.now() / 1000);
  const response = await callColorGateway("desk.agent.auth.encrypt", {
    content: JSON.stringify({
      method: "query",
      param: "appToken",
      timestamp: String(timestamp),
      from: process.platform === "darwin" ? "hio_plugin_joydesk_Mac" : "hio_plugin_joydesk",
      to: "HiOfficeClient",
    }),
    jdmeAppId: ddAppId,
  });
  if (response?.code !== 0 || !response?.data?.aesKey || !response?.data?.content) {
    throw new Error(response?.msg || "desk.agent.auth.encrypt failed");
  }
  return { aesKey: response.data.aesKey, content: response.data.content };
}

async function queryHiOfficeAppToken({ aesKey, content }) {
  const from = encodeURIComponent(
    process.platform === "darwin" ? "hio_plugin_joydesk_Mac" : "hio_plugin_joydesk",
  );
  let lastError = null;
  for (const port of DEFAULT_HIOFFICE_PORTS) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/hioffice?from=${from}`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-AES-Key": aesKey },
        body: content,
      });
      if (!response.ok) throw new Error(`HiOffice ${port}: HTTP ${response.status}`);
      const xAesKey = response.headers.get("X-AES-Key");
      if (!xAesKey) throw new Error(`HiOffice ${port}: missing X-AES-Key`);
      return { appToken: await response.text(), xAesKey };
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError || new Error("HiOffice ports unavailable");
}

async function exchangeLegacyWebToken({ appToken, xAesKey, tenantCode, deviceId }) {
  const { ddAppId } = requireTenantConfig(tenantCode);
  const response = await callColorGateway("desk.agent.auth.getWebToken", {
    token: appToken,
    tenantCode,
    deviceUuid: deviceId,
    aesKey: xAesKey,
    jdmeAppId: ddAppId,
  });
  if (response?.code !== 0 || !response?.data?.accessToken) {
    throw new Error(response?.msg || "desk.agent.auth.getWebToken failed");
  }
  return response.data.accessToken.trim();
}

async function exchangeMeTokenViaHiOffice({ tenantCode, deviceId }) {
  const { ddAppId } = requireTenantConfig(tenantCode);
  const encrypt = await getLegacyEncryptPayload(ddAppId);
  const hiOffice = await queryHiOfficeAppToken(encrypt);
  return exchangeLegacyWebToken({
    appToken: hiOffice.appToken,
    xAesKey: hiOffice.xAesKey,
    tenantCode,
    deviceId,
  });
}

async function resolveAuth(options) {
  const envMeToken = process.env.ME_TOKEN || process.env.me_token || "";
  const envSsoToken = process.env.SSO_TOKEN || process.env.sso_token || "";
  if (envMeToken || envSsoToken) {
    return { mode: "env", meToken: envMeToken, ssoToken: envSsoToken };
  }
  const configTokens = await readConfigTokens(
    options.configPath || path.join(process.env.HOME || process.env.USERPROFILE || os.homedir(), ".joyclaw", "openclaw.json"),
  );
  if (configTokens.meToken || configTokens.ssoToken) {
    return { mode: "config", ...configTokens };
  }
  if (options.startupToken) {
    const meToken = await exchangeMeTokenFromStartupToken({
      startupToken: options.startupToken,
      tenantCode: options.tenantCode,
      deviceId: options.deviceId,
    });
    return { mode: "tokenGrant", meToken, ssoToken: "" };
  }
  // legacy HiOffice fallback (no deviceId requirement; uses default)
  const meToken = await exchangeMeTokenViaHiOffice({
    tenantCode: options.tenantCode,
    deviceId: options.deviceId || "noDeviceId",
  });
  return { mode: "legacy", meToken, ssoToken: "" };
}

async function requestJoySpaceJson({ method, url, cookieHeader, teamHeaderId, body }) {
  const response = await fetch(`${DEFAULT_JOYSPACE_API_BASE}${url}`, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Cookie: cookieHeader,
      "x-team-id": teamHeaderId,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    throw new Error(`${url} HTTP ${response.status} ${response.statusText}`);
  }
  const json = await response.json();
  if (json?.status === "success" || json?.status === "0" || json?.status === 0) {
    return json.data;
  }
  if (json?.errorCode && json.errorCode !== "0") {
    throw new Error(json.errorMsg || json.errMsg || `${url} failed`);
  }
  return json.data ?? json;
}

function extractPageIdFromUrl(pageUrl) {
  const match = pageUrl.match(
    /joyspace\.jd\.com\/(?:pages|doc|sheets?|table|ppt|board|mind|meeting)\/([A-Za-z0-9_-]+)/i,
  );
  if (!match?.[1]) {
    throw new Error(`Unable to extract JoySpace page id from URL: ${pageUrl}`);
  }
  return match[1];
}

/**
 * Reconstruct markdown from JoySpace Slate-style content blocks.
 *
 * Known block types (page_type=13):
 *   p          - paragraph  -> children inline text
 *   list       - list item; value='bullet'|'number' -> "- " or "1. "
 *   img        - image      -> ![](url)
 *   divider    - hr         -> "---"
 *   table      - children: table-row -> table-cell -> p
 *   attachment - file       -> "[file](url)"
 *   code       - code block -> ```lang\n...\n```
 *   blockquote - quote      -> "> "
 *   heading-N  - h1..h6     -> "#" * N
 *
 * Inline leaf node: { text, bold?, italic?, code?, underline?, strikethrough? }
 */
function renderInline(node) {
  if (node == null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(renderInline).join("");
  if (typeof node.text === "string") {
    let t = node.text;
    if (node.code) t = "`" + t + "`";
    if (node.bold) t = "**" + t + "**";
    if (node.italic) t = "*" + t + "*";
    if (node.strikethrough || node.strike) t = "~~" + t + "~~";
    if (node.underline) t = "<u>" + t + "</u>";
    if (node.url || node.link) t = `[${t}](${node.url || node.link})`;
    return t;
  }
  // Inline "link" node: `{ type: "link", url, children: [{text}] }`. Without
  // this case, JoySpace-to-anywhere links inside a paragraph would collapse
  // to just their text and drop the href.
  if (node.type === "link" && (node.url || node.link)) {
    const label = Array.isArray(node.children) ? node.children.map(renderInline).join("") : "";
    const href = node.url || node.link;
    return label ? `[${label}](${href})` : href;
  }
  // Inline "docfile" card can also appear inside a paragraph/list-item — a
  // pasted JoySpace URL is auto-converted to a card. Emit as a markdown link
  // to the corresponding /pages|sheets|documents URL.
  if (node.type === "docfile") {
    return renderDocfile(node);
  }
  if (Array.isArray(node.children)) return node.children.map(renderInline).join("");
  return "";
}

// Map JoySpace `pageType` numeric codes to the URL path used in browser links.
// Observed values: 13=markdown doc (/pages), 18=online sheet (/sheets),
// 10=collection/table (/pages works). Unknown types default to /pages, which
// JoySpace's own frontend also uses as its fallback.
function pageTypeToUrlPath(pageType) {
  switch (Number(pageType)) {
    case 18:
      return "sheets";
    default:
      return "pages";
  }
}

// Attachment blocks carry a `fileId` and a relative `url` like
// "/api/files/<fileId>" — neither is directly clickable. The real download
// entrypoint is `${API_BASE}/v1/files/<fileId>/link`, which 302-redirects to a
// signed eefs.jd.com URL (the same scheme inline images already arrive in).
function resolveAttachmentUrl(v) {
  const fileId =
    v.fileId ||
    (typeof v.url === "string" ? v.url.match(/\/files\/([^/?#]+)/)?.[1] : "") ||
    "";
  if (fileId) return `${DEFAULT_JOYSPACE_API_BASE}/v1/files/${fileId}/link`;
  const raw = v.url || v.link || "";
  if (/^https?:\/\//i.test(raw)) return raw;
  if (raw) return `${DEFAULT_JOYSPACE_API_BASE}${raw.startsWith("/") ? "" : "/"}${raw}`;
  return "";
}

function renderDocfile(node) {
  const v = node?.value || {};
  const id = v.id || v.pageId || "";
  if (!id) return "";
  const title = v.title || v.name || id;
  const path = pageTypeToUrlPath(v.pageType);
  const url = `https://joyspace.jd.com/${path}/${id}`;
  return `[${title}](${url})`;
}

function renderCell(cell, ctx = {}) {
  if (!cell || !Array.isArray(cell.children)) return "";
  // Recurse via renderBlock so nested blocks (images, lists) survive — not
  // just inline text. Previously images inside cells were silently dropped
  // because renderInline only reads text leaves.
  const parts = [];
  for (const child of cell.children) {
    const rendered = renderBlock(child, 0, ctx);
    if (rendered) parts.push(rendered);
  }
  // Markdown cells can't contain real newlines; collapse to <br>.
  return parts.join(" <br> ").replace(/\|/g, "\\|").replace(/\n+/g, " <br> ").trim();
}

// Depth-first search for a block of the given `type` anywhere under `node`
// (arrays, single blocks, or `.children`). Used to detect genuinely nested
// tables (a `table` block living inside a `table-cell`) before deciding how
// to render the outer table.
function containsBlockType(node, type) {
  if (node == null) return false;
  if (Array.isArray(node)) return node.some((n) => containsBlockType(n, type));
  if (typeof node !== "object") return false;
  if (node.type === type) return true;
  if (Array.isArray(node.children)) return containsBlockType(node.children, type);
  return false;
}

function tableHasNesting(block) {
  const rows = (block.children || []).filter((r) => r?.type === "table-row");
  for (const row of rows) {
    const cells = (row.children || []).filter((c) => c?.type === "table-cell");
    for (const cell of cells) {
      if (containsBlockType(cell.children, "table")) return true;
    }
  }
  return false;
}

// Render a table cell's content as HTML (used only inside a genuinely nested
// table). A GFM markdown table cell cannot itself contain another markdown
// table — JoySpace source docs do have this (a "需求详述/示意图" table whose
// left cell holds a full "阶段/校验/判断标准" sub-table) — so once nesting is
// detected the whole outer table renders as real HTML <table>, which Obsidian
// (and any Chromium-based renderer) displays as true nested tables. Bold/
// italic/strike/link markdown syntax inside cell text still works because
// Obsidian's HTML block rendering also runs its markdown post-processor over
// inline content.
function renderCellContentHtml(cell, ctx = {}) {
  if (!cell || !Array.isArray(cell.children)) return "";
  const parts = [];
  for (const child of cell.children) {
    if (child?.type === "table") {
      parts.push(renderTableHtml(child, ctx));
    } else if (child?.type === "foldable-block") {
      parts.push(renderFoldableBlockHtml(child, ctx));
    } else if ((child?.type === "img" || child?.type === "image") && ctx.includeImages !== false) {
      const url = child.url || child.src || "";
      const width = child.width ? ` width="${child.width}"` : "";
      parts.push(`<img src="${url}"${width} />`);
    } else if (child?.type === "img" || child?.type === "image") {
      // includeImages === false: skip, matches renderBlock's own img case.
      continue;
    } else {
      const rendered = renderBlock(child, 0, ctx);
      if (rendered) parts.push(rendered);
    }
  }
  return parts.join(" <br> ").replace(/\n+/g, " <br> ").trim();
}

function renderFoldableBlockHtml(block, ctx = {}) {
  const name = block.name || "";
  const parts = [];
  for (const child of block.children || []) {
    if (child?.type === "table") parts.push(renderTableHtml(child, ctx));
    else {
      const rendered = renderBlock(child, 0, ctx);
      if (rendered) parts.push(rendered);
    }
  }
  return `<details><summary>${name}</summary>${parts.join(" <br> ")}</details>`;
}

function renderTableHtml(block, ctx = {}) {
  const rows = (block.children || []).filter((r) => r?.type === "table-row");
  const rowsHtml = rows.map((row, rowIndex) => {
    const tag = rowIndex === 0 ? "th" : "td";
    const cells = (row.children || []).filter((c) => c?.type === "table-cell");
    const cellsHtml = cells.map((cell) => `<${tag}>${renderCellContentHtml(cell, ctx)}</${tag}>`).join("");
    return `<tr>${cellsHtml}</tr>`;
  });
  return `<table>${rowsHtml.join("")}</table>`;
}

function renderTable(block, ctx = {}) {
  const rows = (block.children || []).filter((r) => r?.type === "table-row");
  if (rows.length === 0) return "";
  // A table whose cell holds another table can't be expressed in GFM markdown
  // (a cell can't contain a newline-separated sub-table). Fall back to real
  // HTML <table> so the nesting survives instead of collapsing into an
  // escaped `\|`-joined mess.
  if (tableHasNesting(block)) return renderTableHtml(block, ctx);
  const matrix = rows.map((r) => (r.children || []).filter((c) => c?.type === "table-cell").map((c) => renderCell(c, ctx)));
  const cols = Math.max(...matrix.map((r) => r.length));
  // Pad ragged rows
  for (const r of matrix) while (r.length < cols) r.push("");
  const header = matrix[0];
  const sep = new Array(cols).fill("---");
  const body = matrix.slice(1);
  const lines = [
    "| " + header.join(" | ") + " |",
    "| " + sep.join(" | ") + " |",
    ...body.map((r) => "| " + r.join(" | ") + " |"),
  ];
  return lines.join("\n");
}

function renderBlock(block, depth = 0, ctx = {}) {
  if (block == null) return "";
  if (typeof block === "string") return block;
  if (Array.isArray(block)) {
    return block.map((b) => renderBlock(b, depth, ctx)).filter(Boolean).join("\n\n");
  }
  const type = block.type || "";

  // Headings: h1..h6 or heading-1..heading-6
  const hMatch = type.match(/^h(?:eading-?)?([1-6])$/i);
  if (hMatch) {
    const n = Number(hMatch[1]);
    return "#".repeat(n) + " " + renderInline(block.children);
  }

  switch (type) {
    case "p":
    case "paragraph":
      return renderInline(block.children);
    case "list":
    case "list-item": {
      const marker = block.value === "number" || block.ordered ? "1." : "-";
      const indent = "  ".repeat(depth);
      return `${indent}${marker} ${renderInline(block.children)}`;
    }
    case "divider":
    case "hr":
      return "---";
    case "img":
    case "image": {
      if (ctx.includeImages === false) return "";
      const url = block.url || block.src || "";
      const alt = block.alt || "";
      const wh = block.width && block.height ? ` <!-- ${block.width}x${block.height} -->` : "";
      return `![${alt}](${url})${wh}`;
    }
    case "diagram": {
      // JoySpace flowcharts (drawio/diagram) render only in-browser; the API
      // exposes no image/XML export, so we emit a pointer to the host doc.
      const link = ctx.docLink || "";
      const label = block.name || block.title || "流程图";
      return link
        ? `> 📊 ${label}（图形需在 JoySpace 中查看）：${link}`
        : `> 📊 ${label}（图形需在 JoySpace 中查看）`;
    }
    case "attachment": {
      const v = block.value || {};
      const name = v.fileName || v.name || "attachment";
      const url = resolveAttachmentUrl(v);
      const size = v.size ? ` (${Math.round(v.size / 1024)} KB)` : "";
      return `📎 [${name}](${url})${size}`;
    }
    case "docfile":
      return renderDocfile(block);
    case "link": {
      // A rare block-level link (usually inline; guard anyway).
      const href = block.url || block.link || "";
      const label = Array.isArray(block.children) ? block.children.map(renderInline).join("") : "";
      if (!href) return label;
      return label ? `[${label}](${href})` : href;
    }
    case "code":
    case "code-block": {
      const lang = block.lang || block.language || "";
      const code =
        typeof block.value === "string"
          ? block.value
          : renderInline(block.children);
      return "```" + lang + "\n" + code + "\n```";
    }
    case "blockquote":
    case "quote":
      return renderInline(block.children)
        .split("\n")
        .map((l) => "> " + l)
        .join("\n");
    case "table":
      return renderTable(block, ctx);
    case "table-row":
    case "table-cell":
      // Should be handled by renderTable; if loose, just inline.
      return renderInline(block.children);
    case "foldable-block": {
      // Collapsible section (e.g. "存档/研发不用看"). The default recursive
      // case would silently drop `block.name` and just emit the children —
      // losing the label that tells a reader *why* this content is folded.
      // Render as HTML <details> so the label survives and Obsidian still
      // shows it as a collapsible block in reading view.
      const inner = (block.children || [])
        .map((c) => renderBlock(c, depth, ctx))
        .filter(Boolean)
        .join("\n\n");
      return `<details><summary>${block.name || ""}</summary>\n\n${inner}\n\n</details>`;
    }
    default:
      // Unknown/container block (e.g. multi-column): recurse into children
      // so nested images/diagrams survive. Recursing wins over inline text
      // because a container may hold block-level nodes (img) whose content
      // renderInline cannot see.
      if (Array.isArray(block.children)) {
        const nested = renderBlock(block.children, depth, ctx);
        if (nested) return nested;
        const inline = renderInline(block.children);
        if (inline) return inline;
      }
      if (typeof block.value === "string") return block.value;
      return "";
  }
}

function blocksToMarkdown(content, ctx = {}) {
  if (!content) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((b) => renderBlock(b, 0, ctx)).filter(Boolean).join("\n\n").trim();
  }
  if (typeof content === "object") {
    if (Array.isArray(content.content)) return blocksToMarkdown(content.content, ctx);
    return renderBlock(content, 0, ctx);
  }
  return String(content);
}

function parseArgs(argv) {
  const options = {
    url: "",
    pageId: "",
    save: "",
    raw: false,
    includeImages: true,
    tenantCode:
      process.env.JMECHAT_TENANT_CODE ||
      process.env.JMECHAT_tenantCode ||
      DEFAULT_TENANT_CODE,
    deviceId:
      process.env.JMECHAT_DEVICE_ID || process.env.JMECHAT_deviceId || "noDeviceId",
    startupToken: process.env.JMECHAT_token || "",
    configPath: path.join(process.env.HOME || process.env.USERPROFILE || os.homedir(), ".joyclaw", "openclaw.json"),
  };
  for (let i = 0; i < argv.length; i += 1) {
    const cur = argv[i];
    const next = argv[i + 1];
    switch (cur) {
      case "--url":
      case "--page-url":
        options.url = next || "";
        i += 1;
        break;
      case "--page-id":
        options.pageId = next || "";
        i += 1;
        break;
      case "--save":
        options.save = next || "";
        i += 1;
        break;
      case "--raw":
        options.raw = true;
        break;
      case "--no-images":
      case "--no-image":
        options.includeImages = false;
        break;
      case "--images":
        options.includeImages = true;
        break;
      case "--tenant-code":
        options.tenantCode = next || options.tenantCode;
        i += 1;
        break;
      case "--device-id":
        options.deviceId = next || options.deviceId;
        i += 1;
        break;
      case "--startup-token":
        options.startupToken = next || options.startupToken;
        i += 1;
        break;
      case "--config":
        options.configPath = next || options.configPath;
        i += 1;
        break;
      default:
        break;
    }
  }
  return options;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (!opts.url && !opts.pageId) {
    throw new Error("Provide --url <joyspace url> or --page-id <id>");
  }
  const pageId = opts.pageId || extractPageIdFromUrl(opts.url);

  const auth = await resolveAuth(opts);
  const { teamHeaderId } = requireTenantConfig(opts.tenantCode);
  const cookieHeader = buildCookieHeader(auth);

  const basic = await requestJoySpaceJson({
    method: "GET",
    url: `/v3/pages/${pageId}/basic?sendRecent=0`,
    cookieHeader,
    teamHeaderId,
  });

  const contentResp = await requestJoySpaceJson({
    method: "POST",
    url: "/v1/pages/content",
    cookieHeader,
    teamHeaderId,
    body: { pageId },
  });

  const docLink = opts.url || `https://joyspace.jd.com/pages/${pageId}`;
  const markdown = blocksToMarkdown(contentResp?.content ?? contentResp, {
    includeImages: opts.includeImages,
    docLink,
  });

  const result = {
    authMode: auth.mode,
    pageId,
    title: basic?.title || basic?.name || "",
    teamId: basic?.team_id || "",
    folderId: basic?.folder_id || "",
    fullName: basic?.full_name || "",
    pageType: basic?.page_type ?? null,
    creator: basic?.creator || basic?.create_user || "",
    updater: basic?.update_user || "",
    updatedAt: basic?.update_time || basic?.updated_at || "",
    link: docLink,
    content: markdown,
  };

  if (opts.save) {
    const target = path.resolve(opts.save);
    const header = `<!-- JoySpace pageId: ${pageId} -->\n<!-- title: ${result.title} -->\n\n`;
    await fs.writeFile(target, header + markdown, "utf8");
    result.savedTo = target;
  }

  if (opts.raw) {
    result.raw = { basic, content: contentResp };
  }

  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}

const __filename = fileURLToPath(import.meta.url);

// Robust entry-point check: realpathSync dereferences symlinks (e.g. a script
// invoked through ~/.claude/skills/...) and normalizes paths, avoiding the
// `file://${process.argv[1]}` string-equality trap that silently skips main().
function isMainModule() {
  if (!process.argv[1]) return false;
  try {
    return realpathSync(process.argv[1]) === realpathSync(__filename);
  } catch {
    return false;
  }
}

if (isMainModule()) {
  main().catch((error) => {
    process.stderr.write(`Error: ${error instanceof Error ? error.message : String(error)}\n`);
    process.exitCode = 1;
  });
}

export { resolveAuth, extractPageIdFromUrl, blocksToMarkdown };
