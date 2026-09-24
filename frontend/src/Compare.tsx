import { useState } from 'react'
import { formatNumber, formatTime, sessionPath, useResource, useAnalysisStream } from './api'
import { AnalysisProgress } from './Progress'
import type { Overview, Session } from './api'
import { ComparisonBars } from './Chart'
import type { ComparisonBarItem } from './Chart'

type Stat = 'avg' | 'p95' | 'max' | 'total'
type Difference = { baseline: number | null; target: number | null; delta: number | null; percent: number | null }
type Metric = { label: string; unit: string; baseline_count: number; target_count: number; comparable: boolean; statistics: Partial<Record<Stat, Difference>> }
type Snapshot = { session: Session; cycles: number; segments: number; duration_seconds: number; formats: string[]; interval: { count: number; min: number | null; max: number | null; median: number | null; invalid: number } }
type Process = { name: string; status: 'matched' | 'baseline_only' | 'target_only'; baseline: { observed: number; pids: number[]; formats: string[] } | null; target: { observed: number; pids: number[]; formats: string[] } | null; metrics: Record<string, Metric> }
type Comparison = { baseline: Snapshot; target: Snapshot; system: Record<string, Metric>; io: Record<string, Metric>; processes: Process[]; warnings: string[] }
const fields = [ ['cpu1c', 'CPU'], ['rss_kb', '内存'], ['rd_kb', '物理读取'], ['wr_kb', '物理写入'], ['rchar_kb', '逻辑读取'], ['wchar_kb', '逻辑写入'] ]
const labels: Record<Stat, string> = { avg: '均值', p95: 'P95', max: '峰值', total: '有效累计' }
type SideLabels = { baselineLabel: string; targetLabel: string }
function signed(value: number | null | undefined) { return value == null ? '—' : `${value > 0 ? '+' : ''}${formatNumber(value, 2)}` }
function unit(field: string, stat: Stat, metric: Metric) { return field.endsWith('_kb') && field !== 'rss_kb' ? (stat === 'total' ? 'KB' : 'KB/周期') : metric.unit }

function SessionChoice({ label, labelValue, onLabelChange, sessions, id, segment, onChange, onSegment }: { label: string; labelValue: string; onLabelChange: (value: string) => void; sessions: Session[]; id: string; segment: string; onChange: (id: string) => void; onSegment: (value: string) => void }) {
  const detail = useResource<Overview>(id ? sessionPath(id) : null)
  return <div className="compare-choice">
    <label>显示标签<input value={labelValue} onChange={(e) => onLabelChange(e.target.value)} placeholder={label} maxLength={40} /></label>
    <label>{label}<select value={id} onChange={(e) => onChange(e.target.value)}><option value="">选择采集会话</option>{sessions.map((s) => <option key={s.id} value={s.id}>{s.name} · {formatTime(typeof s.created === 'string' ? Date.parse(s.created) : s.created)} · {s.id.slice(0, 8)}</option>)}</select></label>
    <label>时段<select value={segment} onChange={(e) => onSegment(e.target.value)} disabled={!detail.data}><option value="">全部时段</option>{detail.data?.segments.map((s) => <option key={s.segment} value={s.segment}>时段 {s.segment} · {formatTime(s.start)} — {formatTime(s.end)}</option>)}</select></label>
    {detail.loading && <small>正在读取时段…</small>}{detail.error && <small role="alert">{detail.error}</small>}
  </div>
}

function Coverage({ title, data }: { title: string; data: Snapshot }) {
  return <div className="compare-coverage"><h3>{title} · {data.session.name}</h3><p>{data.cycles} 个观察周期 · {data.segments} 段 · 跨度合计 {formatNumber(data.duration_seconds, 2)} 秒</p><p>采样间隔：中位 {formatNumber(data.interval.median, 2)} 秒，范围 {formatNumber(data.interval.min, 2)}～{formatNumber(data.interval.max, 2)} 秒</p><small>有效相邻间隔 {data.interval.count} · 无效间隔 {data.interval.invalid} · 系统来源 {data.formats.join(' / ') || '未观察到'}</small></div>
}

function MetricCells({ field, metric, stat }: { field: string; metric: Metric; stat: Stat }) {
  const diff = metric.statistics[stat]
  const suffix = unit(field, stat, metric)
  return <><td>{formatNumber(diff?.baseline, 2)} {suffix}</td><td>{formatNumber(diff?.target, 2)} {suffix}</td><td className={diff?.delta == null ? '' : diff.delta > 0 ? 'compare-up' : diff.delta < 0 ? 'compare-down' : ''}>{signed(diff?.delta)} {diff?.delta == null ? '' : suffix === '%' ? '百分点' : suffix}</td><td>{signed(diff?.percent)}{diff?.percent == null ? '' : '%'}</td><td>{metric.baseline_count} / {metric.target_count}{!metric.comparable && <small>口径不同，不计算差值</small>}</td></>
}

function barItem(name: string, metric: Metric, stat: Stat): ComparisonBarItem {
  const diff = metric.statistics[stat]
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

function Results({ data, baselineLabel, targetLabel }: { data: Comparison } & SideLabels) {
  const statusLabels = { matched: '两侧观察到', baseline_only: `仅${baselineLabel}观察到`, target_only: `仅${targetLabel}观察到` }
  const warningText = (warning: string) => warning.replace(/对比采集|基准采集|基准/g, (side) => side === '对比采集' ? targetLabel : baselineLabel)
  const [stat, setStat] = useState<Stat>('avg')
  const [ioStat, setIoStat] = useState<Stat>('avg')
  const [field, setField] = useState('cpu1c')
  const [processStat, setProcessStat] = useState<Stat>('avg')
  const [order, setOrder] = useState('absolute')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const selectedStat = processStat === 'total' && (field === 'cpu1c' || field === 'rss_kb') ? 'avg' : processStat
  const value = (p: Process) => p.metrics[field]?.statistics[selectedStat]?.delta
  const comparable = data.processes.filter((p) => value(p) != null)
  const rows = data.processes.filter((p) => p.name.toLowerCase().includes(query.toLowerCase()) && (order === 'unavailable' ? value(p) == null : value(p) != null && (order === 'increase' ? value(p)! > 0 : order === 'decrease' ? value(p)! < 0 : true)))
  rows.sort((a, b) => (order === 'absolute' ? Math.abs(value(b)!) - Math.abs(value(a)!) : order === 'increase' ? value(b)! - value(a)! : order === 'decrease' ? value(a)! - value(b)! : 0) || a.name.localeCompare(b.name))
  const pages = Math.max(1, Math.ceil(rows.length / 50))
  const current = Math.min(page, pages - 1)
  const rankedRows = rows.filter((p) => p.metrics[field]?.comparable && value(p) != null).slice(0, 20)
  const rankUnit = rankedRows[0] ? unit(field, selectedStat, rankedRows[0].metrics[field]) : field === 'cpu1c' ? '%' : field === 'rss_kb' || selectedStat === 'total' ? 'KB' : 'KB/周期'
  return <>
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
      <div className="table-scroll"><table><thead><tr><th>原始进程名 / 状态</th><th>{baselineLabel}</th><th>{targetLabel}</th><th>差值</th><th>相对变化</th><th>有效样本（{baselineLabel} / {targetLabel}）</th><th>观察周期（{baselineLabel} / {targetLabel}）</th></tr></thead><tbody>{rows.slice(current * 50, (current + 1) * 50).map((p) => <tr key={p.name}><td className="compare-name">{p.name}<small>{statusLabels[p.status]}</small><small>来源：{p.baseline?.formats.join(' + ') || '—'} / {p.target?.formats.join(' + ')|| '—'}</small><small title={`${baselineLabel} PID：${p.baseline?.pids.join(', ') || '—'}；${targetLabel} PID：${p.target?.pids.join(', ') || '—'}`}>PID 数：{p.baseline?.pids.length ?? '—'} / {p.target?.pids.length ?? '—'}</small></td><MetricCells field={field} metric={p.metrics[field]} stat={selectedStat} /><td>{p.baseline?.observed ?? '—'} / {p.target?.observed ?? '—'}</td></tr>)}</tbody></table></div>
      {!rows.length && <p>当前范围没有进程。可切换指标或查看“无法比较 / 单侧观察”。</p>}
      <div className="pagination"><span>{rows.length} 个进程 · 第 {current + 1} / {pages} 页</span><div><button disabled={!current} onClick={() => setPage(current - 1)}>上一页</button><button disabled={current + 1 >= pages} onClick={() => setPage(current + 1)}>下一页</button></div></div>
    </section>
  </>
}

export function Compare({ sessions }: { sessions: Session[] }) {
  const [baseline, setBaseline] = useState(sessions[1]?.id ?? sessions[0]?.id ?? '')
  const [target, setTarget] = useState(sessions.length > 1 ? sessions[0].id : '')
  const [baselineSegment, setBaselineSegment] = useState('')
  const [targetSegment, setTargetSegment] = useState('')
  const [baselineName, setBaselineName] = useState('')
  const [targetName, setTargetName] = useState('')
  const baselineLabel = baselineName.trim() || '基准'
  const targetLabel = targetName.trim() || '对比'
  const [requested, setRequested] = useState('')
  const [run, setRun] = useState(0)
  const params = new URLSearchParams({ baseline, target })
  if (baselineSegment !== '') params.set('baseline_segment', baselineSegment)
  if (targetSegment !== '') params.set('target_segment', targetSegment)
  const path = `/api/compare/stream?${params}`
  const valid = baseline !== target && sessions.some((s) => s.id === baseline) && sessions.some((s) => s.id === target)
  const result = useAnalysisStream<Comparison>(valid && requested === path ? path : null, run)
  return <div className="compare-view"><section className="panel compare-section"><h2>选择两次采集</h2><p>建议选择相同场景和采样设置；两侧可分别选择完整时段。可设置显示标签（最多 40 字，留空使用默认名称），修改标签无需重新计算。</p>{sessions.length < 2 && <p role="status">请先导入或采集至少两个不同会话。</p>}<div className="compare-columns">
    <SessionChoice label={baselineLabel} labelValue={baselineName} onLabelChange={setBaselineName} sessions={sessions} id={baseline} segment={baselineSegment} onChange={(id) => { setBaseline(id); setBaselineSegment(''); setRequested('') }} onSegment={(s) => { setBaselineSegment(s); setRequested('') }} />
    <SessionChoice label={targetLabel} labelValue={targetName} onLabelChange={setTargetName} sessions={sessions} id={target} segment={targetSegment} onChange={(id) => { setTarget(id); setTargetSegment(''); setRequested('') }} onSegment={(s) => { setTargetSegment(s); setRequested('') }} />
  </div><div className="actions"><button onClick={() => { setBaseline(target); setTarget(baseline); setBaselineName(targetName); setTargetName(baselineName); setBaselineSegment(targetSegment); setTargetSegment(baselineSegment); setRequested('') }}>交换两侧</button><button className="primary" disabled={!valid || result.loading} onClick={() => { setRequested(path); setRun(run + 1) }}>{result.loading ? '正在计算…' : result.error ? '重新对比' : '开始对比'}</button>{result.loading && <button onClick={() => setRequested('')}>取消对比</button>}</div>{baseline && baseline === target && <p role="alert">请选择两次不同的采集。</p>}</section>
    {result.loading && <AnalysisProgress baselineLabel={baselineLabel} targetLabel={targetLabel} progress={result.progress} elapsed={result.elapsed} />}
    {result.error && <div className="banner error" role="alert">{result.error}</div>}
    {result.data && <Results baselineLabel={baselineLabel} targetLabel={targetLabel} key={`${requested}:${run}`} data={result.data} />}
  </div>
}