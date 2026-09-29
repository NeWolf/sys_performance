# xlsx upload flow — verified path (2026-07-16)

Uploads a local `.xlsx / .xls / .csv` to JoySpace as an online sheet
(`page_type=18`). Verified with `me_token` cookie auth. Endpoints borrowed
from o2's `webcli:joyspace upload` implementation, reworked to run without
a browser.

## Four-step flow

### 1. Init multipart upload

```
POST https://apijoyspace.jd.com/v3/web-files/init
Cookie: me_token=<token>
focus-team-id: 00046419
Content-Type: application/json

{"fileSize": <bytes>, "fileName": "<name.xlsx>", "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
```

Response:
```json
{
  "status": "success",
  "data": {
    "uploadId": "DCAppFile_<uuid>",
    "partSize": 16777216,
    "partInfos": [
      {"partNumber": 1, "uploadUrl": "https://joyspace-office.jd.com/minio/...", "headers": {...}}
    ],
    "storeResponseHeaderKeys": ["X-Wps3-Info-Token"]
  }
}
```

For files under `partSize` (16 MB), only one part. Larger files: multi-part
(NOT yet supported by kit — script errors out).

### 2. PUT the bytes to signed S3-like URL

```
PUT <partInfos[i].uploadUrl>
<headers from partInfos[i].headers verbatim>
<file bytes>
```

Capture the response headers listed in `storeResponseHeaderKeys`
(typically just `X-Wps3-Info-Token`) and pass them back in step 3.

### 3. Complete upload

```
POST https://apijoyspace.jd.com/v3/web-files/complete
(same cookie/team headers)

{
  "uploadId": "<from init>",
  "parts": [
    {"partNumber": 1, "storeResponseInfos": [{"name": "X-Wps3-Info-Token", "value": "<from PUT resp>"}]}
  ]
}
```

Response:
```json
{"status": "success", "data": {"fileId": "391134329864192"}}
```

The `fileId` is the WPS numeric file id, used as `relateFileId` next.

### 4. Create the sheet page

```
POST https://apijoyspace.jd.com/v2/pages
(same cookie/team headers)

{
  "title": "<name>",
  "pageType": 18,
  "partCount": 1,
  "uploadId": "<from init>",
  "partSize": 16777216,
  "partInfos": [<echo from init>],
  "storeResponseHeaderKeys": ["X-Wps3-Info-Token"],
  "folderId": "root",
  "teamId": "root",
  "pageId": "",
  "relateFileId": "<from complete>"
}
```

Success: `{"status":"success","data":{"id":"<pageId>", ...}}`.
The new sheet lives at `https://joyspace.jd.com/sheets/<pageId>`.

## Placement

- `teamId="root"` + `folderId="root"` → personal space root
- To place inside a team folder, pass real `teamId` + `folderId` — the kit
  script resolves them from a `--page-url` reference.
- Sub-page mounting via `categoryId` is NOT verified here. `/v2/pages`
  might not honor it the way `/v1/pages` does for markdown docs. If you
  need this, add an explicit probe.

## Caveat — sheet engine lazy init

**Freshly-created sheets cannot be read back via `/v2/sm/export` for a while**
(minutes to hours). During that window the API returns
`errCode 50001 "关联操作未完成"`, and `joyspace-read-sheet` therefore cannot
round-trip a just-uploaded sheet.

**Verified 2026-07-17 — this cannot be forced from outside JoySpace.** Tested:
- Opening `/sheets/<id>` in real Chrome (`open` command) — no effect on export
- Opening the `editUrl` (with `_w_signature`) in real Chrome — no effect
- Playwright headless + injecting sesame SSO cookies + goto sheets URL — no effect
- Playwright headed + full anti-webdriver detection — no effect
- Two-step goto (sheets URL first, then editUrl) with all joyspace-office cookies
  present — no effect
- o2's `webcli:joyspace upload` (which does `page.goto(editUrl) + wait 5s` in
  its sandbox Chrome) — also stayed 50001

The `editUrl` mechanism works for **already-warm sheets** but does not
force association for fresh uploads. JoySpace defers this server-side; the
delay is opaque.

**Design implication for callers:** don't build automations that expect
`upload → immediately read back`. The sheet is fully functional for
viewing/sharing right away — only the export API path is temporarily
blocked. For cell-level editing, prefer "read → transform → upload new"
where the read step operates on an already-warm source sheet.

## Related endpoints

- `apijoyspace.jd.com/v3/web-files/init` — init multipart
- `apijoyspace.jd.com/v3/web-files/complete` — finalize multipart
- `apijoyspace.jd.com/v2/pages` (POST) — create sheet page from uploaded file
- Upload storage: `joyspace-office.jd.com/minio/joyspace-wpsfile-prod/...`
