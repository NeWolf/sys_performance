import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// Offline hook/component checks; no backend or new dependencies required.
const jsx = (type, props) => ({ type, props: props || {} })
const nodes = (tree) => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const plain = (value) => JSON.parse(JSON.stringify(value))
const tick = () => new Promise((resolve) => setImmediate(resolve))
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done }); return { promise, resolve } }
function harness(effectsEnabled = true) {
  const slots = []
  let cursor = 0, changed = false, pending = [], renderFn
  const react = {
    useState(initial) {
      const index = cursor++
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial
      return [slots[index], (value) => {
        const next = typeof value === 'function' ? value(slots[index]) : value
        if (!Object.is(next, slots[index])) { slots[index] = next; changed = true }
      }]
    },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial } },
    useEffect(fn, deps) {
      const index = cursor++, old = slots[index]
      if (!old || deps.some((value, i) => !Object.is(value, old.deps[i]))) {
        pending.push(() => { old?.cleanup?.(); slots[index] = { deps, cleanup: effectsEnabled ? fn() : undefined } })
      }
    },
    useMemo(fn) { return fn() },
    useCallback(fn) { return fn },
    useEffectEvent(fn) { return fn },
    useId() { return `test-${cursor++}` },
  }
  react.useLayoutEffect = react.useEffect
  return {
    react,
    render(fn = renderFn) {
      renderFn = fn
      let result, loops = 0
      do {
        assert.ok(++loops < 30, 'render must settle')
        cursor = 0; changed = false; pending = []; result = fn()
      } while (changed)
      pending.forEach((effect) => effect())
      return result
    },
    close() { slots.forEach((slot) => slot?.cleanup?.()) },
  }
}
function load(file, modules, globals = {}, extra = '') {
  const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8') + extra
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText
  const context = { exports: {}, URL, URLSearchParams, AbortController, AbortSignal, Headers, Response, Blob, TextDecoder, performance, setTimeout, clearTimeout, setInterval, clearInterval, ...globals,
    require(name) { if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' }; assert.ok(name in modules, name); return modules[name] },
  }
  vm.runInNewContext(compiled, context)
  return context.exports
}
const id = 'a'.repeat(32), other = 'b'.repeat(32)
const data = { dates: [
  { date: '2026-11-10', records: 2, segments: [{ segment: 0, start: 1, end: 2, records: 2 }] },
  { date: '2026-11-11', records: 4, segments: [{ segment: 0, start: 3, end: 5, records: 3 }, { segment: 1, start: 6, end: 6, records: 1 }] },
], undated_records: 2 }
const empty = { dates: [], undated_records: 2 }
const h = harness()
const requests = []
const fetchMock = (path, init) => {
  if (path === '/api/token') return Promise.resolve(Response.json({ token: 'test' }))
  const request = { path, init, ...deferred() }; requests.push(request); return request.promise
}
const api = load('api.ts', { react: h.react }, { fetch: fetchMock })
const dates = load('ReportDates.tsx', { react: h.react, './api': api })
assert.equal(dates.validateReportDates(data), data)
for (const value of ['2024-02-29', '2000-02-29', '2026-11-11']) assert.equal(dates.validReportDate(value), true)
for (const value of ['2026-02-29', '1900-02-29', '0000-01-01', '2026-11-31', '2026-1-01']) assert.equal(dates.validReportDate(value), false)
for (const value of [null, {}, { ...data, undated_records: -1 }, { ...data, dates: [...data.dates].reverse() }, { ...data, dates: [data.dates[0], data.dates[0]] }, { ...data, dates: [{ ...data.dates[0], records: 0 }] }]) assert.throws(() => dates.validateReportDates(value))
assert.equal(dates.canGenerateDates(undefined, []), false)
assert.equal(dates.canGenerateDates(data, []), false)
assert.equal(dates.canGenerateDates(data, ['2020-01-01']), false)
assert.equal(dates.canGenerateDates(empty, []), true)
assert.equal(dates.reportQuery('default'), '')
assert.equal(dates.reportQuery('compact', data.dates.map((item) => item.date)), '?style=compact&dates=2026-11-10&dates=2026-11-11')
assert.throws(() => dates.reportQuery('default', []))
const base = `/api/sessions/${id}/report`
for (const suffix of ['', '?style=default', '?style=compact', '?dates=2026-11-11&dates=2026-11-10']) dates.assertReportPath(base + suffix)
for (const path of ['https://evil.test' + base, '//' + base, base + '#x', base + '?token=x', base + '?style=other', base + '?dates=2026-02-29', base + '?dates=2026-11-11&dates=2026-11-11', base + '?style=default&style=compact', base + '?dates=2026%2D11%2D11', base + '/stream']) assert.throws(() => dates.assertReportPath(path))
dates.assertReportPath(base + '?dates=2026-11-10&style=default&dates=2026-11-11', base + '?dates=2026-11-11&dates=2026-11-10')
assert.throws(() => dates.assertReportPath(base, base + '?dates=2026-11-11'))
assert.throws(() => dates.assertReportPath(base, base.replace(id, other)))
const original = JSON.stringify(data)
assert.deepEqual(plain(dates.selectedSegments(data, data.dates.map((item) => item.date), [])), [{ segment: 0, start: 1, end: 5, records: 5 }, { segment: 1, start: 6, end: 6, records: 1 }])
assert.equal(JSON.stringify(data), original)
assert.deepEqual(plain(dates.selectedSegments(data, [], [])), [])
assert.deepEqual(plain(dates.selectedSegments(empty, [], data.dates[0].segments)), data.dates[0].segments)
let session = id, revision = 0
let scope = h.render(() => dates.useReportDates(session, revision))
assert.equal(scope.canGenerate, false)
await tick(); requests.at(-1).resolve(Response.json(data)); await tick()
scope = h.render(); assert.deepEqual(plain(scope.selected), ['2026-11-11'])
scope.onChange([]); scope = h.render(); assert.equal(scope.canGenerate, false)
scope.onChange(['2026-11-10']); h.render()
session = other; scope = h.render(); assert.equal(scope.canGenerate, false)
await tick(); const stale = requests.at(-1)
session = id; scope = h.render(); assert.equal(scope.canGenerate, false)
await tick(); const fresh = requests.at(-1)
assert.equal(stale.init.signal.aborted, true)
stale.resolve(Response.json(empty)); await tick(); assert.equal(h.render().canGenerate, false)
fresh.resolve(Response.json(data)); await tick(); scope = h.render()
assert.deepEqual(plain(scope.selected), ['2026-11-11'])
scope.retry(); h.render(); await tick(); requests.at(-1).resolve(new Response('failure', { status: 500 })); await tick()
scope = h.render(); assert.equal(scope.canGenerate, false); assert.ok(scope.error)
scope.retry(); h.render(); await tick(); requests.at(-1).resolve(Response.json(empty)); await tick()
scope = h.render(); assert.equal(scope.canGenerate, true)
const count = requests.length
revision++; scope = h.render(); assert.equal(scope.canGenerate, false); await tick(); assert.equal(requests.length, count + 1)
h.close()
console.log('PASS: dates, validation, multi-day segments, URL whitelist, session isolation and fail-closed retries')

const sh = harness(), streams = [], resolving = [], disposed = []
const streamApi = load('api.ts', { react: sh.react }, { fetch: (path, init) => {
  if (path === '/api/token') return Promise.resolve(Response.json({ token: 'test' }))
  const request = { path, init, ...deferred() }; streams.push(request); return request.promise
} })
const resolver = (value, signal) => { const job = { value, signal, ...deferred() }; resolving.push(job); return job.promise }
const dispose = (value) => disposed.push(value)
const response = (value) => new Response(JSON.stringify({ type: 'result', data: value }) + '\n')
let streamPath = base + '/stream?dates=2026-11-11'
sh.render(() => streamApi.useAnalysisStream(streamPath, 0, resolver, dispose))
await tick(); streams.at(-1).resolve(response('first')); await tick()
resolving.at(-1).resolve('blob:first'); await tick()
assert.equal(sh.render().data, 'blob:first')
streamPath = base + '/stream?dates=2026-11-10'; assert.equal(sh.render().data, undefined)
assert.deepEqual(disposed, ['blob:first'])
await tick(); streams.at(-1).resolve(response('late')); await tick(); const late = resolving.at(-1)
streamPath = base + '/stream?dates=2026-11-11'; assert.equal(sh.render().data, undefined)
assert.equal(late.signal.aborted, true)
late.resolve('blob:late'); await tick(); assert.equal(sh.render().data, undefined)
assert.deepEqual(disposed, ['blob:first', 'blob:late'])
streams.at(-1).resolve(response('current')); await tick(); resolving.at(-1).resolve('blob:current'); await tick()
assert.equal(sh.render().data, 'blob:current')
streamPath = null; assert.equal(sh.render().data, undefined)
assert.deepEqual(disposed, ['blob:first', 'blob:late', 'blob:current']); sh.close()

let downloads = 0
const dashboard = load('Dashboard.tsx', {
  react: harness(false).react, './ReportDates': dates,
  './api': { apiResponse: async (_path, init) => { downloads++; assert.equal(init.redirect, 'error'); return new Response('report', { headers: { 'content-type': 'text/html' } }) } },
  './Progress': {}, './Groups': {}, './ReportConfig': {}, './ViewPanel': {}, './AdmissionReport': {},
}, {}, '\nexport { loadReport };')
for (const path of ['https://evil.test/report', base, base.replace(id, other) + '?dates=2026-11-11']) {
  await assert.rejects(dashboard.loadReport(path, new AbortController().signal, base + '?dates=2026-11-11'))
}
assert.equal(downloads, 0)
const aborted = new AbortController(); aborted.abort()
await assert.rejects(dashboard.loadReport(base, aborted.signal, base)); assert.equal(downloads, 0)
const blobUrl = await dashboard.loadReport(base, new AbortController().signal, base)
assert.equal(downloads, 1); URL.revokeObjectURL(blobUrl)
console.log('PASS: stale preview disposal, A/B/A streams and report validation before authenticated download')

const ah = harness(false), storage = new Map(), posts = []
const draftId = 'c'.repeat(32), newDraftId = 'd'.repeat(32)
const oldDraft = { draft_id: draftId, state: 'ready', title: 'Existing draft', warnings: [] }
storage.set(`admission-recovery:${id}`, JSON.stringify([{ draft_id: draftId, attempted: false, unknown: false }]))
let dirty = false
const admissionApi = {
  ...api,
  api: async (path) => path.endsWith('/drafts') ? { drafts: [oldDraft] } : oldDraft,
  apiResponse: async () => new Response('existing preview', { headers: { 'content-type': 'text/html' } }),
}
const admission = load('AdmissionReport.tsx', {
  react: ah.react, 'react-dom': { createPortal: (tree) => tree }, './api': admissionApi, './ReportDates': dates,
  './Editing': { useDirty: (value) => { dirty = value }, useEditGuard: () => ({ busy: false, update() {}, acquire: () => true }) }, './AdmissionReport.css': {},
}, {
  document: { body: {} }, window: { confirm: () => true },
  localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
  crypto: { randomUUID: () => newDraftId },
  fetch: async (path, init) => {
    if (path === '/api/token') return Response.json({ token: 'test' })
    posts.push({ path, body: JSON.parse(init.body) })
    return Response.json({ ...oldDraft, draft_id: newDraftId })
  },
})
let admissionScope = { data, selected: ['2026-11-11'], canGenerate: true, loading: false, onChange() {}, retry() {} }
let tree = ah.render(() => admission.AdmissionReport({ id, name: 'Test', segments: data.dates[0].segments, disabled: false, reportDates: admissionScope }))
const find = (predicate) => { const result = nodes(tree).find(predicate); assert.ok(result, 'UI control must exist'); return result }
const button = (label) => find((node) => node.type === 'button' && node.props.children === label)
const picker = () => find((node) => node.type === dates.ReportDates).props.scope
button('生成性能准入报告').props.onClick(); await tick(); tree = ah.render()
assert.deepEqual(plain(picker().selected), ['2026-11-11'])
assert.equal(button('仅生成本地草稿').props.disabled, false)
button('加载本地预览').props.onClick(); await tick(); tree = ah.render()
let frame = find((node) => node.type === 'iframe'); const oldPreview = frame.props.src
frame.props.onLoad(); tree = ah.render()
find((node) => node.type === 'select' && nodes(node).some((child) => child.props.value === 'personal')).props.onChange({ target: { value: 'personal' } }); tree = ah.render()
find((node) => node.type === 'input' && node.props.type === 'checkbox').props.onChange({ target: { checked: true } }); tree = ah.render()
assert.equal(button('确认发布到 JoySpace').props.disabled, false)
find((node) => node.type === 'select' && node.props['aria-label'] === '时间段 1 的场景').props.onChange({ target: { value: 'foreground' } }); tree = ah.render()
picker().onChange(['2026-11-10']); tree = ah.render()
assert.equal(dirty, true); assert.equal(button('确认发布到 JoySpace').props.disabled, false)
assert.equal(find((node) => node.type === 'iframe').props.src, oldPreview)
assert.equal(nodes(tree).some((node) => node.props['aria-label'] === '时间段 1 的场景'), false)
picker().onChange([]); tree = ah.render(); assert.equal(button('仅生成本地草稿').props.disabled, true)
find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await tick(); assert.equal(posts.length, 0)
admissionScope = { ...admissionScope, data: undefined, canGenerate: false, error: 'offline' }; tree = ah.render()
assert.equal(button('仅生成本地草稿').props.disabled, true)
assert.equal(button('确认发布到 JoySpace').props.disabled, false)
admissionScope = { ...admissionScope, data, canGenerate: true, error: undefined }; tree = ah.render()
picker().onChange(['2026-11-10']); tree = ah.render()
find((node) => node.type === 'select' && node.props['aria-label'] === '时间段 0 的场景').props.onChange({ target: { value: 'background' } }); tree = ah.render()
find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} }); await tick(); tree = ah.render()
assert.equal(posts.length, 1)
assert.deepEqual(posts[0].body.dates, ['2026-11-10'])
assert.deepEqual(posts[0].body.scenes, { 0: 'background' })
assert.equal(dirty, false); assert.deepEqual(admissionScope.selected, ['2026-11-11'])
ah.close()
console.log('PASS: admission date copy, dirty state, visible scenes, empty selection and existing preview/publish gates')