# JoySpace HTTP API — verified endpoints

Single source of truth for endpoints used by all JoySpace skills (markdown-to-joyspace, joyspace-read-doc, joyspace-list, joyspace-upload-image, joyspace-search). When a new endpoint is verified, add it here so the other skills don't have to re-verify it.

## Base

- Host: `https://apijoyspace.jd.com`
- Auth: `Cookie: me_token=<jwt>` from `hioffice-auth` skill (HiOffice client on `localhost:8988`, fallback path).
- Tenant `CN.JD.GROUP` headers required on every request:
  - `focus-team-id: 00046419`
  - `focus-client: WEB`
- `Origin: https://joyspace.jd.com` and `Referer: https://joyspace.jd.com/...` are usually expected. `uploadImage` specifically requires `Referer: https://joyspace.jd.com/pages/<pageId>` matching the host page.
- No `Authorization` header — JoySpace is cookie-only despite being a SPA.

## Verified endpoints

### Read

- `POST /v1/pages/content`  body `{pageId}` — Slate-format content
- `GET /v3/pages/<pageId>/basic?sendRecent=0` — title, owner, team_id, folder_id, timestamps

### List / Recent

- `GET /v1/pages/recent` — last ~20 accessed pages, server-side cap (no `limit` honored above ~20)

### Search

- `POST /v2/search/global?sort=-updated_at&search=<kw>&start=0&length=20&classiFication=<n>&timeRange=-1&scene=global&clear=false`
  - Body: `{"search":"<kw>","clear":false,"classiFication":[<n>],"pageType":[],"tags":[],"timeRange":-1,"scene":"global","digestLength":50,"start":0,"length":20}`
  - `classiFication: 0` = global, `6` = received-shares; `received` mode also wants `creators:[],sender:[],receiver:[]`
  - **Keyword must be ≥3 characters**; shorter returns `{total:0, data:[], empty:true}` with no error
  - Response titles/preview have `<em>kw</em>` highlight tags — strip before display
  - `total` is unpaged hit count; `data` is current page only
  - **Do not confuse with `POST /v1/search`** — that endpoint searches **users only**, not pages. Returns `{users:[]}` for any query.

### Create

- `POST /v1/pages` — body `{title, page_type:13, teamId, content, contentType}`
  - `page_type:13` = doc (other types: ?)
  - Empty `folder_id` + private `teamId` (`$c6r4HK7Pb1UL82qFdFd5` for sushixin.1) → root of private space
  - To place under an existing folder, fetch that page's `team_id` + `folder_id` via `/v3/pages/<id>/basic` and pass them.

### Delete

- `DELETE /v1/pages/<pageId>` — soft delete, succeeds silently on owned pages

### Upload image

- `POST /v2/pages/<hostPageId>/uploadImage`
  - multipart/form-data, field name `file`
  - Returns `{imgUrl: "https://apijoyspace.jd.com/v1/files/<fileId>/link", dimensions:{width,height,type}}`
  - **Critical property**: `imgUrl` outlives the host page. You can create a scratch page, upload, delete the scratch page, and the imgUrl still resolves. This is the basis of `joyspace-upload-image --auto-scratch --delete-scratch`.

### Reference info / permissions

- `POST /v2/pages/refResourceInfo` — reference resource info (used for embeds)
- `POST /v3/minor/resource/getResourcePermissions` — page permissions

## Non-existent / blocked paths

These were probed and confirmed dead — don't re-try them:

- **Edit / overwrite a page**: 18 candidate REST endpoints tested. v1 routes return 404; v3 routes return 200 but `{errMsg:"未知错误", errCode:"1"}`. Editing is **socket.io + OT only**: frames look like `42["message",{"channelID":"<pageId>","eventName":"step1"|"step2","message":"<base64>"}]`. The only viable automation path is CDP browser keystroke replay, which is slow and brittle. Record the pageId and tell the user to edit in-browser, or recreate the page.
- **Image proxying**: JoySpace does NOT proxy external image URLs. The JD office network blocks most public image hosts. Only `*.360buyimg.com` and `apijoyspace.jd.com/v1/files/...` render reliably. Markdown imports must upload local images to JoySpace CDN; external URLs in markdown are passed through unchanged but may render broken on JD network.

## Known IDs (sushixin.1, tenant CN.JD.GROUP)

- private-space `teamId`: `$c6r4HK7Pb1UL82qFdFd5`
- tenant `focus-team-id`: `00046419`
- ddAppId: `ee`

## How to discover new endpoints

When the user wants a JoySpace capability we haven't implemented, follow `cdp-network-capture` skill:
1. Launch sandbox Chrome (direct binary, NOT `open -a`).
2. Open the JoySpace page that exposes the feature.
3. Attach a Network capture filtered to `apijoyspace.jd.com`, ignoring already-known endpoints.
4. Have the user click the feature in-browser.
5. Extract the URL + body + headers from the capture and write a minimal node script that reproduces it with `me_token` from `hioffice-auth`.
