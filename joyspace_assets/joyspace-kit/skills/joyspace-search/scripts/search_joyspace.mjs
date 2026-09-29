#!/usr/bin/env node
// JoySpace global search via POST /v2/search/global
// Usage: node search_joyspace.mjs --query "todo" [--limit 20] [--start 0]
//                                  [--scope global|received] [--json]

import { getMeToken, JOYSPACE_API_BASE } from "../../shared-auth.mjs";

const args = process.argv.slice(2);
function getArg(name, def = null) {
  const i = args.indexOf(name);
  return i >= 0 && i + 1 < args.length ? args[i + 1] : def;
}
function hasFlag(name) { return args.includes(name); }

const query  = getArg('--query') || getArg('-q');
const limit  = parseInt(getArg('--limit', '20'), 10);
const start  = parseInt(getArg('--start', '0'), 10);
const scope  = getArg('--scope', 'global'); // global | received
const asJson = hasFlag('--json');

if (!query) {
  console.error('Usage: search_joyspace.mjs --query "<text>" [--limit 20] [--start 0] [--scope global|received] [--json]');
  console.error('Note: keyword must be >= 3 characters; shorter words return empty.');
  process.exit(2);
}

// Auth
const { meToken } = getMeToken();

const classiFication = scope === 'received' ? 6 : 0;
const qs = new URLSearchParams({
  sort: '-updated_at',
  search: query,
  clear: 'false',
  classiFication: String(classiFication),
  timeRange: '-1',
  scene: 'global',
  start: String(start),
  length: String(limit),
});
const url = `${JOYSPACE_API_BASE}/v2/search/global?${qs}`;

const body = {
  search: query,
  clear: false,
  classiFication: [classiFication],
  pageType: [],
  tags: [],
  timeRange: -1,
  scene: 'global',
  digestLength: 50,
  start, length: limit,
};
if (scope === 'received') Object.assign(body, { creators: [], sender: [], receiver: [] });

const resp = await fetch(url, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Cookie': `me_token=${meToken}`,
    'focus-team-id': '00046419',
    'focus-client': 'WEB',
    'Origin': 'https://joyspace.jd.com',
    'Referer': 'https://joyspace.jd.com/',
  },
  body: JSON.stringify(body),
});
if (!resp.ok) { console.error(`HTTP ${resp.status}`); console.error(await resp.text()); process.exit(1); }
const j = await resp.json();
if (j.status !== 'success') { console.error('API error:', JSON.stringify(j)); process.exit(1); }

const items = (j.data?.data || []).map(it => ({
  pageId: it.page_id || it.id,
  title: (it.title || '').replace(/<\/?em>/g, ''),
  preview: (it.preview_text || '').replace(/<\/?em>/g, '').replace(/\s+/g, ' ').trim(),
  creator: it.create_name,
  erp: it.create_erp,
  teamId: it.team_id,
  folderId: it.folder_id,
  pageType: it.page_type,
  url: `https://joyspace.jd.com/pages/${it.page_id || it.id}`,
}));
const total = j.data?.total || 0;

if (asJson) {
  console.log(JSON.stringify({ query, scope, total, returned: items.length, start, items }, null, 2));
} else {
  console.log(`query="${query}" scope=${scope}  total=${total}  showing ${items.length} (start=${start})`);
  console.log('');
  items.forEach((it, i) => {
    console.log(`${String(start + i + 1).padStart(3)}. ${it.title}`);
    console.log(`     ${it.url}`);
    console.log(`     by ${it.creator} (${it.erp})`);
    if (it.preview) console.log(`     ${it.preview.slice(0, 120)}`);
    console.log('');
  });
}
