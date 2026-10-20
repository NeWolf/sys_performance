import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { mkdtemp, readdir, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { comparisonReportDocument, comparisonReportFilename } from '../src/compareReport.ts'

const malicious = '</script><img src=x onerror="window.injected=true"> & 中文'
const date = new Date(2026, 9, 19, 12, 34, 56, 789)
assert.equal(comparisonReportFilename(date), '双次采集对比_20261019_123456_789.html')
const metric = (label, baseline, target, unit = '%', comparable = true) => ({
  label, unit, baseline_count: baseline == null ? 0 : 2, target_count: 2, comparable,
  statistics: Object.fromEntries(['avg', 'p95', 'max', 'total'].map((stat, i) => [stat, {
    baseline: baseline == null ? null : baseline + i, target: target + i,
  }])),
})
const snapshot = (id, segment) => ({
  session: { id, name: malicious }, segment,cycles: 2, segments: 1, duration_seconds: 1,
  formats: ['top'], interval: { count: 1, min: 1, max: 1, median: 1, invalid: 0 },
})
const side = { observed: 2, pids: [1], pid_changes: 0, formats: ['top'] }
const report = {
  baselineLabel: malicious, targetLabel: '新版本', generatedAt: date.toLocaleString('zh-CN'),
  view: { stat: 'p95', ioStat: 'total', field: 'rss_kb', processStat: 'max', order: 'absolute', query: 'proc', page: 1 },
  data: {
    baseline: snapshot('baseline-id', 0), target: snapshot('target-id', null),
    system: { cpu_total: metric('整机 CPU', 1, 2), mem_remaining_mb: metric('剩余内存', 100, 200, 'MB') },
    io: { rd_kb: metric('读取', 0, 100, 'KB') },
    processes: Array.from({ length: 61 }, (_, index) => ({
      name: index === 60 ? malicious : `proc-${String(index).padStart(2, '0')}`,
      status: index === 60 ? 'target_only' : 'matched', baseline: index === 60 ? null : side, target: side,
      metrics: Object.fromEntries(['cpu1c', 'rss_kb', 'rd_kb', 'wr_kb', 'rchar_kb', 'wchar_kb'].map((field) => [field, metric(field, index === 60 ? null : index, index + 10, field === 'cpu1c' ? '%' : 'KB')])),
    })),
    warnings: ['差值=对比采集−基准采集；缺失不补零。', malicious],
  },
}
const files = await readdir(new URL('../dist/assets/', import.meta.url))
const runtimeFile = files.find((name) => name.startsWith('_virtual_comparison-report-runtime-') && name.endsWith('.js'))
assert.ok(runtimeFile, 'Run npm run build before this test')
const { default: runtime } = await import(new URL(`../dist/assets/${runtimeFile}`, import.meta.url))
const html = comparisonReportDocument(runtime, report)
// Opening script text may legitimately occurinside the bundled runtime.
assert.equal((html.match(/<\/script>/g) || []).length, 2)
assert.ok(!html.includes(malicious))
assert.deepEqual(JSON.parse(html.match(/type="application\/json">(.*?)<\/script>/s)[1]), report)
assert.ok(html.includes("connect-src 'none'"))
console.log('PASS: filename, complete payload, HTML escaping and network isolation')

const directory = await mkdtemp(join(tmpdir(), 'compare-report-test-'))
const filename = join(directory, 'report.html')
await writeFile(filename, html)
const chrome = spawn(process.env.CHROME_BIN || '/Applications/Google Chrome Dev.app/Contents/MacOS/Google Chrome Dev', [
  '--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-pipe', `--user-data-dir=${join(directory, 'profile')}`, 'about:blank',
], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] })
let nextId = 0
const pending = new Map()
const failures = []
let buffer = ''
chrome.stdio[4].on('data', (chunk) => {
  buffer += chunk.toString()
  let end
  while ((end = buffer.indexOf('\0')) >= 0) {
    const message = JSON.parse(buffer.slice(0, end))
    buffer = buffer.slice(end + 1)
    if (message.method === 'Runtime.exceptionThrown') failures.push(message.params.exceptionDetails)
    if (message.id && pending.has(message.id)) {
      const { resolve, reject, timer } = pending.get(message.id)
      clearTimeout(timer)
      pending.delete(message.id)
      if (message.error) reject(new Error(JSON.stringify(message.error)))
      else resolve(message.result)
    }
  }
})
chrome.on('error', (error) => {
  for (const { reject, timer } of pending.values()) { clearTimeout(timer); reject(error) }
  pending.clear()
})
function call(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++nextId
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)) }, 15000)
    pending.set(id, { resolve, reject, timer })
    chrome.stdio[3].write(JSON.stringify({ id, method, params, sessionId }) + '\0')
  })
}
try {
  const { targetId } = await call('Target.createTarget', { url: 'about:blank' })
  const { sessionId } = await call('Target.attachToTarget', { targetId, flatten: true })
  const evaluate = async (expression) => {
    const result = await call('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId)
    assert.ok(!result.exceptionDetails, JSON.stringify(result.exceptionDetails))
    return result.result.value
  }
  const waitFor = async (expression) => {
    for (let i = 0; i < 100; i++) {
      if (await evaluate(expression)) return
      await new Promise((resolve) => setTimeout(resolve, 50))
    }
    assert.fail(`Browser condition not met: ${expression}`)
  }
  await call('Runtime.enable', {}, sessionId)
  await call('Network.enable', {}, sessionId)
  await call('Network.emulateNetworkConditions', { offline: true, latency: 0, downloadThroughput: 0, uploadThroughput: 0 }, sessionId)
  await call('Page.navigate', { url: pathToFileURL(filename).href }, sessionId)
  await waitFor('document.querySelectorAll("canvas").length >= 4')
  assert.ok(await evaluate('document.body.textContent.includes("第 2 / 2 页")'))
  assert.equal(await evaluate('document.querySelectorAll(".compare-name").length'), 10)
  assert.deepEqual(await evaluate('[...document.querySelectorAll("select")].map(s => s.value)'), ['p95', 'total', 'rss_kb', 'max', 'absolute'])
  assert.ok(await evaluate('document.body.textContent.includes("时段 0") && document.body.textContent.includes("全部时段")'))
  assert.equal(await evaluate('Boolean(window.injected) || Boolean(document.querySelector("img"))'), false)
  await evaluate('[...document.querySelectorAll("button")].find(b => b.textContent === "上一页").click()')
  await waitFor('document.querySelectorAll(".compare-name").length === 50')
  await evaluate('Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(document.querySelector("input"),"proc-59"); document.querySelector("input").dispatchEvent(new Event("input",{bubbles:true}))')
  await waitFor('document.querySelectorAll(".compare-name").length === 1')
  assert.ok(await evaluate('document.querySelector(".compare-name").textContent.includes("proc-59")'))
  await evaluate('Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,"value").set.call(document.querySelector("input"),""); document.querySelector("input").dispatchEvent(new Event("input",{bubbles:true})); const s=document.querySelectorAll("select")[4]; s.value="unavailable"; s.dispatchEvent(new Event("change",{bubbles:true}))')
  await waitFor('document.querySelector(".compare-name")?.textContent.includes("onerror")')
  assert.ok(await evaluate('document.querySelector(".compare-name").textContent.includes("仅新版本观察到")'))
  await evaluate('{ const metricSelect=document.querySelectorAll("select")[2]; metricSelect.value="cpu1c"; metricSelect.dispatchEvent(new Event("change",{bubbles:true})); }')
  await waitFor('document.querySelectorAll("select")[2].value === "cpu1c"')
  assert.equal(await evaluate('Boolean(window.injected) || Boolean(document.querySelector("img"))'), false)
  assert.equal(await evaluate('performance.getEntriesByType("resource").length'), 0)
  assert.deepEqual(failures, [])
  console.log('PASS: file:// offline charts, saved filters, pagination, search, one-sided processes and injection safety')
  if (process.env.APP_URL) {
    await call('Network.emulateNetworkConditions', { offline: false, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }, sessionId)
    await call('Page.navigate', { url: process.env.APP_URL }, sessionId)
    await waitFor('[...document.querySelectorAll("button")].some(b => b.textContent.includes("双次采集对比"))')
    await evaluate('[...document.querySelectorAll("button")].find(b => b.textContent.includes("双次采集对比")).click()')
    await waitFor('[...document.querySelectorAll("button")].some(b => b.textContent === "导出对比报告（HTML）")')
    assert.ok(await evaluate('[...document.querySelectorAll("button")].find(b => b.textContent === "导出对比报告（HTML）").disabled'))
    assert.ok(await evaluate('document.body.textContent.includes("请先点击“开始对比”")'))
    assert.deepEqual(failures, [])
    console.log('PASS: live comparison page shows disabled export entry before calculation')
  }
} finally {
  chrome.kill()
}