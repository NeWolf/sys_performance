import type { ComparisonReport } from './CompareResults'

export function comparisonReportDocument(runtime: string, report: ComparisonReport) {
  // Escape HTML-significant characters in JSON, including user-supplied labels/names.
  const payload = JSON.stringify(report).replace(/[<>&\u2028\u2029]/g, (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`)
  const script = runtime.replace(/<\/script/gi, '<\\/script')
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; connect-src 'none'; base-uri 'none'; form-action 'none'"><title>双次采集对比报告</title></head><body><div id="root"></div><noscript>请启用 JavaScript 查看对比报告。</noscript><script id="comparison-data" type="application/json">${payload}</script><script>${script}</script></body></html>`
}

export function comparisonReportFilename(date: Date) {
  const pad = (value: number, length = 2) => String(value).padStart(length, '0')
  return `双次采集对比_${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}_${pad(date.getMilliseconds(), 3)}.html`
}

export async function downloadComparisonReport(report: ComparisonReport, date: Date) {
  const { default: runtime } = await import('virtual:comparison-report-runtime')
  const url = URL.createObjectURL(new Blob([comparisonReportDocument(runtime, report)], { type: 'text/html;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = comparisonReportFilename(date)
  document.body.append(anchor)
  try { anchor.click() } finally {
    anchor.remove()
    // Give the browser time to consume the download before releasing its backing data.
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
  }
}