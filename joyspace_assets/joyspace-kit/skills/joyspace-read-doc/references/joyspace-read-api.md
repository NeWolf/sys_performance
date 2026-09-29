# JoySpace Read API Reference

Endpoints used by `read_joyspace_doc.js`. All requests need:

```
Cookie: me_token=<token>; sso.jd.com=<sso>   (either one is enough in practice; me_token preferred)
x-team-id: 00046419            (tenant CN; differs per tenant)
Content-Type: application/json
Accept: application/json
```

Base: `https://apijoyspace.jd.com`

## 1. Page basic metadata

```http
GET /v3/pages/<page_id>/basic?sendRecent=0
```

Response (envelope): `{ status: "success", data: { ... } }`

Useful `data` fields:
- `title`, `full_name`
- `team_id` (string; starts with `$` for private space — normalize to `"root"` for create)
- `folder_id`
- `page_type` (13 = markdown doc)
- `create_user`, `update_user`, `update_time`

## 2. Page content

```http
POST /v1/pages/content
{ "pageId": "<page_id>" }
```

Response: `{ status: "success", data: { content: [...] | "<string>" } }`

For `page_type=13`:
- `data.content` is typically an array of blocks; each block carries the original markdown in `value` / `text` / `markdown` / `content`.
- The reconstructor joins block strings with `\n\n`. Round-trip is usually faithful for plain markdown docs.

For non-markdown types (sheets, mind maps, etc.) the block schema is type-specific. Use `--raw` and parse from the original response.

## 3. URL → pageId

Regex (handles all known page roots):
```
/joyspace\.jd\.com\/(?:pages|doc|sheets?|table|ppt|board|mind|meeting)\/([A-Za-z0-9_-]+)/i
```

## Status envelope notes

JoySpace responses vary:
- `{ status: "success", data: ... }` — success
- `{ status: "0", data: ... }` — also success (older endpoints)
- `{ errorCode: "<nonzero>", errorMsg: "..." }` — failure
- Some endpoints return `data` at top level with no `status` field — treat presence of `data` as success.

## Auth chain (reused from markdown-to-joyspace)

Order tried automatically:
1. `ME_TOKEN` / `SSO_TOKEN` env
2. `~/.joyclaw/openclaw.json` cookies (parses `models.providers.jdcloud.headers.Cookie`)
3. `desk.agent.auth.tokenGrant` with `JMECHAT_token`
4. Local HiOffice (ports 8988–9006) — `desk.agent.auth.encrypt` → POST to HiOffice → `desk.agent.auth.getWebToken`

Tenant `ddAppId` (used in gateway calls):
| Tenant | teamHeaderId | ddAppId |
|--------|--------------|---------|
| CN.JD.GROUP | 00046419 | ee |
| TH.JD.GROUP | 00046420 | th.ee |
| ID.JD.GROUP | 00046421 | id.ee |
| SF.JD.GROUP | 00046422 | sf.ee |
