# JoySpace edit endpoint probe — results

**Verified 2026-05-28.** All write operations to existing pages route through socket.io + OT
(operational-transform) collaborative-editing frames, NOT REST. There is no public "overwrite
this page" REST endpoint.

Don't redo this exploration unless JoySpace announces a new API surface. Re-probe takes ~30 min
and produces zero new options.

## Endpoints tried (28 total, all on apijoyspace.jd.com)

All in the user's logged-in browser context (XMLHttpRequest via CDP Runtime.evaluate, so cookies
+ `x-team-id: 00046419` auto-attach). XHR was used instead of fetch because JD's `sgm-web`
monitoring library wraps `fetch` and surfaces `TypeError: Failed to fetch` on cross-origin XHRs.

Two categories of failure:

| Status | Body | Meaning |
|--------|------|---------|
| 404 + `{message:"Not Found"}` | route does not exist | route truly absent |
| 200 + `{errMsg:"未知错误", errCode:"1", status:"failed"}` | v3 catch-all responder | route exists but the API expects OT-style step/version fields, not REST payload |

### Page-level updates (all FAIL)

- `PUT /v1/pages/<id>` → 404
- `POST /v1/pages/<id>` → 404
- `PATCH /v1/pages/<id>` → network error (not even routed)
- `PUT /v3/pages/<id>` (3 body shapes) → 200 "未知错误"
- `PATCH /v3/pages/<id>` → 200 "未知错误"
- `PUT /v1/pages/<id>/content`, `POST /v1/pages/<id>/content` → 404
- `PUT /v1/pages/<id>/title` → 404
- `POST /v1/pages/<id>/import|replace|markdown|delete` → 404
- `POST /v3/pages/<id>/markdown|save|publish|snapshot|revisions|contents|contents/save|import|delete` → 200 "未知错误"

### Content-level updates (all FAIL)

- `PUT /v1/contents/<editing_content_id>`, `POST /v1/contents/<editing_content_id>` → 404
- `PUT /v3/contents/<editing_content_id>` → 200 "未知错误"
- `POST /v3/contents/<editing_content_id>/save` → 200 "未知错误"

### What DOES work (sibling operations)

- `DELETE /v1/pages/<id>` → 200 `{status:"success"}` (soft delete to recycle bin)
- `POST /v1/pages` (create) → 200 with new page object
- `POST /v1/pages/content` body `{pageId}` (read) → 200 with Slate block tree
- `GET /v1/pages/recent` → 200 with up to ~20 recent pages

## The real edit channel (do NOT try to reverse)

Observed via CDP Network + Page.javascriptDialogOpening listeners while typing in the editor:

- WebSocket frames: `socket.io` over `wss://...`
- Frame shape: `42["message",{"id":"...","channelID":"<pageId>","eventName":"step1"|"step2","message":"<base64>"}]`
- The base64 payload is a binary-packed OT step. Reverse-engineering cost: estimated days, with
  no stable spec — JoySpace can change the binary format at any release.

## Only viable automation paths for "edit existing JoySpace doc"

1. **CDP browser automation** (recommended when actually needed): launch the sandboxed Chrome
   profile from `chrome-cdp-sandbox`, open the page, drive the Slate editor via DOM events
   (`beforeinput`, `input`, clipboard paste). The editor itself emits the right OT steps.
   - Slow (~10s per edit including page load + sync)
   - Stable (mirrors what a human does)
   - Requires the sandboxed Chrome to already be logged in
2. **Delete + recreate** (acceptable for private docs): `DELETE /v1/pages/<id>` then re-create
   from updated markdown.
   - Drawback: pageId changes, so shared links/permissions break. Don't use for team docs.

## How this was confirmed

Probe scripts ran inside `~/.hermes/sandboxes/chrome-joyspace` via CDP. Approach:

1. Create throwaway doc with `POST /v1/pages` (markdown body).
2. Loop through candidate update endpoints, log status + body.
3. Read back content with `POST /v1/pages/content` to verify nothing changed.
4. `DELETE /v1/pages/<id>` to clean up.

Throwaway probe doc id: `7gGsZYKKWHvgcA30i1dG` (already deleted).
