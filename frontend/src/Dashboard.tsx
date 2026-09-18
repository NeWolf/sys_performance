import { useState } from 'react'
import { seriesPath, statisticsPath, sessionPath, useResource } from './api'
import type { Overview, SeriesData, StatisticsData } from './api'
import { formatNumber as n, formatMemory, formatTime, formatStatistic } from './api'
import { SeriesChart, Trend, StatisticsPanel } from './Chart'
import { Groups } from './Groups'
import { ReportConfig } from './ReportConfig'
import { Processes } from './Processes'
import { ViewPanel } from './ViewPanel'

type DashboardProps = { id: string; disabled: boolean; view: string }

const cpuMetrics = [
  { field: 'cpu_total', label: '总 CPU' }, { field: 'cpu_user', label: '用户态' },
  { field: 'cpu_sys', label: '内核态' }, { field: 'cpu_iow', label: 'I/O 等待' }, { field: 'cpu_irq', label: '中断' },
]
const memoryMetrics = [{ field: 'mem_avail_mb', label: '可用内存' }, { field: 'mem_used_mb', label: '已用内存' }, { field: 'mem_total_mb', label: '总内存' }]
const dmaMetrics = [{ field: 'total_mb', label: 'dmabuf 总量' }]
const exporterMetrics = [{ field: 'size_mb', label: 'exporter 大小' }]

// 趋势查询参数统一由 API 层生成。

export function Dashboard({ id, disabled, view }: DashboardProps) {
  const [revision, setRevision] = useState(0)
  const { data, error, loading } = useResource<Overview>(sessionPath(id), revision)
  if (loading) return <div className="panel loading-state" role="status">正在读取会话统计…</div>
  if (error) return <div className="banner error" role="alert">{error} <button disabled={disabled} onClick={() => setRevision((value) => value + 1)}>重试</button></div>
  if (!data) return null
  return <Analysis data={data} disabled={disabled} view={view} />
}

function Analysis({ data, disabled, view }: { data: Overview; disabled: boolean; view: string }) {
  const [tab, setTab] = useState('overview')
  const [reportTab, setReportTab] = useState('groups')
  const [reportRevision, setReportRevision] = useState(0)
  const [segment, setSegment] = useState(data.segments[0]?.segment ?? 0)
  const [processSegment, setProcessSegment] = useState(segment)
  const [reportSegment, setReportSegment] = useState(segment)
  const [exporter, setExporter] = useState(data.exporters[0]?.name ?? '')
  const [revision, setRevision] = useState(0)
  const [statisticsRevision, setStatisticsRevision] = useState(0)
  const overviewActive = view === 'analysis' && tab === 'overview'
  const selectedSegment = data.segments.find((item) => item.segment === segment)
  const systemPath = overviewActive && selectedSegment ? seriesPath(data.id, 'S', segment) : null
  const systemStatisticsPath = overviewActive && selectedSegment ? statisticsPath(data.id, 'S', segment) : null
  const system = useResource<SeriesData>(systemPath, revision)
  const systemStatistics = useResource<StatisticsData>(systemStatisticsPath, statisticsRevision)
  const { stats, summary } = data
  return <>
    {view === 'report' && <p className="banner info">精简 HTML 报告展示全部进程统计，可离线逐进程展开 CPU（整机口径）与系统总 CPU 对比图及 RSS 图；下方配置与进程组保留用于分析，不写入精简报告，下载不会保存或丢弃草稿。</p>}
    <nav className="section-tabs" aria-label={view === 'report' ? '分析配置分区（不写入精简报告）' : '性能分析分区'}>
      {(view === 'report' ? [{ id: 'groups', label: '进程组分析设置' }, { id: 'content', label: '人工分析配置' }] : [{ id: 'overview', label: '系统总览' }, { id: 'processes', label: '进程排行' }, { id: 'quality', label: '日志质量' }]).map((item) => (
        <button key={item.id} aria-pressed={(view === 'report' ? reportTab : tab) === item.id} className={(view === 'report' ? reportTab : tab) === item.id ? 'active' : ''} onClick={() => view === 'report' ? setReportTab(item.id) : setTab(item.id)}>{item.label}</button>
      ))}
    </nav>
    <ViewPanel active={view === 'analysis' && tab === 'overview'}>
    <div className="section-heading"><h2>会话概览</h2><span>全会话统计 · 不随图表时间段变化</span></div>
    <div className="stats-grid">
      <Stat label="系统 CPU 均值" value={n(stats.cpu_avg)} unit="%" detail={`峰值 ${n(stats.cpu_peak)} %`} />
      <Stat label="最低可用内存" value={formatMemory(stats.mem_available_min, 'MB')} unit="" detail={`已用峰值 ${formatMemory(stats.mem_used_peak, 'MB')}`} />
      <Stat label="系统样本" value={n(stats.samples, 0)} unit="条" detail={`${n(summary.cycles, 0)} 个采集周期`} />
      <Stat label="独立时间段" value={n(summary.segments, 0)} unit="段" detail={`${summary.files.length} 个日志文件`} />
    </div>
    <section className="panel segment-bar">
      <div><h3>时间段</h3><p>重启或时间回退后分段，图表不跨段连接。</p></div>
      <label>查看范围 <select value={segment} disabled={disabled || !data.segments.length} onChange={(event) => setSegment(Number(event.target.value))}>
        {data.segments.map((item) => <option key={item.segment} value={item.segment}>时间段 {item.segment} · {formatTime(item.start)} → {formatTime(item.end)}</option>)}
      </select></label>
      <span className="muted">{selectedSegment ? `${n(selectedSegment.records, 0)} 条记录` : '无有效时间段'}</span>
    </section>
    <div className="section-heading"><h2>选段关键指标</h2><span>全部原始 S 样本统计 · 非降采样点推算</span></div>
    {!selectedSegment ? <p className="panel">无有效时间段，暂无系统统计。</p>
      : systemStatistics.loading ? <p className="panel" role="status">正在统计选段全部原始系统样本…</p>
      : systemStatistics.error ? <p className="banner error" role="alert">{systemStatistics.error} <button disabled={disabled} onClick={() => setStatisticsRevision((value) => value + 1)}>重试系统统计</button></p>
      : systemStatistics.data && <>
        <p className="muted">时间段 {systemStatistics.data.scope.segment ?? segment} · {n(systemStatistics.data.samples, 0)} 条原始记录 · {formatTime(systemStatistics.data.start)} → {formatTime(systemStatistics.data.end)}</p>
        <div className="stats-grid">{[{ field: 'cpu_total', label: '系统 CPU', unit: '%' }, { field: 'mem_used_mb', label: '已用内存', unit: 'MB' }].map(({ field, label, unit }) => {
          const metric = systemStatistics.data?.metrics[field]
          const metricUnit = metric?.unit ?? unit
          return <Stat key={field} label={`${label} · 均值`} value={formatStatistic(metric?.avg, metricUnit)} unit="" detail={`峰值 ${formatStatistic(metric?.max, metricUnit)} · P95 ${formatStatistic(metric?.p95, metricUnit)} · P99 ${formatStatistic(metric?.p99, metricUnit)} · 有效样本 ${n(metric?.count ?? 0, 0)}`} />
        })}</div>
        <p className="muted">{systemStatistics.data.method} 分位数仅描述样本分布，不是准入阈值；缩放趋势不会改变统计范围。缺失值不补零。</p>
      </>}
    <div className="section-heading"><h2>系统资源趋势</h2><span>横轴为本地时间 · 提示保留毫秒</span></div>
    {system.error && <button disabled={disabled} onClick={() => setRevision((value) => value + 1)}>重试系统趋势</button>}
    <div className="chart-grid">
      <SeriesChart title="系统 CPU" unit="%" metrics={cpuMetrics} percentiles={systemStatistics.data?.metrics} {...system} />
      <SeriesChart title="系统内存" unit="MB" metrics={memoryMetrics} percentiles={systemStatistics.data?.metrics} {...system} />
      <Trend path={overviewActive && selectedSegment ? seriesPath(data.id, 'D', segment) : null} title="系统 dmabuf · 散点" unit="MB" metrics={dmaMetrics} mode="scatter" note="仅显示实际采样点；缺失不代表零占用。" />
      <div className="exporter-panel">
        <label className="exporter-select">Exporter <select value={exporter} disabled={disabled || !data.exporters.length} onChange={(event) => setExporter(event.target.value)}>
          {!data.exporters.length && <option value="">暂无 exporter</option>}
          {data.exporters.map((item) => <option key={item.name} value={item.name}>{item.name} · 峰值 {formatMemory(item.peak_mb, 'MB')} · {n(item.samples, 0)} 条</option>)}
        </select></label>
        <Trend path={overviewActive && selectedSegment && exporter ? seriesPath(data.id, 'DE', segment, { name: exporter }) : null} title="Exporter 趋势" unit="MB" metrics={exporterMetrics} note="按名称精确匹配；候选来自全会话峰值前 100 名。" />
      </div>
    </div>
    <section className="panel">
      <div className="panel-heading"><h2>进程明细</h2><button disabled={disabled || !selectedSegment} onClick={() => { setProcessSegment(segment); setTab('processes') }}>查看当前时间段进程排行与明细</button></div>
      <p className="muted">先查看系统关键指标与趋势，再定位具体进程；沿用进程排行入口与已保留的筛选状态。</p>
    </section>
    <div className="section-heading"><h2>全字段分析</h2><span>按当前时间段统计 · 点击展开计算原始样本分位数</span></div>
    <StatisticsPanel title="系统 · S 全字段统计" path={overviewActive && selectedSegment ? statisticsPath(data.id, 'S', segment) : null} trendPath={overviewActive && selectedSegment ? seriesPath(data.id, 'S', segment) : null} disabled={disabled} />
    <StatisticsPanel title="系统 dmabuf · D 全字段统计" path={overviewActive && selectedSegment ? statisticsPath(data.id, 'D', segment) : null} trendPath={overviewActive && selectedSegment ? seriesPath(data.id, 'D', segment) : null} disabled={disabled} />
    <StatisticsPanel title={`Exporter · ${exporter || '未选择'} · DE 全字段统计`} path={overviewActive && selectedSegment && exporter ? statisticsPath(data.id, 'DE', segment, { name: exporter }) : null} trendPath={overviewActive && selectedSegment && exporter ? seriesPath(data.id, 'DE', segment, { name: exporter }) : null} disabled={disabled} />
    </ViewPanel>
    <ViewPanel active={view === 'analysis' && tab === 'processes'}>
      <Processes id={data.id} segment={processSegment} onSegment={setProcessSegment} disabled={disabled} segments={data.segments} visible={view === 'analysis' && tab === 'processes'} />
    </ViewPanel>
    <ViewPanel active={view === 'report' && reportTab === 'groups'}>
      <Groups id={data.id} segment={reportSegment} onSegment={setReportSegment} disabled={disabled} settingsRevision={reportRevision} />
    </ViewPanel>
    <ViewPanel active={view === 'report' && reportTab === 'content'}>
      <ReportConfig id={data.id} disabled={disabled} onSaved={() => setReportRevision((value) => value + 1)}/>
    </ViewPanel>
    <ViewPanel active={view === 'analysis' && tab === 'quality'}>
    <section className="panel limitations"><h3>读数与分析边界</h3><ul>
      <li>CPU / cpu1c 与 wait 保留协议原始口径；容量按 1024 进位自动换算，沿用 KB / MB / GB 标签，仅改变显示。时间按本机时区展示；cpu1c 为单核口径，可能超过 100%。CPU均值为原始样本算术平均，非时间加权平均。</li>
      <li>I/O 读写字段为周期增量，不再次差分，也不当作 KB/s。权限不足、内核限制可能导致持续零值，不等于没有 I/O。</li>
      <li>RSS 前 20 进程可能包含 GPU / dmabuf 回填，不能与 dmabuf 简单相加。共享缓冲区可重复归属，DP 合计不必等于 D。大整数网页显示和大额累计可能舍入。</li>
      <li>进程排行按时间段 + 名称合并全部 PID；准入成员仍按时间段 + PID + 名称精确匹配。同段内同名 PID 复用仍可能混合多个生命周期，需结合原始日志确认。</li>
      <li>图表最多请求约 1500 点，采样在点数限额内优先保留极值，不能保证保留所有指标极值及端点，并非每个周期的完整数据；不据采样点推算精确总量。滑块仅缩放已加载点。</li>
    </ul></section>
    <section className="panel diagnostics">
      <div className="panel-heading"><h3>解析质量与日志信息</h3><span className={summary.errors || summary.unknown || summary.orphan_cycles ? 'badge warning' : 'badge'}>{summary.errors || summary.unknown || summary.orphan_cycles ? '有待核查记录' : '未发现解析异常'}</span></div>
      <div className="quality-row"><span>解析异常 <b>{n(summary.errors, 0)}</b></span><span>未知行 <b>{n(summary.unknown, 0)}</b></span><span>孤立周期 <b>{n(summary.orphan_cycles, 0)}</b></span><span>首 / 末时间 <b>{formatTime(summary.first_ts)} / {formatTime(summary.last_ts)}</b></span></div>
      <div className="record-counts">{Object.entries(summary.counts).map(([kind, count]) => <span key={kind}>{kind} · {n(count, 0)}</span>)}</div>
      <details><summary>来源文件（{summary.files.length}）</summary><ul>{summary.files.map((file, index) => <li key={index}>{file}</li>)}</ul></details>
      <details open={summary.issues.length > 0}><summary>解析异常明细（服务端返回 {summary.issues.length} 条）</summary>
        {summary.issues.length ? <ul className="log-list">{summary.issues.map((item, index) => <li key={index}><strong>{item.source} : {item.line}</strong><span>{item.message}</span></li>)}</ul> : <p className="muted">无异常明细。</p>}
      </details>
      <details><summary>日志事件（服务端返回 {summary.events.length} 条）</summary>
        {summary.events.length ? <ul className="log-list">{summary.events.map((item, index) => <li key={index}><strong>{item.source} : {item.line} · {formatTime(item.ts)}</strong><span>{item.message}</span></li>)}</ul> : <p className="muted">无日志事件。</p>}
      </details>
    </section>
    </ViewPanel>
  </>
}

function Stat({ label, value, unit, detail }: { label: string; value: string; unit: string; detail: string }) {
  return <section className="panel stat"><p>{label}</p><div><strong>{value}</strong><span>{unit}</span></div><small>{detail}</small></section>
}