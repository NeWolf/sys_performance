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
import { inflateSync } from "node:zlib";
import { findBrowser, startBrowser } from "./chart_browser.mjs";
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

// Normalize only the known admission-template heading in a publication copy.
// Keep the reviewed file/hash and every other text/style field untouched.
function publicationBlocks(blocks, title) {
  const copy = structuredClone(blocks);
  const first = copy?.[0];
  if (first?.type === 'p' && first.children?.length === 1
      && first.children[0]?.text === '性能准入模版'
      && !first.children[0].type && !first.children[0].children
      && typeof title === 'string' && title.trim()) {
    first.children[0].text = title;
  }
  return copy;
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
    const fields = {
      p: ["textIndent", "indent", "align"],
      list: ["value", "orderedType", "header", "textIndent", "indent"],
      table: ["width", "height"],
      "table-row": ["height"],
      "table-cell": ["rowSpan", "colSpan", "hidden", "bgColor", "align"],
    };
    const leafFields = ["text", "bold", "italic", "underline", "strike", "code",
      "fontSize", "fontColor", "bgColor"];
    const allowed = node.type === undefined ? leafFields
      : ["type", "id", "children", ...fields[node.type]];
    if (Object.keys(node).some(key => !allowed.includes(key))) throw new Error("Unsupported block fields");
    if (node.type === undefined) {
      if (typeof node.text !== "string") throw new Error("Invalid text node");
      if (Object.values(node).some(value => value !== null && typeof value === "object")) {
        throw new Error("Invalid text attributes");
      }
      return;
    }
    if (!Array.isArray(node.children) || !node.children.length) throw new Error("Missing block children");
    for (const [key, value] of Object.entries(node)) {
      if (key === "children") continue;
      if (value !== null && typeof value === "object"
          && !(node.type === "table" && key === "width" && Array.isArray(value)
               && value.length && value.every(n => Number.isInteger(n) && n > 0 && n <= 4000))) {
        throw new Error("Invalid block attributes");
      }
    }
    node.children.forEach((child) => walk(child, depth + 1));
  };
  if (!Array.isArray(blocks) || blocks.length === 0) {
    throw new Error("Image-free report required; regenerate and review before publishing");
  }
  walk(blocks, 0);
}

// Local chart vocabulary is deliberately separate from JoySpace's remote `img`.
// Never pass data URLs into createPage or reuse the Markdown scratch uploader.
function assertLocalCharts(blocks) {
  if (!Array.isArray(blocks) || !blocks.length) throw new Error("Invalid chart report");
  let count = 0;
  const maxBytes = 2 * 1024 * 1024;
  const prefix = "data:image/png;base64,";
  for (const block of blocks) {
    if (block?.type !== "image") {
      assertImageFree([block]);
      continue;
    }
    count += 1;
    const keys = Object.keys(block).sort();
    if (JSON.stringify(keys) !== JSON.stringify(
      ["children", "height", "src", "type", "width"])) {
      throw new Error("Unsupported chart fields");
    }
    if (count > 2 || block.width !== 1200 || block.height !== 400
        || JSON.stringify(block.children) !== '[{"text":""}]') {
      throw new Error("Invalid chart metadata");
    }
    if (typeof block.src !== "string" || !block.src.startsWith(prefix)
        || block.src.length > prefix.length + 4 * Math.ceil(maxBytes / 3)) {
      throw new Error("Only embedded PNG charts are supported");
    }
    const encoded = block.src.slice(prefix.length);
    const png = Buffer.from(encoded, "base64");
    if (png.toString("base64") !== encoded || png.length < 57 || png.length > maxBytes
        || !png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) {
      throw new Error("Invalid PNG signature or encoding");
    }
    let offset = 8, channels = 0, ended = false, idatClosed = false;
    let compressedSize = 0;
    const compressedChunks = [];
    while (offset + 12 <= png.length) {
      const length = png.readUInt32BE(offset);
      if (length > png.length - offset - 12) throw new Error("Truncated PNG");
      const kind = png.toString("latin1", offset + 4, offset + 8);
      let crc = 0xffffffff;
      for (const byte of png.subarray(offset + 4, offset + 8 + length)) {
        crc ^= byte;
        for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
      }
      if (((crc ^ 0xffffffff) >>> 0) !== png.readUInt32BE(offset + 8 + length)) {
        throw new Error("Invalid PNG checksum");
      }
      const data = png.subarray(offset + 8, offset + 8 + length);
      if (offset === 8) {
        if (kind !== "IHDR" || length !== 13 || data.readUInt32BE(0) !== 1200
            || data.readUInt32BE(4) !== 400 || data[8] !== 8
            || ![2, 6].includes(data[9]) || data[10] || data[11] || data[12]) {
          throw new Error("Invalid PNG dimensions or pixel format");
        }
        channels = data[9] === 2 ? 3 : 4;
      } else if (kind === "IDAT") {
        if (idatClosed) throw new Error("Non-contiguous PNG data");
        compressedChunks.push(data);
        compressedSize += length;
      } else if (kind === "IEND") {
        if (length || offset + length + 12 !== png.length) throw new Error("Invalid PNG end");
        ended = true;
      } else if (!["tEXt", "pHYs", "sRGB", "gAMA", "cHRM"].includes(kind)) {
        throw new Error("Unsupported PNG chunk");
      }
      if (compressedSize && kind !== "IDAT") idatClosed = true;
      offset += length + 12;
    }
    if (!compressedSize || !ended) throw new Error("Incomplete PNG");
    const stride = 1200 * channels + 1;
    const expected = stride * 400;
    const compressed = Buffer.concat(compressedChunks);
    const {buffer: pixels, engine} = inflateSync(compressed, {maxOutputLength: expected + 1, info: true});
    if (pixels.length !== expected || engine.bytesWritten !== compressed.length) {
      throw new Error("Invalid PNG scanline data");
    }
    for (let i = 0; i < expected; i += stride) {
      if (pixels[i] > 4) throw new Error("Invalid PNG scanline filter");
    }
  }
  if (!count) throw new Error("Chart mode requires local PNG charts");
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
      case "--inline-charts":
        options.inlineCharts = true; break;
      default:
        throw new Error(`Unknown argument: ${flag}`);
    }
  }
  if (options.imageFree && options.inlineCharts) throw new Error("Conflicting image modes");
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

function assertRemoteCharts(blocks) {
  let count = 0;
  for (const block of blocks) {
    if (block?.type !== 'img') { assertImageFree([block]); continue; }
    count++;
    if (Object.keys(block).some(key => !['type', 'id', 'url', 'width', 'height', 'children'].includes(key))
        || !/^https:\/\/apijoyspace\.jd\.com\/v1\/files\/[A-Za-z0-9_-]{1,128}\/link$/.test(block.url)
        || block.width !== 770 || block.height !== 257
        || JSON.stringify(block.children) !== '[{"text":""}]') throw new Error('Invalid remote chart');
  }
  if (count < 1 || count > 2) throw new Error('Invalid remote chart count');
}

async function uploadChart({ pageId, chart, index, cookieHeader, teamHeaderId }) {
  const form = new FormData();
  form.append('file', new Blob([Buffer.from(chart.src.split(',')[1], 'base64')],
    { type: 'image/png' }), `report-chart-${index + 1}.png`);
  const response = await fetch(`https://apijoyspace.jd.com/v2/pages/${pageId}/uploadImage`, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(25000),
    headers: { Cookie: cookieHeader, 'focus-team-id': teamHeaderId, 'focus-client': 'WEB',
      Referer: `https://joyspace.jd.com/pages/${pageId}` }, body: form,
  });
  if (!response.ok) throw new Error('Chart upload failed; do not retry');
  const data = await response.json();
  if (data.status !== 'success') throw new Error('Chart upload not confirmed');
  const node = { type: 'img', id: createHash('sha256').update(pageId + ':' + index).digest('hex').slice(0, 6),
    url: data.data?.imgUrl, width: 770, height: 257, children: [{ text: '' }] };
  assertRemoteCharts([node]);
  return node;
}

// All writes are single-attempt. Only read-back is polled while autosave settles.
async function publishCharts({ blocks, title, location, cookieHeader, teamHeaderId, browser, signal },
  deps = { createPage, verifyPage, uploadChart, delay: ms => new Promise(r => setTimeout(r, ms)) }) {
  assertLocalCharts(blocks);
  blocks = publicationBlocks(blocks, title);
  const initial = blocks.filter(block => block.type !== 'image');
  if (!initial.length) throw new Error('Chart report requires text');
  const common = { title, cookieHeader, teamHeaderId };
  signal?.throwIfAborted();
  const created = await deps.createPage({ ...common, blocks: initial, location });
  signal?.throwIfAborted();
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(created?.id)) throw new Error('Invalid created page id');
  const pageId = created.id;
  await deps.verifyPage({ ...common, pageId, blocks: initial });
  const final = [];
  let index = 0;
  for (const block of blocks) {
    signal?.throwIfAborted();
    final.push(block.type === 'image'
      ? await deps.uploadChart({ pageId, chart: block, index: index++, cookieHeader, teamHeaderId }) : block);
  }
  signal?.throwIfAborted();
  assertRemoteCharts(final);
  await browser.open(pageId);
  signal?.throwIfAborted();
  await browser.insertCharts(pageId, initial, final, title);
  let verification;
  for (let attempt = 0; attempt < 20; attempt++) {
    signal?.throwIfAborted();
    try {
      verification = await deps.verifyPage({ ...common, pageId, blocks: final, charts: true });
      break;
    } catch (error) {
      signal?.throwIfAborted();
      if (attempt === 19) throw error;
      await deps.delay(750);
    }
  }
  signal?.throwIfAborted();
  await browser.verifyImages(pageId, final, title);
  signal?.throwIfAborted();
  // Recheck after reload too: no concurrent edits may be silently accepted.
  verification = await deps.verifyPage({ ...common, pageId, blocks: final, charts: true });
  signal?.throwIfAborted();
  return { created, verification };
}

async function verifyPage({ pageId, title, blocks, cookieHeader, teamHeaderId, charts = false }) {
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
  if (charts) assertRemoteCharts(stored);
  else assertImageFree(stored);
  // The server prepends a title paragraph, so match our blocks as a suffix
  // rather than expecting a byte-identical array.
  const sent = canonical(blocks);
  const back = canonical(stored);
  const offset = back.length - sent.length;
  const prefix = stored[0];
  const titlePrefix = prefix?.type === 'p' && Array.isArray(prefix.children)
    && prefix.children.length > 0
    && prefix.children.every(leaf => typeof leaf.text === 'string' && !leaf.children && !leaf.type)
    && prefix.children.map(leaf => leaf.text).join('') === title;
  if (offset < 0 || offset > 1 || (offset === 1 && !titlePrefix)
      || JSON.stringify(back.slice(offset)) !== JSON.stringify(sent)) {
    throw new Error("Published content does not match the reviewed report");
  }
  // The content endpoint returns pageType/content, not the page title.
  const basic = await requestJoySpaceJson({
    method: "GET",
    url: `/v3/pages/${pageId}/basic?sendRecent=0`,
    cookieHeader,
    teamHeaderId,
  });
  const storedTitle = basic?.title ?? basic?.name;
  if (typeof storedTitle !=="string" || !storedTitle.trim() || storedTitle !== title) {
    throw new Error("Published title does not match the requested title");
  }
  return { title, blocks: stored.length };
}

async function main(signal) {
  const options = parseArgs(process.argv.slice(2));
  if (!options.filePath) throw new Error("--file is required");
  if (!options.title) throw new Error("--title is required");

  const raw = await fs.readFile(options.filePath);
  if (options.sourceSha256
      && createHash("sha256").update(raw).digest("hex") !== options.sourceSha256) {
    throw new Error("Report changed after review");
  }
  let blocks = JSON.parse(raw);
  let browser;
  if (options.inlineCharts) {
    assertLocalCharts(blocks);
    if (!blocks.some(block => block.type !== 'image')) throw new Error('Chart report requires text');
    const executable = await findBrowser();
    if (!executable) {
      console.log(JSON.stringify({ status: 'unsupported', reason: 'chart-browser-unavailable', remoteAttempted: false }));
      return;
    }
    try { browser = await startBrowser(executable, signal); }
    catch {
      console.log(JSON.stringify({ status: 'unsupported', reason: 'chart-browser-unavailable', remoteAttempted: false }));
      return;
    }
  } else {
    // Omitting a mode must never bypass the image-free safety boundary.
    assertImageFree(blocks);
  }
  let publication;
  try {
    signal?.throwIfAborted();
    const auth = await resolveAuth(options);
    signal?.throwIfAborted();
    const { teamHeaderId } = requireTenantConfig(options.tenantCode);
    const cookieHeader = buildCookieHeader(auth);
    const location = await resolveTargetLocation({
      pageUrl: options.pageUrl,
      parentPageId: options.parentPageId,
      cookieHeader,
      teamHeaderId,
    });
    signal?.throwIfAborted();
    let created, verification;
    if (browser) {
      await browser.authenticate(auth);
      ({ created, verification } = await publishCharts({
        title: options.title, blocks, location, cookieHeader, teamHeaderId, browser, signal,
      }));
    } else {
      blocks = publicationBlocks(blocks, options.title);
      created = await createPage({ title: options.title, blocks, location, cookieHeader, teamHeaderId });
      signal?.throwIfAborted();
      if (!created?.id) throw new Error('JoySpace did not return a page id');
      verification = await verifyPage({
        pageId: created.id, title: options.title, blocks, cookieHeader, teamHeaderId,
      });
    }

    signal?.throwIfAborted();
    publication = {
      authMode: auth.mode,
      pageId: created.id,
      title: verification.title,
      link: created.link || `https://joyspace.jd.com/pages/${created.id}`,
      locationSource: location.source,
      verified: true,
      sourceSha256: options.sourceSha256 || createHash("sha256").update(raw).digest("hex"),
      imageFree: options.imageFree === true,
      chartsVerified: options.inlineCharts === true,
      blocks: verification.blocks,
    };
  } finally {
    if (browser) await browser.close();
  }
  signal?.throwIfAborted();
  console.log(JSON.stringify(publication, null, 2));
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
  const controller = new AbortController();
  // Covers startup, authentication, requests, editor work, and cleanup.
  const deadline = setTimeout(() => controller.abort(new Error('Publication deadline exceeded')), 120000);
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (url, options = {}) => {
    controller.signal.throwIfAborted();
    return originalFetch(url, { ...options, redirect: 'error',
      signal: options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal });
  };
  main(controller.signal).catch(() => {
    console.error('Publication not confirmed; verify manually before retrying');
    process.exitCode = 1;
  }).finally(() => { clearTimeout(deadline); globalThis.fetch = originalFetch; });
}

export { assertImageFree, assertLocalCharts, assertRemoteCharts, canonical, parseArgs,
  publishCharts, verifyPage, uploadChart };