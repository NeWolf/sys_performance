# /v2/sm/export — verified path (2026-07-16)

Verified with `me_token` cookie auth (no focus-token needed) on a WPS-engine sheet
(`https://joyspace.jd.com/sheets/3K6kDuAm2c0Qoww32t1I`). Works for both old (Shimo)
and new (WPS) engines — backend routes automatically.

## Trigger

```
POST https://apijoyspace.jd.com/v2/sm/export
Cookie: me_token=<token>
focus-team-id: 00046419
focus-client: WEB
Content-Type: application/json

{"fileId": "<pageId>", "title": "<any>", "exportType": ""}
```

Success:
```json
{"status": "success", "data": {"taskId": "wps.excel.export.image.float.<hash>"}}
```

Failure — rate limit (kicks in after ~3 rapid exports of the same file):
```json
{"status": "fail", "errCode": "40302", "errMsg": "操作频繁，请稍后再试"}
```
Cooldown appears to be ~30s. The kit script surfaces this as a clear error.

## Poll

```
POST https://apijoyspace.jd.com/v2/sm/getExportProcess
(same headers)

{"fileId": "<pageId>", "taskId": "<taskId>"}
```

While pending: `{"status":"success","data":{"progress":"80","downloadUrl":""}}`
When ready:    `{"status":"success","data":{"progress":"100","downloadUrl":"<S3 signed URL>"}}`

Typical latency: 1-3 poll cycles (~1-3s) for small sheets.

## Download

```
GET <downloadUrl>  # no auth headers — it's an S3 pre-signed URL
```

Returns `application/octet-stream`, magic `50 4b 03 04` (ZIP/OOXML).
`HEAD` returns 403 — that's an S3 quirk, use `GET`.

## Not verified

- Old Shimo engine sheets (only tested a WPS sheet). Per o2's earlier research
  the same `/v2/sm/export` handles both — leaving unverified for now.
- Very large files (>10 MB). May require longer polling timeout.

## Related

- `apijoyspace.jd.com` = same host as the rest of the kit. No new domain.
- Token exchange: reuse `shared-auth.mjs`.
