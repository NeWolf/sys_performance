# JoySpace image handling — what's known so far

Last verified: 2026-05-28.

## Confirmed behavior (markdown ingest)

When `markdown-to-joyspace` posts a markdown body containing `![alt](url)`, the JoySpace
markdown parser produces a Slate block:

```json
{ "type": "img", "id": "<random>", "url": "<original url, unchanged>",
  "children": [{ "text": "" }] }
```

Two surprises a user will hit on first try:

1. **The URL is stored verbatim. JoySpace does NOT proxy or re-host the image.** At view
   time the user's browser fetches the URL directly. Implications:
   - `![](./local.png)` or `file://...` → broken image, no error.
   - Public image hosts (placehold.co, github user-content, S3, etc.) often render broken
     because JD's office network blocks outbound to many CDNs. Expect "broken image" icons
     for non-JD URLs in practice.
   - JD-internal CDNs (`*.360buyimg.com`, `storage.360buyimg.com`) are the safest source.
2. **Alt text is silently dropped.** `![important caption](url)` becomes a bare img block.
   To get a caption, emit a separate paragraph; do not rely on the alt field.

Empirical test: created doc `vArUhbVAWdChvj83v2MK` with three images
(nousresearch.com PNG, JD CDN avatar, placehold.co). Only the JD CDN one rendered on the
user's machine; the other two showed broken-image icons because intranet network policy
blocked the upstream hosts.

## What this means for skill behavior

When a user asks to embed a local image in a JoySpace doc:

1. **Reject `./pic.png` and `file://...` URLs upfront.** They will silently break.
2. **Warn before embedding any non-JD URL.** It will probably render fine on the open
   internet but break on JD office/VPN. Ask whether the doc is for personal vs team use.
3. **For local images, the user needs a JD-hosted URL first.** Three options:
   - User uploads via the JoySpace web UI's image button, then hands the resulting URL back.
   - The skill uploads programmatically to JoySpace's own attachment endpoint (TODO — see below).
   - The user pre-uploads to ERP / Wiki / any internal asset store they already have.

## JoySpace image upload endpoint — NOT YET CAPTURED

Status as of 2026-05-28: the upload endpoint that the JoySpace web editor uses when you
click "insert image" has not been observed yet. The capture attempt during the session was
in progress when the session ended (background CDP capture script was running on a
sandboxed Chrome attached to the doc, waiting for the user to trigger an upload).

Capture procedure to finish next session:

1. Launch the sandboxed Chrome via `chrome-cdp-sandbox` skill (port 9222, profile
   `~/.hermes/sandboxes/chrome-joyspace`).
2. Open any JoySpace markdown doc the user can edit.
3. Run the CDP capture watcher (see `scripts/cdp_capture_upload.mjs` template below).
4. Have the user click the editor's image-insert button OR paste an image from clipboard
   OR drag-and-drop a file. Any of these triggers the same upload path.
5. The watcher will surface the request URL, headers, and (if reachable) response body.
6. Replay with curl from a token-only (no-browser) context to confirm `me_token`-only auth
   is sufficient.

Likely candidate paths to expect (extrapolating from JoySpace's URL conventions):

- `POST https://apijoyspace.jd.com/v1/attachments` (multipart/form-data)
- `POST https://apijoyspace.jd.com/v1/upload`
- `POST https://apijoyspace.jd.com/v1/files`
- `POST https://apijoyspace.jd.com/v3/attachments`
- A presigned-URL flow: first a small JSON request gets a temporary upload URL on
  `*.360buyimg.com`, then a PUT/POST to that URL with the binary.

Both shapes are common in JD's stack; capture before guessing.

## CDP capture watcher pattern

When you eventually capture this, save the script as `scripts/capture_image_upload.mjs`
under `markdown-to-joyspace/` so it's re-runnable. Skeleton:

```js
// Attach to a CDP page, enable Network, log any POST that looks like an upload
// (multipart content-type, or url contains upload|attach|image|file|asset).
// Print URL + request headers + first 800 bytes of postData + status + first 2000 bytes
// of response body. Exit after N seconds or after the first hit, whichever comes first.
```

Pattern reference: `references/api-probing-from-page-context.md` in `chrome-cdp-sandbox`.

## When the upload endpoint is captured, this skill should grow

A new helper script:

```
scripts/upload_image.mjs --file /path/to/img.png
# → outputs a JD CDN URL
```

And `import_markdown_doc.js` should optionally pre-process markdown:

- find `![alt](./relative.png)` and `![alt](/absolute.png)` patterns
- upload each via `upload_image.mjs`
- substitute the returned CDN URL in the markdown
- then proceed with the existing create-doc flow

Until then, the skill must reject local image paths with a clear message.
