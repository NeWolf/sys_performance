import { useState } from 'react'
import { formatNumber, formatStatistic, formatPid, roundMetric, subtractMetric, scaleMetric } from './api'
import type { Session } from './api'
import { ComparisonBars } from './Chart'
import type { ComparisonBarItem } from './Chart'

export type Stat = 'avg' | 'p95' | 'max' | 'total'
type Difference = { baseline: number | null; target: number | null; delta: number | null; percent: number | null }
type Metric = { label: string; unit: string; baseline_count: number; target_count: number; comparable: boolean; statistics: Partial<Record<Stat, Difference>> }
type Snapshot = { session: Session; segment: number | null; cycles: number; segments: number; duration_seconds: number; formats: string[]; interval: { count: number; min: number | null; max: number | null; median: number | null; invalid: number } }
type Process = { name: string; status: 'matched' | 'baseline_only' | 'target_only'; baseline: { observed: number; pids: number[]; pid_changes: number; formats: string[] } | null; target: { observed: number; pids: number[]; pid_changes: number; formats: string[] } | null; metrics: Record<string, Metric> }
export type Comparison = { baseline: Snapshot; target: Snapshot; system: Record<string, Metric>; io: Record<string, Metric>; processes: Process[]; warnings: string[] }
const fields = [ ['cpu1c', 'CPU'], ['rss_kb', '内存'], ['rd_kb', '物理读取'], ['wr_kb', '物理写入'], ['rchar_kb', '逻辑读取'], ['wchar_kb', '逻辑写入'] ]
const labels: Record<Stat, string> = { avg: '均值', p95: 'P95', max: '峰值', total: '有效累计' }
type SideLabels = { baselineLabel: string; targetLabel: string }
function signed(value: number | null | undefined) {
  const rounded = roundMetric(value)
  return rounded == null ? '—' : `${rounded > 0 ? '+' : ''}${formatNumber(rounded)}`
}

function difference(metric: Metric | undefined, stat: Stat): Difference {
  const source = metric?.statistics[stat]
  const baseline = roundMetric(source?.baseline)
  const target = roundMetric(source?.target)
  const delta = metric?.comparable ? subtractMetric(target, baseline) : null
  return { baseline, target, delta, percent: baseline === null || baseline === 0 ? null : scaleMetric(delta, 100, Math.abs(baseline)) }
}
function unit(field: string, stat: Stat, metric: Metric) { return field.endsWith('_kb') && field !== 'rss_kb' ? (stat === 'total' ? 'KB' : 'KB/周期') : metric.unit }

function Coverage({ title, data }: { title: string; data: Snapshot }) {
  return <div className="compare-coverage"><h3>{title} · {data.session.name}</h3><p>会话：{data.session.id} · 范围：{data.segment == null ? '全部时段' : `时段 ${data.segment}`}</p><p>{data.cycles} 个观察周期 · {data.segments} 段 · 跨度合计 {formatNumber(data.duration_seconds, 2)} 秒</p><p>采样间隔：中位 {formatNumber(data.interval.median, 2)} 秒，范围 {formatNumber(data.interval.min, 2)}～{formatNumber(data.interval.max, 2)} 秒</p><small>有效相邻间隔 {data.interval.count} · 无效间隔 {data.interval.invalid} · 系统来源 {data.formats.join(' / ') || '未观察到'}</small></div>
}

function MetricCells({ field, metric, stat }: { field: string; metric: Metric; stat: Stat }) {
  const diff = difference(metric, stat)
  const suffix = unit(field, stat, metric)
  return <><td>{formatStatistic(diff.baseline, suffix)}</td><td>{formatStatistic(diff.target, suffix)}</td><td className={diff?.delta == null ? '' : diff.delta > 0 ? 'compare-up' : diff.delta < 0 ? 'compare-down' : ''}>{diff.delta != null && diff.delta > 0 ? '+' : ''}{formatStatistic(diff.delta, suffix === '%' ? '百分点' : suffix)}</td><td>{signed(diff?.percent)}{diff?.percent == null ? '' : '%'}</td><td>{metric.baseline_count} / {metric.target_count}{!metric.comparable && <small>口径不同，不计算差值</small>}</td></>
}

function barItem(name: string, metric: Metric, stat: Stat): ComparisonBarItem {
  const diff = difference(metric, stat)
  return { name, baseline: diff?.baseline ?? null, target: diff?.target ?? null,
    delta: diff?.delta ?? null, percent: diff?.percent ?? null, comparable: metric.comparable }
}

function ResourceBars({ metrics, stat, baselineLabel, targetLabel }: { metrics: Record<string, Metric>; stat: Stat } & SideLabels) {
  const groups = new Map<string, { title: string; unit: string; items: ComparisonBarItem[] }>()
  Object.entries(metrics).forEach(([field, metric]) => {
    const suffix = unit(field, stat, metric)
    const category = field.startsWith('cpu_') ? '整机 CPU' : field.startsWith('mem_') ? (suffix === '%' ? '内存使用率' : '内存容量') : '已采集进程 IO'
    const key = `${category}:${suffix}`
    if (!groups.has(key)) groups.set(key, { title: category, unit: suffix, items: [] })
    groups.get(key)!.items.push(barItem(metric.label, metric, stat))
  })
  return <div className="compare-charts">{Array.from(groups, ([key, group]) => <ComparisonBars baselineLabel={baselineLabel} targetLabel={targetLabel} key={key} title={`${group.title} · ${labels[stat]}`} unit={group.unit} items={group.items} />)}</div>
}

function ResourceTable({ title, metrics, stat, baselineLabel, targetLabel }: { title: string; metrics: Record<string, Metric>; stat: Stat } & SideLabels) {
  return <section className="panel compare-section"><h2>{title}</h2><ResourceBars baselineLabel={baselineLabel} targetLabel={targetLabel} metrics={metrics} stat={stat} /><div className="table-scroll"><table><thead><tr><th>指标 · {labels[stat]}</th><th>{baselineLabel}</th><th>{targetLabel}</th><th>差值</th><th>相对变化</th><th>有效样本（{baselineLabel} / {targetLabel}）</th></tr></thead><tbody>{Object.entries(metrics).map(([field, metric]) => <tr key={field}><td>{metric.label}</td><MetricCells field={field} metric={metric} stat={stat} /></tr>)}</tbody></table></div></section>
}

export type ComparisonView = { stat: Stat; ioStat: Stat; field: string; processStat: Stat; order: string; query: string; page: number }
export type ComparisonReport = SideLabels & { data: Comparison; view: ComparisonView; generatedAt: string }

export function Results({ data, baselineLabel, targetLabel, initialView, onExport }: { data: Comparison; initialView?: ComparisonView; onExport?: (view: ComparisonView) => Promise<void> } & SideLabels) {
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState('')
  const [exported, setExported] = useState(false)
  async function exportReport() {
    if (!onExport || exporting) return
    setExporting(true)
    setExportError('')
    setExported(false)
    try {
      await onExport({ stat, ioStat, field, processStat: selectedStat, order, query, page: current })
      setExported(true)
    } catch {
      setExportError('报告导出失败，请重试。')
    } finally { setExporting(false) }
  }
  const statusLabels = { matched: '两侧观察到', baseline_only: `仅${baselineLabel}观察到`, target_only: `仅${targetLabel}观察到` }
  const warningText = (warning: string) => warning.replace(/对比采集|基准采集|基准/g, (side) => side === '对比采集' ? targetLabel : baselineLabel)
  const [stat, setStat] = useState<Stat>(initialView?.stat ?? 'avg')
  const [ioStat, setIoStat] = useState<Stat>(initialView?.ioStat ?? 'avg')
  const [field, setField] = useState(initialView?.field ?? 'cpu1c')
  const [processStat, setProcessStat] = useState<Stat>(initialView?.processStat ?? 'avg')
  const [order, setOrder] = useState(initialView?.order ?? 'absolute')
  const [query, setQuery] = useState(initialView?.query ?? '')
  const [page, setPage] = useState(initialView?.page ?? 0)
  const selectedStat = processStat === 'total' && (field === 'cpu1c' || field === 'rss_kb') ? 'avg' : processStat
  const value = (p: Process) => difference(p.metrics[field], selectedStat).delta
  const comparable = data.processes.filter((p) => value(p) != null)
  const rows = data.processes.filter((p) => p.name.toLowerCase().includes(query.toLowerCase()) && (order === 'unavailable' ? value(p) == null : value(p) != null && (order === 'increase' ? value(p)! > 0 : order === 'decrease' ? value(p)! < 0 : true)))
  rows.sort((a, b) => (order === 'absolute' ? Math.abs(value(b)!) - Math.abs(value(a)!) : order === 'increase' ? value(b)! - value(a)! : order === 'decrease' ? value(a)! - value(b)! : 0) || a.name.localeCompare(b.name))
  const pages = Math.max(1, Math.ceil(rows.length / 50))
  const current = Math.min(page, pages - 1)
  const rankedRows = rows.filter((p) => p.metrics[field]?.comparable && value(p) != null).slice(0, 20)
  const rankUnit = rankedRows[0] ? unit(field, selectedStat, rankedRows[0].metrics[field]) : field === 'cpu1c' ? '%' : field === 'rss_kb' || selectedStat === 'total' ? 'KB' : 'KB/周期'
  return <>
    {onExport && <div className="actions"><button className="primary" disabled={exporting} onClick={exportReport}>{exporting ? '正在导出…' : '导出对比报告（HTML）'}</button><span className="muted">包含全部指标及进程，离线可切换统计、搜索和翻页。</span></div>}
    {exportError && <p className="banner error" role="alert">{exportError}</p>}
    {exported && <p className="muted" role="status">已发起报告下载，可在浏览器下载列表查看。</p>}
    <section className="panel compare-section"><div className="compare-columns"><Coverage title={baselineLabel} data={data.baseline} /><Coverage title={targetLabel} data={data.target} /></div><details><summary>比较口径与注意事项</summary><ul>{data.warnings.map((w) => <li key={w}>{warningText(w)}</li>)}</ul></details><p className="muted">差值 = {targetLabel} − {baselineLabel}；缺失不补零。CPU 差值为百分点。剩余内存按每个样本的总量−已用计算后统计，不能用两列 P95 相减。系统内存跨来源仍展示差值，但缓存口径可能不同，仅供参考；进程内存跨来源或混合来源仍计算差值并纳入排名，GPU 回填等口径差异仅作提示，不直接据此判断退化。IO 非 KB/s，累计量受时长及有效覆盖影响。</p></section>
    <div className="metric-select"><label>系统统计 <select value={stat} onChange={(e) => setStat(e.target.value as Stat)}>{(['avg', 'p95', 'max'] as Stat[]).map((s) => <option key={s} value={s}>{labels[s]}</option>)}</select></label></div>
    <ResourceTable baselineLabel={baselineLabel} targetLabel={targetLabel} title="整机 CPU 与内存" metrics={data.system} stat={stat} />
    <div className="metric-select"><label>IO 统计 <select value={ioStat} onChange={(e) => setIoStat(e.target.value as Stat)}>{(Object.keys(labels) as Stat[]).map((s) => <option key={s} value={s}>{labels[s]}</option>)}</select></label></div>
    <ResourceTable baselineLabel={baselineLabel} targetLabel={targetLabel} title="已采集进程 IO 合计（非整机磁盘）" metrics={data.io} stat={ioStat} />
    <section className="panel compare-section"><h2>进程变化排行</h2><p>按原始名称匹配，同周期同名 PID 先合计。默认按差值绝对值从大到小排列；红色表示增加，绿色表示减少，不自动判定好坏。</p>
      <div className="process-toolbar">
        <label>指标<select value={field} onChange={(e) => { setField(e.target.value); setPage(0) }}>{fields.map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label>统计<select value={selectedStat} onChange={(e) => { setProcessStat(e.target.value as Stat); setPage(0) }}>{(Object.keys(labels) as Stat[]).filter((s) => s !== 'total' || (field !== 'cpu1c' && field !== 'rss_kb')).map((s) => <option key={s} value={s}>{labels[s]}</option>)}</select></label>
        <label>排序 / 范围<select value={order} onChange={(e) => { setOrder(e.target.value); setPage(0) }}><option value="absolute">变化最大（差值绝对值）</option><option value="increase">增加最多</option><option value="decrease">减少最多</option><option value="unavailable">无法比较 / 单侧观察</option></select></label>
        <label className="process-search">原始进程名<input value={query} onChange={(e) => { setQuery(e.target.value); setPage(0) }} placeholder="搜索进程名称" /></label>
      </div><p className="muted">当前指标可比较 {comparable.length} 个进程；缺值或单侧观察 {data.processes.length - comparable.length} 个，不纳入排名。</p>
      <ComparisonBars baselineLabel={baselineLabel} targetLabel={targetLabel} title={`进程变化前 20 名 · ${fields.find(([key]) => key === field)?.[1]} · ${labels[selectedStat]}`} unit={rankUnit} ranking items={rankedRows.map((p) => barItem(p.name, p.metrics[field], selectedStat))} note={`差值 = ${targetLabel} − ${baselineLabel}；按当前搜索和排序展示前 ${rankedRows.length} 名，不随表格翻页变化。${field === 'cpu1c' ? '进程 CPU 为单核口径，可超过 100%。' : field === 'rss_kb' ? '跨来源内存仍参与排行，GPU 回填等口径差异可能影响数值，结果仅供参考。' : 'IO 非速率，累计量受时长和有效覆盖影响。'}`} />
      <div className="table-scroll"><table><thead><tr><th>原始进程名 / 状态</th><th>{baselineLabel}</th><th>{targetLabel}</th><th>差值</th><th>相对变化</th><th>有效样本（{baselineLabel} / {targetLabel}）</th><th>观察周期（{baselineLabel} / {targetLabel}）</th></tr></thead><tbody>{rows.slice(current * 50, (current + 1) * 50).map((p) => <tr key={p.name}><td className="compare-name">{p.name}<small>{statusLabels[p.status]}</small><small>来源：{p.baseline?.formats.join(' + ') || '—'} / {p.target?.formats.join(' + ')|| '—'}</small><small>{baselineLabel}：{p.baseline ? formatPid(p.baseline.pids, p.baseline.pid_changes) : '—'}；{targetLabel}：{p.target ? formatPid(p.target.pids, p.target.pid_changes) : '—'}</small></td><MetricCells field={field} metric={p.metrics[field]} stat={selectedStat} /><td>{p.baseline?.observed ?? '—'} / {p.target?.observed ?? '—'}</td></tr>)}</tbody></table></div>
      {!rows.length && <p>当前范围没有进程。可切换指标或查看“无法比较 / 单侧观察”。</p>}
      <div className="pagination"><span>{rows.length} 个进程 · 第 {current + 1} / {pages} 页</span><div><button disabled={!current} onClick={() => setPage(current - 1)}>上一页</button><button disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>下一页</button></div></div>
    </section>
  </>
}

