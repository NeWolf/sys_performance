import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import ts from 'typescript'

// Offline checks for destructive actions: never contact the local service.
const jsx = (type, props) => ({ type, props: props || {} })
const nodes = (tree) => !tree || typeof tree !== 'object' ? [] : Array.isArray(tree) ? tree.flatMap(nodes) : [tree, ...nodes(tree.props?.children)]
const text = (tree) => tree == null ? '' : typeof tree !== 'object' ? String(tree) : Array.isArray(tree) ? tree.map(text).join('') : text(tree.props?.children)
const tick = () => new Promise((resolve) => setImmediate(resolve))
const plain = (value) => JSON.parse(JSON.stringify(value))
function harness() {
  const slots = []
  let cursor, changed, effects, renderFn
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
      if (!old || deps.some((value, i) => !Object.is(value, old.deps[i]))) effects.push(() => {
        old?.cleanup?.(); slots[index] = { deps, cleanup: fn() }
      })
    },
    useMemo(fn) { return fn() },
  }
  return {
    react,
    render(fn = renderFn) {
      renderFn = fn
      let result, loops = 0
      do {
        assert.ok(++loops < 30, 'render must settle')
        cursor = 0; changed = false; effects = []; result = fn()
      } while (changed)
      effects.forEach((effect) => effect())
      return result
    },
    close() { slots.forEach((slot) => slot?.cleanup?.()); slots.length = 0 },
  }
}
function load(file, modules, globals = {}, extra = '') {
  const source = readFileSync(new URL(`../src/${file}`, import.meta.url), 'utf8') + extra
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS } }).outputText
  const context = { exports: {},Error, URL, URLSearchParams, AbortController, AbortSignal, Headers, Response, setTimeout, clearTimeout, ...globals,
    require(name) { if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx }; assert.ok(name in modules, name); return modules[name] },
  }
  vm.runInNewContext(compiled, context)
  return context.exports
}
const sessions = ['a', 'b'].map((id, index) => ({ id: id.repeat(32), name: id, created: 1000 + index, summary: { files: [], cycles: 1 } }))
const usage = { database_bytes: 100, wal_bytes: 2, shm_bytes: 3, free_bytes: 10000, reclaimable_bytes: 50, logs_bytes: 90, total_bytes: 192, pending_cleanup_count: 1, unmanaged_logs: true, warnings: ['统计警告'], estimate_method: '抽样估算', sessions: sessions.map((session, index) => ({ id: session.id, database_bytes: 10, logs_bytes: index * 10, total_bytes: 10 + index * 10, cache_bytes: 2, unmanaged_logs: !index, estimated: true })) }
const guard = { busy: false, confirm: () => true, acquire: () => !guard.busy, update() {} }
let confirmed = true, lastConfirm = '', lastError = '', notice = '', removed = []
const windowMock = { confirm(message) { lastConfirm = message; return confirmed } }
const h = harness(), requests = []
const api = load('api.ts', { react: h.react }, { fetch: (path, init) => {
  if (path === '/api/token') return Promise.resolve(Response.json({ token: 'offline-test' }))
  return new Promise((resolve) => requests.push({ path, init, resolve }))
} })
const storage = load('Storage.tsx', { react: h.react, './api': api, './Editing': { useEditGuard: () => guard }, './Storage.css': {} }, { window: windowMock })
const perform = async (_label, action) => {
  if (guard.busy) return
  guard.busy = true
  try { await action(new AbortController().signal) } catch (error) { lastError = error.message } finally { guard.busy = false }
}
let tree = h.render(() => storage.Storage({ sessions, perform, onDeleted: (ids) => { removed = ids }, onNotice: (value) => { notice = value } }))
const button = (label) => nodes(tree).find((node) => node.type === 'button' && text(node).includes(label))
await tick()
assert.equal(requests.length, 1)
assert.equal(requests[0].path, '/api/storage')
requests[0].resolve(Response.json(usage)); await tick(); tree = h.render()
for (let i = 0; i < 4; i++) tree = h.render()
await tick(); assert.equal(requests.length, 1, 'ordinary rerenders must not fetch statistics')
assert.equal(nodes(tree).filter((node) => node.type === 'tbody')[0].props.children[0].props.children[1].props.children[0].props.children, 'b')
nodes(tree).find((node) => node.type === 'select').props.onChange({ target: { value: 'smallest' } }); tree = h.render()
assert.equal(nodes(tree).filter((node) => node.type === 'tbody')[0].props.children[0].props.children[1].props.children[0].props.children, 'a')
button('全选').props.onClick(); tree = h.render(); assert.match(text(tree), /已选 2 个/)
confirmed = false; button('删除所选').props.onClick(); await tick(); assert.equal(requests.length, 1)
confirmed = true; button('删除所选').props.onClick(); tree = h.render(); await tick()
assert.equal(button('删除所选').props.disabled, true)
assert.match(lastConfirm, /不可撤销/); assert.match(lastConfirm, /共享引用/)
assert.deepEqual(JSON.parse(requests[1].init.body), { confirm: true, session_ids: sessions.map((session) => session.id) })
requests[1].resolve(Response.json({ deleted_ids: [sessions[0].id], missing_ids: [sessions[1].id], logs_freed_bytes: 90, warnings: ['清理警告'] }))
await tick(); tree = h.render(); await tick()
assert.deepEqual(plain(removed), sessions.map((session) => session.id))
assert.match(notice, /90 B/); assert.match(notice, /已不存在/); assert.match(text(tree), /清理警告/)
requests.at(-1).resolve(Response.json(usage)); await tick(); tree = h.render()
assert.match(text(tree), /已选 0 个/)
button('回收数据库').props.onClick(); await tick()
assert.match(lastConfirm, /磁盘空间预检/)
assert.equal(requests.at(-1).path, '/api/storage/reclaim')
assert.deepEqual(JSON.parse(requests.at(-1).init.body), { confirm: true })
requests.at(-1).resolve(new Response('busy', { status: 409 })); await tick(); tree = h.render(); await tick()
assert.match(lastError, /409/)
requests.at(-1).resolve(Response.json(usage)); await tick(); tree = h.render()
button('重试清理日志').props.onClick(); await tick()
assert.equal(requests.at(-1).path, '/api/storage/retry-cleanup')
requests.at(-1).resolve(Response.json({ logs_freed_bytes: 5, warnings: [] })); await tick(); tree = h.render(); await tick()
assert.match(notice, /5 B/)
const manySessions = Array.from({ length: 1001 }, (_, index) => ({ ...usage.sessions[0], id: `session-${index}` }))
requests.at(-1).resolve(Response.json({ ...usage, sessions: manySessions })); await tick(); tree = h.render()
button('全选').props.onClick(); tree = h.render()
assert.match(text(tree), /已选 1000 个/)
assert.match(text(tree), /每次最多删除 1000 个会话/)
let checks = nodes(tree).filter((node) => node.type === 'input' && node.props.type === 'checkbox')
assert.equal(checks.filter((node) => node.props.checked).length, 1000)
assert.equal(checks.filter((node) => node.props.disabled).length, 1)
checks.find((node) => !node.props.checked).props.onChange({ target: { checked: true } }); tree = h.render()
assert.match(text(tree), /已选 1000 个/, 'event handler also enforces the batch limit')
checks.find((node) => node.props.checked).props.onChange({ target: { checked: false } }); tree = h.render()
checks = nodes(tree).filter((node) => node.type === 'input' && node.props.type === 'checkbox')
assert.equal(checks.filter((node) => node.props.disabled).length, 0)
checks.find((node) => !node.props.checked).props.onChange({ target: { checked: true } }); tree = h.render()
button('删除所选').props.onClick(); await tick()
assert.equal(JSON.parse(requests.at(-1).init.body).session_ids.length, 1000)
requests.at(-1).resolve(Response.json({ deleted_ids: [], missing_ids: [], logs_freed_bytes: 0, warnings: [] }))
await tick(); tree = h.render(); await tick()
const outstanding = requests.at(-1)
h.close(); assert.equal(outstanding.init.signal.aborted, true)
console.log('PASS: storage sorting, selection, confirmation, request bodies, busy protection, warnings, refresh and unmount abort')

// Merge UI uses only in-memory samples and a stubbed callback.
const mh = harness()
const merge = load('SessionMerge.tsx', { react: mh.react, './api': api })
const sampleDay = new Date(2026, 10, 20, 10).getTime()
let mergeSessions = sessions.map((session, index) => ({ ...session, summary: { ...session.summary, first_ts: sampleDay + index * 1000, last_ts: sampleDay + index * 1000, device_name: 'device', source_format: 'sysmonitor' } }))
let mergeDisabled = false, mergeCalls = [], finishMerge
let mt = mh.render(() => merge.SessionMerge({ sessions: mergeSessions, disabled: mergeDisabled, onMerge: (ids, name) => { mergeCalls.push({ ids: plain(ids), name }); return new Promise((resolve) => { finishMerge = resolve }) } }))
const mb = (label) => nodes(mt).find((node) => node.type === 'button' && text(node).includes(label))
const mi = (label) => nodes(mt).find((node) => node.type === 'input' && node.props['aria-label'] === label)
const ms = () => nodes(mt).find((node) => node.type === 'form').props.onSubmit({ preventDefault() {} })
assert.equal(mb('合并并分析').props.disabled, true)
mb('全选').props.onClick(); mt = mh.render()
assert.equal(mb('合并并分析').props.disabled, false)
mi('新会话名称').props.onChange({ target: { value: ' 当天汇总 ' } }); mt = mh.render()
let submitted = ms(); mt = mh.render()
assert.equal(mb('正在合并').props.disabled, true)
await ms(); assert.equal(mergeCalls.length, 1)
assert.deepEqual(mergeCalls[0], { ids: sessions.map((session) => session.id), name: '当天汇总' })
finishMerge(false); await submitted; mt = mh.render()
assert.match(text(mt), /已选 2/)
submitted = ms(); finishMerge(true); await submitted; mt = mh.render()
assert.match(text(mt), /已选 0/); assert.equal(mi('新会话名称').props.value, '')
mi('采集日期').props.onChange({ target: { value: '2026-11-19' } }); mt = mh.render()
assert.match(text(mt), /没有符合条件/)
mi('采集日期').props.onChange({ target: { value: '2026-11-20' } }); mt = mh.render()
mb('全选').props.onClick(); mt = mh.render(); assert.match(text(mt), /已选 2/)
mi('名称或设备').props.onChange({ target: { value: 'a' } }); mt = mh.render()
assert.match(text(mt), /已选 0 \/ 1/)
mb('清除筛选').props.onClick(); mt = mh.render(); mb('全选').props.onClick(); mt = mh.render()
mergeSessions[1] = { ...mergeSessions[1], summary: { ...mergeSessions[1].summary, source_format: 'top' } }; mt = mh.render()
assert.match(text(mt), /相同日志格式/); assert.equal(mb('合并并分析').props.disabled, true)
await ms(); assert.equal(mergeCalls.length, 2)
mergeSessions[1].summary.source_format = 'sysmonitor'; mergeSessions[1].summary.device_name = 'other'; mt = mh.render()
assert.match(text(mt), /来自不同设备/)
mergeSessions[1].summary.device_name = '未知设备'; mt = mh.render()
assert.equal(mb('合并并分析').props.disabled, false)
mergeDisabled = true; mt = mh.render(); await ms(); assert.equal(mergeCalls.length, 2)
mergeDisabled = false; mergeSessions = mergeSessions.slice(0, 1); mt = mh.render()
assert.match(text(mt), /已选 1/); assert.equal(mb('合并并分析').props.disabled, true)
mergeSessions = Array.from({ length: 1001 }, (_, index) => ({ ...mergeSessions[0], id: `merge-${index}` }))
mt = mh.render(); mb('全选').props.onClick(); mt = mh.render()
assert.match(text(mt), /已选 1000/)
assert.equal(nodes(mt).filter((node) => node.props.type === 'checkbox' && node.props.disabled).length, 1)
mh.close()
console.log('PASS: merge selection, date/search filters, compatibility, busy state, retry, success and limits')

const appHarness = harness()
let listState = { data: sessions }, deleteResult, finishDelete, appRequest
let now = 0, timerCallback, clearedTimers = 0
const progressWindow = {
  ...windowMock,
  setInterval(callback) { timerCallback = callback; return 1 },
  clearInterval() { timerCallback = undefined; clearedTimers++ },
}
const stubs = Object.fromEntries(['Dashboard', 'Compare', 'Devices', 'ViewPanel', 'SessionMerge'].map((name) => [`./${name}`, { [name]: name }]))
const app = load('App.tsx', {
  react: appHarness.react, ...stubs, './App.css': {},
  './Storage': { Storage: 'Storage', storageDeleteWarning: storage.storageDeleteWarning },
  './Editing': { useEditGuard: () => guard },
  './ReportDates': { ReportDates: 'ReportDates', useReportDates: () => ({ canGenerate: true }) },
  './api': {
    ...api,
    useResource: (path) => path === '/api/sessions' ? listState : { data: { version: 'test' } },
    apiResponse: (path, init, timeout) => new Promise((resolve, reject) => { appRequest = { path, init, timeout, reject }; finishDelete = () => resolve(Response.json(deleteResult)) }),
  },
}, { window: progressWindow, performance: { now: () => now } }, '\nexport { Workspace, OperationProgress }')
let appTree = appHarness.render(() => app.Workspace())
const appNode = (type) => nodes(appTree).find((node) => node.type === type)
const navigate = (label) => {
  nodes(appTree).find((node) => node.type === 'button' && text(node).startsWith(label)).props.onClick()
  appTree = appHarness.render()
}
assert.equal(appNode('Storage'), undefined, 'storage must not mount before entering')
navigate('存储管理'); assert.ok(appNode('Storage'))
appNode('Storage').props.onDeleted([sessions[0].id])
appTree = appHarness.render()
assert.deepEqual(plain(appNode('Storage').props.sessions.map((session) => session.id)), [sessions[1].id], 'stale list response must not resurrect deleted ID')
listState = { error: 'refresh failed' }; appTree = appHarness.render()
navigate('双次采集对比')
assert.equal(appNode('Storage'), undefined, 'storage must unmount on exit')
assert.deepEqual(plain(appNode('Compare').props.sessions.map((session) => session.id)), [sessions[1].id])
navigate('性能分析'); assert.equal(appNode('Dashboard').props.id, sessions[1].id)
navigate('存储管理'); appNode('Storage').props.onDeleted([sessions[1].id]); appTree = appHarness.render()
navigate('性能分析'); assert.equal(appNode('Dashboard'), undefined, 'deleting all sessions must clear selection')
// A later import may legitimately reuse the same ID; it must clear its tombstone.
appNode('Devices').props.onImported(sessions[0].id); appTree = appHarness.render()
assert.equal(appNode('Dashboard').props.id, sessions[0].id)
listState = { data: [sessions[0]] }; appTree = appHarness.render()
deleteResult = { deleted: true, deleted_ids: [sessions[0].id], missing_ids: [], logs_freed_bytes: 12, warnings: ['离线警告'] }
nodes(appTree).find((node) => node.type === 'button' && text(node) === '删除会话').props.onClick()
appTree = appHarness.render()
assert.match(appNode(app.OperationProgress).props.label, /正在删除会话/)
assert.ok(appNode('Dashboard'), 'keep the session until deletion is confirmed')
finishDelete()
await tick(); appTree = appHarness.render()
assert.equal(appNode(app.OperationProgress), undefined, 'hide progress after completion')
assert.equal(appNode('Dashboard'), undefined)
assert.match(text(appTree), /12 B/); assert.match(text(appTree), /离线警告/)
assert.doesNotMatch(text(appTree), /原始日志文件未改动/)
navigate('存储管理')
let failBatch
const pendingBatch = appNode('Storage').props.perform('正在删除所选会话并清理受管日志…', () => new Promise((_resolve, reject) => { failBatch = reject }))
appTree = appHarness.render()
assert.match(appNode(app.OperationProgress).props.label, /正在删除所选会话/)
failBatch(new Error('offline deletion failure'))
await pendingBatch; appTree = appHarness.render()
assert.equal(appNode(app.OperationProgress), undefined, 'hide progress after failure')
assert.match(text(appTree), /offline deletion failure/)
// Creation must keep the new selection while the cached list is still stale.
listState = { data: sessions }; appTree = appHarness.render(); navigate('数据集')
const idsToMerge = sessions.map((session) => session.id)
const previousRequest = appRequest
guard.confirm = () => false
assert.equal(await appNode('SessionMerge').props.onMerge(idsToMerge, '当天'), false)
assert.equal(appRequest, previousRequest)
guard.confirm = () => true
deleteResult = { id: 'merged-new', source_count: 2 }
let appMerge = appNode('SessionMerge').props.onMerge(idsToMerge, '当天'); appTree = appHarness.render()
assert.equal(appRequest.path, '/api/sessions/merge')
assert.equal(appRequest.init.method, 'POST')
assert.deepEqual(JSON.parse(appRequest.init.body), { session_ids: idsToMerge, name: '当天' })
assert.equal(appRequest.timeout, null)
assert.ok(appRequest.init.signal instanceof AbortSignal)
assert.match(appNode(app.OperationProgress).props.label, /正在合并会话/)
assert.equal(await appNode('SessionMerge').props.onMerge(idsToMerge, '重复'), false)
finishDelete(); assert.equal(await appMerge, true); appTree = appHarness.render()
assert.equal(appNode('Dashboard').props.id, 'merged-new')
assert.match(text(appTree), /原会话保留/)
listState = { loading: true }; appTree = appHarness.render()
assert.equal(appNode('Dashboard').props.id, 'merged-new')
listState = { data: [...sessions, { ...sessions[0], id: 'merged-new', name: '当天' }] }; appTree = appHarness.render()
assert.equal(appNode('Dashboard').props.id, 'merged-new')
navigate('数据集')
appMerge = appNode('SessionMerge').props.onMerge(idsToMerge, '失败')
appRequest.reject(new Error('offline merge failure'))
assert.equal(await appMerge, false); appTree = appHarness.render()
assert.match(text(appTree), /offline merge failure/)
assert.ok(appNode('SessionMerge'), 'failed merge must not switch view')
appMerge = appNode('SessionMerge').props.onMerge(idsToMerge, '卸载')
appHarness.close()
assert.equal(appRequest.init.signal.aborted, true)
finishDelete(); assert.equal(await appMerge, false)
console.log('PASS: merge API body, no timeout, guard cancellation, single operation, new-ID refresh, errors and unmount abort')
// Render the shared progress component separately to exercise timer lifecycle.
let progressTree = appHarness.render(() => app.OperationProgress({ label: '正在删除会话…' }))
assert.match(text(progressTree), /已耗时 0 分 0 秒/)
const track = nodes(progressTree).find((node) => node.props.role === 'progressbar')
assert.ok(track)
assert.equal(track.props['aria-valuenow'], undefined, 'never invent a percentage')
now = 65000; timerCallback(); progressTree = appHarness.render()
assert.match(text(progressTree), /已耗时 1 分 5 秒/)
assert.match(text(progressTree), /数据量较大/)
appHarness.close()
assert.equal(clearedTimers, 1, 'unmount clears the timer')
assert.equal(timerCallback, undefined)
console.log('PASS: workspace mounting, stale-list tombstones, comparison candidates, reimport, deletion progress, failure and timer cleanup')