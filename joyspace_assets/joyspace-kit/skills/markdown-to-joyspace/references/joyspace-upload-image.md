# JoySpace File Upload Endpoint

Captured 2026-05-28 via CDP Network capture against a logged-in JoySpace tab. Used by both the local-image preprocessor in `markdown-to-joyspace` and the standalone `joyspace-upload-image` skill.

## Endpoint

```
POST https://apijoyspace.jd.com/v2/pages/<hostPageId>/uploadImage
```

The `<hostPageId>` is mandatory — JoySpace conceptually scopes uploads to a "host page". This is the central design constraint of this endpoint.

## Required headers (verified)

```
Cookie: me_token=<token from hioffice-auth>
focus-team-id: 00046419          # CN.JD.GROUP tenant; other tenants in TENANT_CONFIG
focus-client: WEB
Referer: https://joyspace.jd.com/pages/<hostPageId>
Accept: application/json, text/plain, */*
```

Notes:
- `focus-token`, `token`, and `focus-lang` were sent by the browser as **empty strings** — do NOT need to be set, but server tolerates absence.
- No `x-team-id` header is needed for this specific endpoint (the upload flow uses `focus-team-id` instead). The page-create flow at `/v1/pages` uses `x-team-id`.
- `Referer` matters: server checks origin/referer for CSRF. Setting it to `https://joyspace.jd.com/pages/<hostPageId>` works.

## Request body

`multipart/form-data` with a single file field. Browser used field name `file` and we adopted that. Server appears lenient on field name but no need to deviate.

## Response

```json
{
  "status": "success",
  "data": {
    "imgUrl": "https://apijoyspace.jd.com/v1/files/<fileId>/link",
    "dimensions": { "width": 1232, "height": 634, "type": "image/png" }
  }
}
```

The returned `imgUrl` is what you embed in markdown / Slate img blocks.

## The "scratch host page" pattern

Because `<hostPageId>` is required but you often want to upload images BEFORE you have a real destination page (e.g. when creating a new doc that has local image refs), use the scratch pattern:

1. `POST /v1/pages` to create a throwaway page (title prefixed `[hermes-upload-scratch]` or similar).
2. Upload all needed files to `/v2/pages/<scratchId>/uploadImage`.
3. Collect the `imgUrl`s.
4. `DELETE /v1/pages/<scratchId>` (best-effort, in a `finally` block — leaking a scratch page on error is acceptable since the title is identifiable).

**Critical verified fact: the uploaded `imgUrl` outlives the host page.** After deleting the scratch page, the file's CDN URL still serves the original bytes. So you can freely upload-then-delete-host without losing the image. Verified end-to-end on 2026-05-28: created scratch page, uploaded a 64×64 PNG, deleted scratch page, embedded the imgUrl in a brand-new doc — image renders normally.

This independence makes the upload endpoint usable as a generic "JoySpace CDN" — the host page is just an authorization gate.

## External vs JoySpace-CDN images in the office network

JoySpace stores image blocks as raw URL references (NOT proxied or rehosted). Renderer is the browser, fetching directly. Therefore in JD office network:

- `360buyimg.com` / `apijoyspace.jd.com/v1/files/...` URLs: always render (internal CDN).
- Public image hosts (e.g. `nousresearch.com`, `placehold.co`, generic CDNs): often blocked → broken image icons.

So for any markdown destined for JoySpace, prefer uploading local copies to JoySpace's CDN over linking to public URLs.

## Other JoySpace endpoints discovered while probing (2026-05-28)

These are not part of upload but were captured / verified in the same session and belong here for reference:

- `POST /v1/pages` — create page (already used by `import_markdown_doc.js`)
- `POST /v1/pages/content` body `{pageId}` — read content (used by `verifyJoySpacePage` and `joyspace-read-doc`)
- `GET /v3/pages/<id>/basic?sendRecent=0` — page metadata
- `GET /v1/pages/recent` — recent pages, server-capped near 20 (used by `joyspace-list`)
- `DELETE /v1/pages/<id>` — delete (verified working; v3 variant returns "未知错误")
- `POST /v2/pages/refResourceInfo` — fetched at page open; not needed for our flows
- `POST /v3/minor/resource/getResourcePermissions` — fetched at page open; not needed

## What does NOT work (explicit non-results from probing)

JoySpace edit-in-place via REST is **not available**. 18 candidate endpoints probed, all 404 or `{errMsg:"未知错误"}`. Editing goes through socket.io + OT protocol (step1/step2 base64 frames). For edit, only viable path is CDP browser automation — don't waste time re-probing REST.
