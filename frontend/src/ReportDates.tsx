/* eslint-disable react/only-export-components -- Keep the shared date UI and its helpers in one module. */
import { useMemo, useState } from 'react'
import { errorMessage, sessionPath, useResource } from './api'
import type { Overview } from './api'

type Segment = Overview['segments'][number]
export type ReportDateData = { dates: { date: string; records: number; segments: Segment[] }[]; undated_records: number }

export function validReportDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const leap = year! % 4 === 0 && (year! % 100 !== 0 || year! % 400 === 0)
  return year! > 0 && month! >= 1 && month! <= 12 && day! >= 1 && day! <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month! - 1]!
}

export function validateReportDates(value: ReportDateData): ReportDateData {
  const count = (n: number) => Number.isSafeInteger(n) && n >= 0
  if (!value || !Array.isArray(value.dates) || !count(value.undated_records)) throw new Error('报告日期响应无效，请重试。')
  let previous = ''
  for (const item of value.dates) {
    if (!item || typeof item.date !== 'string' || !validReportDate(item.date) || item.date <= previous || !count(item.records) || !item.records || !Array.isArray(item.segments)) throw new Error('报告日期响应无效，请重试。')
    const seen = new Set<number>()
    for (const segment of item.segments) {
      if (!segment || !count(segment.segment) || seen.has(segment.segment) || !Number.isFinite(segment.start) || !Number.isFinite(segment.end) || segment.start > segment.end || !count(segment.records) || !segment.records) throw new Error('报告日期时间段响应无效，请重试。')
      seen.add(segment.segment)
    }
    previous = item.date
  }
  return value
}

export function canGenerateDates(data: ReportDateData | undefined, selected: string[]): boolean {
  return !!data && (!data.dates.length || (selected.length > 0 && selected.every((date) => data.dates.some((item) => item.date === date))))
}

export function reportQuery(style: 'default' | 'compact', dates?: string[]): string {
  if (dates && (!dates.length || dates.some((date) => !validReportDate(date)))) throw new Error('请至少选择一个有效报告日期。')
  const query = new URLSearchParams()
  if (style === 'compact') query.set('style', style)
  dates?.forEach((date) => query.append('dates', date))
  return query.size ? `?${query}` : ''
}

// Check raw syntax before URL parsing: no origins, fragments, encodings or extra keys.
export function assertReportPath(path: string, expected?: string): void {
  const parse = (value: string) => {
    if (typeof value !== 'string' || !/^\/api\/sessions\/[a-f0-9]{32}\/report(?:\?(?:style=(?:default|compact)|dates=\d{4}-\d{2}-\d{2})(?:&(?:style=(?:default|compact)|dates=\d{4}-\d{2}-\d{2}))*)?$/.test(value)) throw new Error('本地服务返回了无效报告地址，请重试。')
    const [base, query] = value.split('?')
    const params = new URLSearchParams(query)
    const dates = params.getAll('dates')
    if (params.getAll('style').length > 1 || dates.some((date) => !validReportDate(date)) || new Set(dates).size !== dates.length) throw new Error('本地服务返回了无效报告参数，请重试。')
    return JSON.stringify([base, params.get('style') || 'default', dates.sort()])
  }
  const actual = parse(path)
  if (expected !== undefined && actual !== parse(expected)) throw new Error('报告日期、样式或会话与当前选择不一致，请重试。')
}

export function selectedSegments(data: ReportDateData | undefined, selected: string[], fallback: Segment[]): Segment[] {
  if (!data) return []
  if (!data.dates.length) return fallback
  const merged = new Map<number, Segment>()
  for (const item of data.dates.filter((entry) => selected.includes(entry.date))) {
    for (const segment of item.segments) {
      const old = merged.get(segment.segment)
      merged.set(segment.segment, old ? { segment: segment.segment, start: Math.min(old.start, segment.start), end: Math.max(old.end, segment.end), records: old.records + segment.records } : { ...segment })
    }
  }
  return [...merged.values()].sort((a, b) => a.segment - b.segment)
}

export function useReportDates(id: string | null, revision: number) {
  const [retry, setRetry] = useState(0)
  const scopeKey = JSON.stringify([id, revision, retry])
  const [request, setRequest] = useState({ key: scopeKey, generation: 0 })
  if (request.key !== scopeKey) setRequest({ key: scopeKey, generation: request.generation + 1 })
  const resource = useResource<ReportDateData>(id ? `${sessionPath(id)}/report-dates` : null, request.generation)
  const parsed = useMemo(() => {
    if (!resource.data) return { data: undefined, error: resource.error }
    try { return { data: validateReportDates(resource.data), error: undefined } }
    catch (error) { return { data: undefined, error: errorMessage(error) } }
  }, [resource.data, resource.error])
  const key = JSON.stringify([scopeKey, request.generation])
  const [choice, setChoice] = useState<{ key: string; dates: string[] }>()
  const latest = parsed.data?.dates.at(-1)?.date
  const selected = choice?.key === key ? choice.dates : latest ? [latest] : []
  return {
    ...parsed, loading: resource.loading, selected,
    canGenerate: canGenerateDates(parsed.data, selected),
    onChange: (dates: string[]) => setChoice({ key, dates }),
    retry: () => setRetry((value) => value + 1),
  }
}
export type ReportDateScope = ReturnType<typeof useReportDates>

export function ReportDates({ scope, disabled = false }: { scope: ReportDateScope; disabled?: boolean }) {
  const { data, loading, error, selected, onChange, retry } = scope
  return <fieldset className="report-dates config-fields" disabled={disabled}>
    <legend>报告日期（本机日期）</legend>
    {loading && <p role="status">正在读取报告日期，完成前不能生成报告…</p>}
    {error && <p className="banner error" role="alert">日期加载失败：{error} 不会回退生成全量报告。<button type="button" onClick={retry}>重试日期加载</button></p>}
    {data && <>
      {!data.dates.length ? <p>没有可按日选择的记录，使用原先全量报告。</p> : <>
        <div className="report-date-options">{data.dates.map((item) => <label key={item.date}><input type="checkbox" checked={selected.includes(item.date)} onChange={(event) => onChange(data.dates.filter((entry) => entry.date === item.date ? event.target.checked : selected.includes(entry.date)).map((entry) => entry.date))} />{item.date}（{item.records} 条）</label>)}</div>
        <p>{selected.length > 1 ? `将合并所选 ${selected.length} 天的记录生成报告。` : selected.length ? '默认选择最近一天，可勾选多个日期合并生成。' : '请至少选择一天，空选不能生成报告。'}</p>
      </>}
      {data.undated_records > 0 && <p className="muted">有 {data.undated_records} 条无日期记录，按日选择时不包含这些记录。</p>}
    </>}
  </fieldset>
}