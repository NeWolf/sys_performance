# JoySpace Slate block types (observed)

JoySpace markdown pages (page_type=13) are stored server-side as a Slate-style block tree.
Top-level structure: `raw.content.content[]` (array of block nodes).

Each block looks like:
```json
{ "type": "p", "id": "FGhPOS", "children": [ { "text": "..." } ] }
```

## Confirmed block types

| type | meaning | extra fields | rendering tips |
|------|---------|--------------|----------------|
| `p`  | paragraph | optional `header: 1..6` to upgrade to heading | when `header` set, render as `#`*N |
| `list` | bullet/number list | `listType: "bullet"\|"number"`; nested via `children` of type `list` | indent per nesting depth |
| `img` | image block | `url` (required), NO alt/caption field — alt text is dropped on markdown ingest | render `![](url)` on its own line |
| `divider` | horizontal rule | — | `---` |
| `table` / `table-row` / `table-cell` | table | cells contain block children; inline marks in cells don't render bold in glow | flatten to GitHub-flavored md table |
| `attachment` | file attachment | `fileName`, `fileId`, `url` (relative `/api/files/<id>`), `size` | render as `📎 [fileName](full-url) (NN KB)`; url resolved to `apijoyspace.jd.com/v1/files/<fileId>/link` |
| `code` | code block | `language` | fence with ```lang |
| `blockquote` | quote | nested block children | prefix each child line with `> ` |

## Inline marks (on leaf text nodes)

```json
{ "text": "hello", "bold": true, "italic": true, "code": true, "link": "https://..." }
```

Render order: `link` → `code` → `bold` → `italic` from outside in (matches what JoySpace's own renderer does).

## Markdown-ingest behaviors (POST /v1/pages, page_type=13)

These are what JoySpace does when you submit `{contentType:"markdown", content:[{value:"..."}]}` — verified 2026-05-28 by round-trip create + read:

- Headings `## foo` → `{type:"p", header:2, children:[{text:"foo"}]}`. They are NOT a separate `heading` block.
- Images `![alt](url)` → `{type:"img", url:"..."}` — **alt text dropped**, no caption field.
- Image URLs are stored verbatim; JoySpace does not download or proxy. Local paths break silently.
- Tables: GFM tables round-trip into `table`/`table-row`/`table-cell` blocks correctly.
- Code fences: language tag preserved.
- Hard line breaks inside a paragraph become `\n` inside a single `{text:...}` leaf, not a new block.

## Useful endpoints around content shape

- `POST https://apijoyspace.jd.com/v1/pages/content` body `{pageId}` → returns `{data: {pageType, content:[...]}}`
- `GET  https://apijoyspace.jd.com/v3/pages/<id>/basic?sendRecent=0` → returns metadata (title, team_id, folder_id, editing_content_id, page_status, author, …)
- `DELETE https://apijoyspace.jd.com/v1/pages/<id>` → soft delete (verified working — returns `{status:"success"}`)
- `GET  https://apijoyspace.jd.com/v1/pages/recent` → returns up to ~20 recent pages with rich metadata
- `POST https://apijoyspace.jd.com/v1/pages` → create
- **No working REST update endpoint** as of 2026-05-28 — see `references/edit-endpoint-probe.md`

## Auth pattern that works on this machine

`Cookie: me_token=<token>` + `x-team-id: 00046419`, where `me_token` comes from the `hioffice-auth` skill (HiOffice client on :8988 fallback works without any env var setup).
