// Throwaway probe: does POST /v1/pages accept Slate/HTML content?
import { resolveAuth, buildCookieHeader } from "/Users/ext.anle6/.joycode/skills/joyspace-kit/skills/markdown-to-joyspace/scripts/import_markdown_doc.js";

const BASE = "https://apijoyspace.jd.com";

async function call(method, url, cookieHeader, teamHeaderId, body) {
  const response = await fetch(`${BASE}${url}`, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Cookie: cookieHeader,
      "x-team-id": teamHeaderId,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* keep raw */ }
  return { status: response.status, json, text: text.slice(0, 400) };
}

const BLOCKS = [
  { type: "p", children: [{ text: "probe title line" }] },
  { type: "list", value: "ordered", orderedType: "ArabicDotArabic", header: 1, children: [{ text: "一级编号标题" }] },
  { type: "p", children: [{ text: "注：加粗段落", bold: true }] },
  {
    type: "table",
    width: [200, 200],
    children: [
      { type: "table-row", children: [
        { type: "table-cell", children: [{ type: "p", children: [{ text: "表头A", bold: true }] }] },
        { type: "table-cell", children: [{ type: "p", children: [{ text: "表头B", bold: true }] }] },
      ] },
      { type: "table-row", children: [
        { type: "table-cell", children: [{ type: "p", children: [{ text: "1" }] }] },
        { type: "table-cell", children: [{ type: "p", children: [{ text: "2" }] }] },
      ] },
    ],
  },
];

const MD_PROBE = [
  "# H1 一级标题",
  "",
  "## H2 二级标题",
  "",
  "### H3 三级标题",
  "",
  "普通段落文本，含 **加粗片段** 与普通文字。",
  "",
  "**整行加粗段落**",
  "",
  "1. 有序项一",
  "2. 有序项二",
  "",
  "- 无序项一",
  "- 无序项二",
  "",
  "| 作用 | 进程 | 前台需求 |",
  "| --- | --- | --- |",
  "| 普通表头行 | a | b |",
  "",
  "| **粗表头A** | **粗表头B** |",
  "| --- | --- |",
  "| 1 | 2 |",
  "",
  "> 引用块",
  "",
  "`行内代码`",
  "",
  "---",
  "",
  "最后一段。",
].join("\n");

const VARIANTS = {
  slate_str: { contentType: "slate", content: [{ value: JSON.stringify(BLOCKS) }] },
  slate_arr: { contentType: "slate", content: BLOCKS },
  json_str: { contentType: "json", content: [{ value: JSON.stringify(BLOCKS) }] },
  html: { contentType: "html", content: [{ value: "<h1>probe h1</h1><table><tr><th><b>A</b></th><th><b>B</b></th></tr><tr><td>1</td><td>2</td></tr></table>" }] },
  md: { contentType: "markdown", content: [{ value: MD_PROBE }] },
  slate_raw: { content: BLOCKS },
  slate_raw_str: { content: JSON.stringify(BLOCKS) },
};

const [mode, arg] = process.argv.slice(2);
const auth = await resolveAuth({ tenantCode: "CN.JD.GROUP", deviceId: "noDeviceId" });
const cookieHeader = buildCookieHeader(auth);
const teamHeaderId = auth.teamHeaderId || "00046419";

if (mode === "basic") {
  const b = await call("GET", `/v3/pages/${arg}/basic?sendRecent=0`, cookieHeader, teamHeaderId);
  console.log("BASIC", JSON.stringify(b.json, null, 1).slice(0, 2500));
  const r = await call("POST", "/v1/pages/content", cookieHeader, teamHeaderId, { pageId: arg });
  console.log("CONTENT", JSON.stringify(r.json, null, 1).slice(0, 4000));
} else if (mode === "delete") {
  console.log(JSON.stringify(await call("DELETE", `/v1/pages/${arg}`, cookieHeader, teamHeaderId), null, 1));
} else if (mode === "read") {
  const r = await call("POST", "/v1/pages/content", cookieHeader, teamHeaderId, { pageId: arg });
  console.log(JSON.stringify(r.json?.data?.content ?? r.json, null, 1).slice(0, 4000));
} else if (mode === "create") {
  const variant = VARIANTS[arg];
  if (!variant) throw new Error(`unknown variant ${arg}`);
  const payload = {
    title: `[probe-slate] ${arg} ${new Date().toISOString()}`,
    page_type: 13,
    teamId: "root",
    ...variant,
  };
  const created = await call("POST", "/v1/pages", cookieHeader, teamHeaderId, payload);
  console.log("CREATE", created.status, created.text);
  const pageId = created.json?.data?.id || created.json?.data?.pageId;
  if (!pageId) process.exit(0);
  console.log("pageId", pageId);
  const read = await call("POST", "/v1/pages/content", cookieHeader, teamHeaderId, { pageId });
  const content = read.json?.data?.content;
  console.log("READ", JSON.stringify(content, null, 1).slice(0, 12000));
  const del = await call("DELETE", `/v1/pages/${pageId}`, cookieHeader, teamHeaderId);
  console.log("DELETE", del.status, del.text);
} else {
  console.log("usage: create <variant>|read <pageId>|delete <pageId>");
}