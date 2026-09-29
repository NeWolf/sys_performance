# JoySpace Kit — Claude Code Skill

JoySpace document operations: read, search, list, upload images, import markdown, read online sheets, and upload xlsx as online sheets.

## URL Routing (IMPORTANT — read this first)

When user provides a JoySpace URL, choose the correct tool based on URL pattern:

| URL Pattern | Tool | Action |
|-------------|------|--------|
| `joyspace.jd.com/pages/<id>` | **joyspace-read** | Read single document content |
| `joyspace.jd.com/sheets/<id>` | **joyspace-read-sheet** | Read online sheet as structured JSON |
| `joyspace.jd.com/teams/<teamId>/<folderId>` | **joyspace-list-folder** | List all docs in folder |
| `joyspace.jd.com/documents/<id>` | **joyspace-list-folder** | List all docs in folder |

⚠️ **WARNING:** `/documents/<id>` is a FOLDER view (despite the misleading name "documents"). It contains MULTIPLE documents inside. You MUST use **joyspace-list-folder** for `/documents/` URLs, NEVER joyspace-read. This is a JoySpace-specific quirk — the word "documents" here means "document library/folder", not a single document.

⚠️ **`/sheets/<id>` is an online spreadsheet**, not a collaborative doc. Use **joyspace-read-sheet** to get its cells as JSON. `joyspace-read` won't work on these — the export API returns Canvas render data, not blocks.

## Prerequisites

- Node.js >= 18
- HiOffice desktop client running (ports 8988-9006) for auth
- Network access to `apijoyspace.jd.com` (internal or VPN)

## Skill Scripts Location

All scripts are under the installed package root (`$JOYSPACE_KIT`). The `joyspace-kit init` command resolves the path automatically in the generated CLAUDE.md.

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
- **Genuinely nested tables** (a table living inside another table's cell — a recurring PRD pattern: outer "需求详述/示意图" table whose cell holds a full "阶段/校验是否通过/..." sub-table) render as real HTML `<table>` for the whole outer table, so Obsidian/Chromium shows true nesting. Non-nested tables are untouched (still plain `| a | b |` markdown). Collapsible sections (`foldable-block`, e.g. "存档/研发不用看") render as `<details><summary>{label}</summary>...</details>` so the label isn't silently dropped.
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

**How it works (BFS):**
1. `GET /v1/folders/{id}/children?teamId=X` — get sub-folders
2. `GET /v1/spaces/{teamId}/pages?categoryId={folderId}` — get pages in folder
3. For pages with `isParent=1`, recurse using same API with page ID as categoryId

**Example:**
```bash
# By space ID
node $JOYSPACE_KIT/skills/joyspace-list-folder/scripts/list_folder.mjs --space-id e7c8eXROjya45595GNDy --json --output /tmp/tree.json

# By documents URL
node $JOYSPACE_KIT/skills/joyspace-list-folder/scripts/list_folder.mjs --url "https://joyspace.jd.com/documents/WKh8SsRcMyrATwsWQ963"
```

**Limitations:**
- Page discovery relies on search API; very old/rarely-accessed pages may be missed
- Pass domain-specific `--keywords` to improve coverage for niche content
- `spaceIds` filter in search API is unreliable; client-side `full_path` filtering is used

---

## 7. joyspace-read-sheet — Read a JoySpace Online Sheet

**When to use:** User pastes a `joyspace.jd.com/sheets/<id>` URL and wants the cells as structured data (for analysis, diff, migration, feeding into another tool). Works for both old (Shimo) and new (WPS) engine sheets — the export API routes automatically.

**Command:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-sheet/scripts/read_joyspace_sheet.mjs --url "https://joyspace.jd.com/sheets/<id>"
```

**How it works:** triggers JoySpace's server-side export (`/v2/sm/export`), polls until a signed .xlsx URL is ready, downloads it, and parses with exceljs. Typical wall-clock: 2-4 seconds.

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--url` | Full JoySpace sheet URL | — |
| `--page-id` | Sheet page id (alternative to --url) | — |
| `--sheet <name>` | Only return the named worksheet | all sheets |
| `--save-xlsx <path>` | Also save the raw .xlsx file to this path | — |
| `--max-rows <n>` | Cap rows per sheet | 1000 |
| `--max-cols <n>` | Cap cols per sheet | 100 |
| `--keep-formulas` | Emit `{formula, result}` objects instead of just the result | false |
| `--timeout-ms <n>` | Poll timeout for export readiness | 30000 |

**Output:** JSON with `pageId`, `title`, `teamId`, `folderId`, `sheetsUrl`, `sheets[]` (each with `name`, `rowCount`, `colCount`, `rows[][]`).

**Cell value rules:**
- number → number
- string → string  
- date → ISO string
- formula → evaluated result (unless `--keep-formulas`)
- rich text / hyperlink → plain text
- empty → null

**Example — read all sheets:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-sheet/scripts/read_joyspace_sheet.mjs --url "https://joyspace.jd.com/sheets/xxx"
```

**Example — one sheet only, save raw xlsx:**
```bash
node $JOYSPACE_KIT/skills/joyspace-read-sheet/scripts/read_joyspace_sheet.mjs --url "https://joyspace.jd.com/sheets/xxx" --sheet "Sheet1" --save-xlsx ./out.xlsx
```

**Pitfalls:**
- **Rate limit (errCode 40302 "操作频繁"):** kicks in after ~3 rapid exports of the same file. Cooldown ~30s. Script surfaces this clearly.
- **Freshly uploaded sheet (errCode 50001 "关联操作未完成"):** if the sheet was just created via `xlsx-to-joyspace`, JoySpace's WPS backend hasn't finished associating it with its render engine yet. This defers for minutes to hours and CANNOT be forced from outside (verified — opening in a browser doesn't help). Wait and retry, or design your flow to not read back immediately. See §8 caveat.
- **Merged cells / styles / charts are lost** — this reads values, not layout. Use `--save-xlsx` if you need to preserve fidelity locally.
- **Formulas are evaluated by JoySpace before export** — the returned value is the *result*, unless you pass `--keep-formulas` to also get the source expression.

---

## 8. xlsx-to-joyspace — Upload xlsx/xls/csv as JoySpace Online Sheet

**When to use:** User has a local `.xlsx / .xls / .csv` and wants it as a live JoySpace sheet (not attached, but a real editable spreadsheet page). Common flow: read a sheet with joyspace-read-sheet → transform locally → upload the modified xlsx as a new sheet.

**Command:**
```bash
node $JOYSPACE_KIT/skills/xlsx-to-joyspace/scripts/upload_joyspace_sheet.mjs --file /abs/path/data.xlsx
```

**How it works:** four-step JoySpace upload API. (1) `/v3/web-files/init` to allocate an uploadId, (2) PUT the bytes to a signed S3-like URL, (3) `/v3/web-files/complete` to finalize, (4) `/v2/pages` with `pageType=18` to create the sheet page from the uploaded file.

**Options:**

| Flag | Description | Default |
|------|-------------|---------|
| `--file` | Local `.xlsx / .xls / .csv` (required) | — |
| `--title` | Sheet title | file basename |
| `--page-url` | Team folder URL (`joyspace.jd.com/teams/<teamId>/<folderId>` or a page URL) — new sheet lands as a sibling in the same folder | personal space root |

**Output:** JSON with `pageId`, `title`, `url` (the `/sheets/<id>` link), `teamId`, `folderId`, `sizeBytes`, `parts`, plus an `engineReadyHint` string reminding the caller about the init caveat below.

**⚠️ Engine-init caveat:** The upload succeeds and the sheet page exists in JoySpace (visible in the UI, accessible via URL). But `joyspace-read-sheet` on it will return `errCode 50001 "关联操作未完成"` for a while — potentially minutes to hours — because JoySpace's WPS backend defers the file-to-engine association. **This is a JoySpace-side lazy-init that neither joyspace-kit nor o2's `webcli:joyspace upload` can force from the outside** — verified 2026-07-17 by both approaches, both stayed 50001 after triggering all documented client-side handshakes.

**In practice:**
- **A freshly-uploaded sheet CANNOT be read back immediately.** Don't build automations that expect `upload → read` within seconds. Give it time; try again later.
- **The sheet is fine for its normal role** (viewing, sharing, downloading, being read by later automation) — the caveat only affects the export API path used by `joyspace-read-sheet`.
- If you must round-trip fast, do the read + transform BEFORE uploading — see the "Cell-level editing" section below.

**Not yet supported:**
- Files > 16 MB (would need multi-part chunked uploads). Script errors out.
- Sub-page mounting via `--parent-page-id` (like markdown-to-joyspace). `/v2/pages` may not honor `categoryId` the same way `/v1/pages` does; not verified.

**Example — upload to personal space:**
```bash
node $JOYSPACE_KIT/skills/xlsx-to-joyspace/scripts/upload_joyspace_sheet.mjs --file ./report.xlsx --title "月度报表"
```

**Example — upload into a team folder:**
```bash
node $JOYSPACE_KIT/skills/xlsx-to-joyspace/scripts/upload_joyspace_sheet.mjs --file ./data.csv --page-url "https://joyspace.jd.com/teams/kqF7HjtfULriSpSNFH88j/DP7mzVA2I64JPhJYtETH"
```

---

## 8.5. Cell-level editing — how to "edit" a JoySpace sheet

**joyspace-kit CANNOT edit individual cells of an existing JoySpace sheet in place.** The sheet body is rendered on Canvas by the WPS / Shimo engine; the collaborative write path is a private socket.io + OT (operational-transform) binary protocol with no public REST surface. There is no way to `UPDATE cell(row=3, col=2) = 'foo'` on JoySpace's servers from a headless script — this is a product-level limitation, not a joyspace-kit gap. (o2's `webcli:joyspace edit` only handles collaborative-DOC bodies via Slate-editor JS, not sheet cells.)

**The idiom joyspace-kit supports is "read → transform → upload as a new sheet → delete the old one":**

```
   joyspace-read-sheet <src-url>          → structured rows[][]
                                                   ↓
   Node + exceljs (locally)               modify: change cells, add rows/cols,
                                          apply formulas, merge, style, etc.
                                                   ↓
   xlsx-to-joyspace <new.xlsx>            → NEW sheet page (new pageId!)
                                                   ↓
   DELETE /v1/pages/<old-id>              (optional cleanup of the source)
```

**When this is fine:**
- Personal reports, batch data pipelines, "regenerate weekly digest" jobs
- Any automation where the caller controls both ends
- The delta between old and new is entire-sheet-scale (not "one cell")

**When this is NOT fine — the new pageId will BREAK:**
- Shared links pointing to the old sheet
- @-mentions, comments, and revision history on the old sheet
- Team-shared permissions attached to the old sheet
- Anything relying on pageId stability

**For team-shared documents that must keep their pageId, joyspace-kit has no answer today.** You need either:
1. Manual editing in the JoySpace web UI (unavoidable for team docs)
2. o2's `webcli:joyspace edit` (only works for **collaborative DOCs**, not sheet cells anyway — so still no cell-editing path even there)

**Example — "filter status=休假, save as new sheet":**
```bash
# Read source sheet
SRC=$(node $JOYSPACE_KIT/skills/joyspace-read-sheet/scripts/read_joyspace_sheet.mjs \
  --url "https://joyspace.jd.com/sheets/<src-id>")

# ... your local transform: use exceljs (already a kit dependency) to build a new .xlsx
# from the modified rows. The transform script is your responsibility.

# Upload as new sheet
node $JOYSPACE_KIT/skills/xlsx-to-joyspace/scripts/upload_joyspace_sheet.mjs \
  --file ./filtered.xlsx --title "员工在职名单"

# Optionally delete the old one (curl)
curl -X DELETE "https://apijoyspace.jd.com/v1/pages/<src-id>" \
  -H "Cookie: me_token=$TOKEN" -H "focus-team-id: 00046419" -H "focus-client: WEB"
```

Note the round-trip caveat: the newly uploaded sheet will be temporarily unreadable via `joyspace-read-sheet` (see §8 engine-init caveat). Design flows so downstream steps don't immediately read it back.

---

## 9. hioffice-auth — Get Auth Token

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

### Upload images and create a doc with screenshots
```
User: "把这个截图发到JoySpace"
→ Run joyspace-upload-image --file screenshot.png --auto-scratch --delete-scratch
→ Get imgUrl from result
→ Optionally create a doc with the image embedded
```

### List all documents in a space
```
User: "列出新品运营中心空间下所有文档"
→ Run joyspace-list-folder --space-id e7c8eXROjya45595GNDy
→ Show tree structure or save as JSON
```

### Read an online sheet and turn it into structured data
```
User: "把这个表格的数据取出来 https://joyspace.jd.com/sheets/xxx"
→ Run joyspace-read-sheet --url "https://joyspace.jd.com/sheets/xxx"
→ Get sheets[].rows[][] as JSON
→ Feed into downstream analysis, transformation, etc.
```

### Round-trip: read → modify → upload as a new sheet
```
User: "把这个表格里过滤掉状态=休假的行，另存为一份新表"
→ Run joyspace-read-sheet --url <src> to get rows
→ Filter locally, use exceljs (already a dependency) to build a new .xlsx
→ Run xlsx-to-joyspace --file ./filtered.xlsx --title "过滤后的员工表"
→ Return the new sheet URL
Note: the newly-uploaded sheet cannot be read back via joyspace-read-sheet
for a while (minutes to hours — JoySpace-side engine lazy init, no way to
force it). It's fully usable for viewing/sharing right away. If the caller
needs the new data immediately, use the local `filtered.xlsx` — you already
have it before the upload.
```

---

## Auth & Permissions

All scripts authenticate via the local HiOffice client. The auth chain:
1. `ME_TOKEN` / `SSO_TOKEN` env vars (if set)
2. `~/.joyclaw/openclaw.json` cookies
3. `JMECHAT_token` via tokenGrant
4. Local HiOffice legacy exchange (ports 8988-9006)

**If you get `NEED_ACCESS_RIGHT` or `PAGE NOT EXIST`:** The authenticated account doesn't have permission to that document. This is NOT a script error — the doc owner must grant access, or the user must log in with the correct account in HiOffice.
