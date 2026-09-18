import { useEffect, useState } from 'react'
import { sessionPath, useResource } from './api'
import type { Overview, ProcessRow, SeriesData, Sort } from './api'
import { formatNumber as n, formatMemory } from './api'
import { SeriesChart, Trend, StatisticsPanel } from './Chart'
import { seriesPath, statisticsPath } from './api'

const sortOptions: { value: Sort; label: string }[] = [
  { value: 'cpu_peak', label: 'CPU 峰值' }, { value: 'cpu_avg', label: 'CPU 均值' },
  { value: 'cpu_p95', label: 'CPU P95' }, { value: 'cpu_p99', label: 'CPU P99' },
  { value: 'rss_peak_kb', label: 'RSS 峰值' }, { value: 'rss_avg_kb', label: 'RSS 均值' },
  { value: 'rss_p95_kb', label: 'RSS P95' }, { value: 'rss_p99_kb', label: 'RSS P99' },
  { value: 'read_kb', label: '累计读取' },
  { value: 'write_kb', label: '累计写入' }, { value: 'wait_peak', label: 'wait 峰值' },
  { value: 'dmabuf_peak_kb', label: 'dmabuf 峰值' },
]
const cpuMetrics = [{ field: 'cpu', label: 'CPU' }, { field: 'cpu1c', label: 'cpu1c（单核口径）' }]
const rssMetrics = [{ field: 'rss_kb', label: 'RSS' }]
const ioMetrics = [{ field: 'rd_kb', label: '周期读取' }, { field: 'wr_kb', label: '周期写入' }]
const dmaMetrics = [{ field: 'size_kb', label: '进程 dmabuf' }]
type ProcessPage = { items: ProcessRow[]; total: number; all_total: number }
const identity = (row: ProcessRow) => JSON.stringify([row.segment, row.name])
const pidLabel = (row: ProcessRow) => row.pid_changes > 3
  ? `PID变化${row.pid_changes}次`
  : `PID ${row.pid_path.map((pids) => pids.length > 1 ? `[${pids.join(' + ')}]` : String(pids[0])).join(' → ')}`

export function Processes({ id, segment, onSegment, disabled, members = [], memberSegment, onToggle, segments, visible = true }: {
  id: string; segment: number; onSegment: (value: number) => void; disabled: boolean
  members?: { pid: number; name: string }[]; memberSegment?: number
  onToggle?: (row: ProcessRow) => void; segments?: Overview['segments']; visible?: boolean
}) {
  const [sort, setSort] = useState<Sort>('cpu_avg')
  const [allSegments, setAllSegments] = useState(false)
  const scope = segments && !allSegments ? segment : undefined
  const [previousScope, setPreviousScope] = useState(scope)
  const [page, setPage] = useState(0)
  if (scope !== previousScope) { setPreviousScope(scope); setPage(0) }
  const [revision, setRevision] = useState(0)
  const [selected, setSelected] = useState<ProcessRow | null>(null)
  const [pageSize, setPageSize] = useState(100)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(0) }, 300)
    return () => window.clearTimeout(timer)
  }, [search])
  const params = new URLSearchParams({ sort, limit: String(pageSize), offset: String(page * pageSize), q: query, with_total: 'true', merge_names: 'true' })
  if (scope !== undefined) params.set('segment', String(scope))
  const resource = useResource<ProcessPage>(visible ? `${sessionPath(id)}/processes?${params}` : null, revision)
  const rows = resource.data?.items ?? []
  const total = resource.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  const searching = search.trim() !== query
  const pagingDisabled = disabled || resource.loading || searching || Boolean(resource.error)
  const active = selected?.segment === segment ? selected : null
  return <>
    <div className="section-heading"><div><h2>进程资源排行</h2><p>同一时间段内按名称合并全部 PID，跨段不合并；统计原始样本，不按周期求和。点击进程查看趋势。</p></div>
      <label>降序排列 <select value={sort} disabled={disabled} onChange={(event) => { setSort(event.target.value as Sort); setPage(0) }}>{sortOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
    </div>
    <section className="panel process-panel" aria-label="进程资源排行">
      <div className="process-toolbar">
        {segments && <label>排行范围 <select value={allSegments ? 'all' : segment} disabled={disabled} onChange={(event) => {
          const value = event.target.value
          setAllSegments(value === 'all')
          if (value !== 'all') onSegment(Number(value))
        }}><option value="all">全部时间段</option>{segments.map((item) => <option key={item.segment} value={item.segment}>时间段 {item.segment}</option>)}</select></label>}
        <label className="process-search">搜索范围内进程<input type="search" value={search} maxLength={256} disabled={disabled} placeholder="输入进程名称或 PID" onChange={(event) => setSearch(event.target.value)} /></label>
        {search && <button disabled={disabled} onClick={() => setSearch('')}>清空搜索</button>}
        <label>每页 <select value={pageSize} disabled={disabled} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(0) }}>{[20, 50, 100, 200, 500].map((size) => <option key={size} value={size}>{size} 条</option>)}</select></label>
        <span className="muted" role="status">{searching ? '等待搜索…' : resource.data ? `${scope === undefined ? '全会话' : `时间段 ${scope}`} ${resource.data.all_total} 个进程${query ? ` · 匹配 ${total} 个` : ''}` : '范围内搜索，不限当前页'}</span>
      </div>
      {resource.loading ? <div className="loading-state" role="status">正在读取进程排行…</div>
        : resource.error ? <div className="banner error" role="alert">{resource.error} <button disabled={disabled} onClick={() => setRevision((value) => value + 1)}>重试</button></div>
        : <div className="table-scroll"><table><thead><tr>
          <th>序号</th>{onToggle && <th>加入组</th>}<th>进程名称 / PID</th><th>时间段</th><th>样本</th><th>CPU 峰值 %</th><th>CPU 均值 %</th><th>CPU P95 %</th><th>CPU P99 %</th><th>RSS 峰值</th><th>读取总量</th><th>写入总量</th><th>wait 峰值</th><th>dmabuf 峰值</th>
        </tr></thead><tbody>{rows.map((row, index) => (
          <tr key={identity(row)} className={active && identity(row) === identity(active) ? 'selected-row' : ''}>
            <td>{page * pageSize + index + 1}</td>
            {onToggle && <td><details><summary>选择 PID（{row.segment === memberSegment ? members.filter((member) => member.name === row.name && row.pids.includes(member.pid)).length : 0}/{row.pids.length}）</summary>
              <div className="pid-options">{row.pids.map((pid) => {
                const checked = row.segment === memberSegment && members.some((member) => member.pid === pid && member.name === row.name)
                return <label key={pid}><input type="checkbox" aria-label={`选择时间段 ${row.segment} 的 ${row.name} PID ${pid}`} checked={checked} disabled={disabled || !Number.isSafeInteger(pid) || (members.length > 0 && row.segment !== memberSegment) || (members.length >= 50 && !checked)} onChange={() => onToggle({ ...row, pid })} />PID {pid}</label>
              })}</div>
            </details></td>}
            <td><button className="process-name" disabled={disabled} aria-pressed={Boolean(active && identity(row) === identity(active))} onClick={() => { setSelected(row); onSegment(row.segment) }}><strong>{row.name}</strong><small>{pidLabel(row)} · 查看趋势</small>{row.concurrent_pids && <small>存在并发同名 PID（变化不等于重启次数）</small>}</button></td>
            <td>{row.segment}</td><td>{n(row.samples, 0)}</td><td>{n(row.cpu_peak)}</td><td>{n(row.cpu_avg)}</td><td>{n(row.cpu_p95)}</td><td>{n(row.cpu_p99)}</td><td>{formatMemory(row.rss_peak_kb)}</td><td>{formatMemory(row.read_kb)}</td><td>{formatMemory(row.write_kb)}</td><td>{n(row.wait_peak)}</td><td>{formatMemory(row.dmabuf_peak_kb)}</td>
          </tr>
        ))}</tbody></table>{!rows.length && <div className="loading-state">{query ? '没有匹配的进程，请调整名称或 PID。' : '本页没有进程记录。'}</div>}</div>}
      <div className="pagination"><span>{resource.data ? `第 ${page + 1} / ${pageCount} 页 · 共 ${total} 条` : `第 ${page + 1} 页`} · 每页 {pageSize} 条 · — 表示未采集</span><div>
        <button disabled={pagingDisabled || page === 0} onClick={() => setPage(0)}>首页</button>
        <button disabled={pagingDisabled || page === 0} onClick={() => setPage((value) => value - 1)}>上一页</button>
        <button disabled={pagingDisabled || !resource.data || page + 1 >= pageCount} onClick={() => setPage((value) => value + 1)}>下一页</button>
        <button disabled={pagingDisabled || !resource.data || page + 1 >= pageCount} onClick={() => setPage(pageCount - 1)}>末页</button>
      </div></div>
    </section>
    {active ? <ProcessDetail key={identity(active)} id={id} process={active} disabled={disabled} onClose={() => setSelected(null)} />
      : <div className="process-empty">{selected ? '已切换时间段，请在排行中重新选择该段进程。' : '选择进程名称，查看 CPU、RSS、周期 I/O 与 dmabuf 细节。'}</div>}
  </>
}

function ProcessDetail({ id, process, disabled, onClose }: { id: string; process: ProcessRow; disabled: boolean; onClose: () => void }) {
  const [revision, setRevision] = useState(0)
  const filters = { name: process.name }
  const resource = useResource<SeriesData>(seriesPath(id, 'P', process.segment, filters), revision)
  return <section className="process-detail" aria-label="选中进程明细">
    <div className="section-heading"><div><span className="eyebrow">进程深度分析</span><h2 className="wrap-text">{process.name}</h2><p>{pidLabel(process)} · 时间段 {process.segment} · 同名全部 PID 的原始样本统计，不按周期求和。</p><p>变化按相邻有效周期的 PID 集合计算，缺失周期不计变化。{process.concurrent_pids ? '存在并发同名 PID，集合变化不等于重启次数；趋势展示混合样本。' : ''}</p></div><button disabled={disabled} onClick={onClose}>收起明细</button></div>
    {resource.error && <button disabled={disabled} onClick={() => setRevision((value) => value + 1)}>重试进程趋势</button>}
    <div className="chart-grid">
      <SeriesChart title="进程 CPU / cpu1c" unit="%" metrics={cpuMetrics} {...resource} mode={process.concurrent_pids ? 'scatter' : 'line'} />
      <SeriesChart title="进程驻留内存 RSS" unit="KB" metrics={rssMetrics} {...resource} mode={process.concurrent_pids ? 'scatter' : 'line'} />
      <SeriesChart title="I/O 周期增量" unit="KB / 周期" metrics={ioMetrics} {...resource} mode="scatter" note="原始 rd_kb / wr_kb 增量，不再次差分；零值可能源于权限限制。" />
      <Trend path={seriesPath(id, 'DP', process.segment, filters)} title="进程 dmabuf" unit="KB" metrics={dmaMetrics} mode="scatter" note="仅展示实际记录，不以零补齐缺失周期。" />
    </div>
    <StatisticsPanel title="选中进程 · P 全字段统计" path={statisticsPath(id, 'P', process.segment, filters)} trendPath={seriesPath(id, 'P', process.segment, filters)} disabled={disabled} />
    <StatisticsPanel title="选中进程 · DP 全字段统计" path={statisticsPath(id, 'DP', process.segment, filters)} trendPath={seriesPath(id, 'DP', process.segment, filters)} disabled={disabled} />
  </section>
}