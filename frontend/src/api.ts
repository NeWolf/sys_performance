import { useEffect, useRef, useState } from 'react'

export type Kind = 'S' | 'P' | 'D' | 'DE' | 'DP'
export type MetricValue = number | null
export interface LogNotice {
  source: string
  line: number
  message: string
  ts?: number
}
export interface Session {
  id: string
  name: string
  created: string | number
  summary: {
    counts: Record<Kind, number>
    errors: number
    unknown: number
    issues: LogNotice[]
    events: LogNotice[]
    orphan_cycles: number
    segments: number
    cycles: number
    first_ts: number | null
    last_ts: number | null
    files: string[]
  }
}
export interface Overview extends Session {
  stats: {
    samples: number
    cpu_avg: MetricValue
    cpu_peak: MetricValue
    mem_available_min: MetricValue
    mem_used_peak: MetricValue
  }
  segments: { segment: number; start: number; end: number; records: number }[]
  exporters: { name: string; peak_mb: number; samples: number }[]
}
export interface ProcessRow {
  pids: number[]
  pid_path: number[][]
  pid_changes: number
  concurrent_pids: boolean
  segment: number
  pid: number
  name: string
  samples: number
  // Process ranking CPU fields use raw cpu1c samples, not whole-device cpu.
  cpu_peak: MetricValue
  cpu_avg: MetricValue
  cpu_p95: MetricValue
  cpu_p99: MetricValue
  rss_peak_kb: MetricValue
  rss_avg_kb: MetricValue
  rss_p95_kb: MetricValue
  rss_p99_kb: MetricValue
  wait_peak: MetricValue
  read_kb: MetricValue
  write_kb: MetricValue
  dmabuf_peak_kb: MetricValue
}
export type Sort = 'cpu_peak' | 'cpu_avg' | 'cpu_p95' | 'cpu_p99' | 'rss_peak_kb' | 'rss_avg_kb' | 'rss_p95_kb' | 'rss_p99_kb' | 'read_kb' | 'write_kb' | 'wait_peak' | 'dmabuf_peak_kb'
export interface Point {
  ts: number
  segment: number
  cycle: number
  [field: string]: string | number | null
}
export interface SeriesData {
  points: Point[]
  total: number
  sampled: boolean
  cpu_reference_note?: string
}

export interface NumericStatistic {
  label: string
  unit: string
  note: string
  count: number
  min: MetricValue
  max: MetricValue
  avg: MetricValue
  p95: MetricValue
  p99: MetricValue
  total?: MetricValue
}
export interface StatisticsData {
  samples: number
  start: MetricValue
  end: MetricValue
  method: string
  scope: { kind: Kind; pid: number | null; segment: number | null; name: string | null }
  metrics: Record<string, NumericStatistic>
  categories: Record<string, {
    label: string
    note: string
    count: number
    values: { value: string | number; count: number; percent: number }[]
  }>
}

export function statisticsPath(id: string, kind: Kind, segment: number, filters: Record<string, string> = {}) {
  const query = new URLSearchParams({ kind, segment: String(segment), ...filters })
  return `${sessionPath(id)}/statistics?${query}`
}

export function formatStatistic(value: MetricValue | undefined, unit: string, total = false): string {
  if (value == null) return '—'
  const suffix = total ? unit.replace(' / 周期', '') : unit
  if (suffix.startsWith('KB') || suffix.startsWith('MB')) {
    return formatMemory(value, suffix.startsWith('MB') ? 'MB' : 'KB') + (suffix.includes('周期') ? ' / 周期' : '')
  }
  return `${formatNumber(value, 2)} ${suffix}`
}

export function formatNumber(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits })
}

// 日志时间明确为毫秒，不按数值大小猜测单位。
export function formatTime(value: number | null | undefined, style: 'full' | 'axis' = 'full'): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return '—'
  const pad = (part: number, length = 2) => String(part).padStart(length, '0')
  const day = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  return style === 'axis' ? `${day}\n${clock}` : `${date.getFullYear()}-${day} ${clock}.${pad(date.getMilliseconds(), 3)}`
}

// 保留协议的 KB /MB 命名，以 1024 进位，仅影响展示。
export function formatMemory(value: number | null | undefined, source: 'KB' | 'MB' = 'KB'): string {
  if (value == null || !Number.isFinite(value)) return '—'
  const units = ['KB', 'MB', 'GB', 'TB', 'PB']
  let index = source === 'MB' ? 1 : 0
  let amount = value
  while (Math.abs(amount) >= 1024 && index < units.length - 1) {
    amount /= 1024
    index += 1
  }
  while (amount !== 0 && Math.abs(amount) < 1 && index > 0) {
    amount *= 1024
    index -= 1
  }
  return `${formatNumber(amount, 2)} ${units[index]}`
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请稍后重试。'
}

async function check(response: Response): Promise<Response> {
  if (response.ok) return response
  let message = `请求失败（HTTP ${response.status}）`
  try {
    const body = await response.json() as { detail?: unknown; error?: unknown }
    const detail = body.detail ?? body.error
    if (typeof detail === 'string') message += `：${detail}`
  } catch { /* 非 JSON 错误保留状态码，不展示服务端 HTML。 */ }
  throw new Error(message)
}

export async function apiResponse(path: string, init: RequestInit = {}, timeoutMs: number | null = 300_000): Promise<Response> {
  // 流式计算由调用方取消，不套用普通请求的五分钟总时限。
  // 每次读取当前本地服务令牌，服务重启后无需刷新页面；令牌不持久化。
  const timeout = timeoutMs == null ? null : AbortSignal.timeout(timeoutMs)
  const signal = timeout ? (init.signal ? AbortSignal.any([init.signal, timeout]) : timeout) : init.signal
  try {
    const tokenResponse = await check(await fetch('/api/token', { signal, cache: 'no-store', credentials: 'same-origin' }))
    const { token } = await tokenResponse.json() as { token: string }
    if (!token) throw new Error('本地服务未返回有效会话令牌。')
    const headers = new Headers(init.headers)
    headers.set('X-Session-Token', token)
    return await check(await fetch(path, { ...init, headers, signal, cache: 'no-store', credentials: 'same-origin' }))
  } catch (error) {
    if (error instanceof TypeError) throw new Error('无法连接本地分析服务，请确认后端已启动（127.0.0.1:8765）。')
    throw error
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  return (await apiResponse(path, init)).json() as Promise<T>
}

export function sessionPath(id: string): string {
  return `/api/sessions/${encodeURIComponent(id)}`
}

export function seriesPath(id: string, kind: Kind, segment: number, filters: Record<string, string> = {}) {
  const query = new URLSearchParams({ kind, segment: String(segment), limit: '1500', ...filters })
  return `${sessionPath(id)}/series?${query}`
}

export type ManualFlag = '待填写' | 'Y' | 'N'
export interface GroupMember {
  pid: number
  name: string
  process_type: string
  service_category: '未分类' | '应用服务' | '系统服务'
  related_service: string
  purpose: string
  foreground: ManualFlag
  background: ManualFlag
}
export interface GroupConfig {
  name: string
  segment: number
  members: GroupMember[]
  scene: '未标注' | '前台' | '后台'
  start: number | null
  end: number | null
  description: string
}
export interface ProcessGroup extends GroupConfig { id: string }
export interface GroupAnalysis {
  group: ProcessGroup
  statistics: StatisticsData
  series: SeriesData
  coverage: { cycles: number; complete: number; partial: number; missing: number; duplicates: number }
  note: string
}
export interface GroupResources {
  group: ProcessGroup
  settings: { cpu_platform: string; kdmips_per_core: number | null }
  rows: { member: GroupMember; statistics: Record<'cpu1c' | 'rss_kb' | 'rd_kb' | 'wr_kb', Omit<NumericStatistic, 'label' | 'unit' | 'note'>>; kdmips: Record<'min' | 'avg' | 'p95' | 'p99' | 'max', MetricValue> }[]
  note: string
}
export interface ReportSettings {
  cpu_platform: string
  kdmips_per_core: number | null
  hardware: string
  software: string
  tester: string
  test_notes: string
  worst_scenarios: string
  analysis: string
  criteria: string
  conclusion: '待评估' | '准入' | '有条件准入' | '不准入'
  conclusion_notes: string
}
export function groupPath(id: string, gid?: string) {
  return `${sessionPath(id)}/groups${gid ? `/${encodeURIComponent(gid)}` : ''}`
}

export function useResource<T>(path: string | null, revision = 0) {
  const key = `${revision}:${path}`
  const [state, setState] = useState<{ key: string; data?: T; error?: string }>({ key: '' })
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    api<T>(path, { signal: controller.signal }).then(
      (data) => { if (!controller.signal.aborted) setState({ key, data }) },
      (error: unknown) => { if (!controller.signal.aborted) setState({ key, error: errorMessage(error) }) },
    )
    return () => controller.abort()
  }, [path, key])
  const current = state.key === key ? state : undefined
  return {
    data: path ? current?.data : undefined,
    error: path ? current?.error : undefined,
    loading: Boolean(path && !current),
  }
}
export type Progress = { side?: 'baseline' | 'target' | null; stage: string; completed: number | null; total: number | null }
type StreamState<T> = { key: string; data?: T; error?: string; progress?: Progress; elapsed: number; cancelled?: boolean }

export function useAnalysisStream<T>(path: string | null, run: number, validate?: (data: T) => void) {
  const key = JSON.stringify([path, run])
  const controllerRef = useRef<AbortController | null>(null)
  const [state, setState] = useState<StreamState<T>>({ key: '', elapsed: 0 })
  const current: StreamState<T> = state.key === key ? state : { key, elapsed: 0 }
  const loading = !!path && current.data === undefined && !current.error && !current.cancelled
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    controllerRef.current = controller
    let active = true
    let idleTimer: ReturnType<typeof setTimeout> | undefined
    const started = performance.now()
    const elapsed = () => Math.floor((performance.now() - started) / 1000)
    const touch = () => {
      clearTimeout(idleTimer)
      idleTimer = setTimeout(() => controller.abort(new Error('连接长时间未响应，请重试。')), 60_000)
    }
    const update = (patch: Partial<StreamState<T>>) => {
      if (active && !controller.signal.aborted) setState((previous) => {
        const base = previous.key === key ? previous : { key, elapsed: 0 }
        return { ...base, ...patch, elapsed: Math.max(base.elapsed, patch.elapsed ?? elapsed()) }
      })
    }
    const clock = setInterval(() => update({ elapsed: elapsed() }), 1000)
    async function read() {
      touch()
      try {
        const response = await apiResponse(path!, { signal: controller.signal }, null)
        if (!response.body) throw new Error('浏览器无法读取处理进度，请重试。')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        const consume = (line: string) => {
          if (!line.trim()) return false
          const event = JSON.parse(line) as { type: string; data: T; message: string; elapsed: number } & Progress
          if (event.type === 'progress') update({ progress: event })
          if (event.type === 'heartbeat' && Number.isFinite(event.elapsed)) update({ elapsed: event.elapsed })
          if (event.type === 'error') throw new Error(event.message || '分析失败，请重试。')
          if (event.type === 'result') {
            if (event.data == null) throw new Error('本地服务未返回分析结果，请重试。')
            validate?.(event.data)
            update({ data: event.data })
            return true
          }
          return false
        }
        try {
          while (true) {
            const { done, value } = await reader.read()
            if (controller.signal.aborted) throw controller.signal.reason
            touch()
            buffer += decoder.decode(value, { stream: !done })
            let newline: number
            while ((newline = buffer.indexOf('\n')) !== -1) {
              const line = buffer.slice(0, newline)
              buffer = buffer.slice(newline + 1)
              if (consume(line)) return
            }
            if (done) {
              if (consume(buffer)) return
              throw new Error('进度连接已中断，未收到完整结果，请重试。')
            }
          }
        } finally {
          clearTimeout(idleTimer)
          clearInterval(clock)
          await reader.cancel().catch(() => {})
          reader.releaseLock()
        }
      } catch (error) {
        if (!controller.signal.aborted) update({ error: errorMessage(error) })
        else if (active && controller.signal.reason instanceof Error && controller.signal.reason.name !== 'AbortError') {
          setState((previous) => ({ ...(previous.key === key ? previous : { key, elapsed: 0 }), error: errorMessage(controller.signal.reason) }))
        }
      } finally {
        clearTimeout(idleTimer)
        clearInterval(clock)
        if (controllerRef.current === controller) controllerRef.current = null
      }
    }
    void read()
    return () => {
      active = false
      clearTimeout(idleTimer)
      clearInterval(clock)
      controller.abort()
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [path, key, validate])
  function cancel() {
    if (!loading) return
    controllerRef.current?.abort()
    setState((previous) => ({ ...(previous.key === key ? previous : { key, elapsed: 0 }), cancelled: true }))
  }
  return { ...current, loading, cancel }
}
