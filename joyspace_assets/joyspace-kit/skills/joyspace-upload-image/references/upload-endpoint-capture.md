# JoySpace Upload Endpoint — Raw CDP Capture

Captured 2026-05-28 from a logged-in JoySpace browser session via `chrome-cdp-sandbox` + `Network.requestWillBeSent` listener while the user clicked "insert image" in the editor.

## Request

```
POST https://apijoyspace.jd.com/v2/pages/vArUhbVAWdChvj83v2MK/uploadImage
```

### Headers (verbatim)

```
focus-lang:
sec-ch-ua-platform: "macOS"
Referer: https://joyspace.jd.com/pages/vArUhbVAWdChvj83v2MK
sec-ch-ua: "Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"
focus-team-id: 00046419
sec-ch-ua-mobile: ?0
focus-token:
focus-client: WEB
Accept: application/json, text/plain, */*
Content-Type: multipart/form-data; boundary=----WebKitFormBoundaryQdtriUoHZjpqQ3dZ
token:
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36
```

Note: `focus-token`, `token`, `focus-lang` are sent empty by the real client — the actual auth comes from the implicit `Cookie: me_token=...` (visible in the browser cookie jar, omitted by Chrome from the captured headers). Authorization header is NOT used.

### Body

`multipart/form-data` with a single image part. Browser uses field name `file` (Chrome's default for `<input type="file">` form posts and FormData). Server appears lenient about field name in practice; using `file` matches the captured request.

## Response

```
HTTP/1.1 200 OK
Content-Type: application/json;charset=utf-8
access-control-allow-origin: https://joyspace.jd.com
access-control-allow-credentials: true
```

```json
{
  "status": "success",
  "data": {
    "imgUrl": "https://apijoyspace.jd.com/v1/files/ekqzIdrHKdfOpiQbUTvB/link",
    "dimensions": {
      "height": 634,
      "type": "image/png",
      "width": 1232
    }
  }
}
```

## Adjacent requests fired by the editor (for reference)

When the user inserts an image, the editor also fires:

1. `POST /v2/pages/refResourceInfo` — page metadata cache refresh, body `{id, bizId, bizType, title, ...}`. Not needed for upload.
2. `POST /v3/minor/resource/getResourcePermissions` — permission check. Not needed for upload.
3. `POST /v2/pages/<pageId>/uploadImage` — the actual upload. **This one matters.**

The first two can be skipped when scripting; the server doesn't seem to enforce that they precede the upload call.

## Independent file lifecycle (verified)

After uploading via `--auto-scratch --delete-scratch`, the scratch page was deleted (`DELETE /v1/pages/<id>` returned 200 success), then the returned `imgUrl` (`https://apijoyspace.jd.com/v1/files/326VgLP6P5o6OuMHaYF2/link`) was embedded into a new doc. The image still rendered correctly in the new doc. Conclusion: file CDN entries persist beyond their host page's lifetime, so scratch-page-cleanup is safe.

## Tenant note

`focus-team-id: 00046419` is the JD CN tenant id. For other tenants (TH/ID/SF), capture from a logged-in session via `chrome-cdp-sandbox` and update the constant in `scripts/upload_image.mjs` (or make it configurable if multi-tenant becomes a real need).
