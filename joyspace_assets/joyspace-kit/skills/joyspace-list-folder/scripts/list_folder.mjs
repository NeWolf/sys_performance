#!/usr/bin/env node
/**
 * joyspace-list-folder — List all documents under a JoySpace space/folder with hierarchy.
 *
 * Usage:
 *   node list_folder.mjs --space-id <id> [--folder-id <id>] [--json] [--output <path>]
 *   node list_folder.mjs --url "https://joyspace.jd.com/teams/<teamId>/<folderId>" [--json]
 *
 * Strategy (BFS):
 *   1. GET /v1/folders/{id}/children?teamId=X&sort=offset — get sub-folders
 *   2. GET /v1/spaces/{teamId}/pages?categoryId={folderId} — get pages in folder
 *   3. Recurse: for each page with isParent=1, treat it as a container and fetch its children
 */

import { getMeToken, JOYSPACE_API_BASE, TEAM_HEADER_ID } from "../../shared-auth.mjs";
import fs from "node:fs";

// ---- Args ----
const args = process.argv.slice(2);
function getArg(name, def = null) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
}
function hasFlag(name) { return args.includes(name); }

let spaceId = getArg("--space-id");
let folderId = getArg("--folder-id");
let teamId = getArg("--team-id");
const urlArg = getArg("--url");
const asJson = hasFlag("--json");
const outputPath = getArg("--output") || getArg("-o");
const maxDepth = parseInt(getArg("--max-depth", "8"), 10);

if (urlArg) {
  const m = urlArg.match(/teams\/([^/]+)\/([^/?#]+)/);
  if (m) {
    teamId = m[1];
    folderId = m[2];
  } else {
    const m2 = urlArg.match(/documents\/([^/?#]+)/);
    if (m2) {
      folderId = m2[1];
    } else {
      const m3 = urlArg.match(/spaces\/([^/?#]+)/);
      if (m3) spaceId = m3[1];
    }
  }
}

if (!spaceId && !folderId) {
  console.error(`Usage: list_folder.mjs --space-id <id> [--folder-id <id>] [--json] [--output path]
       list_folder.mjs --url "https://joyspace.jd.com/teams/<teamId>/<folderId>"

Options:
  --space-id      JoySpace space ID (root of the tree)
  --folder-id     Specific folder ID to list (optional, defaults to space root)
  --team-id       Team ID (auto-detected from URL or folder detail)
  --url           JoySpace team/folder URL to extract IDs from
  --json          Output as JSON
  --output, -o    Save result to file
  --max-depth     Max folder recursion depth (default: 8)`);
  process.exit(2);
}

// ---- Auth ----
const { meToken } = getMeToken();

// ---- API helpers ----
async function fetchJson(url, opts = {}) {
  const resp = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      Cookie: `me_token=${meToken}`,
      "x-team-id": TEAM_HEADER_ID,
      ...opts.headers,
    },
    method: opts.method || "GET",
    body: opts.body,
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status} for ${url}`);
  return resp.json();
}

async function getFolderChildren(fId) {
  const tid = teamId || TEAM_HEADER_ID;
  const data = await fetchJson(
    `${JOYSPACE_API_BASE}/v1/folders/${fId}/children?teamId=${tid}&sort=offset&start=0&limit=200`
  );
  return data.status === "success" ? (data.data || []) : [];
}

async function getFolderPages(fId, start = 0) {
  const tid = teamId || TEAM_HEADER_ID;
  const data = await fetchJson(
    `${JOYSPACE_API_BASE}/v1/spaces/${tid}/pages?categoryId=${fId}&length=200&start=${start}&sort=-updated_at`
  );
  return data.status === "success" ? (data.data?.pages || []) : [];
}

async function getFolderDetail(fId) {
  const data = await fetchJson(`${JOYSPACE_API_BASE}/v1/folders/${fId}`);
  return data.status === "success" ? data.data : null;
}

// ---- Data structures ----
const folders = new Map(); // id -> { name, parent, children[], pages[], amount }
const allPages = new Map(); // page_id -> page info
const pageChildren = new Map(); // parent_page_id -> [child pages]

// ---- BFS: Build folder tree + collect pages ----
const _visitedFolders = new Set();

async function traverseFolder(rootId, depth = 0) {
  if (depth > maxDepth || _visitedFolders.has(rootId)) return;
  _visitedFolders.add(rootId);

  // Get sub-folders
  const children = await getFolderChildren(rootId);
  const childIds = [];
  for (const child of children) {
    const cid = child.id;
    const name = child.title || child.name || "?";
    if (!folders.has(cid)) {
      folders.set(cid, { name, parent: rootId, children: [], pages: [], amount: child.amount || 0 });
    }
    childIds.push(cid);
  }

  if (folders.has(rootId)) {
    folders.get(rootId).children = childIds;
  } else {
    folders.set(rootId, { name: "(root)", parent: null, children: childIds, pages: [], amount: 0 });
  }

  // Get pages in this folder
  const pages = await getFolderPages(rootId);
  for (const p of pages) {
    const pid = p.id;
    if (!allPages.has(pid)) {
      allPages.set(pid, {
        id: pid,
        title: p.title || "",
        url: `https://joyspace.jd.com/pages/${pid}`,
        isParent: p.isParent || 0,
        folder_id: rootId,
        page_type: p.page_type,
        creator: p.author?.name || "",
        updated_at: p.updated_at || "",
      });
      folders.get(rootId).pages.push(allPages.get(pid));
    }
  }

  // Small delay to be polite
  await new Promise(r => setTimeout(r, 100));

  // Recurse into sub-folders
  for (const cid of childIds) {
    await traverseFolder(cid, depth + 1);
  }

  // Recurse into pages that have children (isParent=1)
  for (const p of pages) {
    if (p.isParent && !_visitedFolders.has(p.id)) {
      await traverseSubPages(p.id, rootId, depth + 1);
    }
  }
}

async function traverseSubPages(parentPageId, parentFolderId, depth) {
  if (depth > maxDepth || _visitedFolders.has(parentPageId)) return;
  _visitedFolders.add(parentPageId);

  const subPages = await getFolderPages(parentPageId);
  if (!subPages.length) return;

  if (!pageChildren.has(parentPageId)) pageChildren.set(parentPageId, []);

  for (const p of subPages) {
    const pid = p.id;
    if (!allPages.has(pid)) {
      const pageInfo = {
        id: pid,
        title: p.title || "",
        url: `https://joyspace.jd.com/pages/${pid}`,
        isParent: p.isParent || 0,
        folder_id: parentFolderId,
        page_type: p.page_type,
        creator: p.author?.name || "",
        updated_at: p.updated_at || "",
      };
      allPages.set(pid, pageInfo);
      pageChildren.get(parentPageId).push(pageInfo);
    }

    // Recurse deeper
    if (p.isParent) {
      await traverseSubPages(pid, parentFolderId, depth + 1);
    }
  }

  await new Promise(r => setTimeout(r, 100));
}

// ---- Resolve IDs ----
async function resolveIds() {
  if (spaceId && !folderId) {
    folderId = spaceId;
  }
  if (folderId && !spaceId) {
    const detail = await getFolderDetail(folderId);
    if (detail) {
      spaceId = detail.space_id || (detail.full_path || "").split("$@$")[0];
      if (!teamId) teamId = detail.team_id;
    }
  }
  if (folderId && !teamId) {
    const detail = await getFolderDetail(folderId);
    if (detail) teamId = detail.team_id;
  }
}

// ---- Render tree ----
function renderTree() {
  const lines = [];
  const rendered = new Set();

  function renderPage(page, indent) {
    if (rendered.has(page.id)) return;
    rendered.add(page.id);
    const prefix = "  ".repeat(indent);
    lines.push(`${prefix}📄 ${page.title}`);
    lines.push(`${prefix}   ${page.url}`);
    const children = pageChildren.get(page.id) || [];
    for (const child of children) {
      renderPage(child, indent + 1);
    }
  }

  function renderFolder(fId, indent) {
    const folder = folders.get(fId);
    if (!folder) return;
    const prefix = "  ".repeat(indent);
    const pageCount = folder.pages.length;
    const subCount = folder.children.length;
    if (indent > 0 || fId !== (folderId || spaceId)) {
      lines.push(`${prefix}📁 ${folder.name}  (${pageCount} docs, ${subCount} subfolders)`);
    } else {
      lines.push(`${prefix}📁 ${folder.name}  [ROOT]  (${pageCount} docs, ${subCount} subfolders)`);
    }

    for (const page of folder.pages) {
      renderPage(page, indent + 1);
    }

    for (const childId of folder.children) {
      renderFolder(childId, indent + 1);
    }
  }

  const rootId = folderId || spaceId;
  renderFolder(rootId, 0);
  return lines.join("\n");
}

function buildJsonOutput() {
  function folderToJson(fId) {
    const folder = folders.get(fId);
    if (!folder) return null;
    return {
      id: fId,
      name: folder.name,
      type: "folder",
      children: [
        ...folder.pages.map(p => pageToJson(p)),
        ...folder.children.map(cid => folderToJson(cid)).filter(Boolean),
      ],
    };
  }

  function pageToJson(page) {
    const children = (pageChildren.get(page.id) || []).map(c => pageToJson(c));
    return {
      id: page.id,
      title: page.title,
      url: page.url,
      type: "page",
      creator: page.creator,
      page_type: page.page_type,
      ...(children.length ? { children } : {}),
    };
  }

  const rootId = folderId || spaceId;
  return {
    space_id: spaceId,
    folder_id: folderId || null,
    team_id: teamId,
    total_folders: folders.size,
    total_pages: allPages.size,
    tree: folderToJson(rootId),
  };
}

// ---- Main ----
async function main() {
  await resolveIds();

  process.stderr.write(`Scanning folder ${folderId} (team: ${teamId})...\n`);

  const rootId = folderId || spaceId;
  const rootDetail = await getFolderDetail(rootId);
  if (rootDetail) {
    folders.set(rootId, {
      name: rootDetail.name || rootDetail.title || "(root)",
      parent: rootDetail.parent || null,
      children: [],
      pages: [],
      amount: rootDetail.amount || 0,
    });
  }

  process.stderr.write("  BFS traversing folder tree + pages...\n");
  await traverseFolder(rootId);
  process.stderr.write(`  Found ${folders.size} folders, ${allPages.size} pages\n`);

  let output;
  if (asJson) {
    output = JSON.stringify(buildJsonOutput(), null, 2);
  } else {
    const header = [
      `Space: ${spaceId}`,
      folderId ? `Folder: ${folderId}` : null,
      teamId ? `Team: ${teamId}` : null,
      `Folders: ${folders.size}  |  Pages: ${allPages.size}`,
      "─".repeat(50),
      "",
    ].filter(Boolean).join("\n");
    output = header + renderTree();
  }

  if (outputPath) {
    fs.writeFileSync(outputPath, output, "utf8");
    process.stderr.write(`  Result saved to: ${outputPath}\n`);
  }

  console.log(output);
}

main().catch(err => {
  console.error("Error:", err.message);
  process.exit(1);
});
