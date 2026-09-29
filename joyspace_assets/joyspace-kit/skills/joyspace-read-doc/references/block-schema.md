# JoySpace Block Schema (observed)

Captured from a real `page_type=13` doc on 2026-05-28. JoySpace returns a
Slate-style block tree from `POST /v1/pages/content`. Each top-level entry is a
block object; inline runs live in `children` arrays as leaf nodes with a `text`
field plus optional formatting flags.

## Top-level block types seen in the wild

| `type`       | Notes                                                                   |
|--------------|-------------------------------------------------------------------------|
| `p`          | Paragraph. `children` = inline leaves.                                  |
| `list`       | Single list item. `value: "bullet" \| "number"`. NOT a wrapping list.   |
| `img`        | `url`, `width`, `height`. `children: [{text:""}]` placeholder.          |
| `divider`    | Horizontal rule. `children: [{text:""}]`.                               |
| `table`      | `children: [table-row...]`; `width: number[]` per column, `height`.     |
| `table-row`  | `children: [table-cell...]`.                                            |
| `table-cell` | Optional `bgColor`. `children` is usually `[{type:"p", children:[...]}]`|
| `attachment` | `value: { fileName, mimeType, size, fileId, url }`, `cardType:"block"`. |
| `docfile`    | JoySpace-to-JoySpace link card. `value: { id, title, pageType, teamId }`. Rendered as `[title](https://joyspace.jd.com/{pages\|sheets}/<id>)`. `pageType=18` → `/sheets/`, else `/pages/`. Can appear at block level or as an inline child of a `p`/`list`. |
| `foldable-block` | Collapsible section, e.g. "存档（旧方案，研发不用看）". `name` is the visible label, `children` is the folded content. Rendered as `<details><summary>{name}</summary>...</details>` so the label survives (previously dropped — see Gotchas). |
| `multi-column` | Side-by-side layout, `children: [multi-column-item...]`, each holding its own block children (often `img`). Recursed into via the generic container fallback — order is preserved but the columns collapse to sequential blocks. |

Also expected (not in the sampled doc but supported by the renderer):

- `heading-1` … `heading-6` (or `h1`..`h6`)
- `code` / `code-block` with `lang` and either `value` string or text children
- `blockquote` / `quote`

## Inline leaf shape

```
{ text: "...", bold?, italic?, code?, underline?, strikethrough?, url?/link? }
```

The renderer wraps in this order: code → bold → italic → strike → underline → link.

## Inline element nodes (not leaves)

A paragraph's `children` array can also contain non-leaf **element nodes** that
carry their own `type`. These are NOT wrapped by the leaf-formatting logic —
`renderInline` special-cases them:

- `{ type: "link", url, children: [{text}] }` — an in-paragraph hyperlink.
  Rendered as `[text](url)`. Without this branch, JoySpace-authored links
  collapse to just their label and drop the href (this was the "internal
  JoySpace links disappear on export" bug fixed 2026-07).
- `{ type: "docfile", value: {...}, children: [{text:""}] }` — a link-card
  auto-created when the user pastes a JoySpace URL into a paragraph or list
  item. See the row in the table above.

## Gotchas

- `list` is flat. The block itself IS one list item; there is no `ul`/`ol`
  wrapper. Consecutive `list` blocks become consecutive `- ` lines.
- A `divider` has a `children: [{text:""}]` placeholder. Do not concat its
  inline text or you get an empty line where the rule should be.
- Table cells can contain block content (paragraphs, even other tables in
  theory). Markdown table cells can't have real newlines, so the renderer
  collapses cell-internal `\n` and multi-paragraph cells with ` <br> ` and
  escapes any literal `|`.
- **Genuinely nested tables** (a `table` block living inside a `table-cell`,
  not just a table that happens to sit near another one) are real in the
  wild — a "需求详述/示意图" outer table whose left cell holds a full
  "阶段/校验是否通过/判断标准/..." sub-table is a recurring PRD authoring
  pattern. GFM markdown cannot express this (a cell can't hold a newline-
  separated sub-table), so `renderTable` detects nesting via
  `tableHasNesting()` and falls back to real HTML `<table>` for the *entire*
  outer table in that case — Obsidian (and any Chromium-based renderer)
  displays genuine nested tables from this. Non-nested tables are unaffected
  and still render as plain `| a | b |` markdown. Before this fix, nested
  tables collapsed into an escaped `\|`-joined text blob inside one giant
  markdown cell — technically present but unreadable.
- `attachment.value.url` is a relative `/api/files/<id>` path and `value.fileId`
  holds the id. The renderer resolves both to the full download entrypoint
  `https://apijoyspace.jd.com/v1/files/<fileId>/link` (302-redirects to a signed
  `eefs.jd.com` URL), so the emitted `📎 [name](url)` link is directly fetchable
  on the JD intranet/VPN.
- For NON-`page_type=13` pages (sheets, mind maps, boards, PPT) the block
  schema is type-specific and almost certainly not in this table. Pass `--raw`
  and inspect before trusting the reconstructed markdown.

## Where the renderer lives

`scripts/read_joyspace_doc.js`, functions `renderInline`, `renderCell`,
`renderTable`, `renderTableHtml`, `renderCellContentHtml`,
`renderFoldableBlockHtml`, `renderBlock`, `blocksToMarkdown`. Unknown block
types fall back to inline-text extraction so the output is degraded but never
empty.
