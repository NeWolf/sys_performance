// Publish a JoySpace page from a Slate block tree instead of Markdown.
//
// POST /v1/pages without `contentType` stores the block array verbatim, so
// tables, merged cells, column widths and colours survive exactly as the
// template had them. Markdown conversion loses all of that, which is why the
// admission report writes into the template tree and uploads it as-is.
import fs from "node:fs/promises";
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import {
  resolveAuth,
  requestJoySpaceJson,
  resolveTargetLocation,
  requireTenantConfig,
  buildCookieHeader,
} from "./import_markdown_doc.js";

const DEFAULT_TENANT_CODE = "CN.JD.GROUP";
const PAGE_TYPE = 13;
const ALLOWED_TYPES = new Set(["p", "list", "table", "table-row", "table-cell"]);

// The server assigns ids to some blocks; they must be ignored when comparing
// what we sent with what came back.
function canonical(node) {
  if (Array.isArray(node)) return node.map(canonical);
  if (!node || typeof node !== "object") return node;
  const out = {};
  for (const key of Object.keys(node).sort()) {
    if (key === "id") continue;
    out[key] = canonical(node[key]);
  }
  return out;
}

// Fail closed: an unknown block type could be an image, a file or an embed.
function assertImageFree(blocks) {
  const walk = (node, depth) => {
    if (depth > 12) throw new Error("Block tree nested too deeply");
    if (Array.isArray(node)) return node.forEach((child) => walk(child, depth + 1));
    if (!node || typeof node !== "object") throw new Error("Invalid block node");
    if (node.type !== undefined && !ALLOWED_TYPES.has(node.type)) {
      throw new Error(`Unsupported block type "${node.type}"; image-free report required`);
    }
    if (Array.isArray(node.children)) node.children.forEach((child) => walk(child, depth + 1));
  };
  if (!Array.isArray(blocks) || blocks.length === 0) {
    throw new Error("Image-free report required; regenerate and review before publishing");
  }
  walk(blocks, 0);
}

function parseArgs(argv) {
  const options = { tenantCode: DEFAULT_TENANT_CODE, deviceId: "noDeviceId" };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    const value = argv[index + 1];
    switch (flag) {
      case "--file":
        options.filePath = value; index += 1; break;
      case "--title":
        options.title = value; index += 1; break;
      case "--page-url":
        options.pageUrl = value; index += 1; break;
      case "--parent-page-id":
        options.parentPageId = value; index += 1; break;
      case "--source-sha256":
        options.sourceSha256 = value; index += 1; break;
      case "--tenant-code":
        options.tenantCode = value; index += 1; break;
      case "--device-id":
        options.deviceId = value; index += 1; break;
      case "--config":
        options.configPath = value; index += 1; break;
      case "--image-free":
        options.imageFree = true; break;
      default:
        throw new Error(`Unknown argument: ${flag}`);
    }
  }
  return options;
}

async function createPage({ title, blocks, location, cookieHeader, teamHeaderId }) {
  const body = {
    title,
    page_type: PAGE_TYPE,
    // No contentType: that is what keeps the block tree verbatim.
    content: blocks,
    team_id: location.teamId,
  };
  if (location.folderId) body.folder_id = location.folderId;
  if (location.categoryId) body.category_id = location.categoryId;
  return requestJoySpaceJson({
    method: "POST",
    url: "/v1/pages",
    cookieHeader,
    teamHeaderId,
    body,
  });
}

async function verifyPage({ pageId, title, blocks, cookieHeader, teamHeaderId }) {
  const data = await requestJoySpaceJson({
    method: "POST",
    url: "/v1/pages/content",
    cookieHeader,
    teamHeaderId,
    body: { pageId, page_id: pageId },
  });
  const stored = data?.content?.content ?? data?.content;
  if (!Array.isArray(stored) || stored.length === 0) {
    throw new Error("Published page returned no content; verify manually before retrying");
  }
  assertImageFree(stored);
  // The server prepends a title paragraph, so match our blocks as a suffix
  // rather than expecting a byte-identical array.
  const sent = canonical(blocks);
  const back = canonical(stored);
  const offset = back.length - sent.length;
  if (offset < 0 || JSON.stringify(back.slice(offset)) !== JSON.stringify(sent)) {
    throw new Error("Published content does not match the reviewed report");
  }
  const storedTitle = data?.title ?? data?.name;
  if (typeof storedTitle === "string" && storedTitle.trim() && storedTitle.trim() !== title) {
    throw new Error("Published title does not match the requested title");
  }
  return { title, blocks: stored.length };
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (!options.filePath) throw new Error("--file is required");
  if (!options.title) throw new Error("--title is required");

  const raw = await fs.readFile(options.filePath, "utf8");
  if (options.sourceSha256
      && createHash("sha256").update(raw).digest("hex") !== options.sourceSha256) {
    throw new Error("Report changed after review");
  }
  const blocks = JSON.parse(raw);
  if (options.imageFree) assertImageFree(blocks);

  const auth = await resolveAuth(options);
  const { teamHeaderId } = requireTenantConfig(options.tenantCode);
  const cookieHeader = buildCookieHeader(auth);
  const location = await resolveTargetLocation({
    pageUrl: options.pageUrl,
    parentPageId: options.parentPageId,
    cookieHeader,
    teamHeaderId,
  });
  const created = await createPage({
    title: options.title, blocks, location, cookieHeader, teamHeaderId,
  });
  if (!created?.id) throw new Error("JoySpace did not return a page id");
  const verification = await verifyPage({
    pageId: created.id, title: options.title, blocks, cookieHeader, teamHeaderId,
  });

  console.log(JSON.stringify({
    authMode: auth.mode,
    pageId: created.id,
    title: verification.title,
    link: created.link || `https://joyspace.jd.com/pages/${created.id}`,
    locationSource: location.source,
    verified: true,
    sourceSha256: options.sourceSha256 || createHash("sha256").update(raw).digest("hex"),
    imageFree: options.imageFree === true,
    blocks: verification.blocks,
  }, null, 2));
}

const __filename = fileURLToPath(import.meta.url);

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

export { assertImageFree, canonical, parseArgs };