---
name: joyspace-kit
description: JoySpace 文档操作工具集 — 读取、搜索、列表、上传图片、导入 markdown。当用户粘贴 joyspace.jd.com 链接（/pages/、/documents/、/teams/）、要求读取/总结/导出/备份 JoySpace 文档、搜索 JoySpace 页面、列出空间或文件夹下的文档、把截图或本地图片上传到 JoySpace、或把 markdown 文件发布成 JoySpace 文档时触发。
---

# JoySpace Kit — Claude Code Skill

JoySpace document operations: read, search, list, upload images, and import markdown.

## URL Routing (IMPORTANT — read this first)

When user provides a JoySpace URL, choose the correct tool based on URL pattern:

| URL Pattern | Tool | Action |
|-------------|------|--------|
| `joyspace.jd.com/pages/<id>` | **joyspace-read** | Read single document content |
| `joyspace.jd.com/teams/<teamId>/<folderId>` | **joyspace-list-folder** | List all docs in folder |
| `joyspace.jd.com/documents/<id>` | **joyspace-list-folder** | List all docs in folder |

⚠️ **WARNING:** `/documents/<id>` is a FOLDER view (despite the misleading name "documents"). It contains MULTIPLE documents inside. You MUST use **joyspace-list-folder** for `/documents/` URLs, NEVER joyspace-read. This is a JoySpace-specific quirk — the word "documents" here means "document library/folder", not a single document.

## Prerequisites

- Node.js >= 18
- HiOffice desktop client running (ports 8988-9006) for auth
- Network access to `apijoyspace.jd.com` (internal or VPN)

## Skill Scripts Location

All scripts are under the installed package root (`$JOYSPACE_KIT`), resolved automatically at install time.

---

## 1. joyspace-read — Read a JoySpace Document

**When to use:** User pastes a `joyspace.jd.com/pages/xxx` URL and wants to read, summarize, quote, export, or back up the document. Or gives a pageId directly.

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-doc/scripts/read_joyspace_doc.js --url "https://joyspace.jd.com/pages/<id>"
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--url` | Full JoySpace page URL | — |
| `--page-id` | Page id (alternative to --url) | — |
| `--save <path>` | Also write markdown to a local file | — |
| `--no-images` | Omit inline images (text-only output, saves tokens) | false (images exported by default) |
| `--raw` | Include raw API responses (debugging) | false |

**Output:** JSON with `title`, `pageType`, `content` (reconstructed markdown), `link`, etc.

**Example — read and summarize:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-doc/scripts/read_joyspace_doc.js --url "https://joyspace.jd.com/pages/avIBigHE3WXuUmspPCa6"
```

**Example — save to local file:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-doc/scripts/read_joyspace_doc.js --url "https://joyspace.jd.com/pages/xxx" --save ./output.md
```

**Example — text-only (skip images, saves tokens):**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-doc/scripts/read_joyspace_doc.js --url "https://joyspace.jd.com/pages/xxx" --no-images --save ./output.md
```

**Pitfalls:**
- `PAGE NOT EXIST` or `NEED_ACCESS_RIGHT` means the auth token's account lacks permission — not a script bug.
- Only `page_type=13` (markdown) is fully rendered. Sheets/mind maps/boards degrade to inline text; use `--raw` for those.
- **Images are exported by default** as `![](url)`. Pass `--no-images` for text-only output (better for pure read/summarize, saves tokens). The URLs are JoySpace CDN links (`apijoyspace.jd.com/v1/files/.../link`) that load on the JD intranet/VPN, but won't display outside the JD network.
- **Flowcharts (drawio / `diagram` blocks) cannot be exported as images** — JoySpace renders them only in-browser and exposes no image/XML API. They are emitted as a `> 📊` placeholder pointing to the host document URL. Open that URL in JoySpace to view the actual diagram.
- **Attachments (files embedded mid-document) are exported** as `📎 [文件名](url) (NN KB)`. The `url` is a full, downloadable link — `apijoyspace.jd.com/v1/files/<fileId>/link` — which 302-redirects to a signed `eefs.jd.com` URL. Loads on the JD intranet/VPN. (Older versions emitted a broken relative `/api/files/<id>` path; fixed in 0.3.2.)
- First call via HiOffice legacy auth is slow (~1-3s port probe); subsequent calls reuse the token.

---

## 2. joyspace-search — Search JoySpace Documents

**When to use:** User wants to find a JoySpace page by keyword in title or content.

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-search/scripts/search_joyspace.mjs --query "关键词" [--limit 20] [--json]
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--query` / `-q` | Search keyword (>= 3 chars) | required |
| `--limit` | Max results | 20 |
| `--start` | Offset for pagination | 0 |
| `--scope` | `global` or `received` | global |
| `--json` | Output raw JSON | false |

**Example:**
```bash
node $JOYSPACE_KIT/skills/joyspace-search/scripts/search_joyspace.mjs --query "新品运营" --limit 10 --json
```

---

## 3. joyspace-list — List Recent Pages

**When to use:** User wants to see what they've been working on, find a doc by partial title, or browse recent JoySpace activity.

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-list/scripts/list_joyspace.mjs [--limit 20] [--grep keyword] [--json]
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--limit` | Max results | 20 |
| `--grep` | Filter by title/preview substring | — |
| `--json` | Output raw JSON | false |

**Example:**
```bash
node $JOYSPACE_KIT/skills/joyspace-list/scripts/list_joyspace.mjs --grep "PRD" --limit 5
```

---

## 4. joyspace-upload-image — Upload Image to JoySpace CDN

**When to use:** User wants to embed a local image into a JoySpace document. JoySpace only supports images by URL (no inline base64), and external image hosts often fail on the office network. This skill uploads to JoySpace's internal CDN.

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-upload-image/scripts/upload_image.mjs --file /path/to/image.png --auto-scratch --delete-scratch
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--file` | Local image path (required) | — |
| `--page-id` | Upload to an existing page | — |
| `--auto-scratch` | Create temp scratch page, upload there | — |
| `--delete-scratch` | Delete scratch page after upload (CDN URL stays valid) | false |

**Output:** JSON with `imgUrl` (the stable CDN URL to use in markdown), `dimensions`.

**Example:**
```bash
node $JOYSPACE_KIT/skills/joyspace-upload-image/scripts/upload_image.mjs --file ./screenshot.png --auto-scratch --delete-scratch
# Returns: { imgUrl: "https://apijoyspace.jd.com/v1/files/xxx/link", ... }
```

---

## 5. markdown-to-joyspace — Import Markdown as JoySpace Document

**When to use:** User wants to create a JoySpace doc from a `.md` file. Auto-uploads local image references to JoySpace's CDN. Can create the page in the personal space, or **mount it as a sub-page under a specified parent page**.

**Command:**
```bash
node $JOYSPACE_KIT/skills/markdown-to-joyspace/scripts/import_markdown_doc.js --file /path/to/doc.md
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--file` | Local markdown file (required) | — |
| `--title` | Document title | derived from H1 or filename |
| `--parent-page-id` | Mount as a **sub-page (child)** of this page. Accepts a raw page id or a full `joyspace.jd.com/pages/<id>` URL. The child inherits the parent's team/space. | — (personal space root) |
| `--page-url` | Create as a **sibling in the same folder** as this page (folder co-location, not parent-child) | — |
| `--skip-image-upload` | Do not upload local images; leave references as-is | false |

**Placement precedence:** `--parent-page-id` (child-of-page, via `categoryId`) wins over `--page-url` (sibling-in-folder, via `folderId`). With neither flag, the page is created at the personal space root.

**Example — create at personal space root:**
```bash
node $JOYSPACE_KIT/skills/markdown-to-joyspace/scripts/import_markdown_doc.js --file ./PRD.md --title "我的PRD"
```

**Example — mount as a sub-page under a parent doc:**
```bash
node $JOYSPACE_KIT/skills/markdown-to-joyspace/scripts/import_markdown_doc.js \
  --file ./TRD.md --title "TRD - 功能标题" \
  --parent-page-id 1YuY9GEhmW4g64hBIxGw
```

**Output:** JSON with `pageId`, `title`, `link`, `teamId`, `parentPageId` (set when mounted as a sub-page), and an `images` upload report.

**Pitfall:** if `--parent-page-id` is wrong or you lack edit rights on it, the API returns `NEED_ACCESS_RIGHT` / `PAGE_NOT_EXIST` — that's a permission/id issue on the parent, not a script bug.

---

## 6. joyspace-list-folder — List All Documents in a Folder/Space with Hierarchy

**When to use:** User wants to see all documents under a JoySpace space or folder, preserving the directory tree structure. Useful for bulk import, migration, or auditing.

**URL routing:** Use this tool (NOT joyspace-read) when the URL matches:
- `/teams/<teamId>/<folderId>` — team folder view
- `/documents/<id>` — folder/space document view (this is a FOLDER, not a single page)

Only use joyspace-read for `/pages/<id>` URLs (single document).

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-list-folder/scripts/list_folder.mjs --space-id <spaceId> [--folder-id <folderId>] [--json] [--output path]
```

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--space-id` | JoySpace space ID (root of tree) | — |
| `--folder-id` | Specific folder to list (subtree only) | space root |
| `--url` | JoySpace team/folder URL to extract IDs | — |
| `--json` | Output as structured JSON tree | false (text tree) |
| `--output`, `-o` | Save result to file | — |
| `--max-depth` | Max folder recursion depth | 8 |
| `--keywords` | Extra search keywords (comma-separated) | built-in set |

**Example:**
```bash
node $JOYSPACE_KIT/skills/joyspace-list-folder/scripts/list_folder.mjs --url "https://joyspace.jd.com/documents/WKh8SsRcMyrATwsWQ963"
```

---

## 7. hioffice-auth — Get Auth Token

**When to use:** Usually called automatically by other scripts. Run manually only for debugging.

**Command:**
```bash
node $JOYSPACE_KIT/skills/hioffice-auth/scripts/hioffice-auth.mjs
```

**Output:** JSON `{ meToken: "ee.xxx...", authMode: "legacy" }`

---

## Common Workflows

### Read a JoySpace URL and summarize
```
User: "帮我看看这个文档 https://joyspace.jd.com/pages/xxx 在说什么"
→ Run joyspace-read with --url
→ Summarize the `content` field
```

### Search for a topic and read the top result
```
User: "搜一下全域通相关的文档"
→ Run joyspace-search --query "全域通"
→ From results, run joyspace-read --page-id <top pageId>
→ Summarize or quote
```

### Import a local markdown file to JoySpace
```
User: "把这个文件发布到JoySpace：./notes.md"
→ Run markdown-to-joyspace --file ./notes.md
→ Return the created doc URL
```

### Publish a markdown file as a sub-page under a specific parent
```
User: "把这个TRD发到JoySpace的这个页面下面：https://joyspace.jd.com/pages/xxx"
→ Extract the parent page id from the URL (the part after /pages/)
→ Run markdown-to-joyspace --file ./TRD.md --parent-page-id xxx
→ Return the created sub-page URL (confirm parentPageId in the output)
Note: if the user says "发布到某个页面下" but gives no URL/id, ASK for the
parent page link first — do not fall back to creating at the space root.
```

### List all documents in a space
```
User: "列出新品运营中心空间下所有文档"
→ Run joyspace-list-folder --space-id <spaceId>
→ Show tree structure or save as JSON
```

---

## Auth & Permissions

All scripts authenticate via the local HiOffice client. The auth chain:
1. `ME_TOKEN` / `SSO_TOKEN` env vars (if set)
2. `~/.joyclaw/openclaw.json` cookies
3. `JMECHAT_token` via tokenGrant
4. Local HiOffice legacy exchange (ports 8988-9006)

**If you get `NEED_ACCESS_RIGHT` or `PAGE NOT EXIST`:** The authenticated account doesn't have permission to that document. This is NOT a script error — the doc owner must grant access, or the user must log in with the correct account in HiOffice.
