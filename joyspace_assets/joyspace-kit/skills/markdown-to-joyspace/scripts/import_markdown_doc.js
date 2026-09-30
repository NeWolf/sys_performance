import fs from "node:fs/promises";
import { realpathSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import { blocksToMarkdown } from "../../joyspace-read-doc/scripts/read_joyspace_doc.js";

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
  if (!cookie) {
    return "";
  }
  const escapedName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = cookie.match(new RegExp(`${escapedName}=([^;]+)`));
  return match?.[1]?.trim() || "";
}

export function readTokensFromConfigText(configText) {
  if (!configText?.trim()) {
    return { meToken: "", ssoToken: "" };
  }

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

export function buildCookieHeader({ meToken = "", ssoToken = "" }) {
  const cookies = [];
  if (meToken) {
    cookies.push(`me_token=${meToken}`);
  }
  if (ssoToken) {
    cookies.push(`sso.jd.com=${ssoToken}`);
  }
  return cookies.join("; ");
}

export function extractTitleFromMarkdown(markdown, filePath) {
  const heading = markdown.match(/^\s*#\s+(.+?)\s*$/m)?.[1]?.trim();
  if (heading) {
    return heading;
  }

  const stem = path.basename(filePath || "untitled.md", path.extname(filePath || "untitled.md"));
  return stem || "untitled";
}

export function normalizeLocationFromBasicInfo({ team_id, folder_id }) {
  const normalizedTeamId =
    typeof team_id === "string" && team_id.trim().startsWith("$") ? "root" : team_id?.trim();
  const normalizedFolderId = folder_id?.trim() || undefined;

  return {
    teamId: normalizedTeamId || "root",
    folderId: normalizedFolderId,
  };
}

export function buildCreatePagePayload({ title, markdown, teamId, folderId, categoryId }) {
  const payload = {
    title,
    page_type: 13,
    teamId,
    content: [{ value: markdown }],
    contentType: "markdown",
  };

  // categoryId mounts the new page as a CHILD of a page (sub-page);
  // folderId places it as a SIBLING inside a folder. categoryId wins.
  if (categoryId) {
    payload.categoryId = categoryId;
  } else if (folderId) {
    payload.folderId = folderId;
  }

  return payload;
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
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      functionId,
      body,
      appid: DEFAULT_APP_ID,
    }),
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

  return {
    aesKey: response.data.aesKey,
    content: response.data.content,
  };
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
        headers: {
          "Content-Type": "application/json",
          "X-AES-Key": aesKey,
        },
        body: content,
      });
      if (!response.ok) {
        throw new Error(`HiOffice ${port}: HTTP ${response.status}`);
      }
      const xAesKey = response.headers.get("X-AES-Key");
      if (!xAesKey) {
        throw new Error(`HiOffice ${port}: missing X-AES-Key`);
      }
      return {
        appToken: await response.text(),
        xAesKey,
      };
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
    return {
      mode: "env",
      meToken: envMeToken,
      ssoToken: envSsoToken,
    };
  }

  const configTokens = await readConfigTokens(
    options.configPath || path.join(process.env.HOME || process.env.USERPROFILE || os.homedir(), ".joyclaw", "openclaw.json"),
  );
  if (configTokens.meToken || configTokens.ssoToken) {
    return {
      mode: "config",
      ...configTokens,
    };
  }

  if (options.startupToken) {
    const meToken = await exchangeMeTokenFromStartupToken({
      startupToken: options.startupToken,
      tenantCode: options.tenantCode,
      deviceId: options.deviceId,
    });
    return {
      mode: "tokenGrant",
      meToken,
      ssoToken: "",
    };
  }

  if (options.deviceId) {
    const meToken = await exchangeMeTokenViaHiOffice({
      tenantCode: options.tenantCode,
      deviceId: options.deviceId,
    });
    return {
      mode: "legacy",
      meToken,
      ssoToken: "",
    };
  }

  throw new Error(
    "Unable to resolve JoyMe auth. Provide ME_TOKEN/SSO_TOKEN, configure ~/.joyclaw/openclaw.json cookies, or pass JMECHAT_token with deviceId/tenantCode.",
  );
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
    const errText = await response.text().catch(() => "");
    throw new Error(`${url} HTTP ${response.status} ${response.statusText} — ${errText.slice(0, 300)}`);
  }

  const json = await response.json();
  if (process.env.DEBUG_JOYSPACE) {
    process.stderr.write(`API ${method} ${url} => ${JSON.stringify(json).slice(0, 400)}\n`);
  }
  if (json?.status === "success" || json?.status === "0" || json?.status === 0) {
    return json.data;
  }
  if (json?.errorCode && json.errorCode !== "0") {
    throw new Error(`${url} — ${json.errorCode}: ${json.errorMsg || json.errMsg || 'unknown'}`);
  }
  // Some APIs return {code:0, msg:'success', data:{...}}
  if (json?.code === 0 || json?.code === "0") {
    return json.data;
  }
  // 未知失败：把整个 json 抛出，方便排错
  throw new Error(`${url} — unexpected response: ${JSON.stringify(json).slice(0, 300)}`);
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

// Accept either a raw page id or a full joyspace.jd.com/.../<id> URL.
function coercePageId(pageIdOrUrl) {
  const value = pageIdOrUrl?.trim();
  if (!value) return "";
  return /^https?:\/\//i.test(value) ? extractPageIdFromUrl(value) : value;
}

async function resolveTargetLocation({ pageUrl, parentPageId, cookieHeader, teamHeaderId }) {
  // Mount as a CHILD of a page (sub-page) — resolve the parent's team so the
  // child lands in the same space, and pass its id through as categoryId.
  if (parentPageId) {
    const parentId = coercePageId(parentPageId);
    const basicInfo = await requestJoySpaceJson({
      method: "GET",
      url: `/v3/pages/${parentId}/basic?sendRecent=0`,
      cookieHeader,
      teamHeaderId,
    });
    const { teamId } = normalizeLocationFromBasicInfo(basicInfo || {});
    return {
      teamId,
      categoryId: parentId,
      source: `sub-page-of:${parentId}`,
    };
  }

  if (!pageUrl) {
    return {
      teamId: "root",
      folderId: undefined,
      source: "private-space-root",
    };
  }

  const pageId = extractPageIdFromUrl(pageUrl);
  const basicInfo = await requestJoySpaceJson({
    method: "GET",
    url: `/v3/pages/${pageId}/basic?sendRecent=0`,
    cookieHeader,
    teamHeaderId,
  });

  const normalized = normalizeLocationFromBasicInfo(basicInfo || {});
  return {
    ...normalized,
    source: pageUrl,
  };
}

async function createJoySpacePage({ markdown, title, location, cookieHeader, teamHeaderId }) {
  const payload = buildCreatePagePayload({
    title,
    markdown,
    teamId: location.teamId,
    folderId: location.folderId,
    categoryId: location.categoryId,
  });

  return requestJoySpaceJson({
    method: "POST",
    url: "/v1/pages",
    cookieHeader,
    teamHeaderId,
    body: payload,
  });
}

export function assertImageFree(markdown) {
  if (!markdown.trim() || /!\[|<\s*(?:img|picture|svg|image)\b/i.test(markdown)) {
    throw new Error("Image-free report required; regenerate and review before publishing");
  }
}

// Compare visible text and table cell boundaries, not Markdown formatting bytes.
// Fail closed for unsupported round trips rather than accept partial content.
function normalizedText(markdown, inline = false) {
  return markdown.replace(/\r\n?/g, "\n").split("\n")
    .filter(line => inline || (!/^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(line)
      && !/^\s*(?:---+|\*\*\*+)\s*$/.test(line)))
    .map(line => inline ? line : line.replace(/^\s*(?:#{1,6}\s+|[-+*]\s+|\d+\.\s+|>\s*)/, ""))
    .join("\n")
    .replace(/<br\s*\/?\s*>/gi, " ")
    .replace(/<\/?u>/gi, "")
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&(?:nbsp|amp|lt|gt|quot|apos);/g, entity => ({
      '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'",
    })[entity])
    .replace(/(?<!\\)(\*\*|~~|`)(?=\S)([^\n]+?\S|\S)\1/g, "$2")
    .replace(/(?<![\\*])\*([^*\n]+)\*(?!\*)/g, "$1")
    .replace(/\\([\\`*{}\[\]()#+\-.!|_>])/g, "$1")
    .split("\n").map(line => line.replace(/[\t ]+/g, " ").trim())
    .filter(Boolean).join("\n");
}

// Split before unescaping or decoding entities: literal pipes stay inside cells.
function tableCells(line) {
  const cells = [];
  let cell = "";
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === "\\" && index + 1 < line.length) {
      cell += char + line[++index];
    } else if (char === "|") {
      cells.push(cell); cell = "";
    } else cell += char;
  }
  if (!cells.length) return null;
  cells.push(cell);
  if (!cells[0].trim()) cells.shift();
  if (!cells.at(-1).trim()) cells.pop();
  return cells;
}

function normalizedBody(markdown) {
  const lines = markdown.replace(/\r\n?/g, "\n").split("\n");
  const blocks = [];
  const separator = cells => cells?.length && cells.every(cell => /^\s*:?-{3,}:?\s*$/.test(cell));
  for (let index = 0; index < lines.length; index += 1) {
    const header = tableCells(lines[index]);
    const divider = tableCells(lines[index + 1] || "");
    if (header && separator(divider)) {
      if (header.length !== divider.length) throw new Error("Invalid report table");
      const rows = [header.map(cell => normalizedText(cell, true))];
      index += 1;
      while (index + 1 < lines.length) {
        const cells = tableCells(lines[index + 1]);
        if (!cells) break;
        if (cells.length !== header.length) throw new Error("Invalid report table width");
        rows.push(cells.map(cell => normalizedText(cell, true)));
        index += 1;
      }
      blocks.push(["table", rows]);
    } else {
      const text = normalizedText(lines[index]);
      if (text) blocks.push(["text", text]);
    }
  }
  return blocks.length ? JSON.stringify(blocks) : "";
}

export async function verifyJoySpacePage({ pageId, title, markdown, scratchPageId,
                                         imageFree = false, cookieHeader, teamHeaderId }) {
  if (typeof pageId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(pageId)
      || pageId === scratchPageId) throw new Error("Invalid formal page ID");
  const basic = await requestJoySpaceJson({
    method: "GET", url: `/v3/pages/${pageId}/basic?sendRecent=0`, cookieHeader, teamHeaderId,
  });
  if (basic?.title !== title || /^\[hermes-upload-scratch\]/i.test(basic.title)) {
    throw new Error("Published page title mismatch");
  }
  const result = await requestJoySpaceJson({
    method: "POST", url: "/v1/pages/content", cookieHeader, teamHeaderId, body: { pageId },
  });
  if (!Array.isArray(result?.content) || !result.content.length) {
    throw new Error("Published page content missing");
  }
  if (imageFree) {
    const inspect = node => {
      if (!node || typeof node !== "object") return;
      if (/^(?:img|image|picture|svg|diagram)$/i.test(node.type || "")) {
        throw new Error("Published page contains images");
      }
      for (const value of Object.values(node)) inspect(value);
    };
    inspect(result.content);
  }
  const actual = blocksToMarkdown(result.content);
  if (imageFree) assertImageFree(actual);
  const expected = normalizedBody(markdown);
  if (!expected || normalizedBody(actual) !== expected) {
    throw new Error("Published page content mismatch");
  }
  return { title: basic.title, sourceSha256: createHash("sha256").update(markdown).digest("hex") };
}

function parseArgs(argv) {
  const options = {
    filePath: "",
    title: "",
    pageUrl: "",
    parentPageId: "",
    teamId: "",
    folderId: "",
    tenantCode:
      process.env.JMECHAT_TENANT_CODE ||
      process.env.JMECHAT_tenantCode ||
      DEFAULT_TENANT_CODE,
    deviceId:
      process.env.JMECHAT_DEVICE_ID || process.env.JMECHAT_deviceId || "noDeviceId",
    startupToken: process.env.JMECHAT_token || "",
    configPath: path.join(process.env.HOME || process.env.USERPROFILE || os.homedir(), ".joyclaw", "openclaw.json"),
  };

  for (let index = 0; index < argv.length; index += 1) {
    const current = argv[index];
    const next = argv[index + 1];
    switch (current) {
      case "--file":
        options.filePath = next || "";
        index += 1;
        break;
      case "--title":
        options.title = next || "";
        index += 1;
        break;
      case "--page-url":
        options.pageUrl = next || "";
        index += 1;
        break;
      case "--parent-page-id":
        options.parentPageId = next || "";
        index += 1;
        break;
      case "--tenant-code":
        options.tenantCode = next || options.tenantCode;
        index += 1;
        break;
      case "--device-id":
        options.deviceId = next || options.deviceId;
        index += 1;
        break;
      case "--startup-token":
        options.startupToken = next || options.startupToken;
        index += 1;
        break;
      case "--config":
        options.configPath = next || options.configPath;
        index += 1;
        break;
      case "--image-free":
        options.imageFree = true;
        break;
      case "--source-sha256":
        options.sourceSha256 = next || "";
        index += 1;
        break;
      case "--skip-image-upload":
        options.skipImageUpload = true;
        break;
      default:
        break;
    }
  }

  return options;
}

// ---- Local image upload pre-processor ---------------------------------
// Scans markdown for image references whose URL is a local file path,
// uploads each via JoySpace's /v2/pages/<scratchId>/uploadImage endpoint
// (a temporary scratch page is used as host, then deleted — verified that
// uploaded files outlive the host page).

const LOCAL_IMG_REGEX = /!\[([^\]]*)\]\(([^)\s]+?)(?:\s+"[^"]*")?\)/g;

function isLocalImagePath(rawUrl) {
  if (!rawUrl) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(rawUrl)) return false; // http:, https:, data:, etc.
  if (rawUrl.startsWith("//")) return false;
  if (rawUrl.startsWith("#")) return false;
  return true;
}

function mimeForExt(ext) {
  return ({
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".heic": "image/heic", ".tiff": "image/tiff",
  })[ext.toLowerCase()] || "application/octet-stream";
}

async function createScratchPageForUpload({ cookieHeader, teamHeaderId }) {
  const url = `${DEFAULT_JOYSPACE_API_BASE}/v1/pages`;
  const body = JSON.stringify({
    title: `[hermes-upload-scratch] ${Date.now()}`,
    page_type: 13,
    teamId: "root",
    content: [{ value: "scratch upload host (auto-deleted)" }],
    contentType: "markdown",
  });
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "application/json",
      "Cookie": cookieHeader,
      "x-team-id": teamHeaderId,
    },
    body,
  });
  if (!response.ok) {
    throw new Error(`scratch page create failed: ${response.status}`);
  }
  const json = await response.json();
  if (json.status !== "success" || !json.data?.id) {
    throw new Error(`scratch page create returned: ${JSON.stringify(json).slice(0, 200)}`);
  }
  return json.data.id;
}

async function deleteScratchPage({ pageId, cookieHeader, teamHeaderId }) {
  const url = `${DEFAULT_JOYSPACE_API_BASE}/v1/pages/${pageId}`;
  const response = await fetch(url, {
    method: "DELETE",
    headers: { "Cookie": cookieHeader, "x-team-id": teamHeaderId },
  });
  if (!response.ok) throw new Error("Image scratch cleanup failed; remote leftovers may exist");
}

async function uploadOneImage({ filePath, hostPageId, cookieHeader, teamHeaderId }) {
  const buf = await fs.readFile(filePath);
  const filename = path.basename(filePath);
  const mime = mimeForExt(path.extname(filename));
  const form = new FormData();
  form.append("file", new Blob([buf], { type: mime }), filename);

  const url = `${DEFAULT_JOYSPACE_API_BASE}/v2/pages/${hostPageId}/uploadImage`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Cookie": cookieHeader,
      "focus-team-id": teamHeaderId,
      "focus-client": "WEB",
      "Referer": `https://joyspace.jd.com/pages/${hostPageId}`,
    },
    body: form,
  });
  const text = await response.text();
  let json = null; try { json = JSON.parse(text); } catch (_) {}
  if (!response.ok || !json || json.status !== "success" || !json.data?.imgUrl) {
    throw new Error(`upload failed for ${filename}: HTTP ${response.status} ${text.slice(0, 200)}`);
  }
  const imageUrl = new URL(json.data.imgUrl);
  if (imageUrl.protocol !== "https:" || imageUrl.username || imageUrl.password ||
      /[\s()]/.test(json.data.imgUrl)) {
    throw new Error("Invalid uploaded image URL");
  }
  return imageUrl.href;
}

export async function preprocessLocalImages({ markdown, markdownFilePath, cookieHeader, teamHeaderId }) {
  // Fail closed: unsupported image syntax must never silently lose images.
  const matches = [...markdown.matchAll(LOCAL_IMG_REGEX)];
  if (/<img\b/i.test(markdown) || matches.length !== (markdown.match(/!\[/g) || []).length) {
    throw new Error("Unsupported image syntax; use inline Markdown image links");
  }
  for (const match of matches) {
    const raw = match[2];
    if (/[()]/.test(raw) || (!isLocalImagePath(raw) && !/^https:\/\/[^\s]+$/i.test(raw))) {
      throw new Error("Unsupported image URL; use HTTPS or local files without parentheses");
    }
  }
  const baseDir = path.dirname(path.resolve(markdownFilePath));
  // collect unique local image paths preserving order
  const seen = new Map(); // absPath -> { rawUrl, alt }
  let m;
  LOCAL_IMG_REGEX.lastIndex = 0;
  while ((m = LOCAL_IMG_REGEX.exec(markdown)) !== null) {
    const [, alt, rawUrl] = m;
    if (!isLocalImagePath(rawUrl)) continue;
    const decoded = decodeURI(rawUrl);
    const abs = path.isAbsolute(decoded) ? decoded : path.resolve(baseDir, decoded);
    if (!seen.has(abs)) seen.set(abs, { rawUrl, alt });
  }
  if (seen.size === 0) {
    return { markdown, imageReport: { uploaded: 0, items: [] } };
  }

  // verify all files exist before doing any network work
  const items = [];
  for (const [abs, meta] of seen) {
    try {
      const stat = await fs.stat(abs);
      if (!stat.isFile()) throw new Error("not a file");
      items.push({ abs, rawUrl: meta.rawUrl, alt: meta.alt, sizeBytes: stat.size });
    } catch (e) {
      throw new Error(`local image not found or not a file: ${meta.rawUrl} (resolved: ${abs}) — ${e.message}`);
    }
  }

  const scratchId = await createScratchPageForUpload({ cookieHeader, teamHeaderId });
  let rewritten = markdown;
  try {
    for (const it of items) {
      const imgUrl = await uploadOneImage({
        filePath: it.abs,
        hostPageId: scratchId,
        cookieHeader,
        teamHeaderId,
      });
      it.imgUrl = imgUrl;
      // Match by resolved path so aliases such as img.png and ./img.png agree.
      rewritten = rewritten.replace(LOCAL_IMG_REGEX, (full, alt, raw) => {
        if (!isLocalImagePath(raw)) return full;
        const resolved = path.resolve(baseDir, decodeURI(raw));
        return resolved === it.abs ? `![${alt}](${imgUrl})` : full;
      });
    }
  } finally {
    await deleteScratchPage({ pageId: scratchId, cookieHeader, teamHeaderId });
  }

  return {
    markdown: rewritten,
    imageReport: {
      uploaded: items.length,
      scratchPageIdUsed: scratchId,
      items: items.map(({ rawUrl, abs, imgUrl, sizeBytes }) => ({ rawUrl, abs, imgUrl, sizeBytes })),
    },
  };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.filePath) {
    throw new Error("--file is required");
  }

  const rawMarkdown = await fs.readFile(options.filePath, "utf8");
  if (options.imageFree) assertImageFree(rawMarkdown);
  if (options.sourceSha256 && createHash("sha256").update(rawMarkdown).digest("hex") !== options.sourceSha256) {
    throw new Error("Report changed after review");
  }
  const auth = await resolveAuth(options);
  const { teamHeaderId } = requireTenantConfig(options.tenantCode);
  const cookieHeader = buildCookieHeader(auth);

  // Pre-process: upload local image references and rewrite markdown.
  const skipUpload = options.skipImageUpload || options.imageFree;
  const { markdown, imageReport } = skipUpload
    ? { markdown: rawMarkdown, imageReport: { skipped: true } }
    : await preprocessLocalImages({
        markdown: rawMarkdown,
        markdownFilePath: options.filePath,
        cookieHeader,
        teamHeaderId,
      });

  const title = options.title || extractTitleFromMarkdown(markdown, options.filePath);
  const location = await resolveTargetLocation({
    pageUrl: options.pageUrl,
    parentPageId: options.parentPageId,
    cookieHeader,
    teamHeaderId,
  });

  const created = await createJoySpacePage({
    markdown,
    title,
    location,
    cookieHeader,
    teamHeaderId,
  });
  const verification = await verifyJoySpacePage({
    pageId: created.id,
    title,
    markdown,
    scratchPageId: imageReport.scratchPageIdUsed,
    imageFree: options.imageFree,
    cookieHeader,
    teamHeaderId,
  });

  console.log(
    JSON.stringify(
      {
        authMode: auth.mode,
        pageId: created.id,
        title: verification.title,
        link: created.link || `https://joyspace.jd.com/pages/${created.id}`,
        teamId: created.team_id || location.teamId,
        folderId: created.folder_id || location.folderId || "",
        parentPageId: location.categoryId || "",
        locationSource: location.source,
        verified: true,
        sourceSha256: verification.sourceSha256,
        imageFree: options.imageFree === true,
        images: imageReport,
      },
      null,
      2,
    ),
  );
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
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  });
}

export { resolveAuth, requestJoySpaceJson, resolveTargetLocation, requireTenantConfig };
